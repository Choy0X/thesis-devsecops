"""The shared evaluator of the fine-tuning phase: every arm and every track reports through this module. No torch, no network, no sklearn.

An *attempt* is one model answer to one view. Its status is ``answered``, ``malformed``, ``abstained``, ``timeout`` or ``env_error`` and
nothing is dropped:

* an abstention, a malformed answer, a timeout or an environment failure on a vulnerable case is a miss (recall is abstention-aware;
  conditional recall, over the answered cases, is reported beside it) and is never a detection, whatever prediction it carries;
* a repair counts as verified only with a receipt, and an environment failure is never a success;
* intervals resample whole groups (issue groups and repositories), never single rows, because cases of one issue or one repository are
  not independent;
* a threshold is chosen on validation rows only (``choose_operating_points``) and applied unchanged elsewhere; with fewer negatives than
  ``min_negatives`` an operating point is an interval on the false-positive rate, not a certification;
* a cell with fewer cases than ``min_cell`` reports its counts and ``insufficient`` instead of a confident rate;
* the two Critical errors are separate rates with their own denominators: not detecting a Critical vulnerability, and finding it but
  under-classifying its severity. A case without a known severity is counted apart, never assumed not Critical.

Results are broken down by ecosystem and by language; no claim about one ecosystem or language rests on another. Tracks with no attempts
are reported as blocked with the reason, never as zero.
"""
from __future__ import annotations

import json
import math
import pathlib
import random
from collections import Counter, defaultdict

SCHEMA = "eval-shared/1"
BREAKDOWNS = ("ecosystem", "language")
BLOCKED_REASON = {"general_utility": "no general-utility attempts (no replay corpus has been selected)",
                  "instruction_integrity": "no instruction-integrity attempts"}


from training.finetune.attempts import (COLUMNS, DECISION_TRACKS, FIELDS, INS, NOF, REQUIRED, SEVERITIES, STATE_LABELS, STATUSES,  # noqa: E402,F401
                                        SUP, TRACKS, VERIFICATION, read_attempts, validate_attempt, write_attempts)

# -- intervals -----------------------------------------------------------------------------------------------------------------------------

def ratio_ci(clusters: dict, *, seed: int = 17, n_boot: int = 1000, alpha: float = 0.05) -> dict:
    """A ratio of sums with a percentile interval from resampling whole groups. ``clusters`` maps a group to (numerator, denominator)."""
    keys = sorted(clusters)
    num = sum(clusters[k][0] for k in keys)
    den = sum(clusters[k][1] for k in keys)
    out = {"estimate": num / den if den else None, "numerator": num, "denominator": den, "clusters": len(keys), "ci": None,
           "status": "ok", "n_boot": n_boot, "seed": seed}
    if den == 0:
        return {**out, "status": "no_denominator"}
    if len(keys) < 2:
        return {**out, "status": "insufficient_clusters"}
    rng = random.Random(seed)
    values = [clusters[k] for k in keys]
    reps = []
    for _ in range(n_boot):
        n = d = 0
        for _ in keys:
            a, b = values[rng.randrange(len(keys))]
            n += a
            d += b
        if d:
            reps.append(n / d)
    if not reps:
        return {**out, "status": "no_denominator"}
    reps.sort()
    lo = reps[int(alpha / 2 * len(reps))]
    hi = reps[min(len(reps) - 1, int((1 - alpha / 2) * len(reps)))]
    return {**out, "ci": [lo, hi]}


def binomial_upper_bound(k: int, n: int, alpha: float = 0.05) -> float:
    """One-sided Clopper-Pearson upper bound on a rate with ``k`` events in ``n`` trials (the false-positive rate bound at k = 0 is the
    rule of three form 1 - alpha**(1/n))."""
    if n <= 0 or k >= n:
        return 1.0
    if k == 0:
        return 1 - alpha ** (1 / n)

    def cdf(p: float) -> float:                                        # P(X <= k)
        lp, lq = math.log(p), math.log1p(-p)
        base = math.lgamma(n + 1)
        return sum(math.exp(base - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq) for i in range(k + 1))
    lo, hi = k / n, 1.0 - 1e-12
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if cdf(mid) > alpha else (lo, mid)
    return (lo + hi) / 2


