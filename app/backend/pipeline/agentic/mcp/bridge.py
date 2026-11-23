"""The MCP bridge: the one program that talks to the MCP servers, and it runs inside the agent sandbox container, never in the app.

An MCP server is a program somebody else wrote that the agent may use through a small JSON-RPC protocol (newline-delimited JSON on the server's standard input and output). Here every server runs in the
project's sandbox container, as a child of this bridge, under a user id that is not the one the agent's own shell has (the container's ``/proc/<pid>/environ`` of this bridge and its servers cannot be read by
that shell, which is where the servers' secrets are). The app starts the bridge with ``docker exec``, hands it the servers' definitions on its standard input (never in its environment, its command line or a file: a program of the same user can read the first
two, and a file is one more place for a secret to stay), and from then on asks it for things through ``client_main`` (another ``docker exec`` as the same user) over a unix socket that only that user can open.
A server runs in an empty folder of the bridge's own, never in the project's copy: a program that looks in its working folder for something to load would otherwise load what the agent's shell put there.

What it does and nothing more: start a server when it is first needed, say ``initialize`` to it, list its tools (all pages), call a tool, list and read its resources, answer the server's own ``ping`` and
refuse every other request a server makes of its client (no sampling, no roots, no elicitation), keep the last of its standard error for an error message, end a server that does not answer in time, and
refuse a message bigger than ``MAX_LINE``.

It uses nothing but the standard library, so it runs in the agent image with nothing added to it (the app passes this file's text as ``python3 -I -c``).
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from collections import deque

PROTOCOL = "2025-06-18"
MAX_LINE = 4_000_000                  # one message from a server
MAX_REQUEST = 400_000                 # one request from the app
MAX_REPLY = 2_000_000                 # one reply to the app
MAX_TOOLS = 500
MAX_PAGES = 10
STDERR_KEEP = 4_000
INIT_SECONDS = 30.0
CALL_SECONDS = 60.0
DESCRIPTION_CHARS = 2_000
MAX_SPEC = 1_000_000                  # the definitions, read from standard input
SOCKET_NAME = "bridge.sock"
PASS_ENV = ("PATH", "LANG", "SYSTEMROOT", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")


class McpError(Exception):
    """A server's refusal, or a server that is gone or silent, as a sentence."""


