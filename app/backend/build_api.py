"""Fixed dataset profiles, durable build requests, inventory and saved split reports."""
from __future__ import annotations

import json
import pathlib
from typing import Annotated
import re

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from app.backend import errors

router = APIRouter(prefix="/api/data")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _root() -> pathlib.Path:
    from app.backend.pipeline import versions
    return pathlib.Path(versions.PROC)


def _read(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@router.get("/build-profiles")
def build_profiles():
    from app.backend.pipeline import build_catalog
    return build_catalog.profiles()


class BuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: str


@router.post("/builds")
def create_build(body: BuildRequest):
    from app.backend.pipeline import build_catalog
    from fastapi import HTTPException
    try:
        return {"build": build_catalog.create(body.profile)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/builds/{tag}/retry")
def retry_build(tag: str):
    from app.backend.pipeline import build_catalog
    from fastapi import HTTPException
    if not _TAG.fullmatch(tag):
        return errors.error("bad tag", 400)
    try:
        return {"build": build_catalog.retry(tag)}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/builds")
def builds(limit: Annotated[int, Query(ge=1, le=500)] = 100, offset: Annotated[int, Query(ge=0)] = 0, search: str = "", state: str = ""):
    """Indexed summaries, with exact totals for the selected page/filter."""
    from app.backend.pipeline import versions
    from app.backend.pipeline import build_catalog
    root = _root()
    try:
        result = build_catalog.list_builds(limit=limit, offset=offset, search=search, state=state, root=root)
    except ValueError as exc:
        return errors.error(str(exc), 400)
    from app.backend.pipeline import evidence_recovery
    fingerprint = evidence_recovery.evidence_fingerprint(root)
    result['builds'] = [{**row, **evidence_recovery.catalog_state(row['tag'], root, _row=row, _fingerprint=fingerprint)}
                        for row in result['builds']]
    return {**result, "next_version": versions.next_unused_version("dataset", root),
            "next_versions": {f: versions.next_unused_version(f, root) for f in ("dataset", "code_pairs")}}


@router.get("/builds/{tag}")
def build_detail(tag: str):
    if not _TAG.match(tag):
        return errors.error("bad tag", 400)
    from app.backend.pipeline import build_catalog
    row = build_catalog.training_summary(build_catalog.get(tag))
    if row:
        tag = row['tag']
    d = _root() / f"dataset_{tag}"
    manifest = _read(d / "manifest.json")
    if not manifest and not row:
        return errors.error("not found", 404)
    evidence = build_catalog.evidence_state(tag, root=_root())
    from training.finetune.quality_gate import assess_manifest
    return {"manifest": manifest, "stats": _read(d / "stats.json"), "full_training": assess_manifest(manifest), "build": {**row, **evidence} if row else row,
            **evidence,
            "reports": {name: _read(d / (name + ".json")) for name in
                        ("security_readiness", "security_partitions", "contamination_report")}}


@router.get('/builds/{tag}/evidence')
def build_evidence(tag: str, limit: Annotated[int, Query(ge=1, le=500)] = 100,
                   offset: Annotated[int, Query(ge=0)] = 0):
    from app.backend.pipeline import evidence_recovery
    if not _TAG.fullmatch(tag):
        return errors.error('bad tag', 400)
    try:
        return evidence_recovery.evidence_page(tag, limit=limit, offset=offset, root=_root())
    except ValueError as exc:
        return errors.error(str(exc), 404)


@router.get('/builds/{tag}/context-repair')
def context_repair_candidates(tag: str, limit: Annotated[int, Query(ge=1, le=500)] = 100,
                              offset: Annotated[int, Query(ge=0)] = 0, state: str = 'all'):
    from app.backend.pipeline import insufficient_context_repair
    try:
        return insufficient_context_repair.page(tag, root=_root(), limit=limit, offset=offset, state=state)
    except ValueError as exc:
        return errors.error(str(exc), 404)


@router.get('/builds/{tag}/context-repair/ids')
def context_repair_ids(tag: str, state: str = 'all'):
    from app.backend.pipeline import insufficient_context_repair
    try:
        return insufficient_context_repair.selected_ids(tag, root=_root(), state=state)
    except ValueError as exc:
        return errors.error(str(exc), 404)


@router.get('/builds/{tag}/context-repair/attempts')
def context_repair_attempts(tag: str, limit: Annotated[int, Query(ge=1, le=500)] = 100,
                            offset: Annotated[int, Query(ge=0)] = 0):
    from app.backend.pipeline import insufficient_context_repair as repair
    try:
        return repair.attempts(tag, root=_root(), limit=limit, offset=offset)
    except ValueError as exc:
        return errors.error(str(exc), 404)


@router.get('/builds/{tag}/context-repair/attempts/{attempt_id}/outcomes')
def context_repair_outcomes(tag: str, attempt_id: str, limit: Annotated[int, Query(ge=1, le=500)] = 100,
                            offset: Annotated[int, Query(ge=0)] = 0):
    from app.backend.pipeline import insufficient_context_repair as repair
    try:
        return repair.outcomes(tag, attempt_id, root=_root(), limit=limit, offset=offset)
    except ValueError as exc:
        return errors.error(str(exc), 404)


@router.get('/builds/{tag}/context-repair/attempts/{attempt_id}/events')
def context_repair_events(tag: str, attempt_id: str, limit: Annotated[int, Query(ge=1, le=500)] = 100,
                          after: Annotated[int, Query(ge=0)] = 0):
    from app.backend.pipeline import insufficient_context_repair as repair
    try:
        return repair.events(tag, attempt_id, root=_root(), limit=limit, after=after)
    except ValueError as exc:
        return errors.error(str(exc), 404)


@router.post('/builds/{tag}/context-repair/attempts/{attempt_id}/cancel')
def cancel_context_repair_attempt(tag: str, attempt_id: str):
    from app.backend.pipeline import insufficient_context_repair as repair
    try:
        return repair.cancel_attempt(tag, attempt_id, root=_root())
    except ValueError as exc:
        return errors.error(str(exc), 404)


@router.get('/builds/{tag}/context-repair/items/{record_id}')
def context_repair_detail(tag: str, record_id: str, attempt_id: str | None = None):
    from app.backend.pipeline import insufficient_context_repair as repair
    try:
        return repair.detail(tag, record_id, root=_root(), attempt_id=attempt_id)
    except ValueError as exc:
        return errors.error(str(exc), 404)


class ContextRepairRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ids: list[str]
    method: str = 'source'


@router.post('/builds/{tag}/context-repair')
def launch_context_repair(tag: str, body: ContextRepairRequest):
    from app.backend import jobs
    from app.backend.pipeline import insufficient_context_repair
    try:
        selection = insufficient_context_repair.resolve(tag, body.ids, method=body.method, root=_root())
        if body.method != 'source':
            return selection
        attempt = insufficient_context_repair.begin_attempt(tag, selection['record_ids'], root=_root())
        try:
            job = jobs.start('repair_insufficient_context', {'tag': tag, 'ids': selection['record_ids'],
                'attempt_id': attempt['attempt_id'], 'build_root': str(_root().resolve())})
        except BaseException:
            insufficient_context_repair.cancel_attempt(tag, attempt['attempt_id'], root=_root())
            raise
        return {'job_id': job.id, 'attempt_id': attempt['attempt_id'], 'source_manifest_sha256': selection['source_manifest_sha256']}
    except ValueError as exc:
        return errors.error(str(exc), 422)
    except (jobs.Blocked, jobs.TooManyJobs) as exc:
        return errors.error(str(exc), 409)


class EvidenceRepairRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str = 'repair'


@router.post('/builds/{tag}/repair-evidence')
def repair_build_evidence(tag: str, body: EvidenceRepairRequest):
    from app.backend import jobs
    from app.backend.pipeline import evidence_recovery
    if not _TAG.fullmatch(tag):
        return errors.error('bad tag', 400)
    try:
        return evidence_recovery.start(tag, action=body.action, root=_root())
    except ValueError as exc:
        return errors.error(str(exc), 422)
    except (jobs.Blocked, jobs.TooManyJobs) as exc:
        return errors.error(str(exc), 409)


@router.get("/split-check")
def split_check():
    """The saved split manifests with their validation roles."""
    from app.backend.pipeline import derived
    saved = []
    with derived.connect() as conn:
        for h in derived.mongo_of(conn).manifest_listing(conn):
            manifest = json.loads(h["body"]) if h["body"] else {}
            saved.append({"version": h["version"], "parent": h["parent"], "sha256": h["sha256"], "summary": h["summary"],
                          "val_roles": manifest.get("val_roles")})
    return {"split_versions": saved}
