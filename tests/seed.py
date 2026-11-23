"""Seed a MongoDB test database with rows in the legacy flat shape (the column names of the old ``records``, ``fix_commits`` and ``code_pairs`` tables).

The tests that used to INSERT into SQLite build a list of dicts with the same column names and call these three functions instead; the data layer's own mappers and writers turn the rows into
documents, so what a test reads back is what the application itself would have stored. The tests must be marked ``mongo`` (tests/conftest.py gives each one its own ``aivf_test_*`` database).

    seed.records([{"key": "GHSA-1", "osv_id": "GHSA-1", "cve_id": "CVE-2024-1", "ecosystem": "npm", "package": "lib-a", ...}])
    seed.commits([{"commit_key": "c1", "host": "github.com", "owner": "o", "repo": "one", "sha": "a1" * 20, "repo_url": "https://github.com/o/one", ...}])
    seed.pairs([{"pair_key": "c1:a.py:render", "commit_key": "c1", "granularity": "function", ...}])

Records come first (a pair takes its advisory fields from them), then commits, then pairs.
"""
from __future__ import annotations

from app.backend.pipeline import codestore
from app.backend.pipeline.db import legacy, mappers as m

STAMP = "2026-01-01T00:00:00Z"


def records(rows: list[dict]) -> None:
    """Advisory records, with the fix-commit pointers of ``fix_commits`` written as links."""
    if not rows:
        return
    db = legacy.handle()
    first = legacy.ids(db, "advisories", len(rows))
    for i, row in enumerate(rows):
        legacy.save_advisory(db, {"id": first + i, "key": row.get("key") or row.get("osv_id"), "created": STAMP, "updated": STAMP, **row})
    legacy.touch(db)


def commits(rows: list[dict]) -> None:
    """Fix commits as they stand after mining (status ``mined`` unless the row says otherwise)."""
    if not rows:
        return
    db = legacy.handle()
    first = legacy.ids(db, "fix_commits", len(rows))
    for i, row in enumerate(rows):
        doc = m.commit_doc({"id": first + i, "status": "mined", "attempts": 0, "created": STAMP, "updated": STAMP, **row})
        db["fix_commits"].replace_one({"_id": doc["_id"]}, doc, upsert=True)
    legacy.touch(db)


def pairs(rows: list[dict]) -> int:
    """Code pairs, written by the application's own upsert (the rows are the flat pair shape)."""
    return codestore.upsert_pairs(rows) if rows else 0
