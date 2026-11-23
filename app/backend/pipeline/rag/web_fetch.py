"""Turning a web search result into clean text: fetch the page (guarded against reaching anywhere but the public internet, even across a
redirect or a DNS answer that changes between the check and the request) and extract its main content with ``trafilatura``. The page's text
is still untrusted data once extracted -- masking for credentials and the "data, not instructions" framing happen a level up, in
``project_chat.leads_web()``, the same place project file excerpts already get that treatment.

The host is resolved exactly once per hop and the validated IP is what the connection actually dials (via urllib3's own
``server_hostname``/``assert_hostname``, so TLS is still verified against the real hostname): checking a hostname and then letting
``requests`` resolve it again for the real connection would leave a DNS-rebinding gap (a malicious name server could answer the check with a
public address and the real connection with a private one). The model only ever supplies a search query, never a fetch target -- the
provider's own result list names the URL -- but this stays defence in depth against a provider, or a redirect from a fetched page, pointing
at this machine or its network.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import urllib.parse

import trafilatura
import urllib3

MAX_HTML_BYTES = 3 * 1024 * 1024
FETCH_TIMEOUT = 10.0
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; AIVDF-research/1.0)"

_CGNAT = ipaddress.ip_network("100.64.0.0/10")           # carrier-grade NAT / shared address space (RFC 6598): ipaddress.is_private does not cover it


class FetchError(ValueError):
    """The page could not be fetched or had nothing worth reading, as a sentence for the person."""


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
                or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT))


def _resolve_public_ip(host: str) -> str:
    """The first address ``host`` resolves to that is public. Raises when resolution fails or nothing public comes back."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise FetchError("That address does not look like a public web page.") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_public_ip(ip):
            return str(ip)
    raise FetchError("That address does not look like a public web page.")


def _request(hostname: str, port: int, scheme: str, path: str):
    """One GET, connected to ``hostname``'s already-validated IP directly (never resolved a second time): TLS, when ``https``, is still
    verified against ``hostname`` itself via urllib3's ``server_hostname``/``assert_hostname``."""
    ip = _resolve_public_ip(hostname)
    timeout = urllib3.Timeout(connect=FETCH_TIMEOUT, read=FETCH_TIMEOUT)
    if scheme == "https":
        pool = urllib3.HTTPSConnectionPool(ip, port, timeout=timeout, retries=False, server_hostname=hostname, assert_hostname=hostname)
    else:
        pool = urllib3.HTTPConnectionPool(ip, port, timeout=timeout, retries=False)
    try:
        return pool.request("GET", path, headers={"Host": hostname, "User-Agent": USER_AGENT}, preload_content=False, redirect=False)
    except urllib3.exceptions.HTTPError as e:
        raise FetchError(f"The page did not answer ({type(e).__name__}).") from e


def fetch(url: str) -> bytes:
    """The page's raw bytes. Refuses a non-http(s) address, a private/loopback/reserved target (checked again at every redirect hop), a
    non-text answer, and anything over ``MAX_HTML_BYTES``."""
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise FetchError("Not a web address.")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = (parsed.path or "/") + (f";{parsed.params}" if parsed.params else "") + (f"?{parsed.query}" if parsed.query else "")
        r = _request(parsed.hostname, port, parsed.scheme, path)
        try:
            if r.status in (301, 302, 303, 307, 308):
                location = r.headers.get("Location")
                if not location:
                    raise FetchError("The page redirected with no destination.")
                url = urllib.parse.urljoin(url, location)
                continue
            if r.status != 200:
                raise FetchError(f"The page answered {r.status}.")
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                raise FetchError(f"The page is not text ({ctype or 'an unknown type'}).")
            chunks: list[bytes] = []
            total = 0
            for chunk in r.stream(65536):
                total += len(chunk)
                if total > MAX_HTML_BYTES:
                    raise FetchError("The page is too large.")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            r.release_conn()
    raise FetchError("Too many redirects.")


def extract(html: bytes, url: str) -> tuple[str, str]:
    """``(title, text)``: the page's clean main-content text. Raises when nothing worth keeping came out of it."""
    result = trafilatura.extract(html, url=url, with_metadata=True, output_format="json", favor_recall=True)
    if not result:
        raise FetchError("Nothing readable could be pulled from that page.")
    data = json.loads(result)
    text = (data.get("text") or "").strip()
    if not text:
        raise FetchError("Nothing readable could be pulled from that page.")
    return data.get("title") or url, text
