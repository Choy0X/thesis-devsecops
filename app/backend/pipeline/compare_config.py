"""What differs between two evaluation runs: the settings of the training runs behind them and the datasets those were trained on.

``compare_runs`` says whether the runs may be compared and which is better; this says why they might differ. An evaluation run folder (``experiments/<tag>/``) names the
training run it came from in ``run_config.json``; that run's ``resolved_config.json`` holds the pinned settings and the dataset it was prepared on. Both sides are
flattened to dotted keys and only the keys whose values are not the same are returned, with a count of the ones that are. Nothing is guessed: a side whose training run
cannot be found says so and its training settings are left out of the comparison (the evaluation settings of both sides are still compared), and a dataset folder that has
changed since the run was prepared (the manifest no longer has the recorded hash) is returned with ``current: false`` so that a reader does not take today's counts for the
counts of the run. Read-only.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from app.backend.pipeline import runs

TRAINING_RUNS = pathlib.Path("training/runs")
PROCESSED = pathlib.Path("data/processed")
RESOLVED = "resolved_config.json"
MAX_CELL = 160                                   # a value longer than this (as text) is cut for display; the comparison uses the whole value
SKIP_EVALUATION = {"timestamp", "run", "created_at", "finished_at", "duration_seconds"}     # what changes with every run and says nothing about how it was made
SKIP_TRAINING = {"config_sha256", "contract_sha256"}                                       # hashes of the settings below, which are compared one by one


def _read(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def flatten(obj, prefix: str = "") -> dict[str, object]:
    """Nested dictionaries as dotted keys; a list or a scalar is one value."""
    out: dict[str, object] = {}
    if isinstance(obj, dict):
        for k in sorted(obj, key=str):
            out.update(flatten(obj[k], f"{prefix}.{k}" if prefix else str(k)))
        if not obj and prefix:
            out[prefix] = {}
        return out
    out[prefix] = obj
    return out


def cell(value):
    """A value as it is sent to the screen: a scalar as it is, anything long cut."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=True)
    return text if len(text) <= MAX_CELL else text[:MAX_CELL - 3] + "..."


