"""Shared evaluation. Every arm reports through this module.

Identical metrics across runs is what makes a comparison valid.
The headline safety metric is the false-negative rate on Critical: a Critical
vulnerability classified as low-risk is the most dangerous failure mode.
"""
import json
import pathlib

import pandas as pd
from sklearn.metrics import (accuracy_score, precision_recall_fscore_support,
                             confusion_matrix, classification_report)

LABELS = ["Recommendation", "Warning", "Critical"]


ALIASES = {"truth": "true", "label": "true", "y_true": "true", "prediction": "pred", "y_pred": "pred"}


def evaluate(df: pd.DataFrame, tag: str, outdir: str = "experiments") -> dict:
    """df needs columns ``true`` and ``pred`` (``truth`` / ``label`` and
    ``prediction`` are accepted), optionally ``confidence``.

    Rows without a usable prediction are not dropped silently: ``coverage``
    reports the share answered, and an unanswered Critical sample counts as a
    missed Critical in the false-negative rate."""
    df = df.rename(columns={k: v for k, v in ALIASES.items() if k in df.columns and v not in df.columns})
    if "true" not in df.columns or "pred" not in df.columns:
        raise ValueError(f"predictions need columns 'true' and 'pred' (got {list(df.columns)})")
    total = len(df)
    crit_all = int((df["true"] == "Critical").sum())
    answered = df["pred"].isin(LABELS)
    df = df[answered]
    y, yhat = df["true"], df["pred"]

    p, r, f, support = precision_recall_fscore_support(
        y, yhat, labels=LABELS, average=None, zero_division=0)
    cm = confusion_matrix(y, yhat, labels=LABELS)

    crit = LABELS.index("Critical")
    caught = int(cm[crit][crit])
    fn_rate = float(1 - caught / crit_all) if crit_all else None   # unanswered = missed

    metrics = {
        "tag": tag,
        "n": int(len(df)),
        "n_total": int(total),
        "coverage": float(len(df) / total) if total else None,
        "accuracy": float(accuracy_score(y, yhat)),
        "macro_f1": float(f.mean()),
        "per_class": {
            l: {"precision": float(a), "recall": float(b),
                "f1": float(c), "support": int(s)}
            for l, a, b, c, s in zip(LABELS, p, r, f, support)
        },
        "critical_false_negative_rate": fn_rate,
        # the rate is "missed Critical vulnerabilities" over all Critical items; unanswered ones count as missed
        "critical_total": crit_all,
        "critical_missed": crit_all - caught,
        "confusion_matrix": cm.tolist(),
        "confusion_matrix_labels": LABELS,
    }

    d = pathlib.Path(outdir) / tag
    d.mkdir(parents=True, exist_ok=True)
    (d / "metrics.json").write_text(json.dumps(metrics, indent=2))

    print(classification_report(y, yhat, labels=LABELS, zero_division=0))
    print("Critical false-negative rate:",
          "n/a" if fn_rate is None else round(fn_rate, 3))
    print("written to", d / "metrics.json")
    return metrics


def compare(tags, outdir="experiments") -> pd.DataFrame:
    """Assemble the comparison grid from saved metrics.json files."""
    rows = []
    for t in tags:
        p = pathlib.Path(outdir) / t / "metrics.json"
        if not p.exists():
            print("missing:", p)
            continue
        m = json.loads(p.read_text())
        rows.append({
            "run": t,
            "n": m["n"],
            "accuracy": round(m["accuracy"], 3),
            "macro_f1": round(m["macro_f1"], 3),
            "critical_fn_rate": (None if m["critical_false_negative_rate"] is None
                                 else round(m["critical_false_negative_rate"], 3)),
        })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Score experiments/<tag>/predictions.csv")
    ap.add_argument("tag", help="experiment tag under experiments/")
    a = ap.parse_args()
    pred = pathlib.Path("experiments") / a.tag / "predictions.csv"
    if not pred.exists():
        raise SystemExit(f"no predictions at {pred}")
    evaluate(pd.read_csv(pred).dropna(subset=["pred"]), a.tag)
