"""Routes of the review queue (``/api/data/review/*``): the queue, one item, decisions, the audit sample.

Thin wrappers over ``pipeline/review.py``; the gate inputs that later stages provide (split manifest,
context completeness, hidden-fact check) are not available here yet, so the gate reports them as not
evaluated and the queue lists candidates, never auto_checked rows.
"""
from __future__ import annotations

import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.backend import errors

router = APIRouter(prefix="/api/data/review")
from app.backend.review_exchange_api import router as exchange_router
router.include_router(exchange_router)


class DecisionIn(BaseModel):
    pair_key: str
    reviewer: str
    rubric_version: str
    review_status: str = "human_reviewed"
    label_state: str
    fields: dict = {}
    notes: str | None = None


class AuditIn(BaseModel):
    audit_id: str | None = None
    n: int = 50
    ecosystem: str | None = None


def _bad(msg: str, code: int = 400) -> JSONResponse:
    return errors.error(msg, code)


@router.get("/queue")
def review_queue(limit: int = 50, offset: int = 0, ecosystem: str = "", include_failed: bool = False, review_state: str = 'waiting'):
    """Candidates waiting for a person, strongest evidence first."""
    from app.backend.pipeline import derived, reviewview
    if review_state not in ('waiting', 'ai_adjudicated', 'all'):
        return _bad('Unknown review queue state')
    from app.backend.pipeline.db import legacy
    # Queue rows need decision identity/completion, not saved explanation bodies.
    decisions = {doc['_id']: doc for doc in legacy.handle()['review_current'].find({}, {
        'decision_id': 1, 'review_status': 1, 'label_state': 1, 'fields._review_exchange': 1})}
    return reviewview.queue(decisions, limit=max(1, min(limit, 500)), offset=max(0, offset), ecosystem=ecosystem or None,
                            include_failed=include_failed, review_state=review_state)


@router.get("/item")
def review_item(pair_key: str):
    from app.backend.pipeline import derived, review
    from app.backend.pipeline.schemas import reviewed
    row = review.load_pair(pair_key)
    if not row:
        return _bad("not found", 404)
    with derived.connect() as conn:
        decision = derived.latest_decisions(conn).get(reviewed.record_id_of(pair_key))
    result = review.gate(row)
    status, label = review.effective_status(result, decision)
    return {"record_id": reviewed.record_id_of(pair_key), "row": row, "gate": result.as_dict(), "decision": decision,
            "effective": {"review_status": status, "label_state": label}, "rubric_version": review.RUBRIC_VERSION}


@router.post("/decision")
def review_decision(body: DecisionIn):
    """Append one review decision (never an update); only a person or a model adjudication can raise a tier."""
    from app.backend.pipeline import derived, review
    from app.backend.pipeline.schemas import reviewed
    if body.rubric_version != review.RUBRIC_VERSION:
        return _bad(f"unknown rubric version {body.rubric_version!r}; the current one is {review.RUBRIC_VERSION}")
    if body.review_status not in ("human_reviewed", "llm_adjudicated"):
        return _bad("a decision is either human_reviewed or llm_adjudicated; auto_checked comes from the gate only")
    row = review.load_pair(body.pair_key)
    if row is None:
        return _bad("not found", 404)
    problems = review.validate_fields(body.fields, body.label_state, row.get("code_before"), row.get('code_after'))
    if problems:
        return _bad("; ".join(problems))
    try:
        with derived.connect() as conn:
            did = derived.add_review_decision(conn, record_id=reviewed.record_id_of(body.pair_key), reviewer=body.reviewer,
                                              rubric_version=body.rubric_version, review_status=body.review_status,
                                              label_state=body.label_state, fields=body.fields, notes=body.notes)
    except ValueError as e:
        return _bad(str(e))
    return {"ok": True, "decision_id": did, "record_id": reviewed.record_id_of(body.pair_key)}


@router.post("/audit")
def review_audit(body: AuditIn):
    """Draw (once) the stratified audit sample of gate candidates; asking again returns the stored sample."""
    from app.backend.pipeline import derived, review
    audit_id = body.audit_id or "audit-" + datetime.date.today().isoformat()
    rows = review.load_rows(body.ecosystem)
    with derived.connect() as conn:
        items = review.draw_audit(conn, audit_id, rows, max(1, min(body.n, 2000)))
        decisions = derived.latest_decisions(conn)
    for i in items:
        d = decisions.get(i["record_id"])
        i["decision"] = {"review_status": d["review_status"], "label_state": d["label_state"]} if d else None
    return {"audit_id": audit_id, "items": items}


@router.get("/error-rates")
def review_error_rates(audit_id: str = "", tolerance: float = 0.10):
    from app.backend.pipeline import derived, review
    with derived.connect() as conn:
        audit_id = audit_id or review.latest_audit_id(conn) or ""
        audit = review.load_audit(conn, audit_id) if audit_id else []
        decisions = derived.latest_decisions(conn)
    return {"audit_id": audit_id or None, **review.error_rates(audit, decisions, tolerance=tolerance)}


@router.get("/summary")
def review_summary():
    """Gate outcomes over every usable pair, what could not be evaluated yet, and the audit estimate."""
    from app.backend.pipeline import derived, review, reviewview
    with derived.connect() as conn:
        decisions = derived.latest_decisions(conn)
    counted = reviewview.summary(decisions)          # the database counts the distinct gate inputs; see reviewview.py
    mix, rows = counted["tiers"], counted["rows"]
    with derived.connect() as conn:
        audit_id = review.latest_audit_id(conn)
        audit = review.load_audit(conn, audit_id) if audit_id else None
    rates = review.error_rates(audit, decisions) if audit_id else None
    return {"rows": rows, "candidates": counted["candidates"], "auto_checked": mix["auto_checked"], "tiers": mix,
            "failed": counted["failed"], "not_evaluated": counted["not_evaluated"],
            "decisions": len(decisions), "audit_id": audit_id, "rubric_version": review.RUBRIC_VERSION,
            "audit_error_rate": rates["rate"] if rates and rates["status"] == "estimated" else "unestimated"}
