"""The knowledge base: embedded training-row chunks, searched with exact cosine.

What goes in (one frozen split manifest version = one ``kb_version``):
* ``code``: the vulnerable code of each usable pair whose node is in a **train** group (first ``MAX_CODE_CHARS`` characters);
* ``advisory``: the summary and details of each train advisory;
* ``commit_message``: the message of each fix commit all of whose usable pairs are train.
A row of a validation group is never read: the corpus iterator takes only ``split == "train"`` nodes, every chunk carries ``split: "train"``
and the query filters on it too (defence in depth). Each chunk records its leakage group (``leak_group``), so a query can exclude the group
of the item it is about (aliases, backports, forks and near-clones included) and never retrieve its own answer.

The pinned embedder, the corpus counts and the build time live in ``rag_index_state`` (``_id`` = ``kb``). A build writes into the collection of
its embedder (``kb_chunks__<slug>``); an unchanged chunk (same text hash) is not embedded again; chunks of rows that left the corpus are
removed; the state only turns ``ready`` after committed chunks answer queries.
"""
from __future__ import annotations

import hashlib
import random
import time
from typing import Callable, Iterator

from app.backend.pipeline.db import database as mongo, schema
from app.backend.pipeline.rag import atlas
from app.backend.pipeline.rag.embed import BATCH, Embedder

MAX_CODE_CHARS = 6000
MAX_ADVISORY_CHARS = 4000
MAX_MESSAGE_CHARS = 1500
STATE_ID = "kb"


class RagError(RuntimeError):
    """Retrieval cannot answer; the message says what to do."""


class RagNotReady(RagError):
    pass


class RagStale(RagError):
    pass


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- the corpus ----------------------------------------------------------------------------------------------------------------

def node_maps(db, kb_version: str) -> tuple[dict[str, str], dict[str, str]]:
    """(node -> split, node -> group id) from the split groups of one manifest version."""
    split_of, group_of = {}, {}
    for g in db["split_groups"].find({"manifest_version": kb_version}, {"group_id": 1, "split": 1, "members": 1}):
        for node in g.get("members", []):
            split_of[node] = g.get("split")
            group_of[node] = g["group_id"]
    return split_of, group_of


def iter_corpus(db, kb_version: str, *, limit: int | None = None) -> Iterator[dict]:
    """Chunk dicts (without embeddings) for the train rows of ``kb_version``. Rows of any other split are never read."""
    split_of, group_of = node_maps(db, kb_version)
    train = {n for n, s in split_of.items() if s == "train"}
    n = 0

    def emit(chunk: dict):
        nonlocal n
        n += 1
        return chunk

    # code of usable train pairs, with the commit's other facts
    commit_pairs: dict[str, list[bool]] = {}
    cursor = db["code_pairs"].find({"flags.available": True, "flags.is_test": {"$ne": True}, "flags.tangled": {"$ne": True}},
                                   {"pair_key": 1, "commit_key": 1, "subject": 1, "unit": 1}).sort("_id", 1)
    batch: list[dict] = []

    def flush(rows):
        codes = {c["_id"]: c for c in db["pair_code"].find({"_id": {"$in": [r["_id"] for r in rows]}}, {"before": 1})}
        for r in rows:
            text = (codes.get(r["_id"]) or {}).get("before") or ""
            if not text.strip():
                continue
            node = "pair:" + r["pair_key"]
            subject, unit = r.get("subject", {}), r.get("unit", {})
            yield emit({"_id": f"{kb_version}:code:{r['pair_key']}:0", "kb_version": kb_version, "split": "train", "kind": "code",
                        "source_id": r["pair_key"], "chunk_ix": 0, "text": text[:MAX_CODE_CHARS], "truncated": len(text) > MAX_CODE_CHARS,
                        "leak_group": group_of.get(node), "cwe": (subject.get("cwe") or [None])[0], "ecosystem": subject.get("ecosystem"),
                        "language": unit.get("language")})

    for row in cursor:
        node = "pair:" + row["pair_key"]
        commit_pairs.setdefault(row.get("commit_key"), []).append(node in train)
        if node not in train:
            continue
        batch.append(row)
        if len(batch) >= 200:
            yield from flush(batch)
            batch = []
            if limit and n >= limit:
                return
    if batch:
        yield from flush(batch)

    # advisory text of train advisories
    for adv in db["advisories"].find({"key": {"$in": [k for k in train if not k.startswith("pair:")]}},
                                     {"key": 1, "summary": 1, "details": 1, "cwe": 1, "ecosystem": 1}):
        text = "\n\n".join(p for p in (adv.get("summary"), adv.get("details")) if p)
        if text.strip():
            yield emit({"_id": f"{kb_version}:advisory:{adv['key']}:0", "kb_version": kb_version, "split": "train", "kind": "advisory",
                        "source_id": adv["key"], "chunk_ix": 0, "text": text[:MAX_ADVISORY_CHARS], "truncated": len(text) > MAX_ADVISORY_CHARS,
                        "leak_group": group_of.get(adv["key"]), "cwe": (adv.get("cwe") or [None])[0], "ecosystem": adv.get("ecosystem"),
                        "language": None})
        if limit and n >= limit:
            return

    # commit messages of commits whose usable pairs are all train
    commits = [c for c, flags in commit_pairs.items() if c and flags and all(flags)]
    for i in range(0, len(commits), 500):
        for fc in db["fix_commits"].find({"commit_key": {"$in": commits[i:i + 500]}}, {"commit_key": 1, "git.message": 1}):
            text = (fc.get("git") or {}).get("message") or ""
            if text.strip():
                yield emit({"_id": f"{kb_version}:commit_message:{fc['commit_key']}:0", "kb_version": kb_version, "split": "train",
                            "kind": "commit_message", "source_id": fc["commit_key"], "chunk_ix": 0, "text": text[:MAX_MESSAGE_CHARS],
                            "truncated": len(text) > MAX_MESSAGE_CHARS, "leak_group": None, "cwe": None, "ecosystem": None, "language": None})
            if limit and n >= limit:
                return


