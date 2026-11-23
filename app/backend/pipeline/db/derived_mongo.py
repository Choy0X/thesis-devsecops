"""Embedded SQLite implementation of the derived store: campaigns, review decisions and audits, leakage graphs, split
manifests, model exposure, context bundles, link recovery.

``derived.connect()`` returns a :class:`MongoDerived`. The modules that use the derived store call the functions below through ``derived.mongo_of(conn)``
(``tests/contract`` pins their answers).
The module and class names are retained for compatibility. Dates are stored as typed UTC values and returned as the ISO text the API has always shown; free-form detail is kept as JSON text.
"""
from __future__ import annotations

import datetime as dt
import json
import zlib

from app.backend.pipeline.db import legacy, mappers as m, mongo

_BATCH = 5000


class MongoDerived:
    """A compatibility handle on the embedded document store. Raw ``execute`` is unavailable; callers use the repository functions."""

    def __init__(self, db):
        self.db = db

    def execute(self, *_a, **_k):
        raise NotImplementedError("raw SQL is not exposed by this repository facade; use the derived repository functions")

    executemany = executescript = execute

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> "MongoDerived":
        return self

    def __exit__(self, *exc) -> None:
        return None


def connect() -> MongoDerived:
    return MongoDerived(legacy.handle())


def _utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _touch(d: MongoDerived) -> None:
    legacy.touch(d.db, "derived")


# -- review decisions and audits --------------------------------------------------------------------------------------------------

def add_review_decision(d: MongoDerived, *, record_id: str, reviewer: str, rubric_version: str, review_status: str, label_state: str,
                        fields: dict | None, notes: str | None) -> int:
    """Append one decision and move the record's current decision forward if this one is newer (the decisions collection is never updated)."""
    with d.db.transaction():
        did = legacy.ids(d.db, "review_decisions")
        doc = {"_id": did, "record_id": record_id, "reviewer": reviewer, "rubric_version": rubric_version, "review_status": review_status,
               "label_state": label_state, "created_at": _utc()}
        m.put(doc, "fields", json.loads(json.dumps(fields or {}, sort_keys=True, default=str)))
        m.put(doc, "notes", notes)
        d.db["review_decisions"].insert_one(doc)
        current = {**doc, "decision_id": did}
        current.pop("_id")
        moved = d.db["review_current"].replace_one({"_id": record_id, "decision_id": {"$lt": did}}, current)
        if not moved.matched_count:
            try:
                d.db["review_current"].insert_one({"_id": record_id, **current})
            except Exception as e:                                       # a newer decision won the race: keep it
                if "E11000" not in str(e):
                    raise
        _touch(d)
        return did


def _decision_row(doc: dict) -> dict:
    return {"decision_id": doc["decision_id"], "record_id": doc["_id"], "reviewer": doc["reviewer"], "rubric_version": doc["rubric_version"],
            "review_status": doc["review_status"], "label_state": doc["label_state"], "notes": doc.get("notes"),
            "created_at": m.fmt_dt(doc["created_at"]), "fields": doc.get("fields") or {}}


def latest_decisions(d: MongoDerived) -> dict[str, dict]:
    return {doc["_id"]: _decision_row(doc) for doc in d.db["review_current"].find()}


def decision_history(d: MongoDerived, record_id: str) -> list[dict]:
    return [_decision_row({**doc, "_id": doc["record_id"], "decision_id": doc["_id"]})
            for doc in d.db["review_decisions"].find({"record_id": record_id}).sort("_id", 1)]


def audit_items(d: MongoDerived, audit_id: str) -> list[dict]:
    doc = d.db["audits"].find_one({"_id": audit_id})
    return [dict(i) for i in (doc or {}).get("items", [])]


def audit_keep(d: MongoDerived, audit_id: str, items: list[dict]) -> None:
    """Store a drawn sample once (a second draw for the same id keeps the first)."""
    d.db["audits"].update_one(
        {"_id": audit_id}, {"$setOnInsert": {"drawn_at": _utc(), "items": [
            {"record_id": i["record_id"], "pair_key": i["pair_key"], "stratum": i.get("stratum") or {}} for i in items]}}, upsert=True)
    _touch(d)


