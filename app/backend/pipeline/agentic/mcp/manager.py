"""The app's side of the MCP bridge: start it in a project's sandbox container, ask it for the tools of the owner's servers, and call them.

The bridge (``bridge.py``) runs in the container as user 10002 with group 10001, not as 10001 which is what the agent's own shell is (``scan/agent_sandbox_image/Dockerfile``), so the shell cannot read the bridge's
or its servers' environment (that is where the servers' secrets are) and cannot reach its socket, and the servers can read the project's copy (by the group) but not change it. Everything here goes through
``docker exec``: a detached one starts the bridge with the definitions in its environment, and a short one, run for each question, carries the question to the socket and brings the answer back. The
text of ``bridge.py`` itself is the program (``python3 -I -c``), so nothing has to be added to the image.
"""
from __future__ import annotations

import json
import pathlib
import re
import threading
import time
from dataclasses import dataclass, field

from app.backend import sandbox
from app.backend.pipeline.agentic.mcp import bridge, config

BRIDGE_USER = "10002:10001"
HOME = "/tmp/aivdf-mcp"
SOURCE = pathlib.Path(bridge.__file__).read_text(encoding="utf-8")
START_SECONDS = 20.0
ASK_SECONDS = 30
LIST_SECONDS = 45
MAX_ARGUMENTS = 100_000
CACHE_SECONDS = 600
NAME_LIMIT = 64


class McpUnavailable(RuntimeError):
    """The bridge or a server could not be reached, as a sentence."""


@dataclass
class Listing:
    """What the servers offer in one container: the tools by the name the model sees, and how each server is."""
    tools: dict[str, dict] = field(default_factory=dict)         # model name -> {"server", "tool", "description", "schema", "annotations", "timeout"}
    servers: list[dict] = field(default_factory=list)            # {"name", "ok", "tools", "error"}
    digest: str = ""
    at: float = 0.0


_LOCKS: dict[str, threading.Lock] = {}
_CACHE: dict[str, Listing] = {}
_GUARD = threading.Lock()


def _lock(container_id: str) -> threading.Lock:
    with _GUARD:
        return _LOCKS.setdefault(container_id, threading.Lock())


def forget(container_id: str | None = None) -> None:
    """Drops what is remembered about a container (all of them when none is named): the next question asks the bridge again."""
    with _GUARD:
        if container_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(container_id, None)


def spec_for(servers: dict) -> dict:
    """What the bridge is given on its standard input: the enabled servers (program and environment)."""
    return {"home": HOME, "servers": {n: {"command": d["command"], "env": d["env"]} for n, d in servers.items()}}


def _run(container_id: str, payload: dict | None, *, role: str, timeout: int) -> tuple[int | None, str]:
    out = []
    code = None
    argv = ["python3", "-I", "-c", SOURCE] + ([json.dumps(payload, separators=(",", ":"))] if payload is not None else [])
    for frame in sandbox.exec_stream(container_id, argv, "/tmp", timeout=timeout, environment={"AIVDF_MCP_ROLE": role, "AIVDF_MCP_HOME": HOME}, user=BRIDGE_USER):
        if "exit_code" in frame:
            code = frame["exit_code"]
        elif frame["stream"] == "stdout":
            out.append(frame["data"])
    return code, b"".join(out).decode("utf-8", "replace").strip()


def ask(container_id: str, payload: dict, timeout: int = ASK_SECONDS) -> dict:
    """One question to the bridge and its answer's ``result``. Raises ``McpUnavailable`` with the reason when the bridge is not there or a server refuses."""
    text = json.dumps(payload, separators=(",", ":"))
    if len(text) > MAX_ARGUMENTS + 2_000:
        raise McpUnavailable(f"The arguments are bigger than {MAX_ARGUMENTS // 1000} KB, which is more than a call to an MCP server may carry.")
    try:
        code, out = _run(container_id, payload, role="client", timeout=timeout + 10)
    except PermissionError:
        raise McpUnavailable("The sandbox is not there.") from None
    if code == 3:
        raise McpUnavailable("The MCP bridge is not running in the sandbox.")
    if code != 0 or not out:
        raise McpUnavailable(f"The MCP bridge did not answer (exit status {code}).")
    try:
        reply = json.loads(out.splitlines()[-1])
    except ValueError:
        raise McpUnavailable("The MCP bridge sent something that is not an answer.") from None
    if not reply.get("ok"):
        raise McpUnavailable(str(reply.get("error") or "the server refused"))
    return reply.get("result") or {}


