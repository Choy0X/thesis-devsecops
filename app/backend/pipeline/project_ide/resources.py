"""FIFO execution capacity shared by independent chat workspaces."""
from collections import defaultdict, deque
from contextlib import contextmanager
import threading


class Cancelled(RuntimeError):
    pass


class Hold:
    """One acquired unit of capacity. ``released()`` gives it up while the holder waits for a person."""

    def __init__(self, queues, key: str, cancelled: threading.Event, capacity: int):
        self._queues, self._key, self._cancelled, self._capacity = queues, key, cancelled, capacity
        self.held = True

    @contextmanager
    def released(self):
        """Release the capacity; take it again (queued behind earlier waiters) when the body ends normally."""
        if not self.held:
            yield
            return
        self._queues._give_back(self._key)
        self.held = False
        yield
        self._queues._take(self._key, self._cancelled, self._capacity)
        self.held = True


class ResourceQueues:
    def __init__(self):
        self._condition = threading.Condition()
        self._waiting = defaultdict(deque)
        self._active = defaultdict(int)

    def _take(self, key: str, cancelled: threading.Event, capacity: int, on_wait=None) -> None:
        ticket = object()
        acquired = False
        announced = False
        with self._condition:
            self._waiting[key].append(ticket)
        try:
            with self._condition:
                while True:
                    if cancelled.is_set():
                        raise Cancelled("The queued work was cancelled before execution.")
                    if self._waiting[key][0] is ticket and self._active[key] < capacity:
                        self._waiting[key].popleft()
                        self._active[key] += 1
                        acquired = True
                        self._condition.notify_all()
                        return
                    if on_wait and not announced:
                        on_wait()
                        announced = True
                    self._condition.wait(0.1)
        finally:
            if not acquired:
                with self._condition:
                    if ticket in self._waiting[key]:
                        self._waiting[key].remove(ticket)
                    self._condition.notify_all()

    def _give_back(self, key: str) -> None:
        with self._condition:
            self._active[key] -= 1
            self._condition.notify_all()

    @contextmanager
    def acquire(self, key: str, cancelled: threading.Event, *, capacity: int = 1, on_wait=None):
        if capacity < 1:
            raise ValueError("Execution capacity must be positive.")
        self._take(key, cancelled, capacity, on_wait)
        hold = Hold(self, key, cancelled, capacity)
        try:
            yield hold
        finally:
            if hold.held:
                self._give_back(key)
