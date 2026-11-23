"""Web pages a conversation's search turned up, chunked and embedded the same way project files are (``atlas.WEB``), so a question is
answered from a retrieved excerpt rather than a raw search result or a whole fetched page. Not project-scoped: a page already indexed for
one project's question answers another's too, since nothing about the page itself is project-specific.

Freshness, not an incremental-reindex record: a page already indexed within ``FRESH_SECONDS`` is not re-fetched; one that is not (or was
never indexed) simply gets indexed again on its next use -- natural, bounded rotation, no separate sweep.
"""
from __future__ import annotations

import hashlib
import time
import urllib.parse

from app.backend.pipeline.rag import atlas, web_fetch, web_search
from app.backend.pipeline.rag.embed import Embedder
from app.backend.pipeline.scan import code_rules

FRESH_SECONDS = 86_400             # 24h: long enough that one research session does not re-fetch the same page, short enough a changing page is not served stale for long
WINDOW, OVERLAP = 1800, 300        # characters: WINDOW matches project_chat.RETRIEVED_CHARS 1:1; the ~16.7% overlap mirrors project_index.windows()'s own ratio (10/60), in characters
                                   # since prose has no lines
K_RESULTS = 5                      # search results fetched per question


def windows(text: str, size: int = WINDOW, overlap: int = OVERLAP) -> list[tuple[int, str]]:
    """``(index, chunk)`` pairs over ``text``, character-windowed with overlap (``project_index.windows()`` does the same for code, by line)."""
    step = max(1, size - overlap)
    out: list[tuple[int, str]] = []
    i = 0
    for start in range(0, max(len(text), 1), step):
        part = text[start:start + size]
        if part.strip():
            out.append((i, part))
            i += 1
        if start + size >= len(text):
            break
    return out


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:24]


def _is_fresh(db, embedder: Embedder, url: str, fresh_seconds: int) -> bool:
    coll = atlas.ensure_collection(db, atlas.WEB, embedder, wait=False)
    doc = coll.find_one({"url_hash": _url_hash(url), "fetched_at": {"$gte": time.time() - fresh_seconds}}, {"_id": 1})
    return doc is not None


def index_page(db, url: str, title: str, text: str, embedder: Embedder) -> dict:
    """Mask, chunk, embed and store one page's text. Returns ``{"url", "chunks"}``."""
    coll = atlas.ensure_collection(db, atlas.WEB, embedder)
    domain = urllib.parse.urlparse(url).hostname or ""
    uh = _url_hash(url)
    now = time.time()
    masked = code_rules.mask(text, config=True)             # a fetched page is prose, never code
    parts = windows(masked)
    vectors = embedder.encode_documents([p for _, p in parts]) if parts else []
    chunks = []
    for (i, part), v in zip(parts, vectors):
        chunks.append({"_id": f"{uh}:{i}", "url": url, "url_hash": uh, "domain": domain, "title": title[:300], "line": i, "text": part,
                        "text_sha256": hashlib.sha256(part.encode()).hexdigest(), "fetched_at": now,
                        "embedding": atlas.vector(v), "embedder_key": embedder.key})
    if chunks:
        atlas.upsert_chunks(coll, chunks)
        atlas.wait_visible(coll, atlas.WEB, chunks)
    return {"url": url, "chunks": len(chunks)}


def search_web(db, query: str, k: int, embedder: Embedder, *, fresh_seconds: int = FRESH_SECONDS) -> list[dict]:
    """The ``k`` nearest fresh web chunks to ``query``."""
    coll = atlas.ensure_collection(db, atlas.WEB, embedder, wait=False)
    qv = embedder.encode_query(query[:4000])
    hits = atlas.search(coll, qv, k, kind=atlas.WEB, filters=[{"fetched_at": {"$gte": time.time() - fresh_seconds}}])
    return [{"url": h["url"], "title": h["title"], "text": h["text"], "score": round(float(h["score"]), 3), "domain": h["domain"]} for h in hits]


def retrieve_and_index(db, query: str, k: int, embedder: Embedder, *, count: int = K_RESULTS, fresh_seconds: int = FRESH_SECONDS) -> tuple[list[dict], str | None]:
    """Search the configured provider, fetch and index whatever is not already fresh, then answer from the index. Never raises: any
    failure along the way (no provider, the provider refused, one page's fetch failing) leaves the chat to its other tools, with a
    sentence saying why when nothing could be searched at all."""
    provider = web_search.configured()
    if provider is None:
        return [], "No web search provider is configured in Settings."
    try:
        hits = provider.search(query, count)
    except web_search.WebSearchError as e:
        return [], str(e)
    for hit in hits:
        url = hit.get("url")
        if not url or _is_fresh(db, embedder, url, fresh_seconds):
            continue
        try:
            html = web_fetch.fetch(url)
            title, text = web_fetch.extract(html, url)
            index_page(db, url, title, text, embedder)
        except web_fetch.FetchError:
            continue                   # ignored: one page failing to fetch/extract should not sink the whole search
    results = search_web(db, query, k, embedder, fresh_seconds=fresh_seconds)
    if not results and not hits:
        return [], "The web search returned nothing."
    return results, None
