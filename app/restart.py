"""Restarting the app (Settings, App and data, Restart the app), and leaving it.

The process the person starts (``python -m app.desktop``, in their terminal) is only a watcher. It starts the app as a child that shares its terminal (its output, its keyboard, Ctrl+C)
and waits for it. A restart is the child ending with the exit code EXIT_RESTART after the orderly shutdown (the page runs the shutdown first and only then asks
``POST /api/shell/reboot``); the watcher then starts the next child at once, in the same terminal. Because the next copy starts when the old one has ended, there is nothing to wait for:
no wait for a process id, no retry of the single-instance lock, no second window, and the terminal the app was started from stays the terminal it runs in.

* **The command** preserves ``sys.orig_argv``. A source launch from system Python uses the checkout's virtual environment when present.
* **The environment** of a new copy is the one the watcher itself was started with, not the one the app laid the saved settings over. That matters: the fields that are read only at start
  (the data and database folders, the window frame) are skipped when the environment already holds a value, so a copy started with the overlaid environment would never see a
  change saved since.
* **A copy without a window** (``ZTSA_HEADLESS``) keeps its port: the app leaves it in a small file (``ZTSA_HANDOFF``) for the watcher, so a browser tab left open finds it again. The file is in a
  folder of its own that the watcher makes for its life (``tempfile.mkdtemp``: a name nobody can guess, private to the user) and removes at its end, not a file with a predictable name in the
  shared temporary folder, and only digits are taken from it.
* **Ending** is a hard end of the process after the single-instance lock is released: on Windows ``TerminateProcess`` on itself, which skips the code the system runs for the libraries of a
  process that exits (Qt prints "QDxgiVSyncService not destroyed in time" there, because its drawing thread is already gone, and waits for it); ``os._exit`` elsewhere or if that fails. The
  shutdown has run by then, so nothing is lost, and the exit code reaches the watcher unchanged. The standard error is also pointed at the null device first, for what else a library says on the way out.
* **Ctrl+C** reaches the app too, as it shares the terminal: the watcher waits for it to finish its own shutdown and ends with its exit code; it never restarts the app for that.
* ``ZTSA_NO_WATCHER=1`` runs the app in the process that was started (tests, debugging); such a copy cannot restart itself and says so.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXIT_RESTART = 75                            # EX_TEMPFAIL: "try again", which is what the watcher does
ENV_CHILD = "ZTSA_CHILD"                     # set by the watcher: this process is the app itself
ENV_HANDOFF = "ZTSA_HANDOFF"                 # a file the app writes for the watcher (the port of a copy without a window)
ENV_PORT = "ZTSA_PORT"
ENV_NO_WATCHER = "ZTSA_NO_WATCHER"
EXIT_DELAY = 0.2                             # the answer to the page goes out before this process ends
INTERRUPT_GRACE = 25.0                       # after Ctrl+C the app gets this long to finish its shutdown

_instance = None
_serving: dict = {"url": None, "windowed": True}


# -- the watcher (the process that was started) -----------------------------------------------------------------------------------------------------------

def command() -> list[str]:
    """Keep the launch flags and use the installed source environment when appropriate."""
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable, "-m", "app.desktop"])
    interpreter = sys.executable
    if sys.prefix == sys.base_prefix and not (ROOT / "runtime" / "portable-runtime.txt").is_file():
        venv = ROOT / ".venv"
        if os.name == "nt":
            program = "pythonw.exe" if pathlib.Path(sys.executable).stem.lower() == "pythonw" else "python.exe"
            candidate = venv / "Scripts" / program
        else:
            candidate = venv / "bin" / "python"
        if (venv / "pyvenv.cfg").is_file() and candidate.is_file():
            interpreter = str(candidate)
    return [interpreter, *argv[1:]]


def supervised() -> bool:
    """This process is the app, started by the watcher (so it can ask the watcher for a restart)."""
    return os.environ.get(ENV_CHILD) == "1"


def watching() -> bool:
    """This process should be the watcher: it was started by a person (or a shortcut), not by a watcher, and nobody asked for the app to run in it."""
    return not supervised() and os.environ.get(ENV_NO_WATCHER) != "1"


def _flags() -> int:
    """Windows: the child shares this console. Without a console of this process (pythonw, a shortcut) a console program would open one of its own, so it is told not to."""
    if os.name != "nt":
        return 0
    if sys.stdout is None or os.path.basename(sys.executable).lower().startswith("pythonw"):
        return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return 0


def _take_port(path: pathlib.Path) -> str:
    """What the app left for the next copy (a port), read once and removed."""
    try:
        port = str(json.loads(path.read_text(encoding="utf-8")).get("port") or "")
    except (OSError, ValueError, AttributeError):
        port = ""
    try:
        path.unlink()
    except OSError:
        pass
    return port if port.isdigit() else ""


def _after_interrupt(child, grace: float = INTERRUPT_GRACE) -> int:
    """Ctrl+C: the app got it as well. It finishes its shutdown and ends; the watcher ends with its code."""
    deadline = time.monotonic() + grace
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            child.terminate()                                                     # its job takes everything it started with it
            try:
                return child.wait(timeout=5) or 130
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                return 130
        try:
            return child.wait(timeout=left)
        except subprocess.TimeoutExpired:
            continue
        except KeyboardInterrupt:
            continue                                                              # a second Ctrl+C changes nothing: the app is already closing


def supervise(spawn: Callable = subprocess.Popen, say: Callable[[str], None] = print) -> int | None:
    """Be the watcher when this process should be: start the app, wait for it and start it again each time it ends with EXIT_RESTART. Returns the code this process ends with, or None
    when this process is the app itself (the caller then runs the app)."""
    if not watching():
        return None
    base = {k: v for k, v in os.environ.items() if k != ENV_HANDOFF}
    folder = pathlib.Path(tempfile.mkdtemp(prefix="ztsa-handoff-"))
    handoff = folder / "handoff.json"
    port = ""
    try:
        while True:
            env = {**base, ENV_CHILD: "1", ENV_HANDOFF: str(handoff)}
            if port and not base.get(ENV_PORT):
                env[ENV_PORT] = port
            kw: dict = {"cwd": str(ROOT), "env": env}
            if os.name == "nt":
                kw["creationflags"] = _flags()
            try:
                child = spawn(command(), **kw)
            except OSError as e:
                say(f"[desktop] the app could not be started: {type(e).__name__}: {e}")
                return 1
            try:
                code = child.wait()
            except KeyboardInterrupt:
                return _after_interrupt(child)
            if code != EXIT_RESTART:
                return code
            port = _take_port(handoff)
            say("[desktop] restarting the app ...")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# -- the app, before it ends --------------------------------------------------------------------------------------------------------------------------

def remember_instance(instance) -> None:
    """The single-instance lock this copy holds, to release it just before it ends."""
    global _instance
    _instance = instance


def note_serving(url: str, windowed: bool) -> None:
    """Where this copy serves, and whether it has a window of its own (without one the next copy is asked for the same port)."""
    _serving.update(url=url, windowed=windowed)


def _hush() -> None:
    """Point the standard error at the null device (the operating system's, below Python): Qt's warning while the process ends goes nowhere, and a terminal that is paused cannot hold the end."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:
            pass                                                   # ignored: a closed or broken stream has nothing more to give
    try:
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 2)
    except OSError:
        pass                                                       # ignored: the warning is then printed, which is all that happens


