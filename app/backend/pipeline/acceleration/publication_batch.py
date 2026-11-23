"""Ordered, descriptor-only coalescing for one owned publication transport.

Registration order belongs to the scheduler. This module never reads artifacts,
claims publication leases, or interprets opaque caller metadata.
"""
from collections import deque
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass
import math
import threading
import time


@dataclass(frozen=True)
class Request:
    token: object
    descriptor: object
    size: int
    context: object


class _Entry:
    def __init__(self, token):
        self.token = token
        self.request = self.future = None
        self.ready_at = None
        self.state = 'registered'
        self.cancelled = False


class OrderedBatch:
    def __init__(self, send, *, abort, max_items=8, max_bytes=32 << 20, wait_seconds=.020):
        if type(max_items) is not int or not 1 <= max_items <= 8:
            raise ValueError('Publication batches require between one and eight items')
        if type(max_bytes) is not int or not 1 <= max_bytes <= 32 << 20:
            raise ValueError('Publication batch bytes must be within 32 MiB')
        if (type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds) or
                not 0 <= wait_seconds <= .020):
            raise ValueError('Publication collection wait must be between zero and 20 ms')
        if not callable(send) or not callable(abort):
            raise TypeError('Publication send and confirmed abort callbacks are required')
        self.send, self.abort = send, abort
        self.max_items, self.max_bytes, self.wait_seconds = max_items, max_bytes, float(wait_seconds)
        self.condition = threading.Condition(threading.RLock())
        self.entries, self.order = {}, deque()
        self.active = None
        self.interrupted = self.abort_busy = self.abort_confirmed = False
        self.cleanup_error = None
        self.closed = False
        self.thread = threading.Thread(target=self._run, name='publication-batch', daemon=True)
        self.thread.start()

    def register(self, token):
        with self.condition:
            if self.closed:raise RuntimeError('Publication batch is closed')
            if token in self.entries:raise ValueError('Publication token already registered')
            # The scheduler owns at most 256 live descriptors. Skipped entries
            # still count until their ordered predecessor releases the frontier.
            if len(self.entries) >= 256:raise MemoryError('Publication registration bound reached')
            entry = _Entry(token)
            self.entries[token] = entry
            self.order.append(entry)
            self.condition.notify_all()

    def submit(self, token, descriptor, *, size, context):
        if type(size) is not int or size < 0:
            raise ValueError('Publication descriptor size must be a nonnegative integer')
        with self.condition:
            if self.closed:raise RuntimeError('Publication batch is closed')
            entry = self.entries.get(token)
            if entry is None:raise KeyError('Publication token was not registered')
            if entry.state != 'registered':raise ValueError('Publication token already submitted or skipped')
            future = Future()
            entry.future, entry.request = future, Request(token, descriptor, size, context)
            entry.ready_at, entry.state = time.monotonic(), 'queued'
            self.condition.notify_all()
        # add_done_callback can run immediately. Never register it under our
        # lock, and never run Future callbacks while holding that lock.
        def cancelled(done):
            if done.cancelled():
                with self.condition:
                    current = self.entries.get(token)
                    notify = current is not None and not current.cancelled
                if notify:
                    try:self.cancel(token)
                    except Exception:
                        # ignored: the failed closer remains owned by the active group.
                        # An explicit cancel/close retries and reports failure.
                        pass
        future.add_done_callback(cancelled)
        return future

    def skip(self, token):
        future = None
        with self.condition:
            entry = self.entries.get(token)
            if entry is None or entry.state in ('active', 'skipped'):return
            future = entry.future
            entry.state, entry.request = 'skipped', None
            self.condition.notify_all()
        if future is not None:future.cancel()

    def cancel(self, token):
        with self.condition:
            entry = self.entries.get(token)
            if entry is None:return
            group = self.active if entry.state == 'active' else None
            if entry.cancelled:
                if group is None or self.abort_confirmed:return
                # An earlier failed abort still owns a live transport. Retry
                # that closer rather than falsely confirming resource release.
                future = None
            else:
                entry.cancelled = True
                future = entry.future
            if group is None:
                entry.state, entry.request = 'skipped', None
            else:
                self.interrupted = True
            self.condition.notify_all()
        if future is not None:future.cancel()
        if group is not None:self._abort(group)

    def _abort(self, group):
        with self.condition:
            while self.active is group and self.abort_busy:
                self.condition.wait()
            if self.active is not group or self.abort_confirmed:return
            self.abort_busy = True
        try:
            self.abort()
        except BaseException as error:
            with self.condition:
                if self.active is group:
                    self.abort_busy, self.cleanup_error = False, error
                    self.condition.notify_all()
            raise
        else:
            with self.condition:
                if self.active is group:
                    self.abort_busy, self.abort_confirmed, self.cleanup_error = False, True, None
                    self.condition.notify_all()

    def _discard_skipped(self):
        while self.order and self.order[0].state == 'skipped':
            entry = self.order.popleft()
            if self.entries.get(entry.token) is entry:del self.entries[entry.token]

    def _take(self):
        # Called only while the condition lock is held.
        while True:
            self._discard_skipped()
            if self.closed:return None
            if not self.order or self.order[0].state != 'queued':
                self.condition.wait()
                continue
            head = self.order[0]
            deadline = head.ready_at + self.wait_seconds
            selected, total, full = [], 0, False
            for entry in self.order:
                if entry.state == 'skipped':continue
                if entry.state != 'queued':break
                amount = entry.request.size
                if selected and (len(selected) == self.max_items or total + amount > self.max_bytes):
                    full = True
                    break
                selected.append(entry)
                total += amount
                if len(selected) == self.max_items or total >= self.max_bytes:
                    full = True
                    break
            remaining = deadline - time.monotonic()
            if not full and remaining > 0:
                self.condition.wait(timeout=remaining)
                continue
            selected_ids = {id(entry) for entry in selected}
            group = []
            while self.order and len(group) < len(selected):
                entry = self.order.popleft()
                if entry.state == 'skipped':
                    if self.entries.get(entry.token) is entry:del self.entries[entry.token]
                    continue
                if id(entry) not in selected_ids:raise RuntimeError('Publication order changed while locked')
                entry.state = 'active'
                group.append(entry)
            self.active = tuple(group)
            self.interrupted = self.abort_busy = self.abort_confirmed = False
            self.cleanup_error = None
            return self.active

    @staticmethod
    def _complete(future, *, value=None, error=None):
        if future is None or future.done():return
        try:
            if error is None:future.set_result(value)
            else:future.set_exception(error)
        except InvalidStateError:
            # A caller may independently cancel between the done check and
            # completion. That cancellation must never leak a sender thread.
            if not future.done():raise

    def _run(self):
        while True:
            with self.condition:
                group = self._take()
                if group is None:return
                interrupted = self.interrupted
            values = failure = None
            send = self.send
            requests = tuple(entry.request for entry in group)
            # Callback lookup/argument preparation can yield to cancellation.
            # The transport must additionally fence its own start atomically
            # against the sticky synthetic Operation cancellation state.
            with self.condition:
                interrupted = self.interrupted or self.closed
            if not interrupted:
                try:
                    values = send(requests)
                    if not isinstance(values, list) or len(values) != len(group):
                        raise ValueError('Publication send must return one result per request')
                except BaseException as error:
                    failure = error
                    try:self._abort(group)
                    except BaseException:
                        # ignored: keep ownership and all healthy futures pending until
                        # cancel/close confirms the retained transport cleanup.
                        pass
            with self.condition:
                while self.active is group and (self.interrupted or failure is not None) and not self.abort_confirmed:
                    self.condition.wait()
                interrupted = self.interrupted
                for entry in group:
                    if self.entries.get(entry.token) is entry:del self.entries[entry.token]
                    entry.state = 'finished'
                self.active = None
                self.condition.notify_all()
            if interrupted:
                failure = ConnectionError('Publication batch interrupted; retry from its durable checkpoint')
            for index, entry in enumerate(group):
                self._complete(entry.future, value=values[index] if failure is None else None, error=failure)
                entry.request = None
            group = values = None

    def close(self):
        pending = []
        with self.condition:
            self.closed = True
            for entry in self.order:
                if entry.future is not None:pending.append(entry.future)
                entry.state, entry.request = 'skipped', None
            group = self.active
            if group is not None:self.interrupted = True
            self.condition.notify_all()
        for future in pending:future.cancel()
        if group is not None:self._abort(group)
        # Retain the thread reference after every failure, allowing the owning
        # resource scope to retry cleanup instead of reporting a false close.
        self.thread.join(timeout=5)
        if self.thread.is_alive():raise OSError('Publication sender thread termination unconfirmed')
        with self.condition:
            self.entries.clear()
            self.order.clear()
