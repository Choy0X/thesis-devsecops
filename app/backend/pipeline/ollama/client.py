"""How the application talks to Ollama: the address comes from ``OLLAMA_HOST`` (read at each call, so a change is found), and every call is plain ``urllib``, because callers are
ordinary threads and job processes. Failures become ``OllamaError`` with a short code: ``unreachable``, ``timeout``, ``not_found`` or ``failed``.
"""
from __future__ import annotations

import json
import ipaddress
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit

DEFAULT_HOST = "http://127.0.0.1:11434"
# How long Ollama keeps a runner the app uses loaded after its last request.
KEEP_ALIVE = "30m"

# The system proxy must never sit between the application and a local Ollama.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class OllamaError(Exception):
    """An Ollama call that did not succeed."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def base_url() -> str:
    """``OLLAMA_HOST`` read the way Ollama reads it (scheme http, host 127.0.0.1, port 11434, or 80 and 443 for an
    explicit http or https scheme without a port). A wildcard bind address (0.0.0.0, ::, empty) is reached on
    127.0.0.1, the address a client can connect to on every platform."""
    value = (os.environ.get("OLLAMA_HOST") or "").strip()
    scheme, separator, rest = value.partition("://")
    default_port = "11434"
    if not separator:
        scheme, rest = "http", value
    elif scheme == "http":
        default_port = "80"
    elif scheme == "https":
        default_port = "443"
    hostport, _, path = rest.partition("/")
    host, port = hostport, ""
    if hostport.startswith("["):
        closing = hostport.find("]")
        host, port = hostport[1:closing], hostport[closing + 1:].removeprefix(":") if closing > 0 else ""
    elif hostport.count(":") == 1:
        host, port = hostport.split(":")
    elif hostport.count(":") > 1:
        host = hostport
    port = port or default_port
    if not port.isdigit() or int(port) > 65535:
        return DEFAULT_HOST
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    url = f"{scheme}://{host}:{int(port)}"
    return url + "/" + path.rstrip("/") if path.strip("/") else url


def _call(method: str, path: str, body: dict | None = None, *, timeout: float | None = 30.0) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base_url() + path, method=method, data=data, headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        message = _error_text(e.read()) or f"HTTP {e.code}"
        raise OllamaError("not_found" if e.code == 404 else "failed", message) from None
    except (urllib.error.URLError, OSError) as e:
        raise _transport_error(e) from None
    try:
        out = json.loads(raw) if raw else {}
    except ValueError:
        raise OllamaError("failed", "Ollama sent an answer that is not JSON") from None
    return out if isinstance(out, dict) else {}


def _transport_error(error: OSError) -> "OllamaError":
    """``timeout`` when a connected server did not answer in time; ``unreachable`` when nothing accepted the connection.
    A loopback connect that times out counts as unreachable: a live local server accepts at once, and Windows reports
    a refused loopback connection as a connect timeout."""
    reason = getattr(error, "reason", error)
    connecting = isinstance(error, urllib.error.URLError)
    waited = isinstance(reason, TimeoutError) and not (connecting and local_endpoint())
    return OllamaError("timeout" if waited else "unreachable", str(reason))


def _error_text(raw: bytes) -> str:
    try:
        return str(json.loads(raw).get("error") or "")
    except (ValueError, AttributeError):
        return ""


def version(timeout: float = 2.0) -> str | None:
    """Ollama's version, or None when it cannot be reached (the cheap way to ask whether it runs)."""
    try:
        return str(_call("GET", "/api/version", timeout=timeout).get("version") or "") or None
    except OllamaError:
        return None


def tags(*, timeout: float = 30.0, include_metadata: bool = False) -> list[dict]:
    """The installed models, each ``{"name", "size", "digest"}``."""
    output = []
    for model in _call("GET", "/api/tags", timeout=timeout).get("models") or []:
        if not isinstance(model, dict):
            continue
        row = {"name": model.get("name") or model.get("model"), "size": model.get("size", 0), "digest": model.get("digest", "")}
        if include_metadata:
            row.update(remote_host=model.get("remote_host"), remote_model=model.get("remote_model"))
        output.append(row)
    return output


def show(name: str, *, timeout: float = 10.0) -> dict:
    """Read a model's capabilities and remote classification without invoking it."""
    return _call("POST", "/api/show", {"model": name}, timeout=timeout)


def create_context_model(source: str, target: str, context: int, *, timeout: float = 30.0) -> None:
    """Copy a model's configuration and set context for the OpenAI-compatible endpoint."""
    result = _call("POST", "/api/create", {"model": target, "from": source,
                                          "parameters": {"num_ctx": context}, "stream": False}, timeout=timeout)
    if result.get("status") != "success":
        raise OllamaError("failed", str(result.get("error") or "Ollama did not create the context variant."))


