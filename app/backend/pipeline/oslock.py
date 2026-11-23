"""An operating-system file lock for work that only one process may do at a time (reading one dump into its table, downloading one dump).

The holder takes an exclusive lock on one byte of a small file (``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere, the same way ``app/single_instance.py`` guards the app). The system drops the
lock when the process ends however it ends, so a crash or a power cut never leaves a lock behind that has to be cleaned up; there is no stale-lock stealing and so no race between two stealers. A
second holder is refused with ``Busy``, which carries the pid of the first (written to a sidecar file for messages only; it is never what decides). Windows can take a moment to release the lock of a
process that was just killed, so ``acquire`` retries for ``wait`` seconds.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time


class Busy(RuntimeError):
    """The lock is held by someone else. ``pid`` is the holder's, when it could be read."""

    def __init__(self, path: pathlib.Path, pid: int | None = None):
        self.path, self.pid = pathlib.Path(path), pid
        super().__init__(f"{self.path.name} is in use" + (f" by process {pid}" if pid else ""))


def _try(fd: int) -> bool:
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


def _free(fd: int) -> None:
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


def _sidecar(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + ".json")


def _holder(path: pathlib.Path) -> int | None:
    try:
        return int(json.loads(_sidecar(path).read_text(encoding="utf-8")).get("pid"))
    except (OSError, ValueError, TypeError):
        return None


class FileLock:
    """``with FileLock(path):`` or ``acquire()`` and ``release()``."""

    def __init__(self, path: str | os.PathLike, *, note: bool = True):
        """``note=False`` skips the file that names the holder (for a lock taken very often, where nobody needs the message)."""
        self.path = pathlib.Path(path)
        self._note = note
        self._fd: int | None = None

    def acquire(self, wait: float = 3.0) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        deadline = time.monotonic() + max(0.0, wait)
        while not _try(fd):
            if time.monotonic() >= deadline:
                os.close(fd)
                raise Busy(self.path, _holder(self.path))
            time.sleep(0.05)
        self._fd = fd
        if not self._note:
            return self
        try:
            _sidecar(self.path).write_text(json.dumps({"pid": os.getpid(), "since": time.time()}), encoding="utf-8")
        except OSError:
            pass                                 # ignored: the pid only improves a message
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        if self._note:
            try:
                _sidecar(self.path).unlink()
            except OSError:
                pass                             # ignored: a message file that is already gone
        _free(self._fd)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def held_elsewhere(path: str | os.PathLike) -> bool:
    """Does somebody hold this lock right now? A missing file means nobody does. (A holder in this very process counts too: the lock is per open handle.)"""
    path = pathlib.Path(path)
    if not path.exists():
        return False
    try:
        FileLock(path).acquire(wait=0.0).release()
        return False
    except Busy:
        return True
