"""Only one copy of the app runs at a time: a second launch brings the first window to the front and exits quietly.

The first launch holds an exclusive lock on a small file (``data/interim/app-instance.lock``; the operating system drops the lock when the process
ends, however it ends, so a crash never leaves the app locked out) and writes into it where it is listening. A second launch fails to take the lock,
reads that address and asks the first copy to show its window (``POST /api/shell/focus``, with the same session token the window uses; the token is a
file both copies can read), then returns. The lock has to be taken before anything else starts, so two copies never fight over the database or the port.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.request

LOCK_NAME = "app-instance.lock"


def _lock(fd: int) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass                                     # ignored: closing the descriptor releases the lock anyway


class Instance:
    """The running copy: it owns the lock and says where it listens."""

    def __init__(self, path: pathlib.Path, fd: int):
        self.path, self._fd = path, fd

    def publish(self, url: str) -> None:
        """Record the address this copy is serving (the lock byte stays at the start of the file; the address follows it)."""
        body = json.dumps({"pid": os.getpid(), "url": url}).encode("utf-8")
        info = self.path.with_name(self.path.name + ".json")
        info.write_bytes(body)

    def release(self) -> None:
        _unlock(self._fd)
        os.close(self._fd)
        try:
            self.path.with_name(self.path.name + ".json").unlink()
        except OSError:
            pass                                 # ignored: a stale address file is overwritten by the next copy


def acquire(folder: pathlib.Path) -> Instance | None:
    """Take the lock, or None when another copy holds it."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / LOCK_NAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\0")                      # one byte to lock on Windows
    if not _lock(fd):
        os.close(fd)
        return None
    return Instance(path, fd)


def owner(folder: pathlib.Path) -> dict | None:
    """Where the running copy listens (``{"pid", "url"}``), or None when it has not said yet."""
    try:
        return json.loads((folder / (LOCK_NAME + ".json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ask_to_show(folder: pathlib.Path, token: str, timeout: float = 3.0) -> bool:
    """Ask the running copy to bring its window to the front. False when it cannot be reached (it is still starting, or hung)."""
    info = owner(folder)
    if not info or not info.get("url"):
        return False
    request = urllib.request.Request(info["url"].rstrip("/") + "/api/shell/focus", method="POST", data=b"{}",
                                     headers={"Cookie": f"aivf_session={token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200 and bool(json.loads(response.read().decode("utf-8") or "{}").get("ok"))
    except (OSError, ValueError):
        return False
