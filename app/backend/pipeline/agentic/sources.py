"""Where an answer's outside information came from: the web pages a session searched for or fetched, numbered so the answer can cite them as [n].

A source is not a record of its own. The result of ``websearch`` and ``webfetch`` carries the numbers (``results[].no``, ``no``), the transcript keeps those results, and everything here is read
from the transcript: the next number to give, the pages a session already knows (the ones a ``webfetch`` may fetch without asking), and the list the page shows. The page does the same from the
same results, so what it shows while a run goes and after the session is read again is the same list.
"""
from __future__ import annotations

import json
import re
import urllib.parse

MAX_URL = 2000
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'`\])]+", re.I)
WEB_TOOLS = ("websearch", "webfetch")


def domain_of(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def normal(url: str) -> str:
    """A web address as two mentions of one page are compared: no fragment, no trailing slash, scheme and host in lower case."""
    try:
        p = urllib.parse.urlparse(str(url or "").strip())
    except ValueError:
        return ""
    path = p.path.rstrip("/")
    return urllib.parse.urlunparse((p.scheme.lower(), p.netloc.lower(), path, p.params, p.query, ""))


def problem_with(url: str) -> str | None:
    """A sentence saying why ``url`` is not an address a page may be fetched from, or None. Only http and https, a host, no name or password in front of it, no spaces, not absurdly long."""
    text = str(url or "").strip()
    if not text or len(text) > MAX_URL or re.search(r"\s", text):
        return "That is not a web address."
    try:
        p = urllib.parse.urlparse(text)
        port = p.port
    except ValueError:
        return "That is not a web address."
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return "Only http and https addresses are fetched."
    if p.username is not None or p.password is not None or "@" in p.netloc:
        return "An address with a name or password in front of the host is not fetched."
    if port is not None and not 0 < port < 65536:
        return "That is not a web address."
    return None


def _parsed(content: str) -> dict | None:
    try:
        value = json.loads(content or "")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def items_of(result: dict | None) -> list[dict]:
    """The sources a result of ``websearch`` or ``webfetch`` names, as ``{no, kind, url, title, domain, snippet, query}``: a search gives one for each hit, a fetch gives the page."""
    if not result:
        return []
    out = []
    if isinstance(result.get("results"), list):
        for r in result["results"]:
            if isinstance(r, dict) and isinstance(r.get("no"), int) and r.get("url"):
                out.append({"no": r["no"], "kind": "result", "url": str(r["url"]), "title": str(r.get("title") or r["url"]), "domain": str(r.get("domain") or domain_of(str(r["url"]))),
                            "snippet": str(r.get("snippet") or ""), "query": str(result.get("query") or "")})
    elif isinstance(result.get("no"), int) and result.get("url"):
        out.append({"no": result["no"], "kind": "page", "url": str(result["url"]), "title": str(result.get("title") or result["url"]), "domain": domain_of(str(result["url"])),
                    "snippet": "", "query": ""})
    return out


def of_transcript(transcript: list[dict]) -> list[dict]:
    """Every source of a session, in the order they were given. A page that was searched for and later fetched is one source (the fetch marks it as read)."""
    by_no: dict[int, dict] = {}
    for m in transcript:
        if m.get("role") != "tool" or m.get("name") not in WEB_TOOLS or m.get("ok") is False:
            continue
        for item in items_of(_parsed(m.get("content") or "")):
            old = by_no.get(item["no"])
            if old is None:
                by_no[item["no"]] = item
            elif item["kind"] == "page":
                by_no[item["no"]] = {**old, "kind": "page", "title": item["title"] or old["title"]}
    return [by_no[n] for n in sorted(by_no)]


def number_for(url: str, known: list[dict]) -> int:
    """The number a page already has in this session, or the next free one."""
    want = normal(url)
    for s in known:
        if normal(s["url"]) == want:
            return s["no"]
    return max((s["no"] for s in known), default=0) + 1


def web_content_seen(transcript: list[dict]) -> bool:
    """Whether anything from the web has come into the session: a page, or the results of a search. It is words from outside the project that a model may be taking orders from, for as long as
    the session lasts (the owner's next message does not make it safe, and what has been read may still be in the model's context). A search made after that could be the way something of
    the project is sent out, as its query, so it asks."""
    return any(m.get("role") == "tool" and ((m.get("name") in WEB_TOOLS and m.get("ok") is not False) or str(m.get("name") or "").startswith("mcp__")) for m in transcript)       # what an MCP tool returns is words from outside the project too, and so is the text of its failure


def known_urls(transcript: list[dict]) -> frozenset[str]:
    """The addresses a ``webfetch`` may use without asking: the pages of this session's searches and fetches, and any address the owner wrote in a message."""
    urls = {normal(s["url"]) for s in of_transcript(transcript)}
    for m in transcript:
        if m.get("role") == "user" and m.get("origin") != "agent":             # a message another agent wrote (the task a subagent was given) is not the owner's word for an address
            urls |= {normal(u.rstrip(".,;:!?")) for u in _URL_IN_TEXT.findall(m.get("content") or "")}
    return frozenset(u for u in urls if u)