def latest_audit_id(d: MongoDerived) -> str | None:
    doc = d.db["audits"].find_one({}, {"_id": 1}, sort=[("drawn_at", -1), ("_id", -1)])
    return doc["_id"] if doc else None


# -- link recovery (a sub-document of the advisory) ---------------------------------------------------------------------------------

def link_recovery_tried(d: MongoDerived) -> set[str]:
    return {a["key"] for a in d.db["advisories"].find({"link_recovery.attempted_at": {"$exists": True}}, {"key": 1})}


def link_recovery_save(d: MongoDerived, key: str, outcome: str, method: str | None, reason: str | None, pointers: list) -> None:
    sub: dict = {"attempted_at": _utc(), "outcome": outcome}
    m.put(sub, "method", method)
    m.put(sub, "reason", reason)
    m.put(sub, "pointers", json.loads(json.dumps(pointers or [], default=str)))
    d.db["advisories"].update_one({"key": key}, {"$set": {"link_recovery": sub}})
    legacy.touch(d.db)


def link_recovery_rows(d: MongoDerived) -> list[dict]:
    out = []
    for a in d.db["advisories"].find({"link_recovery.attempted_at": {"$exists": True}}, {"key": 1, "link_recovery": 1}).sort("_id", 1):
        s = a["link_recovery"]
        out.append({"record_key": a["key"], "attempted_at": m.fmt_dt(s["attempted_at"]), "outcome": s["outcome"], "method": s.get("method"),
                    "reason": s.get("reason"), "pointers": s.get("pointers") or []})
    return out


# -- leakage graphs -------------------------------------------------------------------------------------------------------------------

def leakage_save(d: MongoDerived, version: str, edges: list[tuple], groups: dict[str, str], extra: dict) -> None:
    """``edges``: (src, dst, edge_type, evidence dict, confidence). Replaces an earlier build of the same version."""
    db = d.db
    db["leakage_edges"].delete_many({"graph_version": version})
    db["leakage_groups"].delete_many({"graph_version": version})
    for i in range(0, len(edges), _BATCH):
        db["leakage_edges"].insert_many([
            {"graph_version": version, "src": s, "dst": t, "edge_type": kind, "evidence": ev or {}, "confidence": conf}
            for s, t, kind, ev, conf in edges[i:i + _BATCH]], ordered=False)
    items = list(groups.items())
    for i in range(0, len(items), _BATCH):
        db["leakage_groups"].insert_many([{"graph_version": version, "node_id": n, "group_id": g} for n, g in items[i:i + _BATCH]], ordered=False)
    db["leakage_graphs"].replace_one({"_id": version}, {"created_at": _utc(), **json.loads(json.dumps(extra, sort_keys=True, default=str))},
                                     upsert=True)
    _touch(d)


def leakage_load(d: MongoDerived, version: str) -> tuple[list[tuple], dict[str, str], dict]:
    edges = [(e["src"], e["dst"], e["edge_type"], e.get("evidence") or {}, e.get("confidence"))
             for e in d.db["leakage_edges"].find({"graph_version": version}).sort("_id", 1)]
    groups = {g["node_id"]: g["group_id"] for g in d.db["leakage_groups"].find({"graph_version": version})}
    head = d.db["leakage_graphs"].find_one({"_id": version}) or {}
    head.pop("_id", None)
    head.pop("created_at", None)
    return edges, groups, head


# -- split manifests -------------------------------------------------------------------------------------------------------------------

def manifest_versions(d: MongoDerived) -> list[str]:
    return [h["_id"] for h in d.db["split_manifests"].find({}, {"_id": 1}).sort("created_at", 1)]


def manifest_sha(d: MongoDerived, version: str) -> str | None:
    doc = d.db["split_manifests"].find_one({"_id": version}, {"sha256": 1})
    return doc.get("sha256") if doc else None


