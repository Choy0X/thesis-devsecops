"""The local GPU reservation shared by application and job processes."""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.backend import paths
from app.backend.pipeline import fsio, oslock
from app.backend.pipeline.mining_run import pid_exists


class AlreadyMarked(RuntimeError):
    def __init__(self, owner: str):
        super().__init__(owner)
        self.owner = owner


class ReservationLost(RuntimeError):
    """The owner released the reservation or exited while the GPU was being cleared."""


def _path() -> Path:
    return paths.interim_dir() / "ollama_exclusive.json"


def _lock(path: Path) -> oslock.FileLock:
    return oslock.FileLock(path.with_suffix(".lock"), note=False)


def _read_live(path: Path) -> dict | None:
    """Read or clean up a marker while holding its lock."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:
        record = None
    if isinstance(record, dict) and record.get("owner") and type(record.get("pid")) is int and pid_exists(record["pid"]):
        return record
    path.unlink(missing_ok=True)
    return None


def current(*, wait: float = 3.0) -> dict | None:
    path = _path()
    if not path.exists():
        return None
    lock = _lock(path).acquire(wait=wait)
    try:
        record = _read_live(path)
        return {"owner": record["owner"], "pid": record["pid"]} if record else None
    finally:
        lock.release()


def _mark(path: Path, owner: str, pid: int | None) -> str:
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("A GPU reservation needs an owner.")
    pid = os.getpid() if pid is None else pid
    if type(pid) is not int or pid <= 0:
        raise ValueError("A GPU reservation needs a positive process ID.")
    with _lock(path):
        record = _read_live(path)
        if record:
            raise AlreadyMarked(record["owner"])
        if not pid_exists(pid):
            raise ValueError(f"GPU reservation process {pid} is no longer running.")
        claim = uuid.uuid4().hex
        fsio.write_json(path, {"owner": owner.strip(), "pid": pid, "claim": claim})
        return claim


def mark(owner: str, pid: int | None = None) -> None:
    _mark(_path(), owner, pid)


def clear(owner: str | None = None) -> bool:
    path = _path()
    with _lock(path):
        record = _read_live(path)
        if not record or (owner is not None and record["owner"] != owner):
            return False
        path.unlink()
        return True


@contextmanager
def reservation(owner: str, pid: int | None = None):
    """Roll back only this claim if taking the GPU fails."""
    path = _path()
    claim = _mark(path, owner, pid)
    try:
        yield
    except BaseException:
        with _lock(path):
            record = _read_live(path)
            if record and record.get("claim") == claim:
                path.unlink()
        raise
    else:
        with _lock(path):
            record = _read_live(path)
            if not record or record.get("claim") != claim:
                raise ReservationLost("The GPU reservation ended before Ollama finished unloading its models.")
