"""Who is working on a session: a lease kept with the session.

The in-process claim (``agent_sessions.claim``) keeps two requests of one process out of a session. It is gone when the process is, and it cannot tell a second process (a development copy, a
test) about itself. The lease can: it is a field of the session (``lease``: the owner's boot id, host, process id and the time it runs out), taken in one conditional change and renewed every few
seconds while a run works. A process that ends leaves its lease behind, and the next run sees it:

* a lease whose time has passed, or whose process no longer exists on this computer, is STALE: the run it belonged to did not end, it is marked interrupted, and the lease is taken over;
* a lease that is still good and belongs to another process means that process is working on the session: the new run is refused (409) and does not touch it.

A lease never ends work. It only says who the work belongs to; a long run is not stopped by it.
"""
from __future__ import annotations

import os
import socket
import threading
import time

from app.backend.pipeline import agent_sessions
from app.backend.pipeline.agentic.engine.errors import AgentSessionError

SECONDS = 45.0            # how long a lease is good without being renewed
BEAT = 10.0               # how often it is renewed while a run works


def _alive(pid) -> bool:
    try:
        import psutil
        return bool(pid) and psutil.pid_exists(int(pid))
    except Exception:  # ignored: if the process table cannot be read, the lease's own time decides
        return True


def stale(lease: dict | None, now: float | None = None) -> bool:
    """Whether a lease belongs to a run that is over: it ran out, or its process is gone (on this computer)."""
    if not lease:
        return False
    now = time.time() if now is None else now
    if lease.get("owner") == agent_sessions.BOOT:
        return False
    if float(lease.get("expires_at", 0)) < now:
        return True
    return lease.get("host") == socket.gethostname() and not _alive(lease.get("pid"))


def acquire(project_id: str, session_id: str, now: float | None = None) -> dict | None:
    """Take the session's lease for this process. Returns the stale lease that was taken over (its run did not end), ``{}`` when there was none, and raises ``AgentSessionError`` (409 ``busy``)
    when another live process holds it. The change is conditional on the lease being exactly what was seen, so two processes cannot both take it."""
    store = agent_sessions.store()
    now = time.time() if now is None else now
    mine = {"owner": agent_sessions.BOOT, "host": socket.gethostname(), "pid": os.getpid(), "expires_at": now + SECONDS}
    for _ in range(3):
        meta = store.meta(project_id, session_id)
        if meta is None:
            raise agent_sessions.NotFound()
        seen = meta.get("lease")
        if seen and seen.get("owner") != agent_sessions.BOOT and not stale(seen, now):
            raise AgentSessionError("Another process is working on this session. Wait until it has finished.", 409, "busy")
        where = {"lease": None} if not seen else {"lease.owner": seen.get("owner"), "lease.expires_at": seen.get("expires_at")}
        if store.commit(project_id, session_id, set_={"lease": mine}, where=where) is not None:
            return {} if (not seen or seen.get("owner") == agent_sessions.BOOT) else seen
    raise AgentSessionError("The session's lease is being taken by another process; try again.", 409, "busy")


def renew(project_id: str, session_id: str, now: float | None = None) -> bool:
    """Push the lease's end out. False when it is no longer this process's (it was taken over): the run must stop."""
    now = time.time() if now is None else now
    return agent_sessions.store().commit(project_id, session_id, set_={"lease.expires_at": now + SECONDS}, where={"lease.owner": agent_sessions.BOOT}) is not None


def release(project_id: str, session_id: str) -> None:
    agent_sessions.store().commit(project_id, session_id, unset=["lease"], where={"lease.owner": agent_sessions.BOOT})


class Heartbeat:
    """Renews the lease every ``BEAT`` seconds on a thread of its own while a run works, and runs ``on_beat`` each time (the run's counters are stored then too). ``lost`` is set when the
    lease was taken over, and ``on_lost`` is called once."""

    def __init__(self, project_id: str, session_id: str, on_beat=None, on_lost=None, beat: float = BEAT):
        self.project_id, self.session_id, self.on_beat, self.on_lost, self.beat = project_id, session_id, on_beat, on_lost, beat
        self.lost = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="agent-lease")

    def start(self) -> "Heartbeat":
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.beat):
            try:
                if not renew(self.project_id, self.session_id):
                    self.lost = True
                    if self.on_lost:
                        self.on_lost()
                    return
                if self.on_beat:
                    self.on_beat()
            except Exception:  # ignored: a store that cannot be reached for a moment is retried at the next beat; the lease's own time is what decides
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
