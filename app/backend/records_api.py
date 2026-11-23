"""The Dataset records (the working store): list, edit, import, live fetch, plus the store's status routes."""
from __future__ import annotations

import json

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.backend import errors, jobs
from app.backend.api_common import PROC

router = APIRouter()


class RecordIn(BaseModel):
    language: str | None = None          # set by hand: overrides the git-detected language
    osv_id: str | None = None
    cve_id: str | None = None
    package: str | None = None
    summary: str | None = None
    details: str | None = None
    published: str | None = None
    modified: str | None = None
    withdrawn: str | None = None
    cvss_vector: str | None = None
    cvss_v4_vector: str | None = None
    cvss_score: float | None = None
    osv_cwe: str | None = None
    cwe: str | None = None
    code_before: str | None = None
    code_after: str | None = None
    filename: str | None = None
    source: str | None = None


class FetchReq(BaseModel):
    cve_id: str | None = None
    package: str | None = None


class ImportReq(BaseModel):
    format: str = "json"          # "json" or "csv"
    content: str = ""             # file contents (JSON array or CSV text)


@router.get("/api/dataset/records")
def dataset_records(search: str = "", limit: int = 50, offset: int = 0, language: str = "",
                    code_status: str = "", dependency_status: str = "", severity: str = "", ecosystem: str = "",
                    sort: str = "id", direction: str = Query("desc", alias="dir"), view: str = ""):
    """Records with their code-mining fields (language detected from git, fix commits, code status, pair count), filled in live by Extract / Mine. ``view=list`` gives only what the
    table shows; ``sort`` is id, published, cve_id or ecosystem with ``dir`` asc or desc;
    ``severity`` is low, medium, high, critical or unrated. Counts are exact and page reads are bounded to 500 rows."""
    from app.backend.pipeline import store
    try:
        return store.query(search=search, limit=limit, offset=offset, language=language, code_status=code_status, dependency_status=dependency_status, slim=True,
                           severity=severity, ecosystem=ecosystem, sort=sort, direction=direction, view=view)
    except ValueError as e:
        return errors.error(str(e), 400)


@router.get("/api/dataset/facets")
def dataset_facets(search: str = "", language: str = "", code_status: str = "", dependency_status: str = "", severity: str = "", ecosystem: str = ""):
    """How many records fall under each value of the table's filters (severity, ecosystem, language, code status, dependency status), each counted under the other filters."""
    from app.backend.pipeline import store
    try:
        return store.facets(search=search, language=language, code_status=code_status, dependency_status=dependency_status, severity=severity, ecosystem=ecosystem)
    except ValueError as e:
        return errors.error(str(e), 400)


@router.get("/api/dataset/records/{rec_id}")
def dataset_record(rec_id: int):
    """One record with everything: the advisory text, the affected ranges, the fix pointers and the references (the list leaves them out)."""
    from app.backend.pipeline import store
    rec = store.get(rec_id)
    return rec if rec else errors.error("not found", 404)


@router.post("/api/dataset/records/{rec_id}/dep-versions")
def dataset_record_dep_versions(rec_id: int):
    """Re-check the affected / fixed / safe versions of one record now."""
    from app.backend.pipeline import store
    from app.backend.pipeline.ingest import depversions
    rec = store.get(rec_id)
    if not rec:
        return errors.error("not found", 404)
    depversions.check_record(rec)
    return store.get(rec_id)


@router.get("/api/dataset/records/{rec_id}/fix-commits")
def dataset_record_fix_commits(rec_id: int):
    from app.backend.pipeline import store, codestore
    rec = store.get(rec_id)
    if not rec:
        return errors.error("not found", 404)
    return codestore.fix_commits_for_record(rec)


@router.get("/api/dataset/records/{rec_id}/pairs")
def dataset_record_pairs(rec_id: int):
    from app.backend.pipeline import store, codestore
    rec = store.get(rec_id)
    if not rec:
        return errors.error("not found", 404)
    return codestore.pairs_for_record(rec.get("cve_id"), rec.get("osv_id"))


@router.get("/api/dataset/summary")
def dataset_summary():
    from app.backend.pipeline import store
    return store.summary(funnel_max_age=30)


