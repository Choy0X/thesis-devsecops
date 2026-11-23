"""The review summary and the review queue, answered by the database instead of a loop over every pair.

Both views would otherwise read every usable pair, build a dict for each and run the gate on each, on every request. Two facts make that
unnecessary, and neither needs a stored copy that a writer could leave stale (the gate's inputs are written by a dozen code paths):

* The gate reads a dozen small fields, so the summary counts the **distinct combinations** of them in the database (a grouping aggregation)
  and runs the one real ``review.gate`` per combination. The answer is the same as gating every row because the gate is a function of those
  fields only.
* Only a small part of the pairs can pass the gate at all, so the queue asks the database for a superset of the candidates with conditions that
  every passing row must meet, reads those rows, and runs the unchanged ``review.queue`` (real gate, real ordering) on them. A row the
  prefilter keeps by mistake is dropped by the gate; the prefilter is built from the gate's own constants.

``include_failed`` (the queue of rows that failed the gate) has no small set to start from, so it keeps the whole-collection path.
"""
from __future__ import annotations

from collections import Counter

from app.backend.pipeline import review
from app.backend.pipeline.schemas import reviewed
from app.backend.pipeline.db import pipeline_mongo as _pipeline_mongo


def _group_row(key) -> dict:
    """A stand-in row for one combination of inputs, carrying exactly what the gate reads."""
    fix_check, evidence, parent_unset, reason_empty, quarantine, parse, is_test, tangled, basis, source, needs = key
    return {"fix_check": fix_check, "evidence_level": evidence, "parent_count": None if parent_unset else 1,
            "parent_selection_reason": None if reason_empty else "recorded", "quarantine_reason": quarantine, "parse_status": parse,
            "is_test": is_test, "tangled": tangled, "code_available": 1, "attribution_basis": basis, "explanation_source": source,
            "needs_explanation_enrichment": needs}


def queue(decisions: dict, *, limit: int = 50, offset: int = 0, ecosystem: str | None = None, include_failed: bool = False, review_state: str = 'waiting') -> dict:
    from app.backend.pipeline.review_exchange import queue_decisions
    decisions = queue_decisions(decisions)
    from app.backend.pipeline.db.review_pages import page
    return page(decisions, limit=limit, offset=offset, ecosystem=ecosystem,
                include_failed=include_failed, review_state=review_state)


def _decided_rows(decided: dict) -> dict[str, dict]:
    """The usable pairs behind the decisions, by record id. A decision follows its pair through a hash of the pair key, so the pair is found by
    hashing: the candidates first (people review those), and only when a decision is not among them, every usable pair key."""
    found: dict[str, dict] = {}
    for row in candidate_rows():
        rid = reviewed.record_id_of(row["pair_key"])
        if rid in decided:
            found[rid] = row
    if len(found) < len(decided):
        found.update(_rows_of_decisions(decided, found))
    return found


def summary(decisions: dict) -> dict:
    """Gate outcomes over every usable pair: the same numbers as gating each row, from the combinations of distinct gate inputs."""
    combos: Counter = Counter()
    facts, groups = _gate_inputs()
    for basis, source, parse, quarantine, is_test, tangled, needs, parent_count, reason, commit_key, n in groups:
        fix_check, evidence = facts.get(commit_key, (None, None))
        combos[(fix_check, evidence, parent_count is None, not reason, quarantine, parse, is_test, tangled, basis, source, needs)] += n
    failed: Counter = Counter()
    not_evaluated: set[str] = set()
    tiers: Counter = Counter()
    rows = candidates = 0
    for key, n in combos.items():
        result = review.gate(_group_row(key))
        rows += n
        candidates += n * result.candidate
        for name in result.failed:
            failed[name] += n
        not_evaluated.update(result.not_evaluated)
        tiers[review.effective_status(result, None)[0]] += n
    # A decision can raise the tier of its pair (never lower it): move those pairs from the tier the gate gave them to the decided one.
    decided = {rid: d for rid, d in decisions.items() if d["review_status"] != "unreviewed"}
    if decided and rows:
        for rid, row in _decided_rows(decided).items():
            result = review.gate(row)
            before = review.effective_status(result, None)[0]
            after = review.effective_status(result, decided[rid])[0]
            if after != before:
                tiers[before] -= 1
                tiers[after] += 1
    return {"rows": rows, "candidates": candidates, "tiers": {t: tiers.get(t, 0) for t in reviewed.REVIEW_STATUSES},
            "failed": dict(sorted(failed.items())), "not_evaluated": sorted(not_evaluated)}


# -- embedded data layer (compatibility modules pipeline/db/*_mongo.py), bound under the established public names ---------------------
candidate_rows = _pipeline_mongo.review_candidate_rows
_rows_of_decisions = _pipeline_mongo.review_rows_of_decisions
_gate_inputs = _pipeline_mongo.review_gate_inputs