class McpServer:
    """One server process and the conversation with it."""

    def __init__(self, name: str, spec: dict, home: str):
        self.name, self.spec, self.home = name, spec, home
        self.proc: subprocess.Popen | None = None
        self.info: dict = {}
        self._ids = 0
        self._pending: dict[int, list] = {}
        self._send = threading.Lock()
        self._lock = threading.Lock()                           # one request at a time to a server: a server's tools are not assumed to be safe to run side by side
        self._stderr: deque[str] = deque()
        self._stderr_size = 0
        self._eof = False                                       # its output has ended: it is gone or going, even if the system has not yet said it exited

    # -- the process --------------------------------------------------------------------------------------------------------------------------------

    def _env(self) -> dict:
        env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
        env.update({"HOME": os.path.join(self.home, "home"), "TMPDIR": os.path.join(self.home, "tmp"), "LANG": env.get("LANG", "C.UTF-8")})
        env.update({str(k): str(v) for k, v in (self.spec.get("env") or {}).items()})
        return env

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and not self._eof

    def start(self) -> None:
        for folder in ("home", "tmp", "cwd"):
            os.makedirs(os.path.join(self.home, folder), mode=0o700, exist_ok=True)
        try:
            self.proc = subprocess.Popen(list(self.spec["command"]), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._env(),
                                         cwd=os.path.join(self.home, "cwd"), bufsize=0)       # an empty folder of its own: not the project's copy, which the agent's shell can write
        except (OSError, ValueError) as e:
            raise McpError(f"{self.name} could not be started: {e}") from None
        self._stderr.clear()
        self._stderr_size = 0
        self._eof = False
        threading.Thread(target=self._read_loop, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._err_loop, args=(self.proc,), daemon=True).start()
        try:
            result = self._request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "ai-vulnerability-framework", "version": "1"}}, INIT_SECONDS)
            self.info = {"server": (result or {}).get("serverInfo") or {}, "protocol": (result or {}).get("protocolVersion"), "capabilities": (result or {}).get("capabilities") or {}}
            self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except McpError:
            self.close()
            raise

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        for stream in (proc.stdin,):
            try:
                stream.close()
            except OSError:  # ignored: it is already closed
                pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:  # ignored: it is already gone
                pass
        self._fail_all("the server was stopped")

    # -- the conversation ---------------------------------------------------------------------------------------------------------------------------

    def _write(self, message: dict) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise McpError(f"{self.name} is not running")
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        with self._send:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError):
                raise McpError(f"{self.name} stopped reading: {self.tail() or 'it has exited'}") from None

    def _request(self, method: str, params: dict | None, timeout: float):
        with self._send:
            self._ids += 1
            key = self._ids
            slot = [threading.Event(), None]
            self._pending[key] = slot
        message = {"jsonrpc": "2.0", "id": key, "method": method}
        if params is not None:
            message["params"] = params
        try:
            self._write(message)
        except McpError:
            self._pending.pop(key, None)
            raise
        if not slot[0].wait(timeout):
            self._pending.pop(key, None)
            self.close()                                        # a server that does not answer is ended; the next request starts it again
            raise McpError(f"{self.name} did not answer {method} within {int(timeout)} seconds and was stopped. {self.tail()}".strip())
        reply = slot[1]
        if "error" in reply:
            error = reply["error"] if isinstance(reply["error"], dict) else {"message": str(reply["error"])}
            raise McpError(f"{self.name}: {str(error.get('message') or 'an error')[:500]}")
        return reply.get("result")

    def request(self, method: str, params: dict | None = None, timeout: float = CALL_SECONDS):
        with self._lock:
            if not self.alive():
                self.start()
            return self._request(method, params, timeout)

    def _fail_all(self, why: str) -> None:
        with self._send:
            pending, self._pending = list(self._pending.values()), {}
        for slot in pending:
            slot[1] = {"error": {"message": why}}
            slot[0].set()

    def _read_loop(self, proc: subprocess.Popen) -> None:
        try:
            while True:
                line = proc.stdout.readline(MAX_LINE)
                if not line:
                    break
                if not line.endswith(b"\n") and len(line) >= MAX_LINE:
                    self._fail_all(f"{self.name} sent a message bigger than {MAX_LINE // 1_000_000} MB and was stopped")
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue                                    # a line that is not JSON (a program that prints to its standard output): not a message
                if not isinstance(message, dict):
                    continue
                if "method" in message and "id" in message:
                    self._answer_request(message)
                elif "id" in message and ("result" in message or "error" in message):
                    with self._send:
                        slot = self._pending.pop(message["id"], None) if isinstance(message["id"], int) else None
                    if slot is not None:
                        slot[1] = message
                        slot[0].set()
        except (OSError, ValueError):
            pass
        finally:
            if self.proc is proc:
                self._eof = True
                self._fail_all(f"{self.name} exited. {self.tail()}".strip())

    def _answer_request(self, message: dict) -> None:
        """A server asking its client for something: ``ping`` is answered and everything else refused (this client offers nothing)."""
        if message.get("method") == "ping":
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            reply = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "this client does not support that request"}}
        try:
            self._write(reply)
        except McpError:
            pass                                                # ignored: the server went away while asking

    def _err_loop(self, proc: subprocess.Popen) -> None:
        try:
            while True:
                chunk = proc.stderr.read(1024)
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace")
                self._stderr.append(text)
                self._stderr_size += len(text)
                while self._stderr_size > STDERR_KEEP and len(self._stderr) > 1:
                    self._stderr_size -= len(self._stderr.popleft())
        except (OSError, ValueError):
            pass

    def tail(self) -> str:
        text = "".join(self._stderr).strip()
        return f"Its last words: {text[-STDERR_KEEP:]}" if text else ""


