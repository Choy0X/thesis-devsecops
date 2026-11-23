"""How a running campaign and the rest of the app talk to each other.

* **The pause flag.** The app sets the campaign's ``control`` to ``pause`` (and its state to ``pausing``); the campaign's process reads it through ``Control.should_stop``, at most once a
  second, and ends its stage at the next safe point (see ``stopping``).
* **The heartbeat.** The process writes its progress, and now and then a sample of its counters, through ``Control.tick``.
* **Alive or gone.** A campaign that says it runs must have a process: ``reconcile`` marks the ones whose process is gone ``interrupted`` (a pause that was being carried out when the process
  died counts as paused), and ``watchdog_tick`` ends a pause that takes longer than the grace period by killing the process.
"""
from __future__ import annotations

import os
import time
from typing import Callable

from app.backend.pipeline import derived, util
from app.backend.pipeline.campaigns import repo

GRACE_SECONDS = 120.0           # a pause that takes longer than this is ended by killing the process (ZTSA_CAMPAIGN_GRACE)
START_GRACE = 120.0             # a campaign just stored has no process yet
HEARTBEAT_STALE = 3600.0        # a live process that has not reported for this long is hung, or its pid was reused
CHECK_EVERY = 1.0               # seconds between reads of the pause flag
BEAT_EVERY = 5.0                # seconds between heartbeats
SAMPLE_EVERY = 30.0             # seconds between samples


def grace() -> float:
    try:
        return max(1.0, float(os.environ.get("ZTSA_CAMPAIGN_GRACE", GRACE_SECONDS)))
    except ValueError:
        return GRACE_SECONDS


# -- processes ---------------------------------------------------------------------------------------------------------------------

def identity(pid: int | None = None) -> tuple[int, float | None]:
    """``(pid, create time)`` of a process (this one by default): together they name a process even when the system reuses the number."""
    import psutil
    pid = pid or os.getpid()
    try:
        return pid, psutil.Process(pid).create_time()
    except psutil.Error:
        return pid, None


def process_alive(pid: int | None, started: float | None) -> bool:
    """Is there a process with this pid that was started when the campaign's process was?"""
    if not pid:
        return False
    import psutil
    try:
        p = psutil.Process(int(pid))
        if started is not None and abs(p.create_time() - float(started)) > 1.0:
            return False                              # the number now belongs to another process
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _epoch(text: str | None) -> float | None:
    from app.backend.pipeline.db import mappers as m
    d = m.parse_dt(text)
    return d.timestamp() if d else None


# -- the running process's side ----------------------------------------------------------------------------------------------------

class Control:
    """Held by the process that runs a campaign: ``should_stop`` for the stages, ``tick`` for their progress."""

    def __init__(self, campaign_id: str, conn=None, *, sampler: Callable[[], dict] | None = None, clock: Callable[[], float] = time.monotonic,
                 check_every: float = CHECK_EVERY, beat_every: float = BEAT_EVERY, sample_every: float = SAMPLE_EVERY):
        self.id = campaign_id
        self.conn = conn if conn is not None else derived.connect()
        self.sampler, self.clock = sampler, clock
        self.check_every, self.beat_every, self.sample_every = check_every, beat_every, sample_every
        self._stop = False
        self._checked = self._beat = self._sampled = clock() - 1e9           # the first call of each does its work
        self.progress: dict = {}

    def should_stop(self) -> bool:
        """Did the app ask this campaign to pause? Once yes, always yes."""
        if self._stop:
            return True
        now = self.clock()
        if now - self._checked >= self.check_every:
            self._checked = now
            self._stop = repo.control_of(self.conn, self.id) == "pause"
        return self._stop

    def tick(self, **progress) -> None:
        """What a stage reports: remembered, written with the heartbeat (every few seconds), and sampled (every half minute)."""
        self.progress = {**self.progress, **progress}
        now = self.clock()
        if now - self._beat >= self.beat_every:
            self._beat = now
            repo.beat(self.conn, self.id, progress=self.progress)
        if self.sampler is not None and now - self._sampled >= self.sample_every:
            self.sample()

    def sample(self) -> None:
        """Write a sample now (at the boundary of a stage, and every half minute)."""
        self._sampled = self.clock()
        if self.sampler is None:
            return
        try:
            repo.add_sample(self.conn, self.id, self.sampler())
        except Exception:  # ignored: a sample that cannot be taken is only a gap in a chart
            pass

    def flush(self) -> None:
        """Write the heartbeat and the progress now."""
        self._beat = self.clock()
        repo.beat(self.conn, self.id, progress=self.progress)


