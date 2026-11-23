"""Start the backend as a separate process, wait until it answers, report a failed start with its log, and stop it cleanly.

``Backend.start()`` spawns ``python -m app.backend`` with no console window, sends its output to a log file, and waits for ``GET /api/health`` to answer;
if the process ends first, or nothing answers in time, it raises ``BackendStartError`` carrying a sentence for the person and the last lines of the log.
``Backend.stop()`` asks the process to shut down in order (``POST /api/shutdown``, which stops the jobs and closes the databases), waits, and ends it if it
does not go.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class BackendStartError(RuntimeError):
    def __init__(self, message: str, log_path: pathlib.Path | None = None, tail: str = ""):
        super().__init__(message)
        self.log_path, self.tail = log_path, tail


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tail(path: pathlib.Path, lines: int = 20) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


class Backend:
    def __init__(self, log_path: pathlib.Path, env: dict[str, str] | None = None, cwd: pathlib.Path = ROOT, python: str = sys.executable):
        self.log_path, self.env, self.cwd, self.python = pathlib.Path(log_path), env or {}, pathlib.Path(cwd), python
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None
        self._log = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout: float = 40.0, extra_args: list[str] | None = None) -> str:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        self._log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])), **self.env}
        self.proc = subprocess.Popen([self.python, "-u", "-m", "app.backend", "--port", str(port), *(extra_args or [])], cwd=self.cwd, env=env,
                                     stdout=self._log, stderr=subprocess.STDOUT, creationflags=_NO_WINDOW if os.name == "nt" else 0)
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self._close_log()
                raise BackendStartError(f"The app's engine stopped while starting (exit code {self.proc.returncode}).", self.log_path, _tail(self.log_path))
            try:
                with urllib.request.urlopen(f"{url}/api/health", timeout=1) as r:
                    if r.status == 200:
                        self.url = url
                        return url
            except OSError:
                time.sleep(0.2)
        self.stop(grace=0)
        raise BackendStartError(f"The app's engine did not answer within {timeout:g} seconds.", self.log_path, _tail(self.log_path))

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None

    def stop(self, grace: float = 12.0, token: str | None = None) -> int | None:
        """Ask for an orderly shutdown, wait up to ``grace`` seconds, then end the process. Returns its exit code."""
        if self.proc is None:
            return None
        headers = {"Cookie": f"aivf_session={token}"} if token else {}
        if self.alive() and self.url and grace > 0:
            try:
                urllib.request.urlopen(urllib.request.Request(f"{self.url}/api/shutdown", method="POST", data=b"{}", headers=headers), timeout=3).read()
            except OSError:
                pass                                       # ignored: if the request fails the process is ended below
            end = time.monotonic() + grace
            while self.alive() and time.monotonic() < end:
                st = self._status(headers)
                if st and st.get("done"):
                    break
                time.sleep(0.2)
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self._close_log()
        return self.proc.returncode

    def _status(self, headers: dict) -> dict | None:
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{self.url}/api/shutdown/status", headers=headers), timeout=2) as r:
                return json.loads(r.read().decode("utf-8"))
        except (OSError, ValueError):
            return None
