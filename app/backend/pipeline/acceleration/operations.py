"""Per-commit progress and cancellation shared by blocking acquisition steps."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time
import uuid

from app.backend.pipeline import stopping

_active = ContextVar('mining_operation', default=None)


class DeadlineExpired(TimeoutError):
    pass


class Operation:
    def __init__(self, commit_key, timeout=600, should_stop=lambda: False, *, kind="helper"):
        from .metrics import current
        if kind not in ("helper", "commit"):raise ValueError("Unknown mining operation kind")
        self.kind, self.completion = kind, None
        self.profile, self.commit_key = current, commit_key
        self.identifier = uuid.uuid4().hex
        self.started, self.started_at = time.monotonic(), time.time()
        self.deadline, self.should_stop = self.started+timeout, should_stop
        self.phase_name, self.phase_started = 'starting', self.started
        self.phase_seconds = {}
        self.processed = self.total = self.unit = None
        self.status, self.error, self.finished = 'running', None, False
        self.cancelled, self.guard, self.resources = threading.Event(), threading.RLock(), {}
        self.cleanup_errors = []
        self.cancel_origin = None
        self.cancel_error = None
        self.failed_resources = set()
        self.cancel_guard = threading.Lock()
        self.profile.start_operation(self)

    def check(self):
        if self.should_stop():
            self.cancel()
            raise stopping.Paused()
        if self.cancelled.is_set() or time.monotonic() >= self.deadline:
            early = self.cancelled.is_set() and time.monotonic() < self.deadline
            self.cancel()
            # A cancel that came before the deadline names who asked for it, so the failure is not mistaken for a slow commit (on the first Linux run every commit failed this way).
            raise DeadlineExpired('Commit deadline exceeded: ' + self.commit_key + (' (cancelled early by: ' + str(self.cancel_origin) + (' after ' + self.cancel_error if self.cancel_error else '') + ')' if early and self.cancel_origin else ''))

    def cancel(self):
        if not self.cancelled.is_set():
            import traceback
            self.cancel_origin = ' < '.join(f'{frame.name}:{frame.lineno}' for frame in reversed(traceback.extract_stack(limit=7)[:-1]))
        self.cancelled.set()
        with self.cancel_guard:
            with self.guard:
                closers = list(self.resources.items())
            for token, close in closers:
                try:
                    close()
                except Exception as error:
                    with self.guard:
                        self.cleanup_errors.append(str(error))
                        self.failed_resources.add(token)
                else:
                    with self.guard:
                        self.resources.pop(token, None)
                        self.failed_resources.discard(token)

    @contextmanager
    def resource(self, close):
        token = object()
        with self.guard:
            self.resources[token] = close
        try:
            self.check()
            yield
        except BaseException as error:
            if self.cancel_error is None and not isinstance(error, DeadlineExpired):
                from app.backend.pipeline import redact
                self.cancel_error = redact.text(f'{type(error).__name__}: {error}')[:300]            # the first real failure, kept so a later 'deadline exceeded' does not hide it
            self.cancel()
            raise
        finally:
            with self.guard:
                if token not in self.failed_resources:
                    self.resources.pop(token, None)

    def phase(self, name, *, processed=None, total=None, unit=None):
        self.check()
        with self.guard:
            now = time.monotonic()
            if name != self.phase_name:
                old, elapsed = self.phase_name, now-self.phase_started
                self.phase_seconds[old] = self.phase_seconds.get(old, 0)+elapsed
                self.phase_name, self.phase_started = name, now
                self.processed = self.total = self.unit = None
            if processed is not None: self.processed = processed
            if total is not None: self.total = total
            if unit is not None: self.unit = unit

    def state(self):
        with self.guard:
            now = self.ended if self.finished else time.monotonic()
            phases = dict(self.phase_seconds)
            if not self.finished:
                phases[self.phase_name] = phases.get(self.phase_name, 0)+now-self.phase_started
            return {'commit_key': self.commit_key, 'attempt_id': self.identifier,
                    'kind': self.kind, 'completion': self.completion,
                    'started_at': self.started_at, 'elapsed_seconds': now-self.started,
                    'phase': self.phase_name, 'phase_elapsed_seconds': now-self.phase_started,
                    'phase_seconds': phases, 'processed': self.processed, 'total': self.total,
                    'unit': self.unit, 'status': self.status, 'error': self.error,
                    'cleanup_errors': list(self.cleanup_errors)}

    def finish(self, status='done', error=None, *, completion=None):
        with self.guard:
            if self.finished: return
            if completion not in (None, 'acknowledged', 'legacy', 'reused'):
                raise ValueError('Unknown mining commit completion')
            if completion is not None and (self.kind != 'commit' or status != 'done'):
                raise ValueError('Commit completion requires successful top-level work')
            self.completion = completion
            self.ended = time.monotonic()
            self.phase_seconds[self.phase_name] = self.phase_seconds.get(self.phase_name, 0)+self.ended-self.phase_started
            from app.backend.pipeline import redact
            self.finished, self.status, self.error = True, status, (None if error is None else redact.text(error))
        self.profile.finish_operation(self)


@contextmanager
def use(operation):
    token = _active.set(operation)
    try:
        yield operation
    finally:
        _active.reset(token)


@contextmanager
def commit(key, *, timeout=600, should_stop=lambda: False, kind="helper"):
    operation = Operation(key, timeout, should_stop, kind=kind)
    try:
        with use(operation):
            operation.check()
            yield operation
    except BaseException as error:
        operation.cancel()
        operation.finish('cancelled' if isinstance(error, stopping.Paused) else 'failed', str(error))
        raise
    else:
        operation.finish()


def check(*, disk=True):
    """Keep disk admission by default; audited local reads may check control only."""
    if disk:
        from .disk_budget import check as disk_check
        disk_check()
    if operation := _active.get():
        operation.check()


def current():
    return _active.get()


@contextmanager
def acquire(lock, *, disk=True):
    """A bounded/cancellable wait without holding unrelated global locks."""
    if not current():
        with lock: yield
        return
    while not lock.acquire(timeout=timeout(.2) if disk else timeout(.2, disk=False)):
        if disk:check()
        else:check(disk=False)
    try:
        if disk:check()
        else:check(disk=False)
        yield
    finally:
        lock.release()


def timeout(seconds, *, disk=True):
    if disk:check()
    else:check(disk=False)
    operation = _active.get()
    return min(seconds, max(.001, operation.deadline-time.monotonic())) if operation else seconds


def phase(name, **progress):
    if operation := _active.get():
        operation.phase(name, **progress)


@contextmanager
def resource(close):
    operation = _active.get()
    if operation:
        with operation.resource(close): yield
    else:
        yield