# -- the app's side ----------------------------------------------------------------------------------------------------------------

def request_pause(conn, campaign_id: str) -> bool:
    """Ask a running campaign to pause: it ends its stage at the next safe point, saves where it was and its process exits. False if it is not running."""
    now = util.now_z()
    if repo.transition(conn, campaign_id, "pausing", expect="running", control="pause", control_at=now):
        repo.add_event(conn, campaign_id, "pause_requested")
        return True
    return False


def mark_resumed(conn, campaign_id: str, *, expect=("paused", "interrupted")) -> bool:
    """Put a paused or interrupted campaign back to running (the pause flag cleared, a resume counted). The caller starts the process."""
    now = util.now_z()
    if repo.transition(conn, campaign_id, "running", expect=expect, inc={"resumes": 1}, control="run", control_at=now, error=None, finished_at=None, pid=None, pid_started=None,
                       heartbeat_ts=time.time(), paused_reason=None):
        repo.add_event(conn, campaign_id, "resumed")
        return True
    return False


def mark_interrupted(conn, campaign_id: str, why: str = "the process is gone") -> bool:
    """A campaign that was running or pausing but has no process any more."""
    c = repo.get(conn, campaign_id)
    if c is None or c["state"] not in ("running", "pausing"):
        return False
    to = "paused" if c["state"] == "pausing" else "interrupted"           # a pause that was being carried out has, in effect, been done
    if repo.transition(conn, campaign_id, to, expect=c["state"]):
        repo.add_event(conn, campaign_id, "paused" if to == "paused" else "interrupted", {"why": why})
        return True
    return False


def after_kill(params: dict | None, why: str = "the process was stopped") -> None:
    """A campaign's process was killed: close its miner's run record and mark the campaign (paused if a pause had been asked for, else interrupted)."""
    cid = (params or {}).get("campaign_id")
    if not cid:
        return
    try:
        from app.backend.pipeline import mining_run
        mining_run.finish("stopped", run_key=f"{mining_run.KEY}:{cid}")
    except Exception:  # ignored: the run record only matters to a second miner, and a dead process does not block one
        pass
    with derived.connect() as conn:
        mark_interrupted(conn, cid, why)


def kill_campaign(c: dict) -> None:
    """End the process of a campaign for good (the watchdog's kill): its job when the app started it, else the process the campaign recorded."""
    from app.backend import jobs
    if c.get("job_id") and jobs.get(c["job_id"]) is not None and jobs.cancel(c["job_id"], hard=True):
        return
    if process_alive(c.get("pid"), c.get("pid_started")):
        import psutil
        try:
            root = psutil.Process(int(c["pid"]))
            for child in root.children(recursive=True):
                child.kill()
            root.kill()
        except psutil.Error:
            pass
    after_kill({"campaign_id": c["campaign_id"]}, "the process was killed after the pause took too long")


def pause_for_job(params: dict | None) -> bool:
    """What cancelling the job of a campaign means: ask the campaign to pause (the job is not killed; the watchdog does that if it does not stop in time)."""
    cid = (params or {}).get("campaign_id")
    if not cid:
        return False
    with derived.connect() as conn:
        return request_pause(conn, cid)


def _start_paused_campaign(conn, campaign_id: str) -> bool:
    """Put a campaign paused for memory back to running and start its job, the same way a person resuming it by hand does (``campaigns_api.campaign_resume``), minus the re-claim of
    ecosystems it already holds while paused. False if it changed state under us (tried again at the next watchdog tick)."""
    from app.backend import jobs
    campaign = repo.get(conn, campaign_id) or {}
    if ((campaign.get('plan') or {}).get('compute') or {}).get('version') != 2:
        return False
    name = campaign.get("name")
    if not mark_resumed(conn, campaign_id, expect=("paused",)):
        return False
    try:
        job = jobs.start("campaign_run", {"campaign_id": campaign_id, "campaign_name": name})
    except Exception:
        repo.transition(conn, campaign_id, "paused", expect="running")
        raise
    repo.update(conn, campaign_id, job_id=job.id)
    return True


