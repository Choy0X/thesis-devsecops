"""Code pairs (vulnerable to fixed): list and edit, enrichment, mining control, reset, and the code-pair dataset options, preview and export."""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, Query
from typing import Annotated
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.backend import errors, jobs
from app.backend.api_common import PROC, REPOS_DIR, cached_dir_size, dir_size, forget_dir_size, mining_job

router = APIRouter()
from app.backend.context_json_api import router as context_json_router
router.include_router(context_json_router)


from app.backend.campaigns_api import context_filter_query


@router.get('/api/code/context-items')
def collected_context_items(offset: Annotated[int, Query(ge=0)] = 0,
                            limit: Annotated[int, Query(ge=1, le=500)] = 100,
                            filters: dict = Depends(context_filter_query)):
    """Optional context suggestions, paginated independently of human review."""
    from app.backend.pipeline.acceleration.context_gate import list_items
    return list_items(None, offset=offset, limit=limit, filters=filters)


@router.get('/api/code/context-item-ids')
def collected_context_item_ids(filters: dict = Depends(context_filter_query)):
    from app.backend.pipeline.acceleration.context_gate import item_ids
    return item_ids(None, filters=filters)


def _reviewed_context_generation(ids):
    from app.backend.pipeline import context_exchange, llm_client
    result = context_exchange.generate_configured(ids)
    return {**result, 'enriched': result['completed'] + result['resumed'],
            'errors': [item['reason'] for item in result['failures']], 'model': llm_client.resolved().model_label}


@router.post('/api/code/enrich/export')
def export_context_review(body: dict):
    from app.backend.context_json_api import create
    return create(body)


@router.post('/api/code/enrich/import/preview')
def preview_context_review(body: dict):
    from app.backend.pipeline import context_exchange
    try:
        return context_exchange.default().preview(body.get('response'))
    except (ValueError, OSError, KeyError) as error:
        return errors.error(str(error), 400)


@router.post('/api/code/enrich/import')
def import_context_review(body: dict):
    from app.backend.pipeline import context_exchange
    try:
        selected = body.get('selected')
        if selected is not None and (not isinstance(selected, list) or any(not isinstance(i, str) for i in selected)):
            raise ValueError('selected must be an array of item IDs')
        return context_exchange.default().apply(body.get('response'), selected=selected)
    except (ValueError, OSError, KeyError) as error:
        return errors.error(str(error), 400)


@router.get('/api/code/enrich/local-status')
def local_context_status():
    from app.backend.pipeline import llm_client
    from app.backend.pipeline.gateway import client
    resolved = llm_client.resolved()
    from app.backend.pipeline import model_access
    return {'engine': resolved.engine, 'model': resolved.model_label, 'configured': resolved.configured,
            'execution': client.status(), 'models': model_access.default().models()}


@router.get("/api/code/summary")
def code_summary():
    from app.backend.pipeline import codestore
    from app.backend.pipeline.ingest.codemine import LANG_BY_EXT
    stats = PROC / "code_pairs_stats.json"
    return {"fix_commits": codestore.fix_commit_summary(), "pairs": codestore.pair_summary(),
            "dataset": json.loads(stats.read_text()) if stats.exists() else None,
            "languages": sorted(set(LANG_BY_EXT.values()))}


class PairIn(BaseModel):
    osv_id: str | None = None
    cve_id: str | None = None
    ghsa_id: str | None = None
    ecosystem: str | None = None
    package: str | None = None
    cwe: str | None = None
    cvss_vector: str | None = None
    published: str | None = None
    repo_url: str | None = None
    fix_commit: str | None = None
    parent_commit: str | None = None
    file_path: str | None = None
    language: str | None = None
    function_name: str | None = None
    granularity: str | None = None
    is_test: bool | None = None
    code_before: str | None = None
    code_after: str | None = None
    explanation: str | None = None
    commit_message: str | None = None


@router.post("/api/code/pairs")
def code_pair_create(b: PairIn):
    from app.backend.pipeline import codestore
    try:
        return codestore.create_pair(b.model_dump())
    except ValueError as e:
        return errors.error(str(e), 400)


