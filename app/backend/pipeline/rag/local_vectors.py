"""Bounded exact cosine search over SQL-filtered embedded documents.

No database server or native vector extension is needed. Local GGUF embedding
still uses the model gateway; external embedding still uses the chosen endpoint.
This module only stores and compares the vectors those engines already made.
"""
from __future__ import annotations

import heapq
from itertools import islice

import numpy as np

from app.backend.pipeline.db.types import Binary

BLOCK_BYTES = 8 * 1024 * 1024


def array(value, *, dimension: int | None = None, label: str = "Vector") -> np.ndarray:
    """Decode subtype-9 float32 or legacy arrays, rejecting guessed binary formats.

    The BSON vector header is a dtype byte followed by a padding byte. Float32
    payloads are little-endian. INT8 and PACKED_BIT remain preserved in storage
    but cannot be searched as float32 without explicit model-specific handling.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        if getattr(value, "subtype", None) != 9:
            raise ValueError(f"{label} must use BSON vector subtype 9.")
        raw = memoryview(value)
        if len(raw) < 2:
            raise ValueError(f"{label} has an incomplete float32 vector header.")
        if raw[0] != 0x27:
            raise ValueError(f"{label} has unsupported vector dtype 0x{raw[0]:02x}; float32 is required.")
        if raw[1] != 0:
            raise ValueError(f"{label} has invalid float32 padding.")
        if (len(raw) - 2) % 4:
            raise ValueError(f"{label} has an invalid float32 payload length.")
        result = np.frombuffer(raw[2:], dtype="<f4").astype(np.float64)
    elif isinstance(value, (list, tuple, np.ndarray)):
        try:
            result = np.asarray(value)
            if result.dtype.kind not in "iuf":
                raise ValueError("not a numeric array")
            result = result.astype(np.float64)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{label} must be a numeric vector.") from exc
    else:
        raise ValueError(f"{label} must be a float32 BSON vector or numeric array.")
    if result.ndim != 1 or result.size == 0:
        raise ValueError(f"{label} must have a nonempty one-dimensional vector dimension.")
    if dimension is not None and result.size != dimension:
        raise ValueError(f"{label} dimension {result.size} differs from query dimension {dimension}.")
    if not np.isfinite(result).all():
        raise ValueError(f"{label} values must be finite.")
    norm = np.linalg.norm(result)
    if norm == 0:
        raise ValueError(f"{label} is a zero vector; cosine is undefined.")
    if not np.isfinite(norm):
        raise ValueError(f"{label} norm must be finite.")
    return result


def encode(values) -> Binary:
    values = array(values)
    with np.errstate(over="ignore", invalid="ignore"):
        packed = values.astype("<f4")
    if not np.isfinite(packed).all():
        raise ValueError("Vector values must be finite float32 numbers.")
    if not np.any(packed):
        raise ValueError("Vector becomes a zero vector in float32.")
    return Binary(b"\x27\x00" + packed.tobytes(), subtype=9)


def search(coll, query_vector, k: int, filters: list[dict] | None, *, batch_size: int) -> list[dict]:
    """Keep scoring and document hydration on the same committed read snapshot."""
    database = getattr(coll, "database", None)
    if database is not None and callable(getattr(database, "transaction", None)):
        # Lazy metadata indexes must be created before the read transaction.
        # A schema write inside the snapshot would prevent concurrent writers.
        from app.backend.pipeline.db.query import Query
        clauses = list(filters or [])
        flt = {"$and": clauses} if len(clauses) > 1 else clauses[0] if clauses else {}
        Query(coll).compile(flt)
        with database.transaction(write=False):
            return _search(coll, query_vector, k, filters, batch_size=batch_size)
    return _search(coll, query_vector, k, filters, batch_size=batch_size)


def _search(coll, query_vector, k: int, filters: list[dict] | None, *, batch_size: int) -> list[dict]:
    """Score only eligible rows, keep top-k, then fetch the winning documents."""
    query = array(query_vector, label="Query vector")
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("The number of nearest chunks must be a positive integer.")
    query = query / np.linalg.norm(query)
    clauses = list(filters or [])
    flt = {"$and": clauses} if len(clauses) > 1 else clauses[0] if clauses else {}
    batch_size = max(1, min(batch_size, BLOCK_BYTES // (8 * query.size)))
    cursor = iter(coll.find(flt, {"embedding": 1}))
    best: list[tuple[float, str, object]] = []

    def order(hit):
        return -hit[0], hit[1]

    try:
        while rows := list(islice(cursor, batch_size)):
            matrix = np.empty((len(rows), query.size), dtype=np.float64)
            for i, row in enumerate(rows):
                matrix[i] = array(row.get("embedding"), dimension=query.size, label=f"Chunk {row['_id']}")
            cosine = np.einsum("ij,j->i", matrix, query) / np.linalg.norm(matrix, axis=1)
            scores = (1.0 + np.clip(cosine, -1.0, 1.0)) / 2.0
            candidates = ((float(score), str(row["_id"]), row["_id"]) for row, score in zip(rows, scores))
            block_best = heapq.nsmallest(k, candidates, key=order)
            best = heapq.nsmallest(k, (*best, *block_best), key=order)
    finally:
        close = getattr(cursor, "close", None)
        if close is not None:
            close()
    if not best:
        return []
    # Recheck scope while hydrating so a concurrent metadata change cannot
    # expose another project's content. Keep large top-k requests batched too.
    docs = {}
    ids = [hit[2] for hit in best]
    for start in range(0, len(ids), 500):
        selected = {"$and": [flt, {"_id": {"$in": ids[start:start + 500]}}]}
        for doc in coll.find(selected, {"embedding": 0}):
            docs[doc["_id"]] = doc
    return [{**docs[key], "score": score} for score, _, key in best if key in docs]