# Pauses that wait only for memory: the runner's own gate and the two the mining pipeline raises when it runs out of room. The watchdog resumes all of them.
MEMORY_PAUSE_CODES = frozenset({"insufficient_memory", "pipeline_memory_pressure", "mining_memory_pressure"})


def retry_paused_for_memory(conn, *, resume: Callable[[object, str], bool] = _start_paused_campaign) -> list[str]:
    """Campaigns paused only because a step did not fit in memory at the time (``runner.memory_gate``): tried again by themselves once enough is free, so the owner does not have to
    notice and resume them by hand. A campaign a person paused carries no ``paused_reason`` and is never touched here. Returns the ids resumed."""
    from app.backend.pipeline.campaigns import admission
    resumed = []
    for c in repo.all_campaigns(conn, current_only=True):
        if c["state"] != "paused":
            continue
        why = c.get("paused_reason") or {}
        if why.get("code") not in MEMORY_PAUSE_CODES:
            continue
        if why.get("code") == "insufficient_memory":
            if not admission.memory_now_covers(float(why.get("needs_gb") or 0)):
                continue
        elif not admission.memory_free_at_least(float(why.get("resume_needs_gb") or 0)):
            continue
        try:
            if resume(conn, c["campaign_id"]):
                resumed.append(c["campaign_id"])
        except Exception:  # ignored: the next watchdog tick tries again
            pass
    return resumed


def start_watchdog(interval: float = 5.0) -> Callable[[], None]:
    """Keep an eye on the campaigns from a thread of the app: every few seconds, mark the ones whose process is gone, end the pauses that take too long, and try again the ones paused
    only because memory was tight a moment ago. Returns the function that stops it."""
    import threading
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            try:
                with derived.connect() as conn:
                    reconcile(conn)
                    watchdog_tick(conn, kill=kill_campaign)
                    retry_paused_for_memory(conn)
            except Exception:  # ignored: a store that is down for a moment is looked at again at the next round
                pass
    threading.Thread(target=loop, daemon=True, name="campaign-watchdog").start()
    return stop.set


def _alive(c: dict, now: float, alive: Callable[[int | None, float | None], bool]) -> bool:
    if not c.get("pid"):
        return now - float(c.get("heartbeat_ts") or 0) < START_GRACE         # just stored: its process is on its way
    return alive(c["pid"], c.get("pid_started")) and now - float(c.get("heartbeat_ts") or 0) < HEARTBEAT_STALE


def reconcile(conn, *, now: float | None = None, alive: Callable[[int | None, float | None], bool] = process_alive) -> list[str]:
    """Mark the campaigns that say running or pausing but have no process interrupted (paused, if a pause was under way). Returns their ids."""
    now = time.time() if now is None else now
    changed = []
    for c in repo.all_campaigns(conn, states=('running', 'pausing')):
        if c["legacy"]:
            if c["state"] == "running" and repo.transition(conn, c["campaign_id"], "interrupted", expect="running"):       # the engine that ran it is gone
                changed.append(c["campaign_id"])
        elif c["state"] in ("running", "pausing") and not _alive(c, now, alive):
            if mark_interrupted(conn, c["campaign_id"]):
                changed.append(c["campaign_id"])
    return changed


def watchdog_tick(conn, *, now: float | None = None, grace_seconds: float | None = None, kill: Callable[[dict], None]) -> list[str]:
    """End the pauses that take too long: the process of a campaign that was asked to pause more than the grace period ago is killed (``kill(campaign)``), and the campaign is paused.
    Every stage can be run again, so only the step that was in progress is done again. Returns the ids."""
    now = time.time() if now is None else now
    limit = grace() if grace_seconds is None else grace_seconds
    killed = []
    for c in repo.all_campaigns(conn, current_only=True):
        if c["state"] != "pausing":
            continue
        asked = _epoch(c.get("control_at"))
        if asked is None or now - asked < limit:
            continue
        try:
            kill(c)
        except Exception:  # ignored: a process that is already gone is what the kill was for; reconcile settles the rest
            pass
        if repo.transition(conn, c["campaign_id"], "paused", expect="pausing"):
            repo.add_event(conn, c["campaign_id"], "forced_kill", {"after_seconds": round(now - asked)})
            killed.append(c["campaign_id"])
    return killed
