"""The reads of the review, reviewed-build and dataset modules (``review.py``, ``reviewview.py`` and ``build_reviewed.py`` re-export them under the same names).
Answers are the same dicts and tuples the earlier SQL gave (``tests/contract`` pins it).
"""
from __future__ import annotations

from app.backend.pipeline.db import legacy, mappers as m

_BATCH = 1000
_REVIEW_KEYS = ("pair_key", "commit_key", "ecosystem", "package", "repo_url", "source", "cwe", "granularity", "function_name", "file_path", "language",
                "is_test", "tangled", "code_available", "quarantine_reason", "parent_count", "parent_selection_reason", "parse_status",
                "attribution_basis", "attribution_score", "explanation_source", "needs_explanation_enrichment")


def _review_row(row: dict, commit: dict | None) -> dict:
    """The light columns of a pair for the review gate: its own facts plus the three facts of its fix commit (a missing commit gives None)."""
    out = {k: row.get(k) for k in _REVIEW_KEYS}
    out["fix_check"] = ((commit or {}).get("verification") or {}).get("fix_check")
    out["evidence_level"] = ((commit or {}).get("evidence") or {}).get("level")
    out["commit_kind"] = ((commit or {}).get("git") or {}).get("kind")
    return out


def _review_rows(db, docs: list[dict]) -> list[dict]:
    rows = legacy.pair_rows(db, docs, with_code=False)
    keys = sorted({d["commit_key"] for d in docs if d.get("commit_key")})
    commits = {c["commit_key"]: c for c in db["fix_commits"].find({"commit_key": {"$in": keys}})} if keys else {}
    return [_review_row(r, commits.get(d.get("commit_key"))) for d, r in zip(docs, rows)]


def _in_batches(db, flt: dict, *, with_ids=False) -> list[dict]:
    out: list[dict] = []
    batch: list[dict] = []
    for doc in db["code_pairs"].find(flt).sort("_id", 1):
        batch.append(doc)
        if len(batch) >= _BATCH:
            rows = _review_rows(db, batch)
            out += [{**row, 'pair_id': doc['_id']} for doc, row in zip(batch, rows)] if with_ids else rows
            batch = []
    if batch:
        rows = _review_rows(db, batch)
        out += [{**row, 'pair_id': doc['_id']} for doc, row in zip(batch, rows)] if with_ids else rows
    return out


# -- review ------------------------------------------------------------------------------------------------------------------

def review_rows(ecosystem: str | None = None) -> list[dict]:
    """Every usable pair joined with the facts of its fix commit (no code text), in id order."""
    flt: dict = {"flags.available": True}
    if ecosystem:
        flt["subject.ecosystem"] = ecosystem
    return _in_batches(legacy.handle(), flt)


def review_pair(pair_key: str) -> dict | None:
    db = legacy.handle()
    doc = db["code_pairs"].find_one({"pair_key": pair_key})
    if doc is None:
        return None
    row = legacy.pair_rows(db, [doc])[0]
    commit = db["fix_commits"].find_one({"commit_key": doc["commit_key"]}) if doc.get("commit_key") else None
    out = _review_row(row, commit)
    out.update({k: row.get(k) for k in ("code_before", "code_after", "diff", "explanation", "commit_message")})
    return out


def review_candidate_rows(*, with_ids=False) -> list[dict]:
    """A superset of the pairs that can pass the gate, in id order, read with conditions every passing row must meet (the gate itself decides)."""
    from app.backend.pipeline import review
    flt = {"flags.available": True, "link.fix_check": "verified", "link.evidence_level": {"$in": list(review.LINK_EVIDENCE_OK)},
           "flags.quarantine.0": {"$exists": False}, "extraction.parse.status": {"$in": list(review.PARSE_OK)},
           "flags.is_test": {"$ne": True}, "flags.tangled": {"$ne": True}, "attribution.basis": {"$in": list(review.ATTRIBUTED)},
           "explanation.source": {"$in": list(review.EXPLANATION_OK)}, "explanation.needs_enrichment": {"$ne": True}}
    return _in_batches(legacy.handle(), flt, with_ids=with_ids)


