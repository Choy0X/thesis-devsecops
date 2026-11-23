"""Dataset statistics and files, experiment metrics and reports, notebooks, and the import of a fine-tuned run's predictions."""
from __future__ import annotations

import json
import pathlib
import re

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.backend import errors, jobs
from app.backend.api_common import EXP, PROC

router = APIRouter()


@router.get("/api/results/dataset-stats")
def dataset_stats():
    p = PROC / "dataset_stats.json"
    return json.loads(p.read_text()) if p.exists() else {}


@router.get("/api/results/dataset-file")
def dataset_file():
    from app.backend.pipeline import versions
    f = versions.resolve("dataset", ".parquet", "dataset_preview.parquet", PROC)
    if f.exists():
        return FileResponse(f, filename=f.name, media_type="application/octet-stream")
    return errors.error("no dataset built yet", 404)


@router.get("/api/training/notebook/{arm}")
def training_notebook(arm: str):
    f = pathlib.Path("training") / f"arm_{arm}_colab.ipynb"
    if f.exists():
        return FileResponse(f, filename=f.name, media_type="application/x-ipynb+json")
    return errors.error("unknown notebook", 404)


@router.get("/api/results/experiments")
def experiments():
    out = []
    if EXP.exists():
        for d in sorted(EXP.iterdir()):
            m = d / "metrics.json"
            if m.is_file():
                try:
                    metrics = json.loads(m.read_text())
                    cfg = d / "run_config.json"
                    config = json.loads(cfg.read_text()) if cfg.is_file() else {}
                    if "split" not in metrics:                          # name the split the numbers were measured on
                        metrics["split"] = config.get("split")
                    if metrics.get("schema") == "eval-shared/1":        # what a reader needs to know before trusting an evaluation's numbers
                        metrics["run_info"] = {k: config.get(k) for k in ("run", "baseline", "arm", "role", "model", "smoke_test", "partial", "counts_as_evaluation", "excluded")}
                    out.append(metrics)
                except Exception:  # ignored: a damaged metrics file is skipped; the other runs still list
                    pass
    return out


@router.get("/api/results/{tag}/metrics")
def metrics(tag: str):
    m = EXP / tag / "metrics.json"
    return json.loads(m.read_text()) if m.exists() else errors.error("not found", 404)


@router.get("/api/results/{tag}/report")
def report(tag: str):
    r = EXP / tag / "report.json"
    return json.loads(r.read_text()) if r.exists() else errors.error("not found", 404)


class CompareIn(BaseModel):
    a: str
    b: str


@router.post("/api/compare/runs")
def compare_runs(body: CompareIn):
    """Two evaluation runs side by side: whether they may be compared (the recorded settings that decide what a score means), and on the items both answered,
    whether one is better (a paired exact test, or "not enough data"). Read-only."""
    from app.backend.pipeline import compare_runs as cr
    for tag in (body.a, body.b):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", tag or ""):
            return errors.error("A run name has letters, digits, '_', '.' and '-' only.", 400)
    try:
        return cr.compare(body.a, body.b, EXP)
    except cr.CompareError as e:
        return errors.error(str(e), 404)


@router.get("/api/compare/config")
def compare_config(a: str, b: str):
    """What differs between two evaluation runs besides their scores: the settings of the training runs behind them and the datasets those were trained on. Only the
    settings that are not the same are returned, with the number that are. A side that cannot be read says why. Read-only."""
    from app.backend.pipeline import compare_config as cc, finetune_prep as fp
    for tag in (a, b):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", tag or ""):
            return errors.error("A run name has letters, digits, '_', '.' and '-' only.", 400)
        if not (EXP / tag / "metrics.json").is_file():
            return errors.error(f"There is no scored run called {tag}.", 404)
    return cc.compare(a, b, EXP, fp.RUNS, PROC)


@router.get("/api/compare/cwe")
def compare_cwe(runs: str):
    """The found and false-alarm counts of two to four evaluation runs by weakness class (comma-separated run names). The weakness class of a case is joined from the dataset
    each run was prepared on; a run whose dataset cannot be found says why and gets no column. Read-only."""
    from app.backend.pipeline import compare_cwe as cw, compare_runs as cr, finetune_prep as fp
    tags = [t for t in (runs or "").split(",") if t]
    if not 2 <= len(tags) <= 4 or len(set(tags)) != len(tags):
        return errors.error("Name two to four different runs, separated by commas.", 400)
    for tag in tags:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", tag):
            return errors.error("A run name has letters, digits, '_', '.' and '-' only.", 400)
    try:
        return cw.table(tags, EXP, fp.RUNS, PROC)
    except cr.CompareError as e:
        return errors.error(str(e), 404)


class ImportRun(BaseModel):
    tag: str
    predictions_csv: str
    task: str = "classification"      # "classification" (severity), "detection" or "patch"


@router.post("/api/import-run")
def import_run(b: ImportRun):
    """Import a fine-tuned arm's predictions (from Colab) and score them.

    Writes experiments/<tag>/predictions.csv then runs the eval stage so the new
    arm appears in Results alongside the others, scored by the same evaluator.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", b.tag or ""):
        return errors.error("tag: letters, digits, '_', '.', '-' only", 400)
    d = EXP / b.tag
    if (d / "metrics.json").exists():
        # never overwrite a scored run - new tag, new folder
        return errors.error(f"run '{b.tag}' already exists", 409, hint="Choose a new tag; a scored run is never overwritten.")
    d.mkdir(parents=True, exist_ok=True)
    (d / "predictions.csv").write_text(b.predictions_csv)
    stage = {"patch": "eval_patch", "detection": "eval_detection"}.get(b.task, "eval")
    job = jobs.start(stage, {"tag": b.tag})
    return {"ok": True, "tag": b.tag, "task": b.task, "job": job.id}