@router.get("/api/dataset/lineage")
def dataset_lineage():
    """How far the advisories get towards a dataset and why the rest drop out (the numbers of the flow chart)."""
    from app.backend import build_api
    from app.backend.pipeline import lineage
    builds = build_api.builds()["builds"]
    latest = next((b for b in builds if b["status"] == "frozen"), builds[0] if builds else None)
    return lineage.lineage(latest, funnel_max_age=30)


@router.get("/api/dataset/mix")
def dataset_mix(dataset: str = ""):
    """The make-up by language, ecosystem and severity of the collected advisories, or of one dataset build (``dataset`` is its tag)."""
    from app.backend.pipeline import mix
    if not dataset:
        return mix.store_mix()
    out = mix.dataset_mix(PROC, dataset)
    return out or errors.error("no such dataset", 404)


@router.get("/api/dataset/by-year")
def dataset_by_year():
    """How many vulnerabilities were published in each year, split by severity (low, medium, high, critical, and those without a severity word), for the stacked columns."""
    from app.backend.pipeline import points
    return points.by_year()


@router.post("/api/dataset/records")
def dataset_add(rec: RecordIn):
    from app.backend.pipeline import store
    return store.add(rec.model_dump())


@router.put("/api/dataset/records/{rec_id}")
def dataset_update(rec_id: int, rec: RecordIn):
    from app.backend.pipeline import store
    patch = {k: v for k, v in rec.model_dump().items() if v is not None}
    row = store.update(rec_id, patch)
    return row or errors.error("not found", 404)


@router.delete("/api/dataset/records/{rec_id}")
def dataset_delete(rec_id: int):
    """Deletes the record and the code pairs that were added by hand for it; mined
    pairs belong to their fix commit and stay."""
    from app.backend.pipeline import store, codestore
    rec = store.get(rec_id)
    pairs = codestore.delete_manual_pairs_for_record(rec.get("cve_id"), rec.get("osv_id")) if rec else 0
    return {"deleted": store.delete(rec_id), "manual_pairs_deleted": pairs}


@router.delete("/api/dataset/records")
def dataset_clear():
    from app.backend.pipeline import store
    return {"cleared": store.clear()}


@router.post("/api/dataset/fetch")
def dataset_fetch(b: FetchReq):
    from app.backend.pipeline import store
    try:
        return store.fetch_osv(cve_id=b.cve_id, package=b.package)
    except Exception as e:
        return errors.error(f"{type(e).__name__}: {e}", 400)


@router.post("/api/dataset/import")
def dataset_import(b: ImportReq):
    from app.backend.pipeline import store
    import io, csv
    try:
        if b.format == "csv":
            rows = list(csv.DictReader(io.StringIO(b.content)))
        else:
            rows = json.loads(b.content)
            if isinstance(rows, dict):
                rows = rows.get("records", [rows])
        if not isinstance(rows, list):
            raise ValueError("expected a list of records")
        n = store.import_rows(rows)
        return {"imported": n, "total": store.count()}
    except Exception as e:
        return errors.error(f"{type(e).__name__}: {e}", 400)


@router.get("/api/data/stamp")
def data_stamp():
    """A cheap fingerprint of everything the Data page shows: it changes whenever a
    job or an edit writes records, fix commits, code pairs or a dataset build. The
    UI polls it and reloads its panels only when it changed."""
    from app.backend.pipeline import store
    # Every write to the stores moves the size or the modification time of the database files (WAL), so a few stat calls replace the table
    # scans this used to run (about 5 s on a 3 GB store, and the Data page asked every few seconds).
    parts = [store.change_token()]
    try:
        parts.append(str((json.loads(store.get_meta("mining_run") or "{}") or {}).get("heartbeat")))
    except ValueError:
        pass
    for f in ("code_pairs_stats.json", "dataset_stats.json"):
        path = PROC / f
        parts.append(str(path.stat().st_mtime_ns) if path.exists() else "-")
    running = sorted({j["stage"] for j in jobs.listing() if j.get("status") == "running"})
    return {"stamp": "|".join(parts), "running": running}