def _sha(path: pathlib.Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def dataset_folder(recorded: dict, processed: pathlib.Path) -> tuple[pathlib.Path | None, str | None]:
    """The folder a training run records for its dataset, or ``(None, reason)``. The folder must lie under ``data/processed`` (a path in a configuration file is not
    followed anywhere else)."""
    raw = recorded.get("path")
    if not isinstance(raw, str) or not raw:
        return None, "the run does not record where its dataset is"
    folder = pathlib.Path(raw).resolve()
    try:
        folder.relative_to(pathlib.Path(processed).resolve())
    except ValueError:
        return None, "the recorded dataset folder is not under the processed data folder"
    return folder, None


def dataset_facts(recorded: dict, processed: pathlib.Path) -> tuple[dict | None, str | None]:
    """The facts of the dataset a training run was prepared on, read from its folder; ``(None, reason)`` when the folder cannot be read."""
    folder, why = dataset_folder(recorded, processed)
    if folder is None:
        return None, why
    manifest = _read(folder / "manifest.json")
    if not isinstance(manifest, dict):
        return None, f"the dataset folder {folder.name} has no readable manifest"
    stats = _read(folder / "stats.json")
    langs = stats.get("by_language") if isinstance(stats, dict) else None
    now = _sha(folder / "manifest.json")
    ext = manifest.get("external") if isinstance(manifest.get("external"), dict) else None
    return {
        "tag": str(manifest.get("tag") or recorded.get("version") or folder.name), "status": manifest.get("dataset_status") or recorded.get("status"),
        "manifest_sha256": recorded.get("manifest_sha256") or now, "current": recorded.get("manifest_sha256") in (None, now),
        "counts": manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {},
        "tier_mix": {k: v for k, v in (manifest.get("tier_mix") or {}).items() if isinstance(v, (int, float))},
        "languages": {k: v for k, v in (langs or {}).items() if isinstance(v, (int, float))},
        "external": {k: ext.get(k) for k in ("original_file", "sha256", "imported_at")} if ext else None,
    }, None


def side(tag: str, exp: pathlib.Path, training_runs: pathlib.Path, processed: pathlib.Path) -> dict:
    """The settings of one evaluation run: its own recorded settings, and (when its training run can be found) the training run's."""
    cfg = _read(exp / tag / "run_config.json")
    out = {"available": False, "read": False, "reason": None, "run": None, "evaluation": {}, "training": None, "dataset": None, "folder": None}
    if not isinstance(cfg, dict):
        out["reason"] = f"there is no readable run_config.json for {tag}"
        return out
    out["read"] = True
    out["evaluation"] = flatten({k: v for k, v in cfg.items() if k not in SKIP_EVALUATION})
    run = cfg.get("run")
    out["run"] = run if isinstance(run, str) else None
    if not out["run"]:
        out["reason"] = "the run does not say which training run it came from (a model that was only evaluated has none)"
        return out
    try:
        from app.backend.pipeline import finetune_prep as fp
        fp.check_run_name(out["run"])
    except ValueError:
        out["reason"] = "the training run it names has an invalid name"
        return out
    resolved = _read(training_runs / out["run"] / RESOLVED)
    if not isinstance(resolved, dict) or not isinstance(resolved.get("config"), dict):
        out["reason"] = f"the training run {out['run']} has no resolved configuration here"
        return out
    out["available"] = True
    training = flatten(resolved["config"])
    for k in ("pinned", "input_policy_hash"):
        if k in resolved:
            training.update(flatten(resolved[k], k))
    out["training"] = {k: v for k, v in training.items() if k not in SKIP_TRAINING}
    recorded = resolved.get("dataset")
    if isinstance(recorded, dict):
        out["dataset"], why = dataset_facts(recorded, processed)
        out["folder"] = dataset_folder(recorded, processed)[0] if out["dataset"] else None
        if why:
            out["reason"] = why
    return out


def _diff(a: dict, b: dict) -> tuple[list[dict], int]:
    differing = [{"key": k, "a": cell(a.get(k)), "b": cell(b.get(k))} for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k) or (k in a) != (k in b)]
    return differing, len(set(a) | set(b)) - len(differing)


def compare(a_tag: str, b_tag: str, exp=runs.EXP, training_runs=None, processed=None) -> dict:
    """The settings that differ between two evaluation runs and the datasets they were trained on."""
    exp = pathlib.Path(exp)
    training_runs = pathlib.Path(training_runs or TRAINING_RUNS)
    processed = pathlib.Path(processed or PROCESSED)
    sa, sb = side(a_tag, exp, training_runs, processed), side(b_tag, exp, training_runs, processed)
    both_read = sa["read"] and sb["read"]
    settings_a, settings_b = {f"evaluation.{k}": v for k, v in sa["evaluation"].items()}, {f"evaluation.{k}": v for k, v in sb["evaluation"].items()}
    if sa["training"] is not None and sb["training"] is not None:
        settings_a.update({f"training.{k}": v for k, v in sa["training"].items()})
        settings_b.update({f"training.{k}": v for k, v in sb["training"].items()})
    differing, same = _diff(settings_a, settings_b) if both_read else ([], 0)
    da, db = sa["dataset"], sb["dataset"]
    if da and db:
        same_dataset = da["manifest_sha256"] == db["manifest_sha256"] if da["manifest_sha256"] and db["manifest_sha256"] else da["tag"] == db["tag"]
    else:
        same_dataset = None
    return {
        "a": {k: sa[k] for k in ("available", "reason", "run")}, "b": {k: sb[k] for k in ("available", "reason", "run")},
        "config": {"compared": both_read, "same": same, "differing": differing, "training_compared": sa["training"] is not None and sb["training"] is not None},
        "dataset": {"same": same_dataset, "a": da, "b": db},
    }