def digest_of(spec: dict) -> str:
    """A short value that is the same for the same definitions, to tell whether a running bridge was started with the ones the app has now."""
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode("utf-8")).hexdigest()[:16]


class Hub:
    """The servers of one container, by name, started when first asked."""

    def __init__(self, spec: dict, home: str):
        self.spec, self.home = spec, home
        self.servers: dict[str, McpServer] = {}
        self._lock = threading.Lock()

    def digest(self) -> str:
        return digest_of(self.spec)

    def server(self, name: str) -> McpServer:
        definition = (self.spec.get("servers") or {}).get(name)
        if not isinstance(definition, dict):
            raise McpError(f"there is no MCP server called {name}")
        with self._lock:
            if name not in self.servers:
                self.servers[name] = McpServer(name, definition, self.home)
            return self.servers[name]

    def close_all(self) -> None:
        for server in list(self.servers.values()):
            server.close()

    # -- what the app asks -------------------------------------------------------------------------------------------------------------------------

    def status(self) -> dict:
        return {"spec": self.digest(), "servers": {n: {"alive": s.alive(), "server": s.info.get("server") or {}} for n, s in self.servers.items()}}

    def tools(self, name: str) -> dict:
        server = self.server(name)
        out, cursor = [], None
        for _ in range(MAX_PAGES):
            result = server.request("tools/list", {"cursor": cursor} if cursor else {}, INIT_SECONDS) or {}
            for t in result.get("tools") or []:
                if isinstance(t, dict) and isinstance(t.get("name"), str):
                    out.append({"name": t["name"], "description": str(t.get("description") or "")[:DESCRIPTION_CHARS], "inputSchema": t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {"type": "object"},
                                "annotations": t.get("annotations") if isinstance(t.get("annotations"), dict) else {}})
            cursor = result.get("nextCursor")
            if not cursor or len(out) >= MAX_TOOLS:
                break
        return {"tools": out[:MAX_TOOLS], "server": server.info.get("server") or {}}

    def call(self, name: str, tool: str, arguments: dict, timeout: float) -> dict:
        result = self.server(name).request("tools/call", {"name": tool, "arguments": arguments if isinstance(arguments, dict) else {}}, timeout)
        return result if isinstance(result, dict) else {"content": [{"type": "text", "text": str(result)}]}

    def resources(self, name: str) -> dict:
        result = self.server(name).request("resources/list", {}, INIT_SECONDS) or {}
        return {"resources": [r for r in (result.get("resources") or []) if isinstance(r, dict)][:MAX_TOOLS]}

    def read(self, name: str, uri: str) -> dict:
        result = self.server(name).request("resources/read", {"uri": str(uri)}, CALL_SECONDS) or {}
        return {"contents": [c for c in (result.get("contents") or []) if isinstance(c, dict)][:20]}


