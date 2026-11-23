"""Embedded chunk collections, committed bulk writes and exact vector retrieval.

The historical module name is retained for callers. One collection per purpose and embedder: ``kb_chunks__<slug>`` for the knowledge base built from training rows,
``project_chunks__<slug>`` for the isolated index of a user's project, and ``web_chunks__<slug>`` for pages a conversation's web search
fetched (not project-scoped: a page found for one project's question is free to answer another's too, since nothing about the page itself
is project-specific) (the embedder slug carries the model, dimension and revision, so a different embedder is a different collection). The
collection names are built by :mod:`app.backend.pipeline.db.schema`.

A chunk is ``{_id, text, text_sha256, embedder_key, embedding (BinData float32), <filter fields>}``. Vectors are stored as BinData
vectors and are expected to be L2-normalised (see :mod:`embed`). SQLite applies metadata filters before bounded exact cosine search.
Committed writes are visible immediately. Embedding generation still uses the local model gateway or the configured external endpoint.
"""
from __future__ import annotations

from app.backend.pipeline.db import schema
from app.backend.pipeline.db.operations import ReplaceOne
from app.backend.pipeline.db.types import Binary, BinaryVectorDtype
from app.backend.pipeline.rag import local_vectors
from app.backend.pipeline.rag.embed import Embedder

KB, PROJECT, WEB = "kb_chunks", "project_chunks", "web_chunks"
SEARCH_BATCH_SIZE = 256


def collection_name(kind: str, embedder: Embedder) -> str:
    return schema.vector_collection(kind, embedder.slug)


def ensure_collection(db, kind: str, embedder: Embedder, *, wait: bool = True, wait_seconds: int = 600):
    """Ensure the collection and its SQL metadata indexes exist."""
    coll = db[collection_name(kind, embedder)]
    if kind == KB:
        coll.create_index([("kb_version", 1), ("kind", 1), ("source_id", 1)], name="by_source")
        coll.create_index([("kb_version", 1), ("text_sha256", 1)], name="by_hash")
        coll.create_index([("kb_version", 1), ("split", 1), ("kind", 1), ("language", 1)], name="by_corpus")
        coll.create_index([("kb_version", 1), ("leak_group", 1)], name="by_leak_group")
    elif kind == WEB:
        coll.create_index([("url_hash", 1), ("fetched_at", 1)], name="by_url_freshness")
        coll.create_index([("fetched_at", 1)], name="by_freshness")
    elif kind == PROJECT:
        coll.create_index([("project_id", 1), ("path", 1)], name="by_file")
        coll.create_index([("project_id", 1), ("language", 1)], name="by_language")
    else:
        raise ValueError(f"Unknown chunk kind: {kind}")
    if wait:
        wait_queryable(coll, "", wait_seconds)
    return coll


def wait_queryable(coll, index_name: str, seconds: int = 600) -> float:
    """Committed SQLite data is queryable without an asynchronous index build."""
    coll.find_one({}, {"_id": 1})
    return 0.0


def wait_visible(coll, kind: str, recent: list[dict], seconds: int = 180) -> float:
    """Verify committed chunk IDs are visible, without waiting for another service."""
    for start in range(0, len(recent), 500):
        expected = {c["_id"] for c in recent[start:start + 500]}
        got = {d["_id"] for d in coll.find({"_id": {"$in": list(expected)}}, {"_id": 1})}
        if expected != got:
            raise TimeoutError(f"The newest chunks of {coll.name} are not committed.")
    return 0.0


def vector(v: list[float]) -> Binary:
    return local_vectors.encode(v)


def upsert_chunks(coll, chunks: list[dict], batch: int = 500) -> int:
    """Insert or replace chunks by ``_id`` (an unchanged chunk is written again with the same content: harmless and idempotent)."""
    if batch < 1:
        raise ValueError("The chunk write batch must be positive.")
    done = 0
    for i in range(0, len(chunks), batch):
        part = chunks[i:i + batch]
        coll.bulk_write([ReplaceOne({"_id": c["_id"]}, c, upsert=True) for c in part], ordered=False)
        done += len(part)
    return done


def search(coll, query_vector: list[float], k: int, *, kind: str, filters: list[dict] | None = None, exact: bool = False) -> list[dict]:
    """Exact top-k after every SQL metadata filter, with the historical 0..1 score.

    Both values of ``exact`` use exact cosine. Memory scales with the vector
    block and requested top-k; only winning documents are hydrated with text.
    """
    if kind not in (KB, PROJECT, WEB):
        raise ValueError(f"Unknown chunk kind: {kind}")
    return local_vectors.search(coll, query_vector, k, filters, batch_size=SEARCH_BATCH_SIZE)


def state_collection(db):
    return db["rag_index_state"]