@router.put("/api/code/pairs/{pair_id}")
def code_pair_update(pair_id: int, b: PairIn):
    from app.backend.pipeline import codestore
    row = codestore.update_pair(pair_id, b.model_dump(exclude_unset=True))
    return row or errors.error("not found", 404)


@router.delete("/api/code/pairs/{pair_id}")
def code_pair_delete(pair_id: int):
    from app.backend.pipeline import codestore
    return {"deleted": codestore.delete_pair(pair_id)}


@router.post("/api/code/enrich/candidates")
def code_enrich_candidates(body: dict):
    """Pairs a dataset build would contain, rated by their real context (weak /
    partial / strong), filtered for LLM enrichment; a page of rows + chip counts."""
    from app.backend.pipeline.ingest import enrich
    body = body or {}
    f = {k: v for k, v in (body.get("filters") or {}).items() if k in enrich.DEFAULTS}
    return enrich.candidates(f, limit=min(int(body.get("limit", 50)), 500),
                             offset=max(0, int(body.get("offset", 0))))


@router.post("/api/code/pairs/{pair_id}/enrich")
def code_pair_enrich(pair_id: int):
    """Draft root cause / secure pattern / explanation for one pair with the LLM
    (stored as synthetic *_llm fields; the real advisory text is kept)."""
    from app.backend.pipeline import codestore
    from app.backend.pipeline.ingest import enrich
    if not codestore.get_pair(pair_id):
        return errors.error("not found", 404)
    try:
        res = _reviewed_context_generation([pair_id])
    except Exception as e:
        return errors.error(str(e), 400)
    return {**res, "pair": codestore.get_pair(pair_id)}


@router.post("/api/dataset/records/{rec_id}/enrich")
def dataset_record_enrich(rec_id: int):
    """LLM-enrich every usable pair of one record (its advisory text is given to
    the model when a pair has none)."""
    from app.backend.pipeline import store, codestore
    from app.backend.pipeline.ingest import enrich
    rec = store.get(rec_id)
    if not rec:
        return errors.error("not found", 404)
    ids = [p["id"] for p in codestore.pairs_for_record(rec.get("cve_id"), rec.get("osv_id"))
           if p.get("code_available") and not p.get("is_test") and not p.get("tangled")]
    if not ids:
        return errors.error("this record has no usable code pair to enrich", 400)
    from app.backend.pipeline.ingest import context as _ctx
    advisory = _ctx.advisory_text(rec.get("summary"), rec.get("details"))
    try:
        return _reviewed_context_generation(ids)
    except Exception as e:
        return errors.error(str(e), 400)


@router.get("/api/code/mining/checkpoint")
def mining_checkpoint():
    """The last mining run: interrupted (resumable), active, or finished."""
    from app.backend.pipeline import codestore, mining_run
    rec = mining_run.load()
    st = codestore.fix_status_counts()
    return {"run": rec, "interrupted": mining_run.interrupted(rec), "active": mining_run.alive(rec),
            "active_here": mining_job() is not None,
            "pending": st.get("pending", 0), "failed": st.get("failed", 0),
            "repos_bytes": cached_dir_size(REPOS_DIR) if REPOS_DIR.exists() else 0}


@router.post("/api/code/mining/resume")
def mining_resume():
    """Continue an interrupted run with its settings (the queue is the checkpoint)."""
    from app.backend.pipeline import mining_run
    rec = mining_run.load()
    if mining_run.alive(rec) or mining_job():
        return errors.error("a mining run is already active", 409)
    params = {k: v for k, v in ((rec or {}).get("params") or {}).items() if v is not None}
    job = jobs.start("mine_code", params)
    return {"ok": True, "job": job.id, "params": params}


@router.post("/api/code/mining/discard")
def mining_discard():
    from app.backend.pipeline import mining_run
    mining_run.discard()
    return {"ok": True}


