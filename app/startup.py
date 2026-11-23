"""The steps of starting the app, with a weight, a state and a time each, so the splash window can show a true bar and the step that is still loading.

This is a plain model with no window in it: the launcher marks a step running, done or failed as it goes; the splash reads ``snapshot()`` and draws it. A
step has a weight (how much of the wait it is expected to be), so the bar does not jump from 10 % to 90 % on one slow step. A failure carries a message in
plain words and the path of the log that has the details.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

PENDING, RUNNING, DONE, FAILED, SKIPPED = "pending", "running", "done", "failed", "skipped"


@dataclass
class Step:
    id: str
    label: str
    weight: float = 1.0
    state: str = PENDING
    detail: str = ""
    started: float | None = None
    finished: float | None = None
    log: str | None = None

    def seconds(self, now: float) -> float | None:
        if self.started is None:
            return None
        return round((self.finished if self.finished is not None else now) - self.started, 2)


DEFAULT_STEPS: tuple[tuple[str, str, float], ...] = (
    ("interface", "Checking the interface", 4.0),
    ("server", "Starting the app", 2.0),
    ("answer", "Waiting for it to answer", 1.5),
    ("settings", "Reading the settings", 0.5),
    ("database", "Checking the database", 1.0),
    ("machine", "Reading this computer", 0.5),
    ("warm", "Preparing the first pages", 0.5),
)


@dataclass
class Startup:
    steps: list[Step] = field(default_factory=lambda: [Step(i, label, w) for i, label, w in DEFAULT_STEPS])
    clock: object = time.monotonic

    def __post_init__(self):
        self._lock = threading.Lock()

    def _step(self, id: str) -> Step:
        return next(s for s in self.steps if s.id == id)

    def begin(self, id: str) -> None:
        with self._lock:
            s = self._step(id)
            s.state, s.started, s.finished = RUNNING, self.clock(), None

    def done(self, id: str, detail: str = "") -> None:
        with self._lock:
            s = self._step(id)
            s.state, s.detail, s.finished = DONE, detail, self.clock()
            s.started = s.started if s.started is not None else s.finished

    def note(self, id: str, detail: str) -> None:
        """What a running step is doing right now, in a few words (the splash shows it beside the step)."""
        with self._lock:
            s = self._step(id)
            if s.state == RUNNING:
                s.detail = detail[:160]

    def skip(self, id: str, detail: str = "") -> None:
        with self._lock:
            s = self._step(id)
            s.state, s.detail, s.finished = SKIPPED, detail, self.clock()

    def fail(self, id: str, message: str, log: str | None = None) -> None:
        with self._lock:
            s = self._step(id)
            s.state, s.detail, s.log, s.finished = FAILED, message, log, self.clock()
            s.started = s.started if s.started is not None else s.finished

    def reset(self) -> None:
        with self._lock:
            for s in self.steps:
                s.state, s.detail, s.log, s.started, s.finished = PENDING, "", None, None, None

    @property
    def failed(self) -> Step | None:
        return next((s for s in self.steps if s.state == FAILED), None)

    @property
    def ready(self) -> bool:
        return all(s.state in (DONE, SKIPPED) for s in self.steps)

    def fraction(self) -> float:
        """How far along the start is, by weight (a running step counts for half)."""
        total = sum(s.weight for s in self.steps) or 1.0
        got = sum(s.weight * (1.0 if s.state in (DONE, SKIPPED) else 0.5 if s.state == RUNNING else 0.0) for s in self.steps)
        return round(min(1.0, got / total), 4)

    def snapshot(self) -> dict:
        now = self.clock()
        with self._lock:
            current = next((s for s in self.steps if s.state == RUNNING), None)
            failed = self.failed
            return {"fraction": self.fraction(), "ready": self.ready, "current": current.label if current else None,
                    "failure": {"step": failed.label, "message": failed.detail, "log": failed.log} if failed else None,
                    "steps": [{"id": s.id, "label": s.label, "state": s.state, "detail": s.detail, "seconds": s.seconds(now)} for s in self.steps]}