def review_rows_of_decisions(decided: dict, found: dict) -> dict[str, dict]:
    """The usable pairs behind decisions that the candidates did not hold, found through the stored record id of each pair."""
    wanted = [rid for rid in decided if rid not in found]
    if not wanted:
        return {}
    db = legacy.handle()
    docs = list(db["code_pairs"].find({"record_id": {"$in": wanted}, "flags.available": True}))
    return {d["record_id"]: r for d, r in zip(docs, _review_rows(db, docs))}


def review_gate_inputs() -> tuple[dict, list]:
    """(fix link facts per commit, distinct combinations of the gate's pair-side inputs with their counts). The parent facts live on the commit
    here, so they are looked up by the commit key of each combination."""
    db = legacy.handle()
    facts: dict = {}
    parents: dict = {}
    for c in db["fix_commits"].find({}, {"commit_key": 1, "verification.fix_check": 1, "evidence.level": 1, "git.parents": 1}):
        facts[c["commit_key"]] = (c.get("verification", {}).get("fix_check"), c.get("evidence", {}).get("level"))
        p = c.get("git", {}).get("parents") or {}
        parents[c["commit_key"]] = (p.get("count"), p.get("selection"))
    groups = []
    for g in db["code_pairs"].aggregate([
            {"$match": {"flags.available": True}},
            {"$group": {"_id": {"basis": "$attribution.basis", "source": "$explanation.source", "parse": "$extraction.parse.status",
                                "q": {"$arrayElemAt": ["$flags.quarantine", 0]}, "is_test": "$flags.is_test", "tangled": "$flags.tangled",
                                "needs": "$explanation.needs_enrichment", "ck": "$commit_key"}, "n": {"$sum": 1}}}], allowDiskUse=True):
        k = g["_id"]
        count, reason = parents.get(k.get("ck"), (None, None))
        groups.append((k.get("basis"), k.get("source"), k.get("parse"), k.get("q"), 1 if k.get("is_test") else 0, 1 if k.get("tangled") else 0,
                       None if k.get("needs") is None else int(bool(k["needs"])), count, reason, k.get("ck"), g["n"]))
    return facts, groups


# -- the reviewed build ---------------------------------------------------------------------------------------------------------------

def reviewed_inputs():
    """(vulnerability records, every commit with its fix-link facts by key, every usable pair with its code in pair-key order, licences)."""
    from app.backend.pipeline import build_reviewed, store
    db = legacy.handle()
    records = store.vulnerability_records(tuple(c.strip() for c in build_reviewed._RECORD_COLS.split(",")))
    commits: dict = {}
    batch: list[dict] = []

    def flush():
        for r in legacy.commit_rows(db, batch):
            commits[r["commit_key"]] = {k: r.get(k) for k in ("commit_key", "sha", "repo_url", "commit_message", "commit_date", "fix_check",
                                                              "evidence_level", "commit_kind")}
    for doc in db["fix_commits"].find():
        batch.append(doc)
        if len(batch) >= _BATCH:
            flush()
            batch = []
    if batch:
        flush()
    pairs: list[dict] = []
    batch = []
    for doc in db["code_pairs"].find({"flags.available": True}).sort("pair_key", 1):
        batch.append(doc)
        if len(batch) >= _BATCH:
            pairs += legacy.pair_rows(db, batch)
            batch = []
    if batch:
        pairs += legacy.pair_rows(db, batch)
    licenses = {}
    for doc in db["repositories"].find():
        r = m.repository_row(doc)
        licenses[r["repo_identity"]] = {k: r.get(k) for k in ("repo_identity", "license_spdx", "data_usage_status")}
    return records, commits, pairs, licenses
