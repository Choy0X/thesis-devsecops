"""Routes of CVE Collections: which OSV ecosystems exist and what of each is on this computer. Read-only; downloading is a job, not a route."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.backend import errors

router = APIRouter()


@router.get("/api/collections/ecosystems")
def collections_ecosystems():
    """Every ecosystem OSV publishes (the saved list when OSV cannot be reached) with its dump, snapshot date and ingest state."""
    from app.backend.pipeline.ingest import osv_catalog
    return osv_catalog.catalog()


@router.get("/api/collections/ecosystems/{name}/update")
def collections_ecosystem_update(name: str):
    """Whether OSV has a newer copy of one ecosystem's dump than the one on this computer (one conditional request; nothing is downloaded)."""
    from app.backend.pipeline.ingest import osv_catalog
    return osv_catalog.check_update(name)


@router.get("/api/collections/cves")
def collections_cves(q: str = "", ecosystem: str = "", state: str = "", limit: int = Query(50, ge=1), offset: int = Query(0, ge=0)):
    """Advisories in the downloaded OSV dumps whose CVE id, advisory id, alias or package holds ``q``, newest first, one result per advisory with its packages, each with its state:
    ``in_store`` (the store holds it) or ``downloaded`` (only in the dump). ``state`` keeps one of them. Searches the tables the ingest parsed; needs no network."""
    from app.backend.pipeline.ingest import cve_search
    try:
        return cve_search.search(q, ecosystem, state, limit, offset)
    except ValueError as e:
        return errors.error(str(e), 400)


@router.get("/api/collections/cves/{ecosystem}/{osv_id}")
def collections_cve(ecosystem: str, osv_id: str):
    """One advisory of a downloaded dump in full (text, severity and vectors, weaknesses, aliases, affected packages and ranges, fix commits, references, dates, its state in the
    store and the raw record). Needs no network."""
    from app.backend.pipeline.ingest import cve_search
    try:
        found = cve_search.detail(ecosystem, osv_id)
    except ValueError as e:
        return errors.error(str(e), 400)
    if found is None:
        return errors.error("That advisory is not in the downloaded dump of this ecosystem.", 404)
    return found
