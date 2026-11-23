"""The runs of a comparison by weakness class (CWE).

The attempts of an evaluation carry no weakness class, so it is joined from the dataset the run was prepared on: an attempt names a view, the view names its record, and the
record lists its CWE ids. Nothing is guessed: a run whose dataset cannot be found, or whose folder no longer has the manifest it was prepared with, gets no column and says
why. A case with several weakness classes counts under each of them (the rows are not additive), and a case with none is counted under "none recorded". The words are those
of the other tables: a vulnerable case is found when the run answered with a finding, a case with no supported finding is a false alarm when it did, and a cell with fewer
cases than the run's own minimum says "too few". Read-only.
"""
from __future__ import annotations

import json
import pathlib

from app.backend.pipeline import compare_config, compare_runs, runs
from app.backend.pipeline.eval import shared
from training.finetune import attempts as at

NONE_RECORDED = "none recorded"
DEFAULT_MIN_CELL = 20                  # the evaluator's own default when a run did not record its setting


def cwe_of_views(folder: pathlib.Path, view_ids: set[str]) -> dict[str, list[str]]:
    """The weakness classes of each wanted view, from the dataset's views and records (two columns of each are read, nothing else)."""
    import pyarrow.parquet as pq
    views = pq.read_table(folder / "views.parquet", columns=["view_id", "record_id"]).to_pylist()
    wanted = {v["view_id"]: v["record_id"] for v in views if v["view_id"] in view_ids}
    records = pq.read_table(folder / "records.parquet", columns=["record_id", "cwe_ids"]).to_pylist()
    ids = {r["record_id"]: [c for c in (r["cwe_ids"] or []) if isinstance(c, str) and c] for r in records}
    return {v: ids.get(rec) or [NONE_RECORDED] for v, rec in wanted.items()}


def _min_cell(tag: str, exp: pathlib.Path) -> int:
    try:
        metrics = json.loads((exp / tag / "metrics.json").read_text(encoding="utf-8"))
        return int(metrics.get("settings", {}).get("min_cell") or DEFAULT_MIN_CELL)
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_MIN_CELL


def _cells(by_view: dict[str, dict], cwes: dict[str, list[str]]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for view, row in by_view.items():
        for cwe in cwes.get(view, ()):
            cell = out.setdefault(cwe, {"vulnerable": 0, "detected": 0, "negatives": 0, "false_positives": 0})
            if row.get("truth") == at.SUP:
                cell["vulnerable"] += 1
                cell["detected"] += shared._detected(row)
            elif row.get("truth") == at.NOF:
                cell["negatives"] += 1
                cell["false_positives"] += shared._detected(row)
    return out


def table(tags: list[str], exp=runs.EXP, training_runs=None, processed=None) -> dict:
    """The found and false-alarm counts of each run for each weakness class that any run has."""
    exp = pathlib.Path(exp)
    training_runs = pathlib.Path(training_runs or compare_config.TRAINING_RUNS)
    processed = pathlib.Path(processed or compare_config.PROCESSED)
    columns: list[dict] = []
    for tag in tags:
        _, rows = compare_runs._load(tag, exp)
        by_view = compare_runs._by_view(rows)
        side = compare_config.side(tag, exp, training_runs, processed)
        col: dict = {"tag": tag, "available": False, "reason": side["reason"], "min_cell": _min_cell(tag, exp), "cells": {}, "items": len(by_view), "unmatched": 0}
        folder = side["folder"]
        if folder is None:
            col["reason"] = col["reason"] or "the run's dataset cannot be found"
        elif not side["dataset"]["current"]:
            col["reason"] = f"the dataset folder {folder.name} has changed since the run was prepared, so the weakness classes of its views cannot be trusted"
        elif not (folder / "views.parquet").is_file() or not (folder / "records.parquet").is_file():
            col["reason"] = f"the dataset folder {folder.name} has no views or records file"
        else:
            cwes = cwe_of_views(folder, set(by_view))
            col["cells"], col["unmatched"] = _cells(by_view, cwes), len(by_view) - len(cwes)
            col["available"], col["reason"] = True, None
        columns.append(col)
    names = sorted({c for col in columns for c in col["cells"]}, key=lambda c: (c == NONE_RECORDED, c))
    return {"runs": [{k: col[k] for k in ("tag", "available", "reason", "min_cell", "items", "unmatched")} for col in columns],
            "cwes": [{"cwe": c, "cells": [col["cells"].get(c) if col["available"] else None for col in columns]} for c in names],
            "note": "A case with several weakness classes counts under each of them, so the rows do not add up to the totals."}