def _rate(num: int, den: int):
    return num / den if den else None


def _cluster_counts(rows, key: str, num_fn, den_fn=lambda r: 1) -> dict:
    out: dict = defaultdict(lambda: [0, 0])
    for r in rows:
        k = r.get(key) or f"view:{r.get('view_id')}"
        out[k][0] += int(num_fn(r))
        out[k][1] += int(den_fn(r))
    return {k: tuple(v) for k, v in out.items()}


def _cis(rows, num_fn, den_fn, *, n_boot: int, seed: int) -> dict:
    return {name: ratio_ci(_cluster_counts(rows, key, num_fn, den_fn), seed=seed, n_boot=n_boot)
            for name, key in (("issue_group", "issue_group_id"), ("repository", "repository_identity"))}


# -- the decision tracks --------------------------------------------------------------------------------------------------------------------------

def _answered(r) -> bool:
    return r["status"] == "answered"


def _detected(r) -> bool:
    return _answered(r) and r.get("prediction") == SUP


def _outcome(r) -> str:
    return r["prediction"] if _answered(r) else r["status"]


def _pairs(rows, n_boot: int, seed: int) -> dict:
    sides: dict = defaultdict(dict)
    for r in rows:
        if r.get("pair_id") and r.get("role") in ("vulnerable", "protected"):
            sides[r["pair_id"]][r["role"]] = r
    complete = {p: s for p, s in sides.items() if len(s) == 2}
    both = lambda s: _detected(s["vulnerable"]) and _answered(s["protected"]) and s["protected"]["prediction"] == NOF
    n_correct = sum(1 for s in complete.values() if both(s))
    fps = sum(1 for s in complete.values() if _detected(s["protected"]))
    clusters: dict = defaultdict(lambda: [0, 0])
    for p, s in complete.items():
        k = s["vulnerable"].get("issue_group_id") or p
        clusters[k][0] += int(both(s))
        clusters[k][1] += 1
    return {"n_pairs": len(complete), "incomplete_pairs": len(sides) - len(complete), "both_correct": n_correct,
            "both_correct_rate": _rate(n_correct, len(complete)),
            "both_answered": sum(1 for s in complete.values() if _answered(s["vulnerable"]) and _answered(s["protected"])),
            "protected_side_fpr": {"false_positives": fps, "n": len(complete), "rate": _rate(fps, len(complete))},
            "ci": ratio_ci({k: tuple(v) for k, v in clusters.items()}, seed=seed, n_boot=n_boot)}


def _fp_units(negatives) -> dict:
    out = {}
    keys = {"function": lambda r: r.get("view_id"), "file": lambda r: (r.get("repository_identity"), r.get("path")) if r.get("path") else None,
            "issue": lambda r: r.get("issue_group_id"), "repository": lambda r: r.get("repository_identity")}
    for name, key in keys.items():
        flagged: dict = {}
        missing = 0
        for r in negatives:
            k = key(r)
            if k is None:
                missing += 1
                continue
            flagged[k] = flagged.get(k, False) or _detected(r)
        cell = {"units": len(flagged), "flagged": sum(flagged.values()), "rate": _rate(sum(flagged.values()), len(flagged))}
        if missing:
            cell["missing"] = missing
        out[name] = cell
    return out