def _terminate(code: int) -> None:
    """Windows: end this process at once, without the shutdown code of its libraries (Qt's drawing thread is already gone when that runs, and Qt waits for it and prints a warning). Returns only if
    that did not work. The handle is the pseudo handle of this process, -1, given as a pointer: as a plain int it would be cut to 32 bits and mean nothing."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.TerminateProcess(ctypes.c_void_p(-1), code)
    except Exception:
        pass                                                       # ignored: os._exit follows


def exit_now(code: int = 0) -> None:
    """End this process: release the single-instance lock, then leave without cleanup (the shutdown steps have run; waiting for threads, or for Qt to take its windows down, only holds the next copy up)."""
    try:
        if _instance is not None:
            _instance.release()
    except Exception:
        pass                                                       # ignored: the operating system drops the lock when the process ends
    _hush()
    _terminate(code)
    os._exit(code)


def leave(code: int = 0) -> None:
    """The app's normal end, after its shutdown: the same way out as a restart's, so the terminal gets back what it was given."""
    exit_now(code)


def exit_soon(delay: float = EXIT_DELAY, exit_fn: Callable[[], None] | None = None, code: int = 0) -> None:
    t = threading.Timer(delay, exit_fn or (lambda: exit_now(code)))
    t.daemon = True
    t.start()


def _leave_port() -> None:
    """A copy without a window keeps its port: leave it for the watcher."""
    path = os.environ.get(ENV_HANDOFF)
    url = _serving.get("url") or ""
    port = url.rsplit(":", 1)[-1].strip("/")
    if not path or _serving.get("windowed") or not port.isdigit():
        return
    try:
        pathlib.Path(path).write_text(json.dumps({"port": int(port)}), encoding="utf-8")
    except OSError:
        pass                                                       # ignored: the next copy then takes any port


def reboot() -> dict:
    """What ``POST /api/shell/reboot`` does once the shutdown has finished: this copy ends with the code the watcher starts the next copy on, a moment later (the answer goes out first)."""
    if not supervised():
        return {"ok": False, "detail": "This copy of the app was not started by the launcher that starts it again, so it was not restarted. Close it and start it again by hand."}
    _leave_port()
    exit_soon(code=EXIT_RESTART)
    return {"ok": True}
