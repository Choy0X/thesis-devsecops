"""Names several routers share: the experiment and processed-data folders, and the mining helpers."""
from __future__ import annotations

import os
import pathlib
import threading
import time

from app.backend import jobs


EXP = pathlib.Path("experiments")
PROC = pathlib.Path("data/processed")
REPOS_DIR = pathlib.Path("data/raw/repos")


def dir_size(d: pathlib.Path) -> int:
    total = 0
    for root, _, files in os.walk(d):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


_SIZES: dict[str, tuple[float, int]] = {}          # folder -> (when measured, bytes)
_WALKING: dict[str, threading.Thread] = {}
_SIZE_LOCK = threading.Lock()
SIZE_MAX_AGE = 300.0


def _measure(d: pathlib.Path, key: str) -> None:
    try:
        size = dir_size(d)
        with _SIZE_LOCK:
            _SIZES[key] = (time.monotonic(), size)
    finally:
        with _SIZE_LOCK:
            _WALKING.pop(key, None)


def cached_dir_size(d: pathlib.Path, *, wait: float = 0.25, max_age: float = SIZE_MAX_AGE) -> int | None:
    """The size of a large folder without walking it on every request. The walk (thousands of files, seconds on a cold disk) runs on a
    background thread; the last answer is returned at once and refreshed once it is older than ``max_age``. The first call waits up to
    ``wait`` seconds for the walk and returns None when it has not finished (a small folder finishes in time)."""
    key = str(d.resolve())
    with _SIZE_LOCK:
        hit = _SIZES.get(key)
        fresh = hit is not None and time.monotonic() - hit[0] < max_age
        thread = _WALKING.get(key)
        if not fresh and thread is None:
            thread = _WALKING[key] = threading.Thread(target=_measure, args=(d, key), daemon=True, name="dir-size")
            thread.start()
    if hit is None and thread is not None:
        thread.join(wait)
        with _SIZE_LOCK:
            hit = _SIZES.get(key)
    return hit[1] if hit else None


def forget_dir_size(d: pathlib.Path) -> None:
    """Drop the remembered size (the folder was just changed on purpose)."""
    with _SIZE_LOCK:
        _SIZES.pop(str(d.resolve()), None)


def mining_job():
    """The running mining job of this session, if any."""
    return next(iter(jobs.running("mine_code")), None)
