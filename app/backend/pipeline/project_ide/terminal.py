"""App-owned Windows ConPTY terminals, separate from agent execution.

The hidden broker starts suspended inside a kill-on-close Windows job before it
can create the pseudoconsole or shell. History is durable; input is never queued
or replayed after a restart. This is trusted local execution, not OS isolation.
"""
from __future__ import annotations

import atexit
from collections import deque
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import uuid

from app.backend.pipeline.agentic.exec import toolchain


class TerminalError(ValueError):
    """Terminal validation, capability, or lifecycle failure."""


def terminal_environment(state_dir: Path) -> dict[str, str]:
    """Allow OS essentials, excluding app/provider credentials and plugin hooks."""
    allow = {"systemroot", "windir", "systemdrive", "comspec", "path", "pathext",
             "programfiles", "programfiles(x86)", "programw6432", "commonprogramfiles",
             "number_of_processors", "processor_architecture", "processor_identifier",
             "os", "username", "computername"}
    env = {k: v for k, v in os.environ.items() if k.lower() in allow}
    git = toolchain.find("git")
    if git:
        key = next((k for k in env if k.lower() == "path"), "PATH")
        env[key] = os.pathsep.join(filter(None, (str(git.parent), env.get(key))))
    state_dir = Path(state_dir).resolve()
    for name in ("home", "temp", "appdata", "localappdata"):
        (state_dir / name).mkdir(parents=True, exist_ok=True)
    env.update({"HOME": str(state_dir / "home"), "USERPROFILE": str(state_dir / "home"),
                "TEMP": str(state_dir / "temp"), "TMP": str(state_dir / "temp"),
                "APPDATA": str(state_dir / "appdata"), "LOCALAPPDATA": str(state_dir / "localappdata"),
                "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "TERM": "xterm-256color", "COLORTERM": "truecolor", "NO_UPDATE_NOTIFIER": "1"})
    # Only built-in PowerShell modules; no inherited user module/plugin directories.
    system_root = env.get("SystemRoot", env.get("SYSTEMROOT", r"C:\Windows"))
    env["PSModulePath"] = str(Path(system_root) / "System32/WindowsPowerShell/v1.0/Modules")
    return env


_INSTALLED_PWSH = [Path(os.environ.get(name, "")) / "PowerShell" / "7" / "pwsh.exe"
                   for name in ("ProgramFiles", "ProgramW6432") if os.environ.get(name)]


def find_shell() -> str | None:
    """PowerShell 7 when it is installed normally, else Windows PowerShell; never a Microsoft Store alias.

    A Store app (an alias under WindowsApps) runs in its own package job, outside the terminal's kill-on-close job,
    so closing the terminal could not end what the shell started."""
    candidates = [str(path) for path in _INSTALLED_PWSH if path.is_file()]
    candidates += [shutil.which("pwsh.exe"), shutil.which("powershell.exe")]
    for candidate in candidates:
        if candidate and "windowsapps" not in {part.casefold() for part in Path(candidate).parts}:
            return candidate
    return None


def _dimensions(cols, rows):
    if type(cols) is not int or type(rows) is not int or not 2 <= cols <= 500 or not 1 <= rows <= 300:
        raise TerminalError("Terminal size must be 2..500 columns and 1..300 rows.")


def _chat(chat_id):
    if not isinstance(chat_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", chat_id):
        raise TerminalError("Invalid terminal chat identity.")


@dataclass
class _Session:
    record: dict
    chunks: deque = field(default_factory=deque)
    output_bytes: int = 0
    process: subprocess.Popen | None = None
    job: object | None = None
    thread: threading.Thread | None = None
    shell_pid: int | None = None
    ready: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    send_lock: threading.Lock = field(default_factory=threading.Lock)
    pending: dict = field(default_factory=dict)
    error: str | None = None


class TerminalManager:
    """One app manager. Pass the IDE service root, not an original source root.

    Routes must pass chat_id on every session operation (or call assert_owner)
    after validating the enclosing project/chat. Opaque ids are not authorization.
    """

    def __init__(self, root: Path, *, max_output_bytes: int = 256 * 1024, max_sessions: int = 16):
        if not 1024 <= max_output_bytes <= 4 * 1024 * 1024 or not 1 <= max_sessions <= 128:
            raise TerminalError("Invalid terminal resource limits.")
        self.root = Path(root).resolve()
        self.history = self.root / "terminals"
        self.history.mkdir(parents=True, exist_ok=True)
        self.max_output_bytes = max_output_bytes
        self.max_sessions = max_sessions
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._recover()
        atexit.register(self.close_all)

    def _recover(self):
        for path in self.history.glob("*.json"):
            if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not re.fullmatch(r"[0-9a-f]{32}", record["id"]) or path.stem != record["id"]:
                    continue
                _chat(record["chat_id"])
                _dimensions(record["cols"], record["rows"])
                state = _Session(record)
                for chunk in record.pop("chunks", []):
                    if not isinstance(chunk["data"], str) or type(chunk["sequence"]) is not int:
                        raise ValueError("Invalid terminal history chunk.")
                    state.chunks.append(chunk)
                    state.output_bytes += len(chunk["data"].encode("utf-8"))
                self._trim(state)
                if record["status"] in ("starting", "running"):
                    record["status"] = "interrupted"
                    record["exit_code"] = None
                self._sessions[record["id"]] = state
                self._save(state)
            except (OSError, ValueError, KeyError, TypeError):
                # A partial/corrupt history cannot launch a process.
                continue

    def _save(self, state):
        data = {**state.record, "chunks": list(state.chunks)}
        target = self.history / (state.record["id"] + ".json")
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
        temp.replace(target)

    def _trim(self, state):
        while state.output_bytes > self.max_output_bytes and state.chunks:
            excess = state.output_bytes - self.max_output_bytes
            first = state.chunks[0]
            raw = first["data"].encode("utf-8")
            if len(raw) <= excess:
                state.chunks.popleft()
                state.output_bytes -= len(raw)
            else:
                shortened = raw[excess:].decode("utf-8", errors="ignore")
                state.output_bytes -= len(raw) - len(shortened.encode("utf-8"))
                first["data"] = shortened
            state.record["truncated"] = True

    def _workspace(self, chat_id, workspace):
        _chat(chat_id)
        supplied = Path(workspace).absolute()
        expected = self.root / "workspaces" / chat_id
        resolved = supplied.resolve()
        if not resolved.is_dir() or resolved == expected.resolve() or not resolved.is_relative_to(expected.resolve()):
            raise TerminalError("Terminal working directory must belong to this chat's managed workspace.")
        # Resolve must not silently redirect a junction or symbolic link to source.
        current = supplied
        while current != self.root and current.parent != current:
            if current.is_symlink() or (current.exists() and getattr(current.lstat(), "st_file_attributes", 0) & 0x400):
                raise TerminalError("Terminal working directories cannot traverse links or junctions.")
            current = current.parent
        if not resolved.is_relative_to(self.root / "workspaces"):
            raise TerminalError("The terminal workspace escapes the app-managed workspace root.")
        return resolved

    def open(self, chat_id: str, workspace: Path, *, cols: int = 100, rows: int = 30) -> dict:
        workspace = self._workspace(chat_id, workspace)
        _dimensions(cols, rows)
        if os.name != "nt":
            raise TerminalError("Interactive terminals require Windows ConPTY on this host.")
        try:
            import winpty  # noqa: F401
        except ImportError as exc:
            raise TerminalError("Install requirements-projects-ide.txt in the app Python environment.") from exc
        shell = find_shell()
        if not shell:
            raise TerminalError("PowerShell is required for the Windows terminal.")
        with self._lock:
            if self._closed:
                raise TerminalError("The terminal manager is shutting down.")
            if sum(s.record["status"] in ("starting", "running") for s in self._sessions.values()) >= self.max_sessions:
                raise TerminalError("Close an existing terminal before opening another.")
            identity = uuid.uuid4().hex
            state = _Session({"id": identity, "chat_id": chat_id, "workspace": str(workspace),
                              "backend": "conpty", "status": "starting", "cols": cols, "rows": rows,
                              "sequence": 0, "truncated": False, "exit_code": None})
            self._sessions[identity] = state
            self._save(state)
            from .runtime import WindowsProcessJob
            try:
                state.job = WindowsProcessJob()
                broker = Path(__file__).resolve().parents[4] / "tools/project_ide_terminal.py"
                state.process = subprocess.Popen(
                    [sys.executable, "-I", "-u", str(broker), str(workspace), shell, str(cols), str(rows)],
                    cwd=workspace, env=terminal_environment(self.history / identity), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                    errors="strict", bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW | 0x00000004,
                )
                state.job.assign(state.process)
                state.thread = threading.Thread(target=self._pump, args=(state,), daemon=True,
                                                name="project-terminal-" + identity[:8])
                state.thread.start()
            except BaseException:
                self._stop_process(state)
                state.record["status"] = "interrupted"
                self._save(state)
                raise
        if not state.ready.wait(15):
            self.close(identity)
            raise TerminalError("The Windows ConPTY broker did not start in time.")
        if state.error or state.record["status"] != "running":
            self.close(identity)
            raise TerminalError(state.error or "The Windows ConPTY broker exited during startup.")
        return self.read(identity, chat_id=chat_id)

    def _pump(self, state):
        try:
            for line in state.process.stdout:
                event = json.loads(line)
                with state.lock:
                    kind = event.get("type")
                    if kind == "ready":
                        state.shell_pid = event["pid"]
                        state.record["status"] = "running"
                        state.ready.set()
                    elif kind == "output":
                        state.record["sequence"] += 1
                        data = event["data"]
                        state.chunks.append({"sequence": state.record["sequence"], "data": data})
                        state.output_bytes += len(data.encode("utf-8"))
                        self._trim(state)
                    elif kind == "ack":
                        pending = state.pending.get(event.get("request"))
                        if pending:
                            pending[1].update(event)
                            pending[0].set()
                        continue
                    elif kind == "exit":
                        if state.record["status"] != "closed":
                            state.record["status"] = "exited"
                            state.record["exit_code"] = event.get("code")
                    elif kind == "error":
                        state.error = event.get("message", "ConPTY broker failed.")[:500]
                        state.record["status"] = "interrupted"
                        state.ready.set()
                    self._save(state)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            state.error = "The terminal broker stream was interrupted: " + type(exc).__name__
        finally:
            with state.lock:
                if state.record["status"] in ("starting", "running"):
                    state.record["status"] = "interrupted"
                if state.job is not None:
                    state.job.close()
                    state.job = None
                for pending in state.pending.values():
                    pending[1]["error"] = "The terminal broker exited."
                    pending[0].set()
                self._save(state)
                state.ready.set()
            try:
                state.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                state.process.kill()
                state.process.wait(timeout=5)

    def _get(self, identity, chat_id=None):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise TerminalError("Invalid terminal identity.")
        with self._lock:
            state = self._sessions.get(identity)
        if state is None or (chat_id is not None and state.record["chat_id"] != chat_id):
            raise TerminalError("This terminal does not belong to the requested chat.")
        return state

    def assert_owner(self, identity, chat_id):
        self._get(identity, chat_id)

    def list(self, chat_id: str) -> list[dict]:
        _chat(chat_id)
        with self._lock:
            ids = [key for key, state in self._sessions.items() if state.record["chat_id"] == chat_id]
        return [{k: v for k, v in self.read(key, chat_id=chat_id).items() if k != "chunks"} for key in ids]

    def read(self, identity: str, after_sequence: int = 0, *, chat_id: str | None = None) -> dict:
        if type(after_sequence) is not int or after_sequence < 0:
            raise TerminalError("The terminal cursor must be a nonnegative integer.")
        state = self._get(identity, chat_id)
        with state.lock:
            first = state.chunks[0]["sequence"] if state.chunks else state.record["sequence"] + 1
            return {**state.record, "chunks": [dict(c) for c in state.chunks if c["sequence"] > after_sequence],
                    "first_sequence": first,
                    "overflow": bool(state.record["truncated"] and after_sequence < first)}

    def _send(self, state, command):
        with state.send_lock:
            request = uuid.uuid4().hex
            response = {}
            ready = threading.Event()
            with state.lock:
                if state.record["status"] != "running" or state.process is None or state.process.poll() is not None:
                    raise TerminalError("This terminal has ended. Open a new terminal to continue.")
                state.pending[request] = (ready, response)
            try:
                state.process.stdin.write(json.dumps({**command, "request": request}, ensure_ascii=True) + "\n")
                state.process.stdin.flush()
                if not ready.wait(8):
                    self.close(state.record["id"])
                    raise TerminalError("The terminal stopped responding and was closed.")
                if response.get("error"):
                    raise TerminalError(response["error"])
            except (OSError, ValueError) as exc:
                if isinstance(exc, TerminalError):
                    raise
                raise TerminalError("The terminal broker connection ended.") from exc
            finally:
                with state.lock:
                    state.pending.pop(request, None)

    def write(self, identity: str, data: str, *, chat_id: str | None = None) -> dict:
        state = self._get(identity, chat_id)
        if not isinstance(data, str) or len(data.encode("utf-8")) > 65536 or "\0" in data:
            raise TerminalError("Terminal input must be text of at most 65536 bytes without NUL.")
        self._send(state, {"type": "write", "data": data})
        return {"id": identity, "accepted": len(data.encode("utf-8"))}

    def resize(self, identity: str, cols: int, rows: int, *, chat_id: str | None = None) -> dict:
        state = self._get(identity, chat_id)
        _dimensions(cols, rows)
        self._send(state, {"type": "resize", "cols": cols, "rows": rows})
        with state.lock:
            state.record.update(cols=cols, rows=rows)
            self._save(state)
        return {"id": identity, "cols": cols, "rows": rows}

    def _stop_process(self, state):
        with state.lock:
            if state.job is not None:
                state.job.close()
                state.job = None
        if state.process is not None:
            if state.process.poll() is None:
                state.process.kill()
            state.process.wait(timeout=5)
            if state.thread is not None and state.thread is not threading.current_thread():
                state.thread.join(timeout=5)
            for stream in (state.process.stdin, state.process.stdout):
                if stream:
                    stream.close()

    def close(self, identity: str, *, chat_id: str | None = None) -> dict:
        state = self._get(identity, chat_id)
        with state.lock:
            if state.record["status"] in ("starting", "running"):
                state.record["status"] = "closed"
            self._save(state)
        self._stop_process(state)
        return self.read(identity, chat_id=chat_id)

    def close_all(self):
        with self._lock:
            self._closed = True
            identities = list(self._sessions)
        for identity in identities:
            self.close(identity)
        atexit.unregister(self.close_all)
