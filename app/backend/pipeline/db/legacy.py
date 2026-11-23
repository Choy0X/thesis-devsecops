"""Shared plumbing of the embedded document repositories of ``store.py`` / ``codestore.py``: the database handle, row <-> document helpers and the
change counters.

``handle()`` returns the application database with its schema ensured once per process (native search definitions are created outside tests).
``advisory_rows`` / ``commit_rows`` / ``pair_rows`` batch the reads that rebuild legacy dicts, so a page of 50 rows costs a handful of queries.
"""
from __future__ import annotations

import os
import threading

from app.backend.pipeline.db import mappers as m, mongo

_ready: set[str] = set()
_lock = threading.Lock()


def handle():
    """The application database, schema ensured once per process."""
    db = mongo.database()
    if db.name not in _ready:
        with _lock:
            if db.name not in _ready:
                mongo.require()
                mongo.ensure_schema(db, search=not os.environ.get("PYTEST_CURRENT_TEST"))
                _ready.add(db.name)
    from app.backend.pipeline.acceleration.publication import session_database
    return session_database(db)


def forget() -> None:
    """Forget which databases were set up (tests, switching databases)."""
    with _lock:
        _ready.clear()


def touch(db, store: str = "working") -> None:
    """Bump the change counter of a logical store once per write batch (``store.change_token`` reads it)."""
    db["change_counters"].update_one({"_id": store}, {"$inc": {"n": 1}}, upsert=True)


def token(db) -> str:
    counters = {c["_id"]: c.get("n", 0) for c in db["change_counters"].find()}
    return f"{counters.get('working', 0)}:{counters.get('derived', 0)}"


def ids(db, name: str, count: int = 1) -> int:
    return mongo.next_id(db, name, count)


# -- advisories ---------------------------------------------------------------------------------------------------------------

BULK_CHUNK = 2_000          # a bulk_write or an $in query is kept to this many items at once: generous for one round trip, never one huge request