def pull(name: str, *, progress, timeout: float = 90.0) -> None:
    """Pull an Ollama library model and forward its streaming download reports."""
    request = urllib.request.Request(base_url() + "/api/pull", method="POST",
                                    data=json.dumps({"model": name, "stream": True}).encode("utf-8"),
                                    headers={"Content-Type": "application/json"})
    complete = False
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            while raw := response.readline(1024 ** 2):
                item = json.loads(raw)
                if not isinstance(item, dict):
                    raise ValueError("Expected a pull progress object")
                if item.get("error"):
                    raise OllamaError("failed", str(item["error"]))
                progress(item)
                complete = item.get("status") == "success"
    except urllib.error.HTTPError as error:
        raise OllamaError("failed", _error_text(error.read()) or f"HTTP {error.code}") from None
    except (urllib.error.URLError, OSError) as error:
        raise _transport_error(error) from None
    except ValueError:
        raise OllamaError("failed", "Ollama sent an invalid pull progress report.") from None
    if not complete:
        raise OllamaError("failed", "Ollama ended the pull before confirming success.")


def local_endpoint() -> bool:
    """Only loopback Ollama endpoints can be treated as local for project content."""
    try:
        endpoint = urlsplit(base_url())
        if endpoint.scheme not in ("http", "https") or endpoint.username is not None or endpoint.password is not None:
            return False
        return endpoint.hostname == "localhost" or ipaddress.ip_address(endpoint.hostname).is_loopback
    except ValueError:
        return False


def chat_text(name: str, messages: list[dict], *, context: int, cancelled, on_chunk=None,
              on_input_usage=None, max_output_tokens: int = 2048, timeout: float = 120.0) -> str:
    """Return a plain chat reply from local Ollama without exposing any tools."""
    if not local_endpoint():
        raise OllamaError("failed", "Text chat requires a local Ollama endpoint.")
    body = {"model": name, "messages": messages, "stream": True, "keep_alive": KEEP_ALIVE,
            "options": {"num_ctx": context, "num_predict": max_output_tokens}}
    request = urllib.request.Request(base_url() + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                    headers={"Content-Type": "application/json"})
    chunks = []
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            while not cancelled.is_set() and (line := response.readline(1024 ** 2)):
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError("Expected a chat response object.")
                if item.get("error"):
                    raise OllamaError("failed", str(item["error"]))
                content = (item.get("message") or {}).get("content")
                if isinstance(content, str) and content:
                    chunks.append(content)
                    if on_chunk:
                        on_chunk(content)
                if item.get("done"):
                    text = "".join(chunks)
                    if text.strip():
                        if on_input_usage:
                            on_input_usage(item.get("prompt_eval_count"))
                        return text
                    break
    except urllib.error.HTTPError as error:
        raise OllamaError("failed", _error_text(error.read()) or f"HTTP {error.code}") from None
    except (urllib.error.URLError, OSError) as error:
        raise _transport_error(error) from None
    except (ValueError, AttributeError):
        raise OllamaError("failed", "Ollama sent an invalid text reply.") from None
    if cancelled.is_set():
        return ""
    raise OllamaError("failed", "Ollama ended the turn without a text reply.")


def ps(*, timeout: float | None = 30.0, include_metadata: bool = False) -> list[dict]:
    """Loaded model names, VRAM sizes and expirations, with placement and context metadata on request."""
    rows = []
    for model in _call("GET", "/api/ps", timeout=timeout).get("models") or []:
        row = {"name": model.get("name") or model.get("model"), "size_vram": model.get("size_vram", 0),
               "expires_at": model.get("expires_at")}
        if include_metadata:
            row.update(size=model.get("size"), context_length=model.get("context_length"))
        rows.append(row)
    return rows


def load(name: str, *, keep_alive: str = KEEP_ALIVE, timeout: float | None = None, context: int | None = None) -> None:
    """Load a model by sending an empty generate request; returns when Ollama answers (no timeout unless the caller gives one, a big model takes a while)."""
    body = {"model": name, "prompt": "", "keep_alive": keep_alive, "stream": False}
    if context is not None:
        body["options"] = {"num_ctx": context}
    _call("POST", "/api/generate", body, timeout=timeout)


def unload(name: str, *, timeout: float | None = 30.0) -> None:
    """Free a model now (keep_alive 0)."""
    _call("POST", "/api/generate", {"model": name, "prompt": "", "keep_alive": 0, "stream": False}, timeout=timeout)


def unload_all() -> list[str]:
    """Free every loaded model; the names that were loaded."""
    names = [m["name"] for m in ps()]
    for name in names:
        unload(name)
    return names


def embed(name: str, texts: list[str], *, keep_alive: str | None = None, timeout: float | None = 120.0) -> list[list[float]]:
    """One embedding vector per text."""
    body = {"model": name, "input": list(texts)}
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    return _call("POST", "/api/embed", body, timeout=timeout).get("embeddings") or []