def decision_metrics(rows, *, n_boot: int = 1000, seed: int = 17, min_cell: int = 1) -> dict:
    """Metrics of one arm on one decision track (``generated_decision``, or ``head_decision`` read as a verdict)."""
    rows = list(rows)
    status_counts = Counter(r["status"] for r in rows)
    vulnerable = [r for r in rows if r.get("truth") == SUP]
    negatives = [r for r in rows if r.get("truth") == NOF]
    insufficient = [r for r in rows if r.get("truth") == INS]
    detected = [r for r in vulnerable if _detected(r)]
    v_answered = [r for r in vulnerable if _answered(r)]
    missed = Counter((r["status"] if not _answered(r) else f"answered_{'no_finding' if r['prediction'] == NOF else r['prediction']}")
                     for r in vulnerable if not _detected(r))
    fps = [r for r in negatives if _detected(r)]
    n_answered = [r for r in negatives if _answered(r)]
    v_thin, n_thin = len(vulnerable) < min_cell or not vulnerable, len(negatives) < min_cell or not negatives
    confusion: dict = defaultdict(Counter)
    for r in rows:
        if r.get("truth") in STATE_LABELS:
            confusion[r["truth"]][_outcome(r)] += 1
    return {
        "n": len(rows), "status_counts": dict(status_counts), "coverage": _rate(status_counts["answered"], len(rows)),
        "abstention_rate": _rate(status_counts["abstained"], len(rows)),
        "vulnerable": {"n": len(vulnerable), "detected": len(detected), "answered": len(v_answered), "insufficient": v_thin,
                       "recall": None if v_thin else _rate(len(detected), len(vulnerable)),
                       "conditional_recall": None if v_thin else _rate(len(detected), len(v_answered)),
                       "missed_by_status": dict(missed),
                       "ci": None if v_thin else _cis(vulnerable, _detected, lambda r: 1, n_boot=n_boot, seed=seed)},
        "negative": {"n": len(negatives), "false_positives": len(fps), "answered": len(n_answered), "insufficient": n_thin,
                     "fpr": None if n_thin else _rate(len(fps), len(negatives)),
                     "fpr_among_answered": None if n_thin else _rate(len(fps), len(n_answered)),
                     "ci": None if n_thin else _cis(negatives, _detected, lambda r: 1, n_boot=n_boot, seed=seed)},
        "insufficient_context": {"n": len(insufficient), "correct": sum(1 for r in insufficient if _answered(r) and r["prediction"] == INS),
                                 "recall": _rate(sum(1 for r in insufficient if _answered(r) and r["prediction"] == INS), len(insufficient))},
        "confusion": {t: dict(c) for t, c in confusion.items()},
        "pairs": _pairs(rows, n_boot, seed), "false_positive_units": _fp_units(negatives),
        "unlabeled": sum(1 for r in rows if r.get("truth") not in STATE_LABELS)}


# -- Critical ---------------------------------------------------------------------------------------------------------------------------------------

def critical_metrics(rows, *, n_boot: int = 1000, seed: int = 17) -> dict:
    """The two Critical errors, each with its denominator: a Critical vulnerability that was not detected (abstentions and failures
    included), and one that was found but whose severity was under-classified."""
    vulnerable = [r for r in rows if r.get("truth") == SUP]
    unknown = [r for r in vulnerable if r.get("severity") not in SEVERITIES]
    critical = [r for r in vulnerable if r.get("severity") == "Critical"]
    rated = [r for r in critical if _answered(r) and r.get("predicted_severity") in SEVERITIES]
    under = lambda r: r["predicted_severity"] != "Critical"
    miss = ratio_ci(_cluster_counts(critical, "issue_group_id", lambda r: not _detected(r)), seed=seed, n_boot=n_boot)
    low = ratio_ci(_cluster_counts(rated, "issue_group_id", under), seed=seed, n_boot=n_boot)
    return {"critical_detection_miss_rate": {**miss, "error": "missing a Critical vulnerability (not detected: includes abstentions, "
                                                              "malformed answers, timeouts and environment failures)"},
            "severity_underclassification_rate": {**low, "error": "under-classifying the severity of a Critical vulnerability the model answered "
                                                                  "about (a case with no usable severity is counted in severity_missing)"},
            "n_critical": len(critical), "severity_missing": len(critical) - len(rated), "severity_unknown_truth": len(unknown)}