def handle(hub: Hub, line: str) -> str:
    """One request line from the app, one reply line. Nothing a server does can raise out of here."""
    try:
        req = json.loads(line)
        op = req.get("op")
        if op == "status":
            body = hub.status()
        elif op == "tools":
            body = hub.tools(req["server"])
        elif op == "call":
            body = hub.call(req["server"], req["tool"], req.get("arguments") or {}, min(max(float(req.get("timeout") or CALL_SECONDS), 1.0), 600.0))
        elif op == "resources":
            body = hub.resources(req["server"])
        elif op == "read":
            body = hub.read(req["server"], req["uri"])
        elif op == "stop":
            hub.close_all()
            body = {"stopped": True}
        else:
            raise McpError("unknown request")
        reply = {"ok": True, "result": body}
    except McpError as e:
        reply = {"ok": False, "error": str(e)[:1500]}
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        reply = {"ok": False, "error": f"bad request: {type(e).__name__}"}
    text = json.dumps(reply, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_REPLY:
        text = json.dumps({"ok": False, "error": f"the answer is bigger than {MAX_REPLY // 1_000_000} MB"})
    return text


# -- the socket ---------------------------------------------------------------------------------------------------------------------------------------

def _read_line(conn: socket.socket, limit: int) -> str:
    data = b""
    while b"\n" not in data and len(data) <= limit:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    return data.split(b"\n", 1)[0].decode("utf-8", "replace")


def _serve_one(hub: Hub, conn: socket.socket) -> None:
    try:
        conn.settimeout(700.0)
        if hasattr(socket, "SO_PEERCRED"):                      # only the user the bridge runs as may ask it anything
            import struct
            _pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if uid != os.getuid():
                return
        line = _read_line(conn, MAX_REQUEST)
        reply = handle(hub, line)
        conn.sendall(reply.encode("utf-8") + b"\n")
        if '"stopped":true' in reply:
            os._exit(0)
    except (OSError, ValueError):
        pass
    finally:
        try:
            conn.close()
        except OSError:  # ignored: the client has gone
            pass


def serve(path: str, hub: Hub) -> None:
    """Listen on a unix socket that only this user can open, and answer, a thread to a request."""
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    found = os.lstat(folder)                                    # a folder that was there already must be this user's own, and not a link: the agent's shell could have made it first to stand in the way
    if not stat.S_ISDIR(found.st_mode) or (hasattr(os, "getuid") and found.st_uid != os.getuid()):
        raise SystemExit(f"{folder} is not a folder of this user, so the bridge will not serve from it")
    os.chmod(folder, 0o700)
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(path)
        return                                                  # another bridge already answers there: this one is not needed
    except OSError:
        pass
    finally:
        probe.close()
    try:
        os.unlink(path)
    except OSError:  # ignored: there was no stale socket
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        listener.bind(path)
    finally:
        os.umask(old)
    os.chmod(path, 0o600)
    listener.listen(16)
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=_serve_one, args=(hub, conn), daemon=True).start()


def read_spec(stream) -> dict:
    """The definitions as the app sends them on the bridge's standard input: the number of bytes on a line, then that many bytes of JSON. Anything else is refused."""
    first = stream.readline(32)
    if not first.strip().isdigit() or not 2 <= int(first) <= MAX_SPEC:
        raise ValueError("the definitions did not start with their length")
    data = b""
    while len(data) < int(first):
        chunk = stream.read(int(first) - len(data))
        if not chunk:
            raise ValueError("the definitions ended early")
        data += chunk
    spec = json.loads(data)
    if not isinstance(spec, dict) or not isinstance(spec.get("servers"), dict):
        raise ValueError("the definitions are not a set of servers")
    return spec


def main() -> int:
    os.environ.pop("AIVDF_MCP_SPEC", None)                      # nothing is expected there any more; a value some older app left is not kept
    try:
        spec = read_spec(sys.stdin.buffer)
    except (ValueError, OSError) as e:
        print(f"no usable server definitions: {e}", file=sys.stderr)
        return 2
    null = os.open(os.devnull, os.O_RDWR)                       # the app's connection is closed once the bridge has answered once: nothing is read from or written to it again
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    home = spec.get("home") or "/tmp/aivdf-mcp"
    os.umask(0o077)
    serve(os.path.join(home, SOCKET_NAME), Hub(spec, home))
    return 0


def client_main() -> int:
    """The app's side, run by ``docker exec`` as the same user: send the request given as the argument to the bridge's socket and print its reply. Exit status 3 when nothing is listening."""
    home = os.environ.get("AIVDF_MCP_HOME") or "/tmp/aivdf-mcp"
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(os.path.join(home, SOCKET_NAME))
    except OSError:
        return 3
    conn.settimeout(700.0)
    conn.sendall(sys.argv[1].encode("utf-8") + b"\n")
    sys.stdout.write(_read_line(conn, MAX_REPLY + 1000) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(client_main() if os.environ.get("AIVDF_MCP_ROLE") == "client" else main())      # one text, two uses: the bridge itself, and the app's questions to it (docker exec, python3 -I -c)
