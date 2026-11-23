"""What each OSV ingest run did, kept so the Collections page can show it: per ecosystem, the rows read, the advisories written, how many were new and how many
only updated what was already there, the rows folded into an advisory that had several affected packages, the rows left out by the collection date, and an ecosystem
that failed. The last runs are kept in the store's meta table (a small JSON list), newest first."""
from __future__ import annotations

import datetime
import json

KEY = "ingest_runs"
KEEP = 20


def ecosystem_row(ecosystem: str, *, rows: int = 0, kept: int | None = None, advisories: int = 0, count_before: int = 0, count_after: int = 0,
                  error: str | None = None) -> dict:
    """One ecosystem of a run. ``rows`` are the affected-package rows read, ``kept`` those that stayed after the collection date (all when None), ``advisories`` how
    many advisories the store wrote for them, and the two counts are the number of advisories in the store before and after."""
    kept = rows if kept is None else kept
    new = max(0, count_after - count_before)
    return {"ecosystem": ecosystem, "rows": int(rows), "left_out": int(rows - kept), "advisories": int(advisories), "new": int(min(new, advisories)),
            "updated": int(max(0, advisories - new)), "merged_rows": int(max(0, kept - advisories)), "error": error}


def totals(rows: list[dict]) -> dict:
    keys = ("rows", "left_out", "advisories", "new", "updated", "merged_rows")
    return {**{k: sum(r[k] for r in rows) for k in keys}, "failed": sum(1 for r in rows if r.get("error"))}


def record(rows: list[dict], *, since: str | None = None, refresh: bool = False, store=None) -> dict:
    """Add a run to the kept list and return it."""
    from app.backend.pipeline import store as default
    store = store or default
    run = {"at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "since": since, "refresh": bool(refresh), "ecosystems": rows, "totals": totals(rows)}
    store.set_meta(KEY, json.dumps([run, *runs(store)][:KEEP]))
    return run


def runs(store=None) -> list[dict]:
    """The kept runs, newest first (empty when there is none or the saved text cannot be read)."""
    from app.backend.pipeline import store as default
    raw = (store or default).get_meta(KEY, None)
    try:
        out = json.loads(raw) if raw else []
    except ValueError:
        return []
    return out if isinstance(out, list) else []
