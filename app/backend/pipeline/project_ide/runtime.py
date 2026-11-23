"""A supervised app-private OpenCode process; no legacy agent loop."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import zipfile

from .compatibility import runtime_environment

PINNED_VERSION = "1.18.34"
_DEPENDENCY_LOCK = threading.Lock()


def _dependency_files(folder: Path) -> list[Path]:
    """Select core package files without following links out of a private root."""
    entries = [folder / "package.json", folder / "package-lock.json", folder / "node_modules"]
    entries += list((folder / "node_modules").rglob("*"))
    result = []
    for path in entries:
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Core dependencies cannot contain links or junctions.")
        if path.is_file():
            result.append(path)
    return result


def _is_pinned_core(read) -> bool:
    package = json.loads(read("package.json"))
    lock = json.loads(read("package-lock.json"))
    expected = {"@opencode-ai/plugin": PINNED_VERSION}
    if package.get("dependencies") != expected or lock.get("packages", {}).get("", {}).get("dependencies") != expected:
        return False
    if any(package.get(name) for name in ("devDependencies", "optionalDependencies", "peerDependencies")):
        return False
    for name in ("plugin", "sdk"):
        relative = "node_modules/@opencode-ai/" + name
        installed = json.loads(read(relative + "/package.json"))
        if installed.get("version") != PINNED_VERSION or lock["packages"].get(relative, {}).get("version") != PINNED_VERSION:
            return False
    return True


class RuntimeError(Exception):
    """The selected runtime cannot provide its advertised service."""


class WindowsProcessJob:
    """A private job retains descendants even if their original parent exits."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        from app import jobobject

        self.api, _ = jobobject._api()
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise RuntimeError(f"Cannot create runtime process job: {ctypes.get_last_error()}")
        if not jobobject._set_flags(self.handle, jobobject.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE):
            self.close()
            raise RuntimeError("Cannot enable runtime process cleanup.")

    def assign(self, process):
        import psutil
        # The process starts suspended so it cannot spawn outside its job.
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise RuntimeError("Cannot contain the OpenCode runtime process.")
        psutil.Process(process.pid).resume()

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def find_executable() -> str:
    found = shutil.which("opencode.exe")
    if found:
        return found
    wrapper = shutil.which("opencode.cmd") or shutil.which("opencode")
    if wrapper:
        native = Path(wrapper).parent / "node_modules/opencode-ai/bin/opencode.exe"
        if native.is_file():
            return str(native)
        if os.name != "nt":
            return wrapper
    raise RuntimeError(f"Install OpenCode {PINNED_VERSION}; no native executable was found.")