def manifest_insert(d: MongoDerived, version: str, algorithm: str, parent: str | None, sha: str, summary: dict, body_text: str,
                    groups: dict) -> None:
    """The header (with the canonical body, compressed, so that its digest reads back exactly) and one document per group for lookups."""
    body = zlib.compress(body_text.encode("utf-8"), 6)
    if len(body) > 12 * 1024 * 1024:
        raise ValueError(f"split manifest {version} is {len(body)} bytes compressed; the header document would pass the 16 MB limit")
    doc = {"_id": version, "created_at": _utc(), "algorithm": algorithm, "sha256": sha,
           "summary": json.loads(json.dumps(summary or {}, sort_keys=True, default=str)), "body": body}
    m.put(doc, "parent_version", parent)
    d.db["split_manifests"].insert_one(doc)
    items = list(groups.items())
    for i in range(0, len(items), _BATCH):
        d.db["split_groups"].insert_many([
            {"manifest_version": version, "group_id": gid, "split": g.get("split"), "reason": g.get("reason"),
             "members": sorted(g.get("members") or [])} for gid, g in items[i:i + _BATCH]], ordered=False)
    _touch(d)


def manifest_body(d: MongoDerived, version: str) -> str | None:
    doc = d.db["split_manifests"].find_one({"_id": version}, {"body": 1})
    return zlib.decompress(bytes(doc["body"])).decode("utf-8") if doc and doc.get("body") is not None else None


def manifest_listing(d: MongoDerived) -> list[dict]:
    return [{"version": h["_id"], "parent": h.get("parent_version"), "sha256": h.get("sha256"), "summary": h.get("summary") or {},
             "body": zlib.decompress(bytes(h["body"])).decode("utf-8") if h.get("body") is not None else None}
            for h in d.db["split_manifests"].find().sort("created_at", 1)]


def exposure_add(d: MongoDerived, model_id: str, manifest_version: str, group_ids: list[str]) -> None:
    from app.backend.pipeline.db.operations import UpdateOne
    ops = [UpdateOne({"_id": f"{model_id}|{manifest_version}|{g}"},
                     {"$setOnInsert": {"model_id": model_id, "manifest_version": manifest_version, "issue_group_id": g}}, upsert=True)
           for g in group_ids]
    if ops:
        d.db["model_exposure"].bulk_write(ops, ordered=False)
        _touch(d)


def exposure_of(d: MongoDerived, manifest_version: str) -> list[tuple[str, str]]:
    return [(e["model_id"], e["issue_group_id"]) for e in d.db["model_exposure"].find({"manifest_version": manifest_version})]


def contamination_add(d: MongoDerived, rows: list[tuple[str, str, str, str, str]]) -> None:
    """``(model_id, manifest_version, val_group_id, linked_group_id, evidence_json)``; the same row twice is kept once."""
    from app.backend.pipeline.db.operations import UpdateOne
    ops = [UpdateOne({"_id": f"{a}|{b}|{c}|{e}"},
                     {"$setOnInsert": {"model_id": a, "manifest_version": b, "val_group_id": c, "linked_group_id": e,
                                       "evidence": m.loads(f, {})}}, upsert=True) for a, b, c, e, f in rows]
    if ops:
        d.db["model_contamination"].bulk_write(ops, ordered=False)
        _touch(d)


def contamination_rows(d: MongoDerived, manifest_version: str) -> list[dict]:
    return [{k: v for k, v in c.items() if k != "_id"} for c in d.db["model_contamination"].find({"manifest_version": manifest_version})]


# -- context bundles --------------------------------------------------------------------------------------------------------------------

def bundle_save(d: MongoDerived, record_id: str, bundle_hash: str, policy: str, variant: str, completeness: str, body: dict) -> None:
    d.db["context_bundles"].update_one(
        {"_id": bundle_hash}, {"$setOnInsert": {"policy": policy, "variant": variant, "completeness": completeness,
                                                  "body_json": json.dumps(body, sort_keys=True), "created_at": _utc()}}, upsert=True)
    d.db["context_links"].replace_one({"_id": record_id}, {"bundle_hash": bundle_hash}, upsert=True)


def bundle_body(d: MongoDerived, bundle_hash: str) -> dict | None:
    doc = d.db["context_bundles"].find_one({"_id": bundle_hash}, {"body_json": 1})
    return m.loads(doc.get("body_json"), None) if doc else None


def bundle_of(d: MongoDerived, record_id: str) -> str | None:
    doc = d.db["context_links"].find_one({"_id": record_id})
    return doc["bundle_hash"] if doc else None