@router.post("/api/code/mining/stop-orphan")
def mining_stop_orphan():
    """Stop a miner left running by an earlier session of the app (e.g. after a
    crash), which would otherwise compete with a new run for the same queue."""
    from app.backend.pipeline import mining_run
    rec = mining_run.load()
    if not mining_run.alive(rec) or mining_job():
        return errors.error("no orphaned mining run", 409)
    pid = int(rec.get("pid") or 0)
    try:
        if os.name == "nt":
            import subprocess
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except Exception as e:
        return errors.error(f"could not stop process {pid}: {e}", 500)
    mining_run.finish("stopped")
    return {"ok": True, "pid": pid}


class ResetReq(BaseModel):
    scope: str = "mining"            # mining | all
    keep_manual: bool = True
    delete_repos: bool = False


@router.post("/api/code/reset")
def code_reset(b: ResetReq):
    """Clear mined data (see codestore.reset_mining); optionally the git cache."""
    from app.backend.pipeline import codestore, mining_run
    if mining_run.alive(mining_run.load()) or mining_run.any_alive() or mining_job():
        return errors.error("stop the running mining job first", 409)
    if b.scope not in ("mining", "all"):
        return errors.error("scope must be 'mining' or 'all'", 400)
    res = codestore.reset_mining(b.scope, b.keep_manual)
    if b.delete_repos and REPOS_DIR.exists():
        import shutil
        import stat

        def _force(func, path, _exc):            # git objects are read-only on Windows
            os.chmod(path, stat.S_IWRITE)
            func(path)
        freed = cached_dir_size(REPOS_DIR, wait=0) or dir_size(REPOS_DIR)       # what was last measured, else measure once now
        shutil.rmtree(REPOS_DIR, onerror=_force)
        forget_dir_size(REPOS_DIR)
        res["repos_freed_bytes"] = freed
    return res


@router.get("/api/code/fix-commits")
def code_fix_commits(status: str = "", search: str = "", limit: int = 50, offset: int = 0):
    from app.backend.pipeline import codestore
    return codestore.list_fix_commits(status=status, search=search, limit=limit, offset=offset)


@router.get("/api/code/pairs")
def code_pairs(language: str = "", granularity: str = "", search: str = "",
               available: str = "", source: str = "", limit: int = 50, offset: int = 0):
    from app.backend.pipeline import codestore
    return codestore.list_pairs(limit=limit, offset=offset, language=language,
                                granularity=granularity, search=search,
                                available=available, source=source)


@router.get("/api/code/pairs/{pair_id}")
def code_pair(pair_id: int):
    from app.backend.pipeline import codestore
    row = codestore.get_pair(pair_id)
    return row or errors.error("not found", 404)


@router.get("/api/code/dataset/options")
def code_dataset_options():
    """Filter choices from the mined Dataset records, the collection window and the
    temporal-split cutoff."""
    from app.backend.pipeline.ingest import build_code_dataset
    return build_code_dataset.options()


@router.post("/api/code/dataset/preview")
def code_dataset_preview(filters: dict):
    """What a build with these filters would produce (nothing is written)."""
    from app.backend.pipeline.ingest import build_code_dataset
    f = {k: v for k, v in (filters or {}).items() if k in build_code_dataset.DEFAULTS}
    return build_code_dataset.preview(f)


@router.get("/api/code/export")
def code_export(fmt: str = "jsonl"):
    """Download the built code dataset (latest frozen version if present, else preview):
    jsonl (one sample per pair), detection (vulnerable=1 / safe=0 rows) or parquet."""
    from app.backend.pipeline import versions
    if fmt == "dependencies":                # affected / fixed / safe versions per package
        f = versions.resolve("dependencies", ".jsonl", "dependencies_preview.jsonl", PROC)
    else:
        suffix = {"jsonl": ".jsonl", "detection": "_detection.jsonl"}.get(fmt, ".parquet")
        f = versions.resolve("code_pairs", suffix, f"code_pairs_preview{suffix}", PROC)
    if f.exists():
        return FileResponse(f, filename=f.name, media_type="application/octet-stream")
    return errors.error("no code-pair dataset built yet", 404)