def _chunks(items: list, size: int = BULK_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def links_of(db, advisory_ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for chunk in _chunks(advisory_ids):
        for link in db["fix_links"].find({"advisory_id": {"$in": chunk}}).sort("_id", 1):
            out.setdefault(link["advisory_id"], []).append(link)
    return out


def advisory_rows(db, docs: list[dict]) -> list[dict]:
    links = links_of(db, [d["_id"] for d in docs]) if docs else {}
    return [m.advisory_row(d, links.get(d["_id"])) for d in docs]


def save_advisory(db, row: dict, *, doc: dict | None = None) -> dict:
    """Write a legacy ``records`` dict (with ``id``/``key``/``created``/``updated``) as the advisory document and its links."""
    doc = doc or m.advisory_doc(row)
    db["advisories"].replace_one({"_id": doc["_id"]}, doc, upsert=True)
    sync_links(db, doc["_id"], doc["key"], m.link_docs(row))
    return doc


def _link_ops(advisory_id: int, key: str, wanted: list[dict], have: dict[str, dict]):
    """The writes that make one advisory's links equal ``wanted``, given what it already has (by commit_key): a ``$in`` delete of the ones no longer
    wanted (combined below into one bulk op per advisory, not one per gone link), and an insert or an update for each wanted one that is new or
    changed. Pure: used both for one advisory at a time (``sync_links``) and for many at once (``save_advisories_bulk``)."""
    from app.backend.pipeline.db.operations import DeleteMany, InsertOne, UpdateOne
    merged = {}
    for link in wanted:
        value = merged.setdefault(link["commit_key"], dict(link))
        for field in ("via", "sources"):
            value[field] = sorted(set(value.get(field, [])) | set(link.get(field, [])))
    wanted = list(merged.values())
    keep = {link["commit_key"] for link in wanted}
    gone = [k for k in have if k not in keep]
    ops = [DeleteMany({"advisory_id": advisory_id, "commit_key": {"$in": gone}})] if gone else []
    for link in wanted:
        old = have.get(link["commit_key"])
        if old is None:
            ops.append(InsertOne({**link, "advisory_key": key}))
        elif {k: old.get(k) for k in ("via", "sources")} != {k: link.get(k) for k in ("via", "sources")}:
            ops.append(UpdateOne({"_id": old["_id"]}, {"$set": {"via": link.get("via", []), "sources": link.get("sources", [])}}))
    return ops


def sync_links(db, advisory_id: int, key: str, wanted: list[dict]) -> None:
    """Make the links of one advisory equal ``wanted`` (a record's pointer list is the whole truth for that record)."""
    have = {link["commit_key"]: link for link in db["fix_links"].find({"advisory_id": advisory_id})}
    ops = _link_ops(advisory_id, key, wanted, have)
    if ops:
        db["fix_links"].bulk_write(ops, ordered=False)


def save_advisories_bulk(db, items: list[tuple[dict, dict]]) -> None:
    """``save_advisory`` for many rows at once, in a handful of round trips instead of several per row (a large ecosystem's ingest write-back was
    measured taking most of its time here: one find, and usually one replace, per advisory, serially). Same decisions as ``save_advisory`` plus
    ``sync_links``, batched: every advisory document in one bulk write, every advisory's current links read in one query (``links_of``, chunked by
    ``$in``, as every other bulk lookup here already is), and every link add/remove/update collected into one more bulk write."""
    if not items:
        return
    from app.backend.pipeline.db.operations import ReplaceOne
    for chunk in _chunks(items):
        db["advisories"].bulk_write([ReplaceOne({"_id": doc["_id"]}, doc, upsert=True) for _, doc in chunk], ordered=False)
    wanted_by_id = {row["id"]: (row["key"], m.link_docs(row)) for row, _ in items}
    have_by_id = links_of(db, list(wanted_by_id))
    link_ops = [op for advisory_id, (key, wanted) in wanted_by_id.items()
                for op in _link_ops(advisory_id, key, wanted, {link["commit_key"]: link for link in have_by_id.get(advisory_id, [])})]
    for chunk in _chunks(link_ops):
        db["fix_links"].bulk_write(chunk, ordered=False)


# -- commits and pairs ---------------------------------------------------------------------------------------------------------

def commit_rows(db, docs: list[dict]) -> list[dict]:
    keys = [d["commit_key"] for d in docs]
    links: dict[str, list[dict]] = {}
    for link in db["fix_links"].find({"commit_key": {"$in": keys}}):
        links.setdefault(link["commit_key"], []).append(link)
    adv_ids = sorted({link["advisory_id"] for ls in links.values() for link in ls})
    advisories = {a["_id"]: a for a in db["advisories"].find({"_id": {"$in": adv_ids}})} if adv_ids else {}
    return [m.commit_row(d, links.get(d["commit_key"], []), advisories) for d in docs]


def owners_of(db, commit_keys: list[str]) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """(commit documents, linked advisory documents) by commit key, for resolving the copies on pairs."""
    commits = {c["commit_key"]: c for c in db["fix_commits"].find({"commit_key": {"$in": commit_keys}})} if commit_keys else {}
    links: dict[str, list[dict]] = {}
    for link in db["fix_links"].find({"commit_key": {"$in": commit_keys}}):
        links.setdefault(link["commit_key"], []).append(link)
    adv_ids = sorted({link["advisory_id"] for ls in links.values() for link in ls})
    advisories = {a["_id"]: a for a in db["advisories"].find({"_id": {"$in": adv_ids}})} if adv_ids else {}
    by_commit = {k: sorted((advisories[link["advisory_id"]] for link in ls if link["advisory_id"] in advisories), key=lambda a: a["_id"])
                 for k, ls in links.items()}
    return commits, by_commit


def pair_rows(db, pairs: list[dict], *, with_code: bool = True) -> list[dict]:
    """Legacy ``code_pairs`` dicts for pair documents (code and enrichment fetched in two queries, owners in three)."""
    if not pairs:
        return []
    pids = [p["_id"] for p in pairs]
    codes = {c["_id"]: c for c in db["pair_code"].find({"_id": {"$in": pids}})} if with_code else {}
    enr = {e["_id"]: e for e in db["pair_enrichments"].find({"_id": {"$in": pids}})}
    commits, advisories = owners_of(db, sorted({p["commit_key"] for p in pairs if p.get("commit_key")}))
    out = []
    for p in pairs:
        key = p.get("commit_key")
        manual = advisories.get(key, []) if key else linked_by_subject(db, p)
        out.append(m.pair_row(p, codes.get(p["_id"]), enr.get(p["_id"]), commits.get(key), manual))
    return out


def linked_by_subject(db, pair: dict) -> list[dict]:
    """The advisory of a pair that has no commit (a hand-made pair): found by its subject."""
    sid = pair.get("subject", {}).get("advisory_id")
    doc = db["advisories"].find_one({"_id": sid}) if sid else None
    return [doc] if doc else []
