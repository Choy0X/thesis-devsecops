"""Optional one-process publication lane with bounded descriptor-only IPC."""
import atexit
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import threading
import time

from . import operations
from .metrics import current
from .pipeline_spool import DiskPressure, StaleAttempt, encoded

MAX_MESSAGE = 64 << 10


def enabled():
    value = os.environ.get('ZTSA_EXPERIMENTAL_PUBLICATION_PROCESS', '')
    if value not in ('', '0', '1'):
        raise ValueError('ZTSA_EXPERIMENTAL_PUBLICATION_PROCESS must be unset, 0, or 1')
    return value == '1'


def batch_size():
    value = os.environ.get('ZTSA_EXPERIMENTAL_PUBLICATION_BATCH', '')
    if value not in ('', '1', '8'):
        raise ValueError('ZTSA_EXPERIMENTAL_PUBLICATION_BATCH must be unset, 1, or 8')
    if value == '8' and not enabled():
        raise ValueError('Publication batches require ZTSA_EXPERIMENTAL_PUBLICATION_PROCESS=1')
    return 8 if value == '8' else 1


class PublicationProcess:
    def __init__(self, publisher, spool, *, resources=None, should_stop=lambda: False):
        self.batch_size = batch_size()
        self.publisher, self.spool = publisher, spool
        self.resources, self.should_stop = resources, should_stop
        self.request_lock, self.state_lock = threading.RLock(), threading.RLock()
        self.termination_lock = threading.RLock()
        self.process = self.executor = None
        self.ready = {}
        self.busy = self.closed = False
        self._deadline = float('inf')
        self._batch_operation = None
        self._batch = None
        # Keep a cleanup owner before starting the first owned thread. Failed
        # cleanup retains this atexit binding for a visible retry.
        atexit.register(self.close)
        try:
            if self.batch_size > 1:
                from .publication_batch import OrderedBatch
                self._batch = OrderedBatch(self._send_batch, abort=self._abort_batch, max_items=self.batch_size)
            if resources is not None:
                resources.publication_process = self
                resources.trimmers.append(self.trim)
        except BaseException:
            self.close()
            raise

    @property
    def worker_pid(self):
        with self.state_lock:
            return self.process.pid if self.process is not None else None

    def snapshot(self):
        with self.state_lock:
            state = {'pid': self.process.pid if self.process is not None else None,
                     'busy': self.busy, 'startup_admission_bytes': self.admission_cost()}
            if self.batch_size > 1:
                state.update(batch_size=self.batch_size, batch_collect_milliseconds=20,
                             batch_input_limit_bytes=32 << 20)
            return state

    def admission_cost(self):
        with self.state_lock:
            from .memory_profile import current as memory_profile
            return (32 << 20) if self.process is not None and self.process.poll() is None else memory_profile().publish_ticket

    @staticmethod
    def _raise(error):
        from app.backend.pipeline import stopping
        classes = {'StaleAttempt': StaleAttempt, 'DiskPressure': DiskPressure, 'Paused': stopping.Paused,
                   'DeadlineExpired': operations.DeadlineExpired, 'MemoryError': MemoryError,
                   'ValueError': ValueError, 'TypeError': TypeError, 'OSError': OSError,
                   'PermissionError': PermissionError, 'ConnectionError': ConnectionError}
        classes.update({name: getattr(sqlite3, name) for name in ('Error', 'DatabaseError', 'OperationalError', 'IntegrityError', 'ProgrammingError', 'DataError', 'NotSupportedError', 'InterfaceError', 'InternalError')})
        failure = classes.get(error.get('type'), RuntimeError)(error.get('message', 'Publication worker failed'))
        if isinstance(failure, sqlite3.Error):
            for key in ('sqlite_errorcode', 'sqlite_errorname'):
                if key in error:
                    setattr(failure, key, error[key])
        if 'reason' in error:
            failure.reason = error['reason']
        raise failure

    def _send(self, message):
        raw = encoded(message) + b'\n'
        if len(raw) > MAX_MESSAGE:
            raise ValueError('Publication descriptor exceeds 64 KiB')
        operations.check()
        with self.state_lock:
            process = self.process
        if process is None:
            operations.check()
            raise EOFError('Publication worker is unavailable')
        try:
            process.stdin.write(raw)
            process.stdin.flush()
        except (OSError, ValueError) as error:
            operations.check()
            raise EOFError('Publication worker input pipe closed') from error

    def _receive(self):
        with self.state_lock:
            process, executor = self.process, self.executor
        if process is None or executor is None:
            operations.check()
            raise EOFError('Publication worker is unavailable')
        try:
            future = executor.submit(process.stdout.readline, MAX_MESSAGE + 1)
        except RuntimeError as error:
            operations.check()
            raise EOFError('Publication worker reader closed') from error
        while True:
            operations.check()
            from app.backend.pipeline import stopping
            stopping.check(self.should_stop)
            if time.monotonic() >= self._deadline:
                raise operations.DeadlineExpired('Publication worker deadline exceeded')
            try:
                raw = future.result(timeout=.1)
                break
            except TimeoutError:
                continue
            except (OSError, ValueError) as error:
                operations.check()
                raise EOFError('Publication worker output pipe closed') from error
        operations.check()
        if not raw or len(raw) > MAX_MESSAGE:
            raise EOFError('Publication worker exited or exceeded bounded response size')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('Invalid publication worker response')
        if 'error' in result:
            self._raise(result['error'])
        return result

    def _start(self):
        try:
            self._start_admitted()
        finally:
            ticket = getattr(self, "_startup_ticket", None)
            if ticket is not None:
                self.resources.release(ticket)
                self._startup_ticket = None

    def _start_admitted(self):
        with self.state_lock:
            process = self.process
        if process is not None and process.poll() is None:
            return
        self._abort()
        if self.closed:
            from app.backend.pipeline import stopping
            raise stopping.Paused('Publication process is closed')
        from .resource_control import CAP_BYTES, HEADROOM_BYTES, process_memory, AdmissionDeferred
        from .memory_profile import current as memory_profile
        need = memory_profile().publish_ticket
        if self.resources is not None:
            state = self.resources.refresh(force=True)
            operation = operations.current()
            ticket = getattr(operation, 'resource_ticket', None)
            prepaid = self.resources.tickets.get(ticket, 0)
            if prepaid < need:
                ticket = 'publication-start-' + str(id(self))
                if not self.resources.reserve(ticket, need):
                    self.resources.forget_blocked(ticket)
                    current.count('publication_worker_startup_denied')
                    raise AdmissionDeferred('Publication process startup was not admitted',
                        resource='publication_process',required_bytes=need)
                self._startup_ticket = ticket
        else:
            state = process_memory()
            private = state.get('private_bytes')
            commit = state.get('commit_available_bytes')
            if private is None or private + need + HEADROOM_BYTES > CAP_BYTES or (commit is not None and commit < need + HEADROOM_BYTES):
                raise MemoryError('Publication process startup exceeds campaign memory allowance')
        root = Path(__file__).resolve().parents[4]
        database = self.publisher.db
        from app.backend.pipeline import campaign_ctx
        from app.backend import paths
        from .processes import spawn
        started = time.perf_counter()
        operations.check()
        operation = operations.current()
        with self.termination_lock, self.state_lock:
            # Cancellation callbacks take termination_lock. Inspect only passive
            # state here so Operation.cancel cannot invert its own cancel_guard.
            cancelled = operation is not None and (operation.cancelled.is_set() or time.monotonic() >= operation.deadline)
            closed = self.closed
            if not cancelled and not closed:
                self.process = spawn([sys.executable, '-u', '-m', 'app.backend.pipeline.acceleration.publication_worker'],
                    cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                    env={**os.environ, 'PYTHONPATH': str(root), 'ZTSA_COMPUTE_GPU': '0', 'CUDA_VISIBLE_DEVICES': '-1',
                         **{key: '1' for key in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'BLIS_NUM_THREADS', 'NUMEXPR_NUM_THREADS')}})
                self.executor = ThreadPoolExecutor(1, thread_name_prefix='publication-reader')
        if cancelled:
            raise operations.DeadlineExpired('Publication startup was cancelled or exceeded its deadline')
        if closed:
            from app.backend.pipeline import stopping
            raise stopping.Paused('Publication process is closed')
        self._send({'operation': 'initialize', 'database_path': str(database.path), 'database_name': database.name,
            'spool_root': str(self.spool.root), 'scope': self.publisher.scope, 'attempt': self.publisher.attempt,
            'generation': self.publisher.generation, 'campaign_id': campaign_ctx.current(),
            'interim_root': str(paths.interim_dir().resolve())})
        self.ready = self._receive()
        if not self.ready.get('ready'):
            raise RuntimeError('Publication worker did not initialize')
        current.count('publication_worker_starts')
        current.count('publication_worker_startup_seconds', time.perf_counter() - started)
        if self.resources is not None:
            self.resources.refresh(force=True)

    def register(self, task):
        if self._batch is not None and task.stage == 'publish':
            self._batch.register(task.operation.identifier)

    def skip(self, task):
        if self._batch is not None and task.stage == 'publish':
            self._batch.skip(task.operation.identifier)

    def _publish_coalesced(self, row, task):
        from contextvars import copy_context
        from concurrent.futures import CancelledError
        from app.backend.pipeline import stopping
        self.spool.check(task.epoch)
        previous = self.publisher.receipt(task.publication_id)
        if previous is not None:
            return previous, {}
        path = self.spool.artifact_path(task.key, 'context')
        if path is None:
            raise ValueError('Publication context artifact is missing')
        message = {'task': task.key, 'epoch': task.epoch, 'publication_id': task.publication_id,
            'input_sha256': hashlib.sha256(encoded(row)).hexdigest(),
            'context_sha256': path.stem, 'deadline': task.operation.deadline}
        token = task.operation.identifier
        with operations.resource(lambda: self._batch.cancel(token)):
            future = self._batch.submit(token, message, size=path.stat().st_size,
                context={'task': task, 'context': copy_context()})
            while True:
                operations.check()
                stopping.check(self.should_stop)
                try:
                    result = future.result(timeout=.1)
                    break
                except TimeoutError:
                    if future.done():
                        raise
                except CancelledError as error:
                    task.operation.check()
                    raise ConnectionError('Queued publication was cancelled before dispatch') from error
            if 'error' in result:
                self._raise(result['error'])
            receipt, delta = result.get('receipt'), result.get('delta')
            if not isinstance(receipt, dict) or receipt.get('publication_id') != task.publication_id or not receipt.get('receipt') or not isinstance(delta, dict):
                raise ValueError('Invalid publication worker receipt or counts')
            current.count('publication_writer_completed')
            return receipt, delta

    def _abort_batch(self):
        # Cancellation must also prevent a child being created after an abort
        # which found no process during the cold-start handoff.
        with self.state_lock:
            if self._batch_operation is not None:
                self._batch_operation.cancelled.set()
        self._abort()

    def _send_batch(self, requests):
        return requests[0].context['context'].run(self._send_batch_active, requests)

    def _send_batch_active(self, requests):
        # Exactly one ordered sender owns this worker. Each peer retains its
        # scheduler ticket until its future resolves after confirmed completion.
        with operations.acquire(self.request_lock):
            messages = [request.descriptor for request in requests]
            operation = operations.Operation('publication-batch', 600, self.should_stop)
            operation.deadline = min(message['deadline'] for message in messages)
            operation.resource_ticket = getattr(requests[0].context['task'].operation, 'resource_ticket', None)
            self._deadline = operation.deadline
            with self.state_lock:
                self._batch_operation = operation
                if self.closed or any(request.context['task'].operation.cancelled.is_set() for request in requests):
                    operation.cancelled.set()
                self.busy = True
            try:
                with operations.use(operation), operations.resource(self._abort):
                    self._start()
                    self._send({'operation': 'publish_batch', 'items': messages})
                    current.count('publication_worker_requests')
                    current.count('publication_batch_requests')
                    current.count('publication_batch_items', len(messages))
                    result = self._receive()
                results = result.get('results')
                if not isinstance(results, list) or len(results) != len(messages) or any(
                        not isinstance(value, dict) or value.get('task') != message['task']
                        for value, message in zip(results, messages)):
                    raise ValueError('Invalid ordered publication batch response')
                return results
            except (EOFError, BrokenPipeError, ConnectionError, json.JSONDecodeError, operations.DeadlineExpired) as error:
                self._abort()
                results = []
                for request, message in zip(requests, messages):
                    previous = self.publisher.receipt(message['publication_id'])
                    if previous is not None:
                        current.count('publication_receipt_recoveries')
                        current.count('publication_outcome_counts_authoritative')
                        results.append({'task': message['task'], 'receipt': previous, 'delta': {}})
                    else:
                        owner = request.context['task'].operation
                        expired = isinstance(error, operations.DeadlineExpired) and (
                            owner.cancelled.is_set() or time.monotonic() >= owner.deadline)
                        results.append({'task': message['task'], 'error': {
                            'type': 'DeadlineExpired' if expired else 'ConnectionError',
                            'message': ('Publication item deadline exceeded' if expired else
                                'Publication batch interrupted; retry its durable checkpoint')}})
                return results
            finally:
                with self.state_lock:
                    self.busy = False
                with self.state_lock:
                    if self._batch_operation is operation:
                        self._batch_operation = None
                operation.finish()


    def publish(self, row, task):
        current.count('publication_writer_queued')
        if self._batch is not None:
            return self._publish_coalesced(row, task)
        with operations.acquire(self.request_lock):
            self.spool.check(task.epoch)
            previous = self.publisher.receipt(task.publication_id)
            if previous is not None:
                return previous, {}
            path = self.spool.artifact_path(task.key, 'context')
            if path is None:
                raise ValueError('Publication context artifact is missing')
            message = {'operation': 'publish', 'task': task.key, 'epoch': task.epoch,
                'publication_id': task.publication_id, 'input_sha256': hashlib.sha256(encoded(row)).hexdigest(),
                'context_sha256': path.stem, 'deadline': task.operation.deadline}
            self._deadline = task.operation.deadline
            with self.state_lock:
                self.busy = True
            try:
                with operations.resource(self._abort):
                    self._start()
                    self._send(message)
                    current.count('publication_worker_requests')
                    result = self._receive()
                receipt, delta = result.get('receipt'), result.get('delta')
                if not isinstance(receipt, dict) or receipt.get('publication_id') != task.publication_id or not receipt.get('receipt') or not isinstance(delta, dict):
                    raise ValueError('Invalid publication worker receipt or counts')
                current.count('publication_writer_completed')
                return receipt, delta
            except (EOFError, BrokenPipeError, ConnectionError, json.JSONDecodeError) as error:
                self._abort()
                previous = self.publisher.receipt(task.publication_id)
                if previous is not None:
                    current.count('publication_receipt_recoveries')
                    current.count('publication_outcome_counts_authoritative')
                    return previous, {}
                raise ConnectionError('Publication worker connection failed; retry from its durable checkpoint') from error
            finally:
                with self.state_lock:
                    self.busy = False

    def _abort(self):
        # This lock is independent of request_lock: cancellation can interrupt a
        # request that is waiting for its reply without waiting on that request.
        with self.termination_lock:
            with self.state_lock:
                process, executor = self.process, self.executor
            if process is None:
                return
            from .processes import terminate
            terminate(process)
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            process.stdin.close()
            process.stdout.close()
            with self.state_lock:
                self.process = self.executor = None
            if self.resources is not None:self.resources.retired(id(process))

    def trim(self, budget):
        if budget is None:
            return
        if self.resources is not None:
            with self.state_lock:worker=id(self.process)
            if not self.resources.reclaim_needed(budget,worker=worker):return False
        # Never hold state_lock while acquiring termination_lock. The request
        # lock prevents an idle child becoming active during its retirement.
        if not self.request_lock.acquire(blocking=False):
            return
        try:
            with self.state_lock:
                if self.busy or self.process is None:
                    return
            if self.resources is not None:
                self.resources.refresh(force=True)
                if not self.resources.reclaim_needed(budget,worker=worker):return False
            self._abort()
            return True
        finally:
            self.request_lock.release()

    def close(self):
        self.closed = True
        if self._batch is not None:
            self._batch.close()
        self._abort()
        atexit.unregister(self.close)
        if self.resources is not None:
            if getattr(self.resources, 'publication_process', None) is self:
                del self.resources.publication_process
            if self.trim in self.resources.trimmers:
                self.resources.trimmers.remove(self.trim)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

