"""Original model vs fine-tuned model, side by side.

Both runs must have been scored by the shared evaluators (classification:
``metrics.py``; detection: ``detection_metrics.py``; patch: ``patch_metrics.py``),
ideally on the same input (same dataset, split and sample size; their
run_config.json files are compared and a mismatch is reported). The result is a
table of every shared metric with the change from the original to the
fine-tuned model, written to a new run folder.
"""
from __future__ import annotations
import json
import pathlib

from app.backend.pipeline import runs

EXP = pathlib.Path("experiments")
# metrics where lower is better (the change is reported with that direction)
LOWER_IS_BETTER = {"critical_false_negative_rate", "false_positive_rate_safe", "false_negative_rate", "critical_detection_miss_rate",
                   "severity_underclassification_rate", "abstention_rate", "protected_side_fpr"}
KEYS = ["n", "coverage", "accuracy", "macro_f1", "critical_false_negative_rate", "precision",
        "recall", "f1", "false_positive_rate_safe", "false_negative_rate", "exact_match",
        "edit_similarity", "fix_line_recall", "vuln_line_removal", "plausible_fix_rate", "critical_detection_miss_rate",
        "severity_underclassification_rate", "both_correct_rate", "abstention_rate", "protected_side_fpr", "verified_rate"]
SAME_INPUT = ("dataset", "split", "n", "sample_seed", "task", "manifest_sha256", "context_policy_hash")


def _load(tag: str) -> tuple[dict, dict]:
    d = EXP / tag
    m = json.loads((d / "metrics.json").read_text())
    cfg = json.loads((d / "run_config.json").read_text()) if (d / "run_config.json").exists() else {}
    return m, cfg


def compare(base_tag: str, tuned_tag: str, tag: str | None = None) -> dict:
    base, bcfg = _load(base_tag)
    tuned, tcfg = _load(tuned_tag)
    rows = []
    for k in KEYS:
        b, t = base.get(k), tuned.get(k)
        if not isinstance(b, (int, float)) or not isinstance(t, (int, float)):
            continue
        delta = t - b
        better = (delta < 0) if k in LOWER_IS_BETTER else (delta > 0)
        rows.append({"metric": k, "original": b, "fine_tuned": t, "change": delta,
                     "improved": None if delta == 0 else bool(better)})
    mismatch = {k: (bcfg.get(k), tcfg.get(k)) for k in SAME_INPUT
                if k in bcfg and k in tcfg and bcfg.get(k) != tcfg.get(k)}
    out = {"task": "compare", "original": base_tag, "fine_tuned": tuned_tag,
           "original_model": bcfg.get("model"), "fine_tuned_model": tcfg.get("model"),
           "rows": rows, "input_mismatch": mismatch}
    tag, d = runs.new_run(tag or f"compare_{base_tag}_vs_{tuned_tag}")
    runs.write_config(d, {"task": "compare", "original": base_tag, "fine_tuned": tuned_tag})
    (d / "metrics.json").write_text(json.dumps({**out, "tag": tag}, indent=2))
    print(f"{'metric':<30}{'original':>12}{'fine-tuned':>12}{'change':>10}")
    for r in rows:
        mark = "" if r["improved"] is None else ("  better" if r["improved"] else "  worse")
        print(f"{r['metric']:<30}{r['original']:>12.3f}{r['fine_tuned']:>12.3f}{r['change']:>+10.3f}{mark}")
    if mismatch:
        print("NOTE: the two runs did not use the same input:", mismatch)
    return out
