"""Making sure an Ollama server answers: start ``ollama serve`` when nothing does, and stop only a server this module started (a server that was already running, or that the user
started, is never touched).
"""
from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import subprocess
import time
import threading
import urllib.parse
from pathlib import Path

from ..acceleration import processes
from .client import KEEP_ALIVE, OllamaError, base_url, version

_NOT_INSTALLED = "Ollama is not installed. Install it from ollama.com and start the app again."
_MANAGED_CHILD = "ZTSA_OLLAMA_MANAGED_CHILD"

# The process this module started; None when it started nothing or after shutdown().
_proc = None
_lock = threading.Lock()
_generation = 0
_startup_error: str | None = None


def executable() -> str | None:
    """The ``ollama`` program: the one on PATH, else the per-user Windows install when it exists."""
    found = shutil.which("ollama")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "Programs" / "Ollama" / "ollama.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def ensure_serving(*, timeout: float = 30.0, popen=processes.spawn, sleep=time.sleep, _expected_generation=None) -> None:
    """Return when an Ollama server answers; start ``ollama serve`` first when none does, and raise ``OllamaError("unreachable", ...)`` when it cannot be reached in ``timeout`` seconds."""
    global _proc
    deadline = time.monotonic() + timeout
    if timeout <= 0:
        raise OllamaError("timeout", "Ollama startup timed out.")
    with _lock:
        generation = _generation if _expected_generation is None else _expected_generation
    if version(timeout=min(2.0, timeout)) is not None:
        return
    if os.environ.get(_MANAGED_CHILD) == "1":
        # A job may need Ollama while the backend startup is still in progress.
        # Only the backend spawns it, so it can also stop the process on exit.
        if executable() is None:
            raise OllamaError("unreachable", _NOT_INSTALLED)
        while (remaining := deadline - time.monotonic()) > 0:
            sleep(min(0.5, remaining))
            remaining = deadline - time.monotonic()
            if remaining > 0 and version(timeout=min(1.0, remaining)) is not None:
                return
        raise OllamaError("unreachable", "The app's Ollama server did not answer. Check the Serving page and try again.")
    if time.monotonic() >= deadline:
        raise OllamaError("timeout", "Ollama startup timed out.")
    endpoint = urllib.parse.urlsplit(base_url())
    address = endpoint.netloc
    try:
        loopback = endpoint.port != 0 and (endpoint.hostname == "localhost" or ipaddress.ip_address(endpoint.hostname).is_loopback)
    except ValueError:
        loopback = False
    if not loopback:
        raise OllamaError("unreachable", "The configured Ollama server did not answer. The app can only start Ollama on a loopback address.")
    program = executable()
    if program is None:
        raise OllamaError("unreachable", _NOT_INSTALLED)
    env = _server_env(address)
    kwargs = {"env": env, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    flag = getattr(subprocess, "CREATE_NO_WINDOW", None)
    if flag is not None:
        kwargs["creationflags"] = flag
    with _lock:
        if generation != _generation:
            raise OllamaError("unreachable", "Ollama startup was cancelled.")
        if _proc is None or _proc.poll() is not None:
            try:
                if _proc is not None:
                    _stop(_proc)
                    _proc = None
                _proc = popen([program, "serve"], **kwargs)
            except OSError as e:
                raise OllamaError("unreachable", f"Ollama could not be started from {program}: {e}") from None
        process = _proc
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OllamaError("unreachable", f"Ollama started from {program} did not answer on {address} within {timeout:g} seconds.")
        if version(timeout=min(1.0, remaining)) is not None:
            return
        if process.poll() is not None:
            with _lock:
                if _proc is process:
                    _stop(process)
                    _proc = None
            raise OllamaError("unreachable", f"Ollama exited right after it was started from {program}.")
        sleep(max(0.0, min(0.5, deadline - time.monotonic())))


def _server_env(address: str) -> dict:
    """The environment of an app-started ``ollama serve``: without the app's own secrets, with the runner keep-alive
    the app relies on (unless the user set one)."""
    from app.backend.config import ENV_MAP, SECRET_FIELDS
    secrets = {ENV_MAP[field] for field in SECRET_FIELDS if field in ENV_MAP} | {"GITHUB_TOKEN", "GH_TOKEN", "HF_TOKEN"}
    env = {key: value for key, value in os.environ.items() if key.upper() not in secrets}
    env["OLLAMA_HOST"] = address
    env.setdefault("OLLAMA_KEEP_ALIVE", KEEP_ALIVE)
    return env


def started_by_app() -> bool:
    """True while the process this module started is still alive."""
    with _lock:
        return _proc is not None and _proc.poll() is None


def startup_error() -> str | None:
    return _startup_error


def start_in_background() -> None:
    """Start the application-owned startup attempt without blocking the backend."""
    global _startup_error
    with _lock:
        generation = _generation
        _startup_error = None

    def start():
        global _startup_error
        try:
            ensure_serving(_expected_generation=generation)
        except OllamaError as error:
            with _lock:
                if generation == _generation:
                    _startup_error = str(error)
            logging.getLogger(__name__).info("Ollama startup: %s", error)

    threading.Thread(target=start, name="ollama-startup", daemon=True).start()


def _stop(process) -> None:
    if getattr(process, "_owned_job", None) is not None:
        # Windows does not propagate TerminateProcess to Ollama's model runners.
        processes.terminate(process, timeout=5)
        return
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def shutdown() -> None:
    """Stop the Ollama process tree this module started."""
    global _proc, _generation
    with _lock:
        _generation += 1
        process, _proc = _proc, None
    if process is not None:
        _stop(process)
