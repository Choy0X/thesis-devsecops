"""What this computer has right now: the processor, memory, every disk, and the graphics card with its free video memory.

The graphics card is read from ``nvidia-smi`` in a separate process, not from torch: importing torch in the API process would cost seconds and hundreds
of megabytes for one number, and would hold CUDA. The answer is cached for a few seconds because the figures move but are not worth a process
spawn on every request. Every figure that cannot be read is absent with a reason, never guessed.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from typing import Callable

from app.backend import compute

CACHE_SECONDS = 5.0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_QUERY = "name,driver_version,memory.total,memory.free,memory.used,utilization.gpu"

Runner = Callable[[list[str]], str]


def _run(cmd: list[str]) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout).strip().splitlines()[-1][:200] if (done.stderr or done.stdout).strip() else f"exit code {done.returncode}")
    return done.stdout


def gpus(run: Runner = _run, which: Callable[[str], str | None] = shutil.which) -> dict:
    """The NVIDIA cards (``available`` False with a ``reason`` when there are none or the tool cannot be run)."""
    if not compute.gpu_enabled():
        return {"available": False, "reason": compute.OFF_REASON, "cards": []}
    exe = which("nvidia-smi")
    if not exe:
        return {"available": False, "reason": "nvidia-smi was not found, so no NVIDIA graphics card (or no driver) is available", "cards": []}
    try:
        text = run([exe, f"--query-gpu={_QUERY}", "--format=csv,noheader,nounits"])
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        return {"available": False, "reason": f"nvidia-smi could not be read: {e}", "cards": []}
    cards = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            cards.append({"name": parts[0], "driver": parts[1], "total_mb": int(parts[2]), "free_mb": int(parts[3]), "used_mb": int(parts[4]),
                          "utilization_pct": int(parts[5])})
        except ValueError:
            continue                                  # ignored: a card that reports "[N/A]" is skipped, the others are still shown
    if not cards:
        return {"available": False, "reason": "nvidia-smi answered but listed no usable card", "cards": []}
    return {"available": True, "reason": None, "cards": cards}


def disks() -> list[dict]:
    """Every mounted disk with its size and free space in GB (a disk that cannot be read, such as an empty card reader, is left out)."""
    import psutil
    out = []
    for part in psutil.disk_partitions(all=False):
        try:
            use = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue                                  # ignored: a disk that cannot be read (an empty card reader) is left out
        out.append({"drive": part.mountpoint.rstrip("\\") or part.mountpoint, "fstype": part.fstype, "total_gb": round(use.total / 1e9, 1), "free_gb": round(use.free / 1e9, 1)})
    return out


_cache: dict = {"at": 0.0, "value": None}
_lock = threading.Lock()


def snapshot(run: Runner = _run, which: Callable[[str], str | None] = shutil.which, now: Callable[[], float] = time.monotonic, use_cache: bool = True) -> dict:
    with _lock:
        if use_cache and _cache["value"] is not None and now() - _cache["at"] < CACHE_SECONDS:
            return _cache["value"]
        info = compute.machine_info()
        value = {"cpu": {"physical_cores": info["physical_cores"], "logical_cores": info["logical_cores"], "workers": info["cpu_workers"]},
                 "ram": {"total_gb": info["ram_gb"], "free_gb": info["ram_free_gb"]}, "disks": disks(), "gpu": gpus(run, which)}
        if use_cache:
            _cache.update(at=now(), value=value)
        return value


def forget() -> None:
    _cache.update(at=0.0, value=None)