def gateway_configuration(base_url: str, api_key: str, *, context: int = 16384, model_id: str) -> dict:
    """Use the selected provider model for primary and auxiliary generation."""
    return {
        "model": "framework/" + model_id, "small_model": "framework/" + model_id, "share": "disabled", "autoupdate": False,
        "compaction": {"auto": True, "prune": True, "reserved": min(1024, context // 8),
                       "preserve_recent_tokens": min(2048, context // 4), "tail_turns": 2},
        "provider": {"framework": {
            "npm": "@ai-sdk/openai-compatible", "name": "Framework selected agent",
            "options": {"baseURL": base_url, "apiKey": api_key},
            "models": {model_id: {"name": "Selected coding agent", "tool_call": True,
                               "limit": {"context": context, "output": min(4096, context // 8)}}},
        }},
    }


class OpenCodeRuntime:
    def __init__(self, workspace: str | Path, state_dir: str | Path, *, executable: str | None = None,
                 configuration: dict | None = None):
        self.workspace = Path(workspace).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.executable = executable or find_executable()
        self.configuration = configuration or {}
        self.process: subprocess.Popen | None = None
        self.port: int | None = None
        self.password = secrets.token_urlsafe(32)
        self._log = None
        self._containment = None
        self.on_phase = None
        self.dependency_cache: Path | None = None
        self.dependency_sources = ()
        self.dependency_status = None
        self._lifecycle_lock = threading.RLock()
        self._stopped = False

    def _seed_dependencies(self, folder: Path) -> bool:
        """Copy verified core SDK bytes; each chat retains private writable state."""
        if self.dependency_cache is None or any((folder / name).exists() for name in ("node_modules", "package.json", "package-lock.json")):
            return False
        validated = False
        cache = Path(self.dependency_cache)
        try:
            if not cache.exists():
                for source in self.dependency_sources:
                    self._remember_dependencies(Path(source))
                    if cache.exists():
                        break
            for path in (cache, cache / "manifest.json", cache / "core.zip"):
                if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                    raise ValueError("The core SDK cache cannot contain links or junctions.")
            manifest = json.loads((cache / "manifest.json").read_text(encoding="utf-8"))
            package = cache / "core.zip"
            with package.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if manifest != {"version": PINNED_VERSION, "sha256": digest}:
                raise ValueError("The cached core SDK bytes changed.")
            with zipfile.ZipFile(package) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(item.file_size for item in entries) > 256 * 1024 * 1024 or not _is_pinned_core(archive.read):
                    raise ValueError("The cached core SDK does not match the pinned version.")
                for item in entries:
                    name = item.filename
                    if ("\\" in name or name.startswith("/") or any(part in ("", ".", "..") or ":" in part for part in name.split("/"))
                            or (name not in ("package.json", "package-lock.json") and not name.startswith("node_modules/"))
                            or (item.external_attr >> 16) & 0o170000 == 0o120000):
                        raise ValueError("The cached core SDK contains an unsafe path.")
                validated = True
                # The lock is written last. An interrupted extraction has no
                # complete lock, so native Npm.install reifies dependencies.
                for item in sorted(entries, key=lambda item: item.filename == "package-lock.json"):
                    archive.extract(item, folder)
            return True
        except Exception:  # ignored: Native installation retries failed cache extraction; damaged cache is quarantined below.
            if not validated:
                # Preserve damaged bytes for diagnosis, then let a future start
                # rebuild from a verified private installation. Never delete it.
                try:
                    with _DEPENDENCY_LOCK:
                        if (cache.exists() and cache.resolve().parent == cache.parent.resolve() and not cache.is_symlink()
                                and not getattr(cache.lstat(), "st_file_attributes", 0) & 0x400):
                            quarantine = cache.with_name(cache.name + ".invalid-" + secrets.token_hex(8))
                            cache.rename(quarantine)
                except OSError:
                    pass
            return False

    def _remember_dependencies(self, folder: Path):
        if self.dependency_cache is None:
            return
        try:
            cache = Path(self.dependency_cache)
            with _DEPENDENCY_LOCK:
                if cache.exists() or not _is_pinned_core(lambda name: (folder / name).read_bytes()):
                    return
                files = _dependency_files(folder)
                cache.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".core-sdk-", dir=cache.parent) as staging:
                    staged = Path(staging) / "package"
                    staged.mkdir()
                    package = staged / "core.zip"
                    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                        for path in files:
                            archive.write(path, path.relative_to(folder).as_posix())
                    with zipfile.ZipFile(package) as archive:
                        if not _is_pinned_core(archive.read):
                            return
                    with package.open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    (staged / "manifest.json").write_text(json.dumps({"version": PINNED_VERSION, "sha256": digest}), encoding="utf-8")
                    staged.rename(cache)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass  # Cache failure must retain the normal pinned native installer.

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise RuntimeError("The OpenCode server has not been started.")
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 30.0, *, initialization_timeout: float = 180.0) -> "OpenCodeRuntime":
        with self._lifecycle_lock:
            if self._stopped:
                raise RuntimeError("This coding runtime was stopped before startup completed.")
            if self.process is not None and self.process.poll() is None:
                return self
        env = runtime_environment(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "OPENCODE_CONFIG_DIR"):
            Path(env[key]).mkdir(parents=True, exist_ok=True)
        version = subprocess.run([self.executable, "--version"], capture_output=True, text=True,
                                 timeout=20, env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if version.returncode != 0 or version.stdout.strip() != PINNED_VERSION:
            raise RuntimeError(f"Expected OpenCode {PINNED_VERSION}, got {version.stdout.strip()!r}.")
        seeded_at = time.monotonic()
        seeded = self._seed_dependencies(Path(env["OPENCODE_CONFIG_DIR"]))
        self.dependency_status = {"reused_core_sdk": seeded, "elapsed_ms": round((time.monotonic() - seeded_at) * 1000)}
        self.workspace.mkdir(parents=True, exist_ok=True)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(self.configuration)
        env["OPENCODE_SERVER_PASSWORD"] = self.password
        permission_dir = self.state_dir / "permissions"
        permission_dir.mkdir(exist_ok=True)
        for pending in permission_dir.glob("appcmd_*.json"):
            pending.rename(pending.with_suffix(".interrupted"))
        for reply in permission_dir.glob("appcmd_*.reply"):
            reply.unlink()
        env["PROJECTS_IDE_PERMISSION_DIR"] = str(permission_dir)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self._log = (self.state_dir / "runtime.log").open("a", encoding="utf-8")
        try:
            with self._lifecycle_lock:
                if self._stopped:
                    raise RuntimeError("This coding runtime was stopped before startup completed.")
                if os.name == "nt":
                    self._containment = WindowsProcessJob()
                self.process = subprocess.Popen(
                    [self.executable, "serve", "--hostname", "127.0.0.1", "--port", str(self.port)],
                    cwd=self.workspace, env=env, stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT,
                    creationflags=(subprocess.CREATE_NO_WINDOW | 0x00000004) if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
                if self._containment is not None:
                    self._containment.assign(self.process)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("OpenCode exited during startup; inspect its private runtime.log.")
                try:
                    health = self.request("GET", "/global/health", timeout=1)
                except (OSError, RuntimeError):
                    health = {}
                if health.get("healthy") is True and health.get("version") == PINNED_VERSION:
                    # Cold native SDK installation is separate from server health.
                    # Warm with one read, before any uncertain session mutation.
                    if self.on_phase:
                        self.on_phase("initializing_runtime")
                    self.request("GET", "/config", timeout=initialization_timeout)
                    return self
                time.sleep(0.1)
            raise RuntimeError("OpenCode did not become healthy within the startup timeout.")
        except BaseException:
            self.stop()
            raise

    def request(self, method: str, path: str, body: dict | None = None, *, timeout: float = 30):
        if method == "POST" and (match := re.fullmatch(r"/permission/(appcmd_[0-9a-f]{32})/reply", path)):
            identity = match[1]
            pending = self.state_dir / "permissions" / (identity + ".json")
            if not pending.is_file() or (body or {}).get("reply") not in ("once", "reject"):
                raise RuntimeError("The application command permission is stale or invalid.")
            reply = pending.with_suffix(".reply")
            if reply.exists():
                raise RuntimeError("This command permission was already answered.")
            from .workspace import _write
            _write(reply, json.dumps({"reply": body["reply"]}).encode())
            return True
        if method == "POST" and (match := re.fullmatch(r"/session/([^/]+)/abort", path)):
            self.reject_commands(match[1])
        if not path.startswith("/") or path.startswith("//"):
            raise RuntimeError("A runtime request must use a local API path.")
        if not path.startswith(("/global/", "/doc")):
            if "directory" in urllib.parse.parse_qs(urllib.parse.urlsplit(path).query):
                raise RuntimeError("Runtime directory selection belongs to the chat workspace.")
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode({"directory": str(self.workspace)})
        token = base64.b64encode(f"opencode:{self.password}".encode()).decode()
        request = urllib.request.Request(
            self.base_url + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise RuntimeError("The runtime response exceeded the capture limit.")
                result = json.loads(raw) if raw else None
                if method == "GET" and path.split("?")[0] == "/permission":
                    result = (result or []) + self.command_permissions()
                return result
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"OpenCode {method} {path.split('?')[0]} returned HTTP {exc.code}.") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            raise RuntimeError(f"OpenCode {method} {path.split('?')[0]} did not answer within {timeout:g} seconds. The request was not retried.") from exc

    def capture_contract(self) -> dict:
        schema = self.request("GET", "/doc")
        if not isinstance(schema, dict) or not schema.get("openapi") or not isinstance(schema.get("paths"), dict):
            raise RuntimeError("OpenCode returned no usable OpenAPI contract.")
        (self.state_dir / "openapi.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")
        return schema

    def connect_tools(self):
        """Initialize configured MCP services before reporting model generation."""
        return self.request("GET", "/mcp", timeout=180)

    def command_permissions(self):
        result = []
        for pending in (self.state_dir / "permissions").glob("appcmd_*.json"):
            try:
                if pending.stat().st_size <= 65536 and not pending.with_suffix(".reply").exists():
                    value = json.loads(pending.read_text(encoding="utf-8"))
                    if value.get("source") == "application_command" and value.get("id") == pending.stem:
                        result.append(value)
            except (ValueError, OSError):
                continue
        return result

    def reject_commands(self, session=None):
        for pending in self.command_permissions():
            if session is None or pending.get("sessionID") == session:
                self.request("POST", "/permission/" + pending["id"] + "/reply", {"reply": "reject"})

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stopped = True
            self._stop_process()

    def _stop_process(self) -> None:
        try:
            self.reject_commands()
        except (OSError, RuntimeError):
            pass  # Process cleanup must continue even if the permission journal is unavailable.
        if self._containment is not None:
            self._containment.close()
            self._containment = None
        if self.process is not None and os.name != "nt":
            import signal
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.process is not None and self.process.poll() is None:
            import psutil
            try:
                children = psutil.Process(self.process.pid).children(recursive=True)
            except psutil.Error:
                children = []
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            _, alive = psutil.wait_procs(children, timeout=2)
            for child in alive:
                try:
                    child.kill()
                except psutil.Error:
                    pass
        if self._log is not None:
            self._log.close()
            self._log = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.stop()
