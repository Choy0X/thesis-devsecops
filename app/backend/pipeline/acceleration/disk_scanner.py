"""Owned, bounded full disk recounts outside the campaign interpreter's GIL."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from . import operations
from .disk_budget import count_used

MAX_MESSAGE = 64 << 10
STARTUP_BYTES = 32 << 20


class _TransportFailure(ConnectionError):
    pass


class _HelperTimeout(TimeoutError):
    pass


class _AdmissionDenied(MemoryError):
    pass


class _ProtocolFailure(ValueError):
    pass


def enabled():
    value = os.environ.get('ZTSA_EXPERIMENTAL_DISK_SCANNER', '')
    if value not in ('', 'process', 'parallel'):
        raise ValueError('ZTSA_EXPERIMENTAL_DISK_SCANNER must be unset, empty, process, or parallel')
    return value in ('process', 'parallel')


class ScanProcess:
    def __init__(self, root, baselines, *, resources=None, should_stop=lambda: False, timeout=30):
        self.root = Path(root).resolve()
        self.scan_threads = 4 if os.environ.get('ZTSA_EXPERIMENTAL_DISK_SCANNER') == 'parallel' else 1
        self.baselines = dict(baselines)
        if (type(timeout) not in (int, float) or not 0 < timeout <= 30 or
            any(not isinstance(path, str) or not Path(path).is_absolute() or
                type(size) is not int or size < 0 for path, size in self.baselines.items())):
            raise ValueError('Invalid bounded disk scan configuration')
        self.resources, self.should_stop, self.timeout = resources, should_stop, float(timeout)
        self.request_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.termination_lock = threading.RLock()
        self.process = self.executor = None
        self.closed = self.busy = False
        self.binding = uuid.uuid4().hex
        self.identifier = 0
        self._startup_ticket = None
        self.reference_only_reason = None
        self.execution, self.fallback_reason = 'not_started', None

    def snapshot(self):
        with self.state_lock:
            return {'pid': self.process.pid if self.process is not None else None,
                    'scan_threads': self.scan_threads,
                    'busy': self.busy, 'startup_admission_bytes': STARTUP_BYTES,
                    'execution': 'closed' if self.closed else self.execution,
                    'fallback_reason': self.fallback_reason}

    def _check(self, operation, deadline):
        # Never use operations.check/timeout/acquire here: they enter Budget again.
        if operation is not None:
            operation.check()
        from app.backend.pipeline import stopping
        if self.should_stop():
            raise stopping.Paused()
        if self.closed:
            raise stopping.Paused('Disk scanner is closed')
        if time.monotonic() >= deadline:
            raise _HelperTimeout('Disk scan helper deadline exceeded')

    def _wait(self, future, operation, deadline):
        while True:
            self._check(operation, deadline)
            try:
                value = future.result(timeout=.05)
            except FutureTimeout:
                if future.done():
                    try:
                        value = future.result()
                    except (OSError, ValueError, RuntimeError) as error:
                        raise _TransportFailure('Disk scanner pipe failed') from error
                    self._check(operation, deadline)
                    return value
                continue
            except (OSError, ValueError, RuntimeError) as error:
                raise _TransportFailure('Disk scanner pipe failed') from error
            self._check(operation, deadline)
            return value

    def _exchange(self, request, operation, deadline):
        raw = json.dumps(request, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        if len(raw) > MAX_MESSAGE:
            raise _ProtocolFailure('Disk scan descriptor exceeds 64 KiB')
        with self.state_lock:
            process, executor = self.process, self.executor
        if process is None or executor is None:
            raise _TransportFailure('Disk scanner is unavailable')
        def send():
            process.stdin.write(raw)
            process.stdin.flush()
        try:
            self._wait(executor.submit(send), operation, deadline)
            response = self._wait(executor.submit(process.stdout.readline, MAX_MESSAGE + 1), operation, deadline)
        except RuntimeError as error:
            raise _TransportFailure('Disk scanner reader is closed') from error
        if not response:
            raise _TransportFailure('Disk scanner exited without a reply')
        if len(response) > MAX_MESSAGE or not response.endswith(b'\n'):
            raise _ProtocolFailure('Disk scanner reply exceeds its bounded frame')
        try:
            value = json.loads(response)
        except (ValueError, UnicodeError) as error:
            raise _ProtocolFailure('Disk scanner reply is malformed') from error
        if (not isinstance(value, dict) or value.get('binding') != self.binding or
            type(value.get('id')) is not int or value['id'] != request['id']):
            raise _ProtocolFailure('Disk scanner reply identity mismatch')
        return value

    def _start(self, operation, deadline):
        with self.state_lock:
            if self.process is not None and self.process.poll() is None:
                return
        self._abort()
        self._check(operation, deadline)
        ticket = 'disk-scan-start-' + self.binding
        reserved = initialized = False
        try:
            if self.resources is not None:
                reserved = self.resources.reserve(ticket, STARTUP_BYTES)
                if not reserved:
                    self.resources.forget_blocked(ticket)
                    raise _AdmissionDenied('Disk scanner startup was not admitted')
                with self.state_lock:
                    self._startup_ticket = ticket
            else:
                from .resource_control import process_memory, CAP_BYTES, HEADROOM_BYTES
                state = process_memory()
                private, commit = state.get('private_bytes'), state.get('commit_available_bytes')
                if (private is None or private + STARTUP_BYTES + HEADROOM_BYTES > CAP_BYTES or
                    commit is not None and commit < STARTUP_BYTES + HEADROOM_BYTES):
                    raise _AdmissionDenied('Disk scanner startup exceeds the campaign allowance')
            self._check(operation, deadline)
            from .processes import spawn
            root = Path(__file__).resolve().parents[4]
            with self.termination_lock:
                with self.state_lock:
                    # Passive checks only while cancellation's cleanup locks are held.
                    if self.closed or operation is not None and (operation.cancelled.is_set() or time.monotonic() >= operation.deadline):
                        cancelled = True
                    else:
                        cancelled = False
                        self.process = spawn([sys.executable, '-B', '-u', '-m',
                            'app.backend.pipeline.acceleration.disk_scan_worker'], cwd=root,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                            env={**os.environ, 'PYTHONPATH': str(root), 'ZTSA_COMPUTE_GPU': '0',
                                 'CUDA_VISIBLE_DEVICES': '-1'})
                        self.executor = ThreadPoolExecutor(1, thread_name_prefix='disk-scan-pipe')
                        self.execution, self.fallback_reason = 'process', None
                        from .metrics import current
                        current.count('disk_scan_worker_starts')
            self._check(operation, deadline)
            if cancelled:
                raise operations.DeadlineExpired('Disk scanner startup was cancelled')
            response = self._exchange({'operation': 'initialize', 'id': 0, 'binding': self.binding,
                'root': str(self.root), 'baselines': self.baselines,
                **({'threads': self.scan_threads} if self.scan_threads != 1 else {})}, operation, deadline)
            if response != {'id': 0, 'binding': self.binding, 'ready': True}:
                raise _ProtocolFailure('Disk scanner initialization failed')
            if self.resources is not None:
                self.resources.refresh(force=True)
            initialized = True
        finally:
            with self.state_lock:
                no_process = self.process is None
            if reserved and (initialized or no_process):
                self._release_startup()

    @staticmethod
    def _worker_error(value):
        if not isinstance(value, dict) or not isinstance(value.get('message'), str):
            return _ProtocolFailure('Invalid disk scanner error')
        classes = {'OSError': OSError, 'PermissionError': PermissionError,
                   'MemoryError': MemoryError, 'ValueError': ValueError, 'TypeError': TypeError}
        kind = classes.get(value.get('type'))
        if kind is None:
            return RuntimeError('Unknown disk scanner failure: ' + value['message'])
        return kind(value['message'])

    @contextmanager
    def _resource(self, operation):
        if operation is not None:
            with operation.resource(self._abort):
                yield
        else:
            try:
                yield
            except BaseException:
                self._abort()
                raise

    def scan(self):
        operation, deadline = operations.current(), time.monotonic() + self.timeout
        while True:
            self._check(operation, deadline)
            if self.request_lock.acquire(timeout=.05):
                break
        try:
            self._check(operation, deadline)
            with self.state_lock:
                self.busy = True
            failure = fallback = None
            # Only the request owner registers cleanup. Waiting callers cannot
            # cancel somebody else's active scan. Expected failures leave this
            # resource scope normally so reference fallback retains the Operation.
            with self._resource(operation):
                try:
                    with self.state_lock:
                        reference_only = self.reference_only_reason
                    if reference_only is not None:
                        fallback = reference_only
                        from .metrics import current
                        current.count('disk_scan_reference_only_calls')
                    else:
                        self._start(operation, deadline)
                        self.identifier += 1
                        response = self._exchange({'operation': 'scan', 'id': self.identifier,
                            'binding': self.binding}, operation, deadline)
                        if 'error' in response:
                            failure = self._worker_error(response['error'])
                        elif type(response.get('bytes')) is not int or response['bytes'] < 0:
                            failure = _ProtocolFailure('Invalid disk scanner byte total')
                        else:
                            result = response['bytes']
                            from .metrics import current
                            current.count('disk_scan_worker_recounts')
                except _AdmissionDenied as error:
                    fallback = 'admission'
                except (_TransportFailure, _HelperTimeout) as error:
                    fallback = 'transport' if isinstance(error, _TransportFailure) else 'timeout'
                except MemoryError as error:
                    failure = error
                except _ProtocolFailure as error:
                    failure = error
                if fallback is not None or failure is not None:
                    self._abort()  # A failed termination retains ownership and raises.
                    with self.state_lock:
                        if fallback is not None:
                            self.execution, self.fallback_reason = 'reference', fallback
                            if fallback in ('transport', 'timeout'):
                                self.reference_only_reason = fallback
                        else:
                            self.execution, self.fallback_reason = 'failed', None
            if failure is not None:
                raise failure
            if fallback is not None:
                # Helper timeout is recoverable, but the caller's own deadline
                # and cancellation remain authoritative before inline fallback.
                self._check(operation, float('inf'))
                from .metrics import current
                current.count('disk_scan_reference_fallbacks')
                current.count('disk_scan_fallback_' + fallback)
                result = count_used(self.root, self.baselines)
                self._check(operation, float('inf'))
            return result
        finally:
            with self.state_lock:
                self.busy = False
            self.request_lock.release()

    def _release_startup(self):
        with self.state_lock:
            ticket = self._startup_ticket
        if ticket is not None:
            self.resources.release(ticket)
            with self.state_lock:
                if self._startup_ticket == ticket:
                    self._startup_ticket = None

    def _abort(self):
        with self.termination_lock:
            with self.state_lock:
                process, executor = self.process, self.executor
            if process is None:
                self._release_startup()
                return
            from .processes import terminate
            terminate(process, timeout=3)
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            process.stdin.close()
            process.stdout.close()
            with self.state_lock:
                self.process = self.executor = None
                self.execution, self.fallback_reason = 'not_started', None
            if self.resources is not None:
                self.resources.retired(id(process))
            self._release_startup()

    def close(self):
        with self.state_lock:
            self.closed = True
        self._abort()
