"""Where a chat's web search comes from: a pluggable provider, chosen in Settings (Brave, Google Custom Search, or DuckDuckGo's unofficial,
no-key HTML endpoint). A provider only ever returns a short list of leads (url, title, snippet); the backend fetches and reads the pages
itself (``web_fetch``), so the provider never sees anything about the project, and the model never names a fetch target directly -- it only
ever supplies the search query.
"""
from __future__ import annotations

import html as _html
import os
import re
import urllib.parse
from typing import Protocol

import requests

TIMEOUT = 10.0


class WebSearchError(ValueError):
    """No provider is configured, or the provider refused the request, as a sentence for the person."""

    def __init__(self, message: str, status: int = 400, code: str = "not_configured"):
        super().__init__(message)
        self.status, self.code = status, code


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, count: int = 5) -> list[dict]:
        """Up to ``count`` leads: ``[{"url", "title", "snippet"}, ...]``."""


class BraveProvider:
    name = "brave"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(self, query: str, count: int = 5) -> list[dict]:
        try:
            r = requests.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": count},
                              headers={"Accept": "application/json", "X-Subscription-Token": self.api_key}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise WebSearchError(f"Brave Search did not answer ({type(e).__name__}).", 502, "upstream_error") from e
        if r.status_code != 200:
            raise WebSearchError(f"Brave Search answered {r.status_code}: {r.text[:200]}", 502, "upstream_error")
        results = (r.json().get("web") or {}).get("results") or []
        return [{"url": it.get("url", ""), "title": it.get("title", ""), "snippet": it.get("description", "")}
                for it in results[:count] if it.get("url")]


class GoogleProvider:
    name = "google"

    def __init__(self, api_key: str, cse_id: str):
        self.api_key, self.cse_id = api_key, cse_id

    def search(self, query: str, count: int = 5) -> list[dict]:
        try:
            r = requests.get("https://www.googleapis.com/customsearch/v1",
                              params={"key": self.api_key, "cx": self.cse_id, "q": query, "num": min(max(count, 1), 10)}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise WebSearchError(f"Google Custom Search did not answer ({type(e).__name__}).", 502, "upstream_error") from e
        if r.status_code != 200:
            raise WebSearchError(f"Google Custom Search answered {r.status_code}: {r.text[:200]}", 502, "upstream_error")
        items = r.json().get("items") or []
        return [{"url": it.get("link", ""), "title": it.get("title", ""), "snippet": it.get("snippet", "")}
                for it in items[:count] if it.get("link")]


_RESULT_LINK = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_RESULT_SNIPPET = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)
_TAG = re.compile(r"<[^>]+>")


def _clean_html_text(s: str) -> str:
    return _html.unescape(_TAG.sub("", s)).strip()


def _unwrap_duckduckgo_redirect(href: str) -> str:
    """DuckDuckGo's result links go through its own ``/l/?uddg=<encoded target>`` redirector; unwrap it to the real URL. ``parse_qs``
    already percent-decodes the value, so it is not decoded again here."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path == "/l/":
        return urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
    return href if href.startswith("http") else ""


class DuckDuckGoProvider:
    """The unofficial, no-key HTML endpoint: undocumented and more likely to break than the two providers above (its markup is not a
    published contract), kept as the no-signup option. Parsed with a couple of targeted regexes, not a new HTML-parsing dependency,
    consistent with this app's existing stance elsewhere (``code_rules.py`` also masks with plain regexes, no parser)."""
    name = "duckduckgo"

    def search(self, query: str, count: int = 5) -> list[dict]:
        try:
            r = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, headers={"User-Agent": "Mozilla/5.0"}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise WebSearchError(f"DuckDuckGo did not answer ({type(e).__name__}).", 502, "upstream_error") from e
        if r.status_code != 200:
            raise WebSearchError(f"DuckDuckGo answered {r.status_code}.", 502, "upstream_error")
        links = _RESULT_LINK.findall(r.text)
        snippets = _RESULT_SNIPPET.findall(r.text)
        out = []
        for i, (href, title) in enumerate(links[:count]):
            url = _unwrap_duckduckgo_redirect(href)
            if not url:
                continue
            snippet = _clean_html_text(snippets[i]) if i < len(snippets) else ""
            out.append({"url": url, "title": _clean_html_text(title), "snippet": snippet})
        return out


def configured() -> SearchProvider | None:
    """The provider chosen in Settings, or ``None`` when none is set up (web search is then simply skipped for that turn, with a note)."""
    provider = (os.getenv("WEB_SEARCH_PROVIDER") or "").strip().lower()
    if provider == "brave":
        key = os.getenv("BRAVE_API_KEY") or ""
        return BraveProvider(key) if key else None
    if provider == "google":
        key, cse = os.getenv("GOOGLE_API_KEY") or "", os.getenv("GOOGLE_CSE_ID") or ""
        return GoogleProvider(key, cse) if key and cse else None
    if provider == "duckduckgo":
        return DuckDuckGoProvider()
    return None