def ensure_bridge(container_id: str, spec: dict) -> None:
    """The bridge running in the container with exactly these definitions: started if it is not there, and replaced if it was started with others."""
    digest = bridge.digest_of(spec)
    with _lock(container_id):
        try:
            status = ask(container_id, {"op": "status"}, 15)
            if status.get("spec") == digest:
                return
            try:
                ask(container_id, {"op": "stop"}, 15)                  # started with other definitions (the owner changed them): it ends, its servers with it
            except McpUnavailable:
                pass                                                   # ignored: it went away on its own, which is what was wanted
            time.sleep(0.4)
        except McpUnavailable:
            pass                                                       # ignored: no bridge yet, so one is started below
        blob = json.dumps(spec, separators=(",", ":")).encode("utf-8")
        link = sandbox.exec_with_stdin(container_id, ["python3", "-I", "-c", SOURCE], f"{len(blob)}\n".encode() + blob, "/tmp", environment={"AIVDF_MCP_ROLE": "bridge"}, user=BRIDGE_USER)
        try:                                                           # the definitions go in on its standard input; the connection stays open until the bridge has answered, so nothing is cut short
            deadline = time.monotonic() + START_SECONDS
            while time.monotonic() < deadline:
                try:
                    if ask(container_id, {"op": "status"}, 10).get("spec") == digest:
                        return
                except McpUnavailable:
                    pass                                               # ignored: it has not opened its socket yet
                time.sleep(0.3)
        finally:
            try:
                link.close()
            except Exception:  # ignored: the connection is closed already
                pass
    raise McpUnavailable("The MCP bridge did not start in the sandbox.")


def model_name(server: str, tool: str, taken: set[str]) -> str:
    """The name the model sees: ``mcp__<server>__<tool>`` within the 64 characters function names may have, the tool's own name made of letters, digits, underscore and hyphen, and never one already taken."""
    base = f"mcp__{server}__"
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", tool) or "tool"
    name = (base + clean)[:NAME_LIMIT]
    n = 2
    while name in taken:
        suffix = f"_{n}"
        name = (base + clean)[:NAME_LIMIT - len(suffix)] + suffix
        n += 1
    return name


def known(container_id: str) -> Listing | None:
    """What was last listed for this container, whatever its age: the tools a session was offered are the ones it may call, even if the definitions have changed since."""
    with _GUARD:
        return _CACHE.get(container_id)


def listing(container_id: str, *, refresh: bool = False, servers: dict | None = None) -> Listing:
    """The tools of the owner's enabled servers in this container, asked of the bridge (which starts each server) and remembered for ten minutes or until the definitions change. A server that
    cannot be started or listed is shown with the reason and has no tools; the others are not held up by it."""
    servers = config.enabled() if servers is None else servers
    spec = spec_for(servers)
    digest = bridge.digest_of(spec)
    with _GUARD:
        cached = _CACHE.get(container_id)
    if cached is not None and not refresh and cached.digest == digest and time.monotonic() - cached.at < CACHE_SECONDS:
        return cached
    out = Listing(digest=digest, at=time.monotonic())
    if not servers:
        with _GUARD:
            _CACHE[container_id] = out
        return out
    try:
        ensure_bridge(container_id, spec)
    except (McpUnavailable, OSError, ValueError) as e:
        out.servers = [{"name": n, "ok": False, "tools": 0, "error": str(e)} for n in servers]
        return out                                                     # not remembered: the next session tries again
    for name, definition in servers.items():
        try:
            found = ask(container_id, {"op": "tools", "server": name}, LIST_SECONDS)
        except McpUnavailable as e:
            out.servers.append({"name": name, "ok": False, "tools": 0, "error": str(e)})
            continue
        count = 0
        for t in found.get("tools") or []:
            mname = model_name(name, t["name"], set(out.tools))
            out.tools[mname] = {"server": name, "tool": t["name"], "description": t.get("description") or "", "schema": t.get("inputSchema") or {"type": "object"},
                                "annotations": t.get("annotations") or {}, "timeout": definition["timeout"]}
            count += 1
        out.servers.append({"name": name, "ok": True, "tools": count, "error": "", "info": found.get("server") or {}})
    with _GUARD:
        _CACHE[container_id] = out
    return out


def call(container_id: str, server: str, tool: str, arguments: dict, timeout: int) -> dict:
    """The result of one tool call as the server gave it (``content`` blocks, ``isError``)."""
    return ask(container_id, {"op": "call", "server": server, "tool": tool, "arguments": arguments, "timeout": timeout}, timeout + 5)
