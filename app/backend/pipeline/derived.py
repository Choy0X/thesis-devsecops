"""The derived store: everything computed from the raw collections, in local SQLite.

The mined collections (advisories, fix commits, code pairs) stay the record of what was collected. What is computed from them lives beside them, so that computing it never rewrites a raw document:
review decisions (append-only: a new decision is a new document, and the current one of a record is its newest), link recovery attempts, leakage graphs, split manifests, model exposure and
contamination, context bundles and the campaigns. The functions that read and write them are in ``db/derived_mongo.py`` and ``campaigns/repo_mongo.py``; this module is the door the rest of the
application goes through. The ``*_mongo`` module names are retained for import compatibility; they use the embedded document facade.
"""
from __future__ import annotations

from app.backend.pipeline.db import derived_mongo
from app.backend.pipeline.schemas import reviewed


def connect():
    """The repository handle: ``with connect() as conn:``. Closing it is a no-op; the application owns the shared SQLite connection."""
    return derived_mongo.connect()


def mongo_of(conn):
    """The module that implements the derived store's functions (``conn`` is the handle from ``connect``; it is passed on to them)."""
    return derived_mongo


def add_review_decision(conn, *, record_id: str, reviewer: str, rubric_version: str, review_status: str, label_state: str, fields: dict | None = None, notes: str | None = None) -> int:
    """Append one decision. The status and label must come from the schema vocabularies."""
    if review_status not in reviewed.REVIEW_STATUSES:
        raise ValueError(f"review_status {review_status!r} is not one of {list(reviewed.REVIEW_STATUSES)}")
    if label_state not in reviewed.LABEL_STATES:
        raise ValueError(f"label_state {label_state!r} is not one of {list(reviewed.LABEL_STATES)}")
    if not (record_id and reviewer and rubric_version):
        raise ValueError("record_id, reviewer and rubric_version are required")
    return derived_mongo.add_review_decision(conn, record_id=record_id, reviewer=reviewer, rubric_version=rubric_version, review_status=review_status,
                                             label_state=label_state, fields=fields, notes=notes)


def latest_decisions(conn) -> dict[str, dict]:
    """The current decision per record (its newest); older ones stay as history."""
    return derived_mongo.latest_decisions(conn)
