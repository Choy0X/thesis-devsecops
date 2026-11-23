"""Compare two evaluation runs (the folders under ``experiments/`` that the shared evaluator wrote): may they be compared, and is one better?

Two steps, each by the rule that is already written down: ``compare_guard`` names every recorded difference that would make a comparison misleading (what the
model was shown, the split, the dataset version, the evaluation views, the model that answered), and ``compare_stats`` decides "better" with a paired exact test
on the items both runs answered, or says there is not enough data. The items are the views of the discovery track (``generated_decision``); an answer is right
when it was given and equals the truth, so an abstention, a malformed answer, a timeout or an environment failure is wrong, as it is everywhere else.

Nothing is read from a run's name. A run folder without ``attempts.parquet`` cannot be compared item by item; that is said, not guessed around. A run is of one kind: the discovery
decision of the fine-tuning controls and of the vulnerable-code detector (``generated_decision``, right when the answer is the truth), or the severity classifier
(``severity_triage``, right when the severity it named is the true one); two runs of different kinds are not compared.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from app.backend.pipeline import compare_guard, compare_stats, runs
from training.finetune import attempts as at

TRACK = "generated_decision"
SEVERITY_TRACK = "severity_triage"
MAX_LISTED = 50                      # disagreements returned (the total is returned beside them)


class CompareError(ValueError):
    """A run that cannot be compared (no such run, no attempts); the message says which."""


def track_of(cfg: dict) -> str:
    """The track a run's items are on: the severity classifier's, else the discovery decision."""
    return SEVERITY_TRACK if cfg.get("task") == "classification" else TRACK


def _load(tag: str, root: pathlib.Path) -> tuple[dict, list[dict]]:
    folder = root / tag
    if not (folder / "metrics.json").is_file():
        raise CompareError(f"There is no scored run called {tag}.")
    cfg = json.loads((folder / "run_config.json").read_text(encoding="utf-8")) if (folder / "run_config.json").is_file() else {}
    if not (folder / "attempts.parquet").is_file():
        raise CompareError(f"The run {tag} has no attempts file, so its answers cannot be compared item by item.")
    track = track_of(cfg)
    return cfg, [r for r in at.read_attempts(folder / "attempts.parquet") if r.get("track") == track]


def guard_config(cfg: dict) -> dict:
    """The settings of an evaluation run under the names ``compare_guard`` checks. A setting the run did not record is left out, so that it is reported as
    unconfirmed and never as a match."""
    out: dict = {}
    for key, value in (("input_policy_hash", cfg.get("context_policy_hash")), ("split", cfg.get("split")), ("dataset_version", cfg.get("manifest_sha256"))):
        if value is not None:
            out[key] = value
    if isinstance(cfg.get("external_data"), dict):
        out["external_data"] = cfg["external_data"].get("sha256") or "imported"
    if cfg.get("role") is not None or cfg.get("tasks") is not None:
        out["eval_view"] = {"role": cfg.get("role"), "tasks": sorted(cfg.get("tasks") or [])}
    if cfg.get("model") is not None:
        out["engine"] = {"model": f"{cfg['model']}@{cfg.get('model_revision') or 'unpinned'}"}
    if isinstance(cfg.get('evaluation_dataset'), dict):
        # Preserve the historical projection for old runs, while carrying the
        # explicit independent-evaluation contract through the actual API.
        for key in ('manifest_sha256', 'evaluation_dataset', 'training_dataset', 'model_revision', 'tokenizer_revision',
                    'serialization', 'context_policy_hash', 'evaluation_cohort_sha256', 'generation',
                    'context_policy', 'rag_enabled', 'prompt_version', 'loss_mode'):
            if key in cfg:
                out[key] = cfg[key]
        if cfg.get('input_policy_hash') is not None:
            out['input_policy_hash'] = cfg['input_policy_hash']
    if cfg.get("task") in ("classification", "detection"):                        # the classifier and the detector record these under other names
        if cfg.get("dataset") is not None:
            out.setdefault("dataset_version", str(cfg["dataset"]))
        if cfg.get("prompt_version") is not None:
            out["prompt_version"] = cfg["prompt_version"]
        elif cfg.get("prompt_system") is not None:
            out["prompt_version"] = hashlib.sha256(str(cfg["prompt_system"]).encode("utf-8")).hexdigest()[:16]
        retrieval = cfg.get("retrieval")
        if isinstance(retrieval, dict) and "enabled" in retrieval:
            out["rag_enabled"] = bool(retrieval["enabled"])
            if retrieval.get("enabled"):
                out["retrieval"] = {k: retrieval[k] for k in ("embedder", "kb_version") if retrieval.get(k) is not None}
    return out


def _severity(row: dict) -> bool:
    return row.get("track") == SEVERITY_TRACK


def correct(row: dict) -> bool:
    """Right: answered, and the answer is the truth (for the severity classifier, the severity it named is the true one)."""
    if _severity(row):
        return row.get("status") == "answered" and row.get("predicted_severity") == row.get("severity")
    return row.get("status") == "answered" and row.get("prediction") == row.get("truth")


def truth_of(row: dict) -> str | None:
    return row.get("severity") if _severity(row) else row.get("truth")


def _answer(row: dict) -> str:
    if row.get("status") != "answered":
        return str(row.get("status"))
    return row["predicted_severity"] if _severity(row) else row["prediction"]


def _by_view(rows: list[dict]) -> dict[str, dict]:
    """One attempt per view (the first by attempt id when a view was attempted more than once), only the views whose truth is known."""
    out: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: str(r.get("attempt_id"))):
        known = r.get("severity") in at.SEVERITIES if _severity(r) else r.get("truth") in at.STATE_LABELS
        if known and r.get("view_id") not in out:
            out[r["view_id"]] = r
    return out


def _warning(label: str, cfg: dict) -> str | None:
    if cfg.get("counts_as_evaluation") is False:
        why = " and ".join(w for w, on in (("a software check on a small model", cfg.get("smoke_test")), ("capped at a few views", cfg.get("partial"))) if on)
        return f"Run {label} is {why or 'not complete'}, so it is not an evaluation of a control; the comparison is shown for what it is worth."
    return None


def _independent_security(cfg):
    evaluation = cfg.get('evaluation_dataset')
    training = cfg.get('training_dataset')
    readiness = cfg.get('evaluation_data')
    if not isinstance(evaluation, dict):
        return False
    # Only an explicit, matching same-dataset contract retains the legacy path.
    # Omitting independence provenance must not restore an item-level verdict.
    same_dataset = (isinstance(training, dict) and isinstance(readiness, dict)
                    and readiness.get('independent') is False
                    and evaluation.get('manifest_sha256') == training.get('manifest_sha256'))
    return not same_dataset


def _independent_cohort_problem(cfg_a, cfg_b, rows_a, rows_b, va, vb):
    for cfg in (cfg_a, cfg_b):
        accounting = cfg.get('coverage_accounting') or {}
        if cfg.get('excluded') or accounting.get('excluded_views'):
            return 'Independent comparison requires a complete cohort; pre-generation exclusions remain unresolved.'
    if not va or not vb or set(va) != set(vb) or len(rows_a) != len(va) or len(rows_b) != len(vb):
        return 'Independent comparison requires the same nonempty cohort without duplicate or unknown-label views.'
    keys = ('truth', 'repository_identity', 'issue_group_id', 'pair_id')
    if any(any(va[view].get(key) != vb[view].get(key) for key in keys) for view in va):
        return 'Independent comparison requires matching view truths and repository/issue/pair grouping metadata.'
    return None


def compare(a_tag: str, b_tag: str, root=runs.EXP) -> dict:
    """The guard's answer, the paired test on the views both runs answered, and the views on which they disagree."""
    root = pathlib.Path(root)
    cfg_a, rows_a = _load(a_tag, root)
    cfg_b, rows_b = _load(b_tag, root)
    if track_of(cfg_a) != track_of(cfg_b):
        words = {TRACK: "the discovery decision (found a vulnerability or not)", SEVERITY_TRACK: "the severity of a described vulnerability"}
        raise CompareError(f"The runs measure different things: {a_tag} is scored on {words[track_of(cfg_a)]} and {b_tag} on {words[track_of(cfg_b)]}.")
    guard = compare_guard.check(guard_config(cfg_a), guard_config(cfg_b))
    va, vb = _by_view(rows_a), _by_view(rows_b)
    shared = sorted(set(va) & set(vb))
    out = {"a": a_tag, "b": b_tag, "guard": guard, "items": {"a": len(va), "b": len(vb), "shared": len(shared)}, "stats": None, "stats_note": None,
           "warnings": [w for w in (_warning("A", cfg_a), _warning("B", cfg_b)) if w], "disagreements": [], "disagreements_total": 0}
    independent = _independent_security(cfg_a) or _independent_security(cfg_b)
    if independent:
        out['grouped_paired'] = None
        problem = _independent_cohort_problem(cfg_a, cfg_b, rows_a, rows_b, va, vb)
        if problem:
            guard['comparable'] = False
            guard['blockers'].append({'field': 'independent_cohort', 'reason': problem,
                                      'a': len(va), 'b': len(vb)})
            out['stats_note'] = problem
            return out
        if not guard['comparable']:
            out['stats_note'] = 'Independent benchmark comparison is blocked by incompatible recorded inputs or model protocols.'
            return out
    if not shared:
        out["stats_note"] = "The runs share no item, so they cannot be compared item by item."
        return out
    if len(shared) < max(len(va), len(vb)):
        out["warnings"].append(f"Only {len(shared)} items were answered by both runs (A {len(va)}, B {len(vb)}); the test uses those.")
    ca, cb = [correct(va[v]) for v in shared], [correct(vb[v]) for v in shared]
    if independent:
        from training.finetune import security_validation
        out['grouped_paired'] = security_validation.paired_differences([va[v] for v in shared], [vb[v] for v in shared])
        out['descriptive'] = {'items': len(shared), 'both_right': sum(x and y for x, y in zip(ca, cb)),
            'both_wrong': sum(not x and not y for x, y in zip(ca, cb)),
            'only_a': sum(x and not y for x, y in zip(ca, cb)), 'only_b': sum(y and not x for x, y in zip(ca, cb))}
        out['stats_note'] = ('Independent security benchmark: use descriptive outcomes and repository/issue-group paired intervals. '
                             'No categorical significance verdict is issued.')
        out['warnings'].append('The item-independent McNemar test and item bootstrap are disabled for this correlated security benchmark.')
    else:
        out["stats"] = compare_stats.compare(ca, cb)
    differ = [v for v, x, y in zip(shared, ca, cb) if x != y]
    out["disagreements_total"] = len(differ)
    out["disagreements"] = [{"view_id": v, "truth": truth_of(va[v]), "a": _answer(va[v]), "b": _answer(vb[v])} for v in differ[:MAX_LISTED]]
    return out
