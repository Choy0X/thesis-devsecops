"""The severity classifier and the vulnerable-code detector, written as attempts and scored by the shared evaluator.

Both jobs score their answers with their own scorers (macro-F1 and the Critical miss rate; precision, recall and the false-positive rate on safe code), and those numbers stay in
``metrics.json`` where the pages that read them expect them. This also writes each answer as an attempt (``training/finetune/attempts.py``: one model answer to one view, with a status
and nothing dropped) into ``attempts.parquet`` and puts the shared evaluator's report beside the others under ``shared``. So these runs are scored by the same code, with the same
rule for an unanswered item (it is wrong, never dropped), and the Compare view can set two of them against each other with the same paired test as the fine-tuning controls.

The classifier is the ``severity_triage`` track (truth ``severity``, answer ``predicted_severity``); the detector is the ``generated_decision`` track (a vulnerable function is the
truth ``vulnerability_supported`` and a safe one ``no_supported_finding_in_scope``; an answer of "vulnerable" is a finding). Neither names a repository, so the intervals of the
shared report treat each item as its own cluster: they are not wider for the fact that functions of one project resemble each other, and the report's note says so.
"""
from __future__ import annotations

import json
import pathlib

import pandas as pd

from app.backend.pipeline import fsio
from app.backend.pipeline.eval import shared
from training.finetune import attempts as at

NOTE = ("Scored by the shared evaluator. These items name no repository, so its intervals treat every item as independent of the others, which makes them narrower than they would be "
        "if the items of one project were grouped.")


def _text(value) -> str | None:
    return None if value is None or (isinstance(value, float) and value != value) or str(value).strip() in ("", "nan", "None") else str(value)


def severity_attempts(frame: pd.DataFrame, *, arm: str, split: str | None) -> list[dict]:
    """One ``severity_triage`` attempt per classified row. A row with no usable label is a malformed attempt (counted as wrong), never left out."""
    rows = []
    for i, r in enumerate(frame.to_dict("records")):
        truth, pred = _text(r.get("true")), _text(r.get("pred"))
        answered = pred in at.SEVERITIES
        confidence = r.get("confidence")
        rows.append({"attempt_id": f"s{i:06d}", "arm": arm, "track": "severity_triage", "view_id": _text(r.get("cve_id")) or f"row-{i}", "status": "answered" if answered else "malformed",
                     "severity": truth if truth in at.SEVERITIES else None, "predicted_severity": pred if answered else None,
                     "score": float(confidence) if isinstance(confidence, (int, float)) and confidence == confidence else None,
                     "issue_group_id": _text(r.get("cve_id")), "ecosystem": _text(r.get("ecosystem")), "language": _text(r.get("language")), "split": split,
                     "detail": None if answered else "the model gave no label from Recommendation, Warning, Critical"})
    return rows


def detection_attempts(frame: pd.DataFrame, *, arm: str, split: str | None) -> list[dict]:
    """One ``generated_decision`` attempt per detected row; a row with no answer is malformed. The twin pairs are kept (``pair_id``, ``role``) so the shared report's pair metrics work."""
    rows = []
    for i, r in enumerate(frame.to_dict("records")):
        label, pred = r.get("label"), r.get("pred")
        label = int(label) if label in (0, 1) else None
        answered = pred in (0, 1)
        kind = _text(r.get("kind"))
        role = {"vulnerable": "vulnerable", "fixed": "protected"}.get(kind or "")
        rows.append({"attempt_id": f"d{i:06d}", "arm": arm, "track": "generated_decision", "view_id": _text(r.get("id")) or f"row-{i}", "status": "answered" if answered else "malformed",
                     "truth": at.SUP if label == 1 else at.NOF if label == 0 else None, "prediction": (at.SUP if pred == 1 else at.NOF) if answered else None,
                     "score": float(r["score"]) if isinstance(r.get("score"), (int, float)) and r["score"] == r["score"] else None,
                     "issue_group_id": _text(r.get("cve")), "language": _text(r.get("language")), "pair_id": _text(r.get("pair_id")), "role": role, "path": _text(r.get("file")), "split": split,
                     "detail": None if answered else "the model gave neither vulnerable nor safe"})
    return rows


def attach(folder, rows: list[dict], *, n_boot: int = 1000) -> dict:
    """Write ``attempts.parquet`` into the run folder, score it with the shared evaluator and keep the report in ``metrics.json`` under ``shared``; ``run_config.json`` says which
    evaluator did. The numbers the run already has are left as they are."""
    folder = pathlib.Path(folder)
    at.write_attempts(folder / "attempts.parquet", rows)
    report = {**shared.evaluate(rows, n_boot=n_boot), "note": NOTE}
    path = folder / "metrics.json"
    metrics = json.loads(path.read_text(encoding="utf-8"))
    metrics["shared"] = report
    fsio.write_json(path, metrics, indent=2, default=str)
    cfg_path = folder / "run_config.json"
    if cfg_path.is_file():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        fsio.write_json(cfg_path, {**cfg, "evaluator": shared.SCHEMA, "n_attempts": len(rows)}, indent=2, default=str)
    return report
