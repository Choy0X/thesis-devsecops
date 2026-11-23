"""The training curves of a fine-tuning run, read from the metrics file the trainer wrote (one JSON object per optimizer step).

A run folder can hold the curves of the run itself (``run/metrics.jsonl``) and of local GPU smoke runs (``local_gpu/<name>/run/metrics.jsonl``). The
file is read once, line by line; a line that is not valid or lacks a step is skipped and counted, never guessed. The answer is columns aligned to the
steps, which is much smaller than one object per step.
"""
from __future__ import annotations

import json
import math
import pathlib

MAX_ROWS = 200_000
SCALARS = ("loss", "grad_norm", "lr", "family_pass", "tokens", "step_seconds", "supervised_tokens_per_second")
EVALUATION_NUMBERS = ("step", "pass", "family_pass", "metric", "val_loss", "n_examples")


def sources(run_dir: pathlib.Path) -> list[pathlib.Path]:
    run_dir = pathlib.Path(run_dir)
    found = [run_dir / "run" / "metrics.jsonl"] + sorted(run_dir.glob("local_gpu/*/run/metrics.jsonl"))
    found += [p.parent / "metrics.jsonl" for p in sorted(run_dir.glob("imports/*/import.json"))]
    return [p for p in found if p.is_file()]


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def read_curves(path: pathlib.Path, max_rows: int = MAX_ROWS) -> dict:
    steps: list[int] = []
    cols: dict[str, list[float | None]] = {k: [] for k in SCALARS}
    fields: dict[str, list[float | None]] = {}
    skipped = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            if len(steps) >= max_rows:
                skipped += 1
                continue
            try:
                row = json.loads(line)
                step = int(row["step"])
            except (ValueError, KeyError, TypeError):
                skipped += 1
                continue
            n = len(steps)
            steps.append(step)
            for k in SCALARS:
                cols[k].append(_num(row.get(k)))
            for name, v in (row.get("fields") or {}).items():
                fields.setdefault(name, [None] * n).append(_num(v))
            for name, col in fields.items():                                      # a field absent from this row leaves a gap, not a shifted column
                if len(col) <= n:
                    col.append(None)
    return {"rows": len(steps), "skipped": skipped, "steps": steps, **{k: v for k, v in cols.items() if any(x is not None for x in v)},
            "fields": {k: v for k, v in fields.items() if any(x is not None for x in v)}}


def read_evaluations(path: pathlib.Path) -> list[dict]:
    """The validation points the trainer wrote beside the metrics (``evaluations.json``): the step, the family pass, the metric it selects on, the validation loss and how many
    examples were scored. A point without a step, and any value that is not a finite number, is left out."""
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    out = []
    for e in data if isinstance(data, list) else []:
        if not isinstance(e, dict) or _num(e.get("step")) is None:
            continue
        row = {k: _num(e.get(k)) for k in EVALUATION_NUMBERS}
        row["metric_name"] = e["metric_name"] if isinstance(e.get("metric_name"), str) else None
        out.append(row)
    return sorted(out, key=lambda r: r["step"])


def _label(run_dir: pathlib.Path, path: pathlib.Path) -> str:
    if path.parent.parent == run_dir / "imports":
        return "Import " + path.parent.name
    return "run" if path == run_dir / "run" / "metrics.jsonl" else path.parent.parent.name


def curves(run_dir: pathlib.Path) -> dict:
    run_dir = pathlib.Path(run_dir)
    from training.finetune.analytics import read_report_header
    result = []
    for path in sources(run_dir):
        entry = {"label": _label(run_dir, path), **read_curves(path), "evaluations": read_evaluations(path.parent / "evaluations.json")}
        report = path.parent / "training_analytics.json"
        if report.exists():
            try:
                header = read_report_header(report)
                entry["analytics"] = {key: header.get(key) for key in ("schema", "run", "summary", "environment", "dataset", "warnings", "provenance")}
                if path.parent.parent == run_dir / "imports":
                    entry["analytics"]["import_id"] = path.parent.name
            except (ValueError, OSError):
                entry["analytics_error"] = "Detailed analytics could not be read"
        result.append(entry)
    return {"sources": result}
