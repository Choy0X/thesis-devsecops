"""Starting and stopping the app cleanly, and noticing when it was not stopped cleanly.

Start: a small run marker (``data/interim/run-marker.json``) says "this app is running" and is removed at a clean shutdown. If the marker is still there at
the next start and its process is gone, the last exit was unclean (a crash, a power cut, the task manager); the next start says so, the job records of that
session already show their running jobs as interrupted (``jobs.load_persisted``).

Stop: an ordered list of steps with a time limit each and an overall deadline: stop the jobs (and record them as cancelled because the app closed), stop
anything else registered (servers, the GPU), close the database connections, and remove the marker. A step that fails or runs out of time is reported and the
next step still runs, because a stuck step must never keep the window open forever. ``POST /api/shutdown`` runs it and ``GET /api/shutdown/status`` reports.
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import threading
import time
from typing import Callable

from app.backend import paths

DEADLINE_SECONDS = 40.0
STEP_SECONDS = 10.0
CAMPAIGN_STEP_SECONDS = 15.0              # the campaigns running get this long to pause themselves and save where they were
CAMPAIGN_STEP = "Pausing campaigns"


def _marker() -> pathlib.Path:
    return paths.INTERIM / "run-marker.json"


def _stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# -- start --------------------------------------------------------------------------------------------------------------------------

_previous: dict = {"clean": True, "note": "", "at": None}


def mark_started(pid_alive: Callable[[int], bool] | None = None) -> dict:
    """Write the run marker and return how the previous run ended: ``{"clean": bool, "note": str, "at": iso | None}``."""
    from app.backend.pipeline import mining_run
    from app.backend import sandbox_runtime
    sandbox_runtime.set_stopping(False)
    alive = pid_alive or mining_run.pid_exists
    path = _marker()
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = {"clean": True, "note": "", "at": None}
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = None
    if old and old.get("pid") != os.getpid() and not alive(int(old.get("pid") or 0)):
        previous = {"clean": False, "at": old.get("started_at"),
                    "note": "The app did not close properly last time (it stopped unexpectedly). Jobs that were running were marked as interrupted; nothing else needs doing."}
    from app.backend.pipeline import fsio
    fsio.write_json(path, {"pid": os.getpid(), "started_at": _stamp()})
    _previous.update(previous)
    return previous


def previous_exit() -> dict:
    return dict(_previous)


# -- stop ---------------------------------------------------------------------------------------------------------------------------

Step = tuple[str, Callable[[], str | None]]
_extra: list[Step] = []
_state: dict = {"running": False, "done": False, "started": None, "steps": [], "deadline": DEADLINE_SECONDS}
_lock = threading.Lock()
_admission_generation = 0


def admission_token() -> int | None:
    """Identify the active run; work admitted before shutdown cannot resume later."""
    with _lock:
        return None if _state["running"] or _state["done"] else _admission_generation


def resume_after_failed_restart() -> bool:
    """Explicitly resume this copy only after a completed, unsuccessful reboot."""
    global _admission_generation
    with _lock:
        if _state["running"] or not _state["done"]:
            return False
        _admission_generation += 1
        _state.update(running=False, done=False, started=None, steps=[], deadline=DEADLINE_SECONDS)
        mark_started()
        from app.backend import sandbox_runtime
        sandbox_runtime.update(lambda state: state.update(restart_handoff_until=0))
    return True


def register_step(name: str, fn: Callable[[], str | None]) -> None:
    """Add something to stop at shutdown (a model server, a GPU holder). It runs after the jobs and before the databases close."""
    _extra.append((name, fn))


def _pause_campaigns() -> str:
    """The campaigns are asked to pause first and given time to save where they were; the ones that do not stop in time are stopped with the other jobs."""
    from app.backend import jobs
    from app.backend.pipeline.campaigns import control
    running = jobs.running("campaign_run")
    if not running:
        return "no campaign was running"
    for job in running:
        control.pause_for_job(job.params)
    end = time.monotonic() + jobs.CAMPAIGN_PAUSE_WAIT
    while time.monotonic() < end and any(j.status == "running" for j in running):
        time.sleep(0.1)
    left = [j for j in running if j.status == "running"]
    return f"{len(running) - len(left)} of {len(running)} campaign{'s' if len(running) != 1 else ''} paused" + (f"; {len(left)} did not stop in time" if left else "")


def _stop_jobs() -> str:
    from app.backend import jobs
    from app.backend.pipeline import context_automation
    try:
        context_automation.stop()               # prevent queued explanations from applying after database shutdown
    finally:
        n = len(jobs.stop_all("Cancelled because the app was closed.", campaign_wait=0.5))       # the campaigns had their time in the step before
    return f"{n} job{'s' if n != 1 else ''} stopped" if n else "no job was running"


def _stop_ollama() -> str:
    from app.backend.pipeline.ollama import server
    owned = server.started_by_app()
    server.shutdown()
    return "the app's Ollama process was stopped" if owned else "the app had no Ollama process to stop"


def _close_databases() -> str:
    from app.backend.pipeline.db import mongo
    mongo.close()
    return "database connections closed"


def _remove_marker() -> str:
    try:
        _marker().unlink()
    except FileNotFoundError:
        pass                                      # ignored: nothing to remove means the marker was never written (a run without the lifecycle)
    return "marked as a clean exit"


def steps() -> list[Step]:
    from app.backend import sandbox_runtime
    from app.backend.pipeline import agent_background
    return [(CAMPAIGN_STEP, _pause_campaigns), ('Stopping chat tasks', agent_background.stop_all), ("Stopping jobs", _stop_jobs),
            ('Stopping sandboxes', sandbox_runtime.stop_for_shutdown),
            ("Stopping Ollama", _stop_ollama), *_extra, ("Closing the databases", _close_databases), ("Finishing", _remove_marker)]


def _run_step(name: str, fn: Callable[[], str | None], limit: float) -> dict:
    out: dict = {}

    def target():
        try:
            out["detail"] = fn() or ""
            out["ok"] = True
        except Exception as e:
            out.update(ok=False, detail=f"{type(e).__name__}: {e}")

    started = time.monotonic()
    worker = threading.Thread(target=target, daemon=True, name=f"shutdown-{name}")
    worker.start()
    worker.join(limit)
    if worker.is_alive():
        return {"name": name, "ok": False, "detail": f"did not finish within {limit:g} s; moved on", "seconds": round(time.monotonic() - started, 2)}
    return {"name": name, "ok": out.get("ok", False), "detail": out.get("detail", ""), "seconds": round(time.monotonic() - started, 2)}


def _claim(deadline: float) -> bool:
    """Take the shutdown for this caller: False when one is already running. The state says "running, not done" at once, so a page that asks for the status right after
    starting it never reads the end of an earlier one."""
    global _admission_generation
    with _lock:
        if _state["running"]:
            return False
        from app.backend import sandbox_runtime
        sandbox_runtime.set_stopping(True)
        _admission_generation += 1
        _state.update(running=True, done=False, started=_stamp(), steps=[], deadline=deadline)
        return True


def _run(deadline: float, step_limit: float, plan: list[Step] | None) -> dict:
    begin = time.monotonic()
    for name, fn in (plan if plan is not None else steps()):
        left = deadline - (time.monotonic() - begin)
        if left <= 0:
            _state["steps"].append({"name": name, "ok": False, "detail": "skipped: the overall time ran out", "seconds": 0})
            continue
        _state["steps"].append(_run_step(name, fn, min(CAMPAIGN_STEP_SECONDS if name == CAMPAIGN_STEP else step_limit, left)))
    _state.update(running=False, done=True)
    return status()


def shutdown(deadline: float = DEADLINE_SECONDS, step_limit: float = STEP_SECONDS, plan: list[Step] | None = None) -> dict:
    """Run the steps in order. Returns the status (also kept for ``status()``). Calling it again while it runs returns the running status."""
    if not _claim(deadline):
        return status()
    return _run(deadline, step_limit, plan)


def start_shutdown() -> dict:
    """Begin the shutdown in the background (the window polls ``status``)."""
    if _claim(DEADLINE_SECONDS):
        threading.Thread(target=_run, args=(DEADLINE_SECONDS, STEP_SECONDS, None), daemon=True, name="shutdown").start()
    return status()


def status() -> dict:
    return {"running": _state["running"], "done": _state["done"], "started": _state["started"], "deadline_seconds": _state["deadline"],
            "steps": list(_state["steps"]), "ok": _state["done"] and all(s["ok"] for s in _state["steps"])}


def reset() -> None:
    """Forget a finished shutdown (tests)."""
    _state.update(running=False, done=False, started=None, steps=[], deadline=DEADLINE_SECONDS)
    _extra.clear()