# -- building --------------------------------------------------------------------------------------------------------------------

def build_kb(db, embedder: Embedder, kb_version: str, *, progress: Callable[..., None] | None = None, limit: int | None = None,
             wait_seconds: int = 900) -> dict:
    """Embed the train corpus of ``kb_version`` into ``kb_chunks__<slug>`` and mark it ready. Resumable and incremental."""
    mongo.require()
    state = atlas.state_collection(db)
    coll = atlas.ensure_collection(db, atlas.KB, embedder, wait=False)
    started = time.time()
    state.update_one({"_id": STATE_ID}, {"$set": {"building": True, "ready": False, "kb_version": kb_version, "embedder": embedder.as_dict(),
                                                  "collection": coll.name, "started_at": started}}, upsert=True)
    have = {d["_id"]: d["text_sha256"] for d in coll.find({"kb_version": kb_version}, {"text_sha256": 1})}
    seen: set[str] = set()
    pending: list[dict] = []
    written = embedded = skipped = 0
    recent: list[dict] = []

    def flush():
        nonlocal written, embedded, pending, recent
        if not pending:
            return
        vectors = embedder.encode_documents([c["text"] for c in pending])
        for c, v in zip(pending, vectors):
            c["embedding"] = atlas.vector(v)
            c["embedder_key"] = embedder.key
        written += atlas.upsert_chunks(coll, pending)
        embedded += len(pending)
        recent = pending
        pending = []
        if progress:
            progress(state="running", label="Embedding the knowledge base", processed=written + skipped, total=None,
                     counts={"embedded": embedded, "unchanged": skipped})

    for chunk in iter_corpus(db, kb_version, limit=limit):
        assert chunk["split"] == "train", "a non-train row reached the knowledge base"
        chunk["text_sha256"] = sha(chunk["text"])
        seen.add(chunk["_id"])
        if have.get(chunk["_id"]) == chunk["text_sha256"]:
            skipped += 1
            continue
        pending.append(chunk)
        if len(pending) >= BATCH * 4:
            flush()
    flush()
    removed = coll.delete_many({"kb_version": kb_version, "_id": {"$nin": list(seen)}}).deleted_count if limit is None else 0
    atlas.wait_queryable(coll, schema.vector_index(embedder.dimension, atlas.KB).name, wait_seconds)
    atlas.wait_visible(coll, atlas.KB, recent, wait_seconds)
    counts = {k: coll.count_documents({"kb_version": kb_version, "kind": k}) for k in ("code", "advisory", "commit_message")}
    state.update_one({"_id": STATE_ID}, {"$set": {"building": False, "ready": True, "built_at": time.time(), "counts": counts,
                                                  "chunks": sum(counts.values())}})
    if progress:
        progress(state="done", label="Knowledge base ready", processed=sum(counts.values()), total=sum(counts.values()))
    return {"kb_version": kb_version, "collection": coll.name, "embedder": embedder.key, "counts": counts, "embedded": embedded,
            "unchanged": skipped, "removed": removed, "seconds": round(time.time() - started, 1)}


# -- querying --------------------------------------------------------------------------------------------------------------------

