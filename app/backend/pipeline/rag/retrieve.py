"""Retrieval against the embedded knowledge base and recall@k measurement. See :mod:`kb`."""
from __future__ import annotations

from app.backend.pipeline.rag import kb


def retrieve(query: str, k: int = 5, **options):
    """The ``k`` nearest train chunks as ``[{id, document, score, distance, metadata}]`` (the shape the classifier prompt expects)."""
    return kb.retrieve(query, k, **options)


def recall_at_k(n: int = 200, k: int = 5, dataset: str | None = None) -> float | None:
    """Exact-search recall@k on validation pairs (``dataset`` is ignored: the corpus is the split manifest in the database)."""
    return kb.recall_at_k(n=n, k=k)


if __name__ == "__main__":
    recall_at_k()