# -- operating points ----------------------------------------------------------------------------------------------------------------------------------

def _scored(rows):
    return [r for r in rows if _answered(r) and r.get("score") is not None and r.get("truth") in STATE_LABELS]


def _split_scores(rows):
    scored = _scored(rows)
    return ([r["score"] for r in scored if r["truth"] == SUP], [r["score"] for r in scored if r["truth"] != SUP], [r["score"] for r in scored])


def _point(pos, neg, threshold, target, min_negatives, source) -> dict:
    fp = sum(1 for s in neg if s >= threshold)
    tp = sum(1 for s in pos if s >= threshold)
    return {"target_fpr": target, "threshold": threshold, "threshold_source": source, "n_pos": len(pos), "negatives": len(neg),
            "false_positives": fp, "fpr": _rate(fp, len(neg)), "recall": _rate(tp, len(pos)), "insufficient": len(neg) < min_negatives,
            "fpr_upper_bound": binomial_upper_bound(fp, len(neg))}


def choose_operating_points(rows, *, targets, min_negatives: int) -> list[dict]:
    """The strictest threshold whose false-positive rate on these rows is within each target. VALIDATION ROWS ONLY: any other split is
    refused, so a threshold can never be tuned on the data it is reported on."""
    rows = list(rows)
    bad = {r.get("split") for r in rows if r.get("split") not in (None, "val")}
    if bad:
        raise ValueError(f"thresholds are chosen on validation rows only (got split {sorted(bad)})")
    pos, neg, every = _split_scores(rows)
    out = []
    for target in targets:
        if not neg:
            out.append({"target_fpr": target, "threshold": None, "threshold_source": "validation", "n_pos": len(pos), "negatives": 0,
                        "false_positives": 0, "fpr": None, "recall": None, "insufficient": True, "fpr_upper_bound": 1.0})
            continue
        allowed = min(int(math.floor(target * len(neg) + 1e-9)), len(neg))
        ordered = sorted(neg, reverse=True)
        t = ordered[allowed] if allowed < len(neg) else -math.inf
        above = [s for s in every if s > t]
        out.append(_point(pos, neg, min(above) if above else math.inf, target, min_negatives, "validation"))
    return out


def apply_operating_points(rows, points, *, min_negatives: int) -> list[dict]:
    """Recall and false-positive rate at thresholds chosen elsewhere (on validation); nothing is re-chosen on these rows."""
    pos, neg, _ = _split_scores(rows)
    return [_point(pos, neg, p["threshold"], p["target_fpr"], min_negatives, p.get("threshold_source", "validation"))
            if p.get("threshold") is not None else {**p, "recall": None, "fpr": None, "insufficient": True} for p in points]


def head_metrics(rows, *, points, min_negatives: int, n_boot: int = 1000, seed: int = 17) -> dict:
    rows = list(rows)
    scored = _scored(rows)
    return {"n": len(rows), "n_scored": len(scored), "missing_scores": len(rows) - len(scored),
            "status_counts": dict(Counter(r["status"] for r in rows)),
            "operating_points": apply_operating_points(rows, points, min_negatives=min_negatives)}


# -- the other tracks --------------------------------------------------------------------------------------------------------------------------------------

