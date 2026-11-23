"""Embedded database status, contents, backups and local initialization."""
from __future__ import annotations

from fastapi import APIRouter

from app.backend import errors

router = APIRouter()


@router.get("/api/db/status")
def db_status():
    """The local SQLite file, engine, schema and usable storage state."""
    from app.backend.pipeline.db import database
    return database.status()


@router.get("/api/db/overview")
def db_overview():
    """The collections of the data model with their document counts and sizes (the Database page's table)."""
    from app.backend.pipeline.db import database, schema
    try:
        database.require()
    except database.DatabaseUnavailable as e:
        return errors.error(str(e), 503, code="database_unavailable")
    db = database.database()
    present = set(db.list_collection_names())
    known = {c.name: c.doc for c in schema.COLLECTIONS}
    rows = []
    for name in [*known, *sorted(n for n in present if n not in known and not n.startswith("system."))]:
        what = known.get(name) or "Stored application documents or local retrieval chunks."
        if name not in present:
            rows.append({"name": name, "what": what, "documents": 0, "bytes": 0, "storage_bytes": 0, "index_bytes": 0, "indexes": 0})
            continue
        st = db.command("collStats", name)
        rows.append({"name": name, "what": what, "documents": int(st.get("count", 0)), "bytes": int(st.get("size", 0)),
                     "storage_bytes": int(st.get("storageSize", 0)), "index_bytes": int(st.get("totalIndexSize", 0)), "indexes": int(st.get("nindexes", 0))})
    return {"engine": "sqlite", "database": db.name, "collections": rows, "documents": sum(r["documents"] for r in rows), "bytes": sum(r["bytes"] for r in rows)}


@router.get("/api/db/backups")
def db_backups():
    """The backups on disk, newest first, with the receipt written beside each."""
    from app.backend.pipeline.db import backup
    return {"backups": backup.list_backups(), "folder": str(backup.backup_dir())}


@router.post("/api/db/start")
def db_start():
    """Compatibility action: initialize the local file, without starting another process."""
    from app.backend.pipeline.db import database
    try:
        database.require()
        database.ensure_schema()
        return {"ok": True, "detail": "The local database is ready."}
    except database.DatabaseUnavailable as e:
        return errors.error(str(e), 503, hint="Check the database folder's permissions and available space.", code="database_unavailable")


@router.post("/api/db/ensure-schema")
def db_ensure_schema():
    """Create missing local collections and indexes, without dropping records."""
    from app.backend.pipeline.db import database
    try:
        database.require()
        return {"ok": True, **database.ensure_schema()}
    except database.DatabaseUnavailable as e:
        return errors.error(str(e), 503)