def status(db=None, *, current: Embedder | None = None) -> dict:
    """What the Engine and Settings pages show: is a knowledge base built, with which embedder, how big, and is it stale."""
    db = db if db is not None else mongo.database()
    doc = atlas.state_collection(db).find_one({"_id": STATE_ID})
    if not doc:
        return {"built": False, "ready": False, "stale": False, "reason": "No knowledge base has been built yet."}
    out = {"built": True, "ready": bool(doc.get("ready")), "building": bool(doc.get("building")), "kb_version": doc.get("kb_version"),
           "embedder": doc.get("embedder"), "collection": doc.get("collection"), "counts": doc.get("counts"), "chunks": doc.get("chunks"),
           "built_at": doc.get("built_at"), "stale": False}
    if current is not None and current.key != doc["embedder"]["key"]:
        out.update(stale=True, reason=f"The knowledge base was built with {doc['embedder']['key']} but the embedder is now {current.key}. "
                                      "Rebuild it for the new embedder (the old one is kept until you remove it).")
    return out


def retrieve(query: str, k: int = 5, *, db=None, kinds: list[str] | None = None, exclude_groups: list[str] | None = None,
             exclude_sources: list[str] | None = None, languages: list[str] | None = None, exact: bool = False,
             embedder: Embedder | None = None, as_embedder: Embedder | None = None) -> list[dict]:
    """The ``k`` nearest train chunks to ``query``.

    The query is embedded with the embedder that built the index. Pass ``embedder`` (the configured one) to get ``RagStale`` instead of an
    answer when the index belongs to another embedder. ``exclude_groups`` / ``exclude_sources`` keep an item's own group and rows out of the
    answer. ``as_embedder`` overrides the query embedder (tests)."""
    db = db if db is not None else mongo.database()
    doc = atlas.state_collection(db).find_one({"_id": STATE_ID})
    if not doc or not doc.get("ready"):
        raise RagNotReady("The knowledge base is not built (or still building). Build it first.")
    built = Embedder.from_dict(doc["embedder"])
    if embedder is not None and embedder.key != built.key:
        raise RagStale(status(db, current=embedder)["reason"])
    coll = db[doc["collection"]]
    qv = (as_embedder or built).encode_query(query[:4000])
    filters: list[dict] = [{"kb_version": doc["kb_version"]}, {"split": "train"}]
    if kinds:
        filters.append({"kind": {"$in": list(kinds)}})
    if languages:
        filters.append({"language": {"$in": list(languages)}})
    if exclude_groups:
        filters.append({"leak_group": {"$nin": list(exclude_groups)}})
    if exclude_sources:
        filters.append({"source_id": {"$nin": list(exclude_sources)}})
    hits = atlas.search(coll, qv, k, kind=atlas.KB, filters=filters, exact=exact)
    return [{"id": h["_id"], "document": h["text"], "score": h["score"], "distance": 1.0 - h["score"],
             "metadata": {k_: h.get(k_) for k_ in ("kind", "source_id", "cwe", "ecosystem", "language", "leak_group", "kb_version")}}
            for h in hits]


def recall_at_k(n: int = 200, k: int = 5, *, db=None, seed: int = 42, as_embedder: Embedder | None = None) -> float | None:
    """Share of validation pairs whose top-k **exact** neighbours share a CWE, with the pair's own leakage group excluded. Exact search keeps
    approximate-search noise out of the metric. Validation rows are only queries; the index never contains them."""
    db = db if db is not None else mongo.database()
    doc = atlas.state_collection(db).find_one({"_id": STATE_ID})
    if not doc or not doc.get("ready"):
        raise RagNotReady("The knowledge base is not built.")
    split_of, group_of = node_maps(db, doc["kb_version"])
    val = [n_[len("pair:"):] for n_, s in split_of.items() if s == "val" and n_.startswith("pair:")]
    rows = list(db["code_pairs"].find({"pair_key": {"$in": val}, "subject.cwe.0": {"$exists": True}}, {"pair_key": 1, "subject": 1}))
    if not rows:
        print("no validation pairs with a CWE")
        return None
    random.Random(seed).shuffle(rows)
    hits = total = 0
    for r in rows[:n]:
        code = (db["pair_code"].find_one({"_id": r["_id"]}, {"before": 1}) or {}).get("before") or ""
        if not code.strip():
            continue
        group = group_of.get("pair:" + r["pair_key"])
        got = retrieve(code, k, db=db, kinds=["code"], exclude_groups=[group] if group else None, exact=True, as_embedder=as_embedder)
        total += 1
        hits += any(g["metadata"].get("cwe") in r["subject"]["cwe"] for g in got)
    score = hits / total if total else None
    print(f"recall@{k} (CWE-match proxy, exact search, n={total} validation pairs): {score if score is None else round(score, 3)}")
    return score