def repair_metrics(rows, *, n_boot: int = 1000, seed: int = 17) -> dict:
    """Repairs by input (the reported vulnerability handed to the model, or the model's own discovery). Verified needs a receipt."""
    by_input: dict = defaultdict(list)
    for r in rows:
        by_input[r.get("repair_input") or "unspecified"].append(r)
    out = {}
    for name, group in sorted(by_input.items()):
        receipt = lambda r: bool(r.get("receipt_id"))
        verified = [r for r in group if r.get("verification") == "passed" and receipt(r)]
        no_receipt = [r for r in group if r.get("verification") == "passed" and not receipt(r)]
        failed = [r for r in group if r.get("verification") == "failed"]
        env = [r for r in group if r.get("verification") == "env_error"]
        has_receipt = [r for r in group if receipt(r) and r.get("verification") in ("passed", "failed", "env_error")]
        out[name] = {"n": len(group), "verified": len(verified), "failed": len(failed), "env_error": len(env),
                     "unavailable": len(group) - len(verified) - len(failed) - len(env), "receipt_missing": len(no_receipt),
                     "verified_rate": _rate(len(verified), len(group)), "receipt_coverage": _rate(len(has_receipt), len(group)),
                     "status_counts": dict(Counter(r["status"] for r in group)),
                     "ci": ratio_ci(_cluster_counts(group, "issue_group_id", lambda r: r in verified), seed=seed, n_boot=n_boot)}
    return {"n": len(list(rows)), "by_input": out}


def integrity_metrics(rows, *, n_boot: int = 1000, seed: int = 17) -> dict:
    rows = list(rows)
    if not rows:
        return {"status": "blocked", "reason": BLOCKED_REASON["instruction_integrity"]}
    scored = [r for r in rows if r.get("followed_injection") is not None]
    return {"n": len(rows), "unscored": len(rows) - len(scored), "status_counts": dict(Counter(r["status"] for r in rows)),
            "followed_rate": ratio_ci(_cluster_counts(scored, "issue_group_id", lambda r: r["followed_injection"]), seed=seed, n_boot=n_boot)}


def utility_metrics(rows) -> dict:
    rows = list(rows)
    if not rows:
        return {"status": "blocked", "reason": BLOCKED_REASON["general_utility"]}
    scored = [r for r in rows if r.get("utility_pass") is not None]
    passed = sum(1 for r in scored if r["utility_pass"])
    return {"n": len(rows), "scored": len(scored), "passed": passed, "pass_rate": _rate(passed, len(scored))}


def severity_metrics(rows) -> dict:
    """Severity triage: its own track, never discovery ground truth. An unanswered Critical counts as a missed Critical."""
    rows = list(rows)
    answered = [r for r in rows if _answered(r) and r.get("predicted_severity") in SEVERITIES and r.get("severity") in SEVERITIES]
    critical = [r for r in rows if r.get("severity") == "Critical"]
    caught = [r for r in critical if _answered(r) and r.get("predicted_severity") == "Critical"]
    confusion: dict = defaultdict(Counter)
    for r in rows:
        if r.get("severity") in SEVERITIES:
            confusion[r["severity"]][r["predicted_severity"] if r in answered else r["status"]] += 1
    right = sum(1 for r in answered if r["predicted_severity"] == r["severity"])
    return {"n": len(rows), "answered": len(answered), "coverage": _rate(len(answered), len(rows)), "accuracy": _rate(right, len(answered)),
            "critical_false_negative_rate": _rate(len(critical) - len(caught), len(critical)),
            "critical_n": len(critical), "confusion": {k: dict(v) for k, v in confusion.items()}}


# -- a run ----------------------------------------------------------------------------------------------------------------------------------------------------

def _track_report(track: str, rows, *, n_boot: int, seed: int, min_cell: int, min_negatives: int, targets) -> dict:
    if not rows:
        return {"status": "blocked", "reason": BLOCKED_REASON.get(track, f"no {track} attempts")}
    if track == "generated_decision":
        cut = lambda key: {v: decision_metrics([r for r in rows if r.get(key) == v], n_boot=n_boot, seed=seed, min_cell=min_cell)
                           for v in sorted({r.get(key) for r in rows if r.get(key)})}
        return {"overall": decision_metrics(rows, n_boot=n_boot, seed=seed, min_cell=min_cell), "by_ecosystem": cut("ecosystem"),
                "by_language": cut("language"), "critical": critical_metrics(rows, n_boot=n_boot, seed=seed)}
    if track == "head_decision":
        val = [r for r in rows if r.get("split") == "val"]
        points = choose_operating_points(val, targets=targets, min_negatives=min_negatives) if val else []
        cut = lambda key: {v: head_metrics([r for r in rows if r.get(key) == v], points=points, min_negatives=min_negatives)
                           for v in sorted({r.get(key) for r in rows if r.get(key)})}
        return {"overall": head_metrics(rows, points=points, min_negatives=min_negatives), "by_ecosystem": cut("ecosystem"),
                "by_language": cut("language"), "thresholds": points}
    if track == "repair":
        return repair_metrics(rows, n_boot=n_boot, seed=seed)
    if track == "instruction_integrity":
        return integrity_metrics(rows, n_boot=n_boot, seed=seed)
    if track == "general_utility":
        return utility_metrics(rows)
    return severity_metrics(rows)


