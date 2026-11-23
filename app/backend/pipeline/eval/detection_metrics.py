"""Shared scorer for vulnerable-code detection (binary: vulnerable = 1, safe = 0).

Predictions: a CSV with ``id`` (the detection-JSONL row id, e.g.
``...:file:func#vuln`` / ``#fixed``) and ``pred`` (1 / 0, or
"vulnerable" / "safe"), optionally ``score`` (probability of vulnerable) and
``label`` (the truth; otherwise read from the detection JSONL by id).

Reported: precision, recall, F1, accuracy, **false-positive rate on safe code**
(safe code flagged as vulnerable - the error the owner most wants to avoid) and
the false-negative rate, overall, per language and per kind of row (``vulnerable``,
``fixed`` twin, ``untouched`` function; see ``ingest/detection.py``); ``coverage`` counts rows the
model answered (an unanswered row is scored as wrong, never dropped).
"""
from __future__ import annotations
import json
import pathlib

import pandas as pd

PROC = pathlib.Path("data/processed")
_POS = {"1", "true", "vulnerable", "vuln", "yes", "positive"}
_NEG = {"0", "false", "safe", "fixed", "no", "negative", "not_vulnerable"}


def default_dataset() -> pathlib.Path:
    from app.backend.pipeline import versions
    return versions.resolve("code_pairs", "_detection.jsonl",
                            "code_pairs_preview_detection.jsonl", PROC)


def _binary(v):
    if v is None or (isinstance(v, float) and v != v):     # missing (pandas NaN) = unanswered
        return None
    s = str(v).strip().lower()
    if s in ("", "nan", "none", "null"):
        return None
    if s in _POS:
        return 1
    if s in _NEG:
        return 0
    try:
        x = float(s)
    except ValueError:
        return None
    return None if x != x else int(x >= 0.5)


def _scores(y: pd.Series, p: pd.Series) -> dict:
    """Unanswered (None) predictions count as wrong."""
    n = len(y)
    tp = int(((p == 1) & (y == 1)).sum()); tn = int(((p == 0) & (y == 0)).sum())
    fp = int(((p == 1) & (y == 0)).sum()); fn = int(((p != 1) & (y == 1)).sum())
    neg = int((y == 0).sum()); pos = int((y == 1).sum())
    wrong_safe = int((y == 0).sum() - tn)                 # flagged or unanswered safe code
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / pos if pos else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else (0.0 if prec is not None and rec is not None else None)
    return {"n": n, "positives": pos, "negatives": neg, "coverage": float(p.notna().mean()) if n else None,
            "accuracy": (tp + tn) / n if n else None, "precision": prec, "recall": rec, "f1": f1,
            "false_positive_rate_safe": wrong_safe / neg if neg else None,
            "false_negative_rate": fn / pos if pos else None,
            "tp": tp, "fp": fp, "tn": tn, "fn": fn}


def score_frame(preds: pd.DataFrame, dataset: str | pathlib.Path | None = None) -> pd.DataFrame:
    df = preds.copy()
    if "pred" not in df.columns and "prediction" in df.columns:
        df = df.rename(columns={"prediction": "pred"})
    df["pred"] = df["pred"].map(_binary)
    truth = {}
    path = pathlib.Path(dataset) if dataset else default_dataset()
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                truth[r["id"]] = r
    if "label" not in df.columns:
        if not truth:
            raise FileNotFoundError(f"{path} not found and the predictions have no 'label' column")
        df["label"] = df["id"].map(lambda i: truth.get(i, {}).get("label"))
    df["label"] = df["label"].map(_binary)
    for col in ("language", "kind"):
        if col not in df.columns:
            df[col] = df["id"].map(lambda i: truth.get(i, {}).get(col))
    missing = int(df["label"].isna().sum())
    if missing:
        print(f"{missing} predictions have no known truth (id not in {path.name}) and are left out")
    return df.dropna(subset=["label"])


def evaluate(preds: pd.DataFrame, tag: str, dataset=None, outdir: str = "experiments") -> dict:
    df = score_frame(preds, dataset)
    overall = _scores(df["label"], df["pred"])
    by_lang = {str(l): _scores(g["label"], g["pred"]) for l, g in df.groupby(df.language.fillna("?"))}
    # false alarms by kind of safe code: the fixed twin (hard) vs untouched code (ordinary)
    by_kind = {str(k): _scores(g["label"], g["pred"]) for k, g in df.groupby(df.kind.fillna("?"))}
    metrics = {"task": "detection", "tag": tag, **overall, "by_language": by_lang, "by_kind": by_kind}
    d = pathlib.Path(outdir) / tag
    d.mkdir(parents=True, exist_ok=True)
    (d / "metrics.json").write_text(json.dumps(metrics, indent=2))
    f = lambda x: "n/a" if x is None else f"{x:.3f}"
    print(f"{tag}: n={overall['n']} precision={f(overall['precision'])} recall={f(overall['recall'])} "
          f"F1={f(overall['f1'])} false-positive rate on safe code={f(overall['false_positive_rate_safe'])}")
    return metrics
