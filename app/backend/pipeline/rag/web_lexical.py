"""Retrieve excerpts from the current web results with a temporary local index."""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from urllib.parse import urlsplit

from app.backend.pipeline.rag import web_fetch, web_search
from app.backend.pipeline.scan import code_rules

WINDOW = 1800
STEP = 1500
MAX_PAGE_CHARS = 60_000


def retrieve(query: str, limit: int, *, count: int = 5, cancelled=None) -> tuple[list[dict], str | None]:
    provider = web_search.configured()
    if provider is None:
        return [], "No web search provider is configured in Settings."
    try:
        results = provider.search(query, count)
    except web_search.WebSearchError as exc:
        return [], str(exc)
    if not results:
        return [], "The web search returned nothing."

    terms = list(dict.fromkeys(re.findall(r"[^\W_]{2,64}", query[:4000].lower())))[:24]
    if not terms:
        return [], None
    match = " OR ".join(f'"{term}"' for term in terms)

    seen = set()
    with closing(sqlite3.connect(":memory:")) as index:
        index.execute("CREATE VIRTUAL TABLE passages USING fts5(title, text, url UNINDEXED, domain UNINDEXED)")
        for result in results[:count]:
            if cancelled and cancelled.is_set():
                return [], None
            url = result.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                title, content = web_fetch.extract(web_fetch.fetch(url), url)
                hostname = urlsplit(url).hostname or ""
            except ValueError:
                continue
            title = code_rules.masked(title, 300, config=True)
            content = code_rules.mask(content[:MAX_PAGE_CHARS], config=True)
            passages = ((title, content[start:start + WINDOW], url, hostname) for start in range(0, len(content), STEP))
            index.executemany("INSERT INTO passages VALUES (?, ?, ?, ?)", passages)
        matches = index.execute(
            "SELECT title, text, url, domain FROM passages WHERE passages MATCH ? ORDER BY bm25(passages), rowid LIMIT ?",
            (match, limit),
        ).fetchall()
    return [{"title": title, "text": text, "url": url, "domain": domain, "score": round(1 / rank, 3)}
            for rank, (title, text, url, domain) in enumerate(matches, 1)], None