def evaluate(rows, *, n_boot: int = 1000, seed: int = 17, min_cell: int = 20, min_negatives: int = 100, targets=(0.001, 0.01)) -> dict:
    """The full report: every arm, every track, with ecosystem and language breakdowns; empty tracks are reported as blocked."""
    rows = list(rows)
    by_arm: dict = defaultdict(list)
    for r in rows:
        by_arm[r["arm"]].append(r)
    arms = {arm: {track: _track_report(track, [r for r in group if r["track"] == track], n_boot=n_boot, seed=seed, min_cell=min_cell,
                                       min_negatives=min_negatives, targets=targets) for track in TRACKS}
            for arm, group in sorted(by_arm.items())}
    present = {r["track"] for r in rows}
    return {"schema": SCHEMA, "n_attempts": len(rows), "arms": arms,
            "tracks_blocked": {t: BLOCKED_REASON.get(t, f"no {t} attempts") for t in TRACKS if t not in present},
            "settings": {"n_boot": n_boot, "seed": seed, "min_cell": min_cell, "min_negatives": min_negatives, "targets": list(targets)}}


def write_run(rows, config: dict, *, tag: str, outdir="experiments", metric_attempts=None, **kw) -> dict:
    """A new run folder (never an existing one) with metrics.json, attempts.parquet and run_config.json."""
    from app.backend.pipeline import runs
    rows = list(rows)
    problems = [f"{r.get('attempt_id')}: {p}" for r in rows for p in validate_attempt(r)]
    if problems:
        raise ValueError("invalid attempts: " + "; ".join(problems[:5]))
    scoring = rows if metric_attempts is None else list(metric_attempts)
    if metric_attempts is not None:
        indexed = {row['attempt_id']: row for row in scoring}
        if len(indexed) != len(scoring) or any(indexed.get(row['attempt_id']) != row for row in rows):
            raise ValueError('Metric population must preserve every raw attempt unchanged')
        raw_ids = {row['attempt_id'] for row in rows}
        extras = [row for row in scoring if row['attempt_id'] not in raw_ids]
        if any(validate_attempt(row) or row.get('status') != 'env_error'
               or not str(row.get('detail') or '').startswith('excluded_before_generation:') for row in extras):
            raise ValueError('Additional metric outcomes must be explicit pre-generation exclusions')
    metrics = evaluate(scoring, **kw)
    if metric_attempts is not None:
        metrics['metric_population'] = {'raw_attempts': len(rows), 'scored_outcomes': len(scoring),
            'excluded_before_generation': len(scoring) - len(rows),
            'basis': 'Raw attempts plus authenticated pre-generation failures; failure placeholders are metric-only.'}
    tag, folder = runs.new_run(tag, outdir)
    write_attempts(folder / "attempts.parquet", rows)
    (folder / "metrics.json").write_text(json.dumps({**metrics, "tag": tag}, indent=2, default=str), encoding="utf-8")
    runs.write_config(folder, {**config, "evaluator": SCHEMA, "n_attempts": len(rows)})
    return {"tag": tag, "folder": str(folder), "metrics": metrics}
