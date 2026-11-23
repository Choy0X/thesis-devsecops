"""The mining run record: a checkpoint to resume after a crash, and a guard
against two miners working on the same database.

The real checkpoint is the queue itself: each fix commit is marked only after its
pairs are stored, so a commit cut off mid-way stays 'pending' and is mined again.
This record adds what the queue cannot tell: that a run was interrupted (state
'running' but no heartbeat for HEARTBEAT_STALE seconds - crash, app closed, PC
off), with which settings, and how far it got.
"""
from __future__ import annotations
import json
import os
import time

from app.backend.pipeline import campaign_ctx
from app.backend.pipeline import store
from app.backend.pipeline import util

KEY = "mining_run"                 # the record of the manual miner; a campaign's miner has "mining_run:<campaign id>"
HEARTBEAT_STALE = 60          # seconds without a heartbeat = the run is gone
_last_beat = [0.0]


def key() -> str:
    """The key of this process's run record: a campaign's job has its own, so that campaigns on different ecosystems can mine at the same time."""
    cid = campaign_ctx.current()
    return f"{KEY}:{cid}" if cid else KEY


def load(run_key: str | None = None) -> dict | None:
    raw = store.get_meta(run_key or key())
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _save(rec: dict, run_key: str | None = None) -> None:
    store.set_meta(run_key or key(), json.dumps(rec))


def runs() -> dict[str, dict]:
    """Every run record, by key: the manual miner's and the campaigns'."""
    out = {}
    for k in store.meta_keys(KEY):
        if k == KEY or k.startswith(KEY + ":"):
            rec = load(k)
            if rec:
                out[k] = rec
    return out


def any_alive() -> bool:
    """Is some miner (manual or of a campaign) running now?"""
    return any(alive(rec) for rec in runs().values())


def pid_exists(pid) -> bool:
    """Whether a process with this id exists, without signalling it (on Windows os.kill would terminate it)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        handle = k32.OpenProcess(0x1000, False, pid)                  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259     # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def alive(rec: dict | None) -> bool:
    """A run is alive when it says it is running, its heartbeat is fresh and its process still exists: a miner that
    died a moment ago does not block the next one for the length of the heartbeat window."""
    return bool(rec and rec.get("state") == "running"
                and time.time() - float(rec.get("heartbeat_ts") or 0) < HEARTBEAT_STALE
                and pid_exists(rec.get("pid")))


def interrupted(rec: dict | None) -> bool:
    return bool(rec and rec.get("state") == "running" and not alive(rec))


def start(params: dict, total: int) -> dict:
    """Register this process's run. It is refused when another miner works on the same queue: the same kind of miner again, or (the manual miner works on the
    whole queue) a manual miner beside a campaign's and the other way round. Campaigns on their own ecosystems do not conflict with each other."""
    mine = key()
    prev = load(mine)
    if alive(prev) and prev.get("pid") != os.getpid():
        raise RuntimeError(
            f"Another mining run is active (process {prev.get('pid')}, started {prev.get('started')}). "
            "Stop it first; two miners must not work on the same queue.")
    for other_key, other in runs().items():
        if other_key != mine and (mine == KEY or other_key == KEY) and alive(other) and other.get("pid") != os.getpid():
            raise RuntimeError(
                f"Another mining run is active ({'a campaign' if other_key != KEY else 'the manual miner'}, process {other.get('pid')}, started {other.get('started')}). "
                "A manual run and a campaign must not mine the same queue at once.")
    rec = {"state": "running", "params": params, "started": util.now_z(), "pid": os.getpid(),
           "processed": 0, "total": total, "batch": 0, "heartbeat": util.now_z(),
           "heartbeat_ts": time.time(),
           "resumed_from": prev.get("started") if interrupted(prev) else None}
    _save(rec)
    _last_beat[0] = time.time()
    return rec


def beat(force: bool = False, **progress) -> None:
    """Heartbeat (at most every 10 s unless forced) with the latest progress."""
    if not force and time.time() - _last_beat[0] < 10:
        return
    rec = load() or {}
    if rec.get("pid") != os.getpid():
        return                                   # not our run any more
    rec.update(progress, heartbeat=util.now_z(), heartbeat_ts=time.time())
    _save(rec)
    _last_beat[0] = time.time()


def finish(state: str, *, run_key: str | None = None, **progress) -> None:
    """Close a run record (this process's, or the one of ``run_key``: the API closes a campaign's record from outside)."""
    rec = load(run_key)
    if not rec:
        return
    rec.update(progress, state=state, finished=util.now_z())
    _save(rec, run_key)


def discard() -> None:
    rec = load()
    if rec and rec.get("state") == "running" and not alive(rec):
        finish("discarded")
