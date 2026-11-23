"""One warmed numeric worker and GPU reservation for a complete campaign attempt."""
import os
import time
import uuid

from app.backend.pipeline import stopping
from .metrics import current
from .runtime import Runtime


class CudaPaused(stopping.Paused):
    def __init__(self, message, code='cuda_failure'):
        super().__init__(message)
        self.reason = {'code': code, 'detail': message, 'retryable': True}


class CampaignRuntime(Runtime):
    def __init__(self, policy, should_stop, progress, campaign_id, on_update=lambda state: None):
        self.closing = False
        from .resource_control import Governor
        self.resources = Governor() if (policy.get('mining') or {}).get('version') == 4 else None
        super().__init__(policy, lambda: self.closing or should_stop(), progress)
        self.attempt_id = uuid.uuid4().hex
        self.campaign_id, self.owner = campaign_id, None
        self.restarts, self.prepared, self.closed = 0, False, False
        self.started = time.perf_counter()
        self.times = dict.fromkeys(('queue', 'startup', 'warmup', 'processing', 'cleanup'), 0.0)
        self.processing_since = None
        self.phase = 'preparing'
        self.phase_started_at, self.queue_since, self.cleanup_error = time.time(), None, None
        self.on_update, self.last_update, self.context = on_update, 0.0, {}

    def _benchmarks(self):
        return {}  # New policies are explicit CPU/CUDA, never calibration-driven.

    def selected(self, operation, count, work_units=None):
        return self.policy['mode']

    def bind_progress(self, progress, **context):
        self.progress = progress
        self.context = context

    def publish(self, phase=None, **extra):
        now = time.perf_counter()
        if phase and phase != 'processing' and self.processing_since is not None:
            self.times['processing'] += now-self.processing_since
            self.processing_since = None
        if phase == 'processing' and self.processing_since is None:
            self.processing_since = now
        previous = self.phase
        self.phase = phase or self.phase
        if previous != self.phase:
            self.phase_started_at = time.time()
        times = {k+'_seconds': v for k, v in self.times.items()}
        times['processing_seconds'] += now-self.processing_since if self.processing_since is not None else 0
        times['queue_seconds'] += now-self.queue_since if self.queue_since is not None else 0
        times['total_seconds'] = now-self.started
        ready = getattr(self.worker, 'ready', {}) or {}
        state = {'numeric_runtime': 'cpu' if self.policy['mode'] == 'cpu' else ready.get('numeric_runtime'),
                 'numeric_device': ready.get('numeric_device'), 'version': 2, 'mode': self.policy['mode'], 'attempt_id': self.attempt_id,
                 'phase': self.phase, 'owner': self.owner, 'worker_pid': getattr(getattr(self.worker, 'process', None), 'pid', None),
                 'restarts': self.restarts, 'phase_started_at': self.phase_started_at, 'cleanup_error': self.cleanup_error,
                 **times, **self.context, **extra}
        current.execution(state)
        if previous != self.phase or now-self.last_update >= 1:
            self.last_update = now
            self.on_update(state)
        return state

    def prepare(self):
        if self.prepared:
            return
        stopping.check(self.should_stop)
        if self.policy['mode'] == 'cpu':
            self.prepared = True
            self.publish('processing')
            return
        from app.backend import compute
        from app.backend.pipeline.gateway import client
        if not compute.gpu_enabled() or not compute.gpu_python():
            raise CudaPaused('Enable GPU compute and configure a CUDA Python runtime before resuming', 'cuda_configuration')
        if self.resources:
            self.resources.wait_for(700 << 20, should_stop=self.should_stop, progress=self.progress)
        self.owner = f'campaign:{self.campaign_id}:{self.attempt_id}'
        queued = time.perf_counter()
        self.queue_since = queued
        self.publish('waiting_gpu')
        while True:
            stopping.check(self.should_stop)
            status = client.status()
            if not status:
                raise CudaPaused('The GPU scheduler is unavailable', 'cuda_configuration')
            busy = status.get('exclusive') or next(({'owner': role} for role, slot in status.get('slots', {}).items()
                if slot.get('in_flight') or slot.get('state') in ('loading', 'draining', 'unloading')), None)
            if not busy:
                busy = next((c for c in status.get('consumers', []) if (status.get('slots', {}).get(c['role']) or {}).get('gpu_bytes')), None)
            if busy:
                self.progress(state='waiting_gpu', label='Waiting for GPU owner ' + str(busy.get('owner')), gpu_owner=busy.get('owner'))
                self.publish('waiting_gpu')
                stopping.sleep(.25, self.should_stop, step=.25)
                continue
            try:
                client.begin_exclusive(self.owner, pid=os.getpid(), wait=0, timeout=10)
                break
            except Exception as error:
                if getattr(error, 'code', '') not in ('busy', 'gpu_exclusive'):
                    raise CudaPaused(str(error), 'cuda_configuration') from error
                stopping.sleep(.25, self.should_stop, step=.25)
        self.times['queue'] += time.perf_counter()-queued
        self.queue_since = None
        self._start_worker()
        self.prepared = True
        self.publish('processing')

    def _start_worker(self):
        from app.backend import compute
        from .worker_client import Worker
        self.publish('warming_cuda')
        self.progress(state='warming_cuda', label='Starting and validating CUDA kernels')
        start = time.perf_counter()
        try:
            self.worker = Worker(compute.gpu_python(), lease=False, should_stop=self.should_stop)
        except stopping.Paused:
            raise
        except Exception as error:
            raise CudaPaused('CUDA worker could not start: ' + str(error), 'cuda_warmup') from error
        finally:
            self.times['startup'] += time.perf_counter()-start
        start = time.perf_counter()
        try:
            result = self.worker.warmup()
            if not result.get('validated') or result.get('device') != 'cuda':
                raise ValueError('CUDA warmup parity or device validation failed')
        except stopping.Paused:
            raise
        except Exception as error:
            raise CudaPaused('CUDA warmup failed: ' + str(error), 'cuda_warmup') from error
        finally:
            self.times['warmup'] += time.perf_counter()-start
            self.publish('warming_cuda')

    def execute(self, operation, data):
        stopping.check(self.should_stop)
        if self.policy['mode'] == 'cpu':
            return super().execute(operation, data)
        with self.lock:
            self.prepare()
            while True:
                stopping.check(self.should_stop)
                try:
                    self.publish('processing', device='cuda', operation=operation)
                    if operation == 'minhash':
                        import numpy as np
                        return self.worker.signatures([np.asarray(row, dtype=np.uint64) for row in data['rows']],
                            np.asarray(data['a'], dtype=np.uint64), np.asarray(data['b'], dtype=np.uint64),
                            powers=np.asarray(data['powers'], dtype=np.uint64) if data.get('powers') is not None else None)
                    return self.worker.request({'operation': operation, **data})
                except stopping.Paused:
                    raise
                except (OSError, RuntimeError) as error:
                    if self.restarts >= 1:
                        raise CudaPaused('CUDA failed again; resume to retry the unfinished work: ' + str(error)) from error
                    self.worker.close()
                    self.restarts += 1
                    self._start_worker()
                    self.publish('processing')

    def close(self):
        if self.closed:
            return
        self.closing = True
        self.publish('releasing_gpu')
        start = time.perf_counter()
        try:
            if self.worker:
                self.worker.close()
                if self.worker.process.poll() is None:
                    raise RuntimeError('CUDA worker termination could not be confirmed')
            if self.owner:
                from app.backend.pipeline.gateway import client
                released = client.end_exclusive(self.owner)
                if not released:
                    status = client.status()
                    if not status or (status.get('exclusive') or {}).get('owner') == self.owner:
                        raise RuntimeError('GPU reservation release could not be confirmed')
                self.owner = None
            self.closed = True
        except Exception as error:
            self.cleanup_error = str(error)
            self.publish('cleanup_failed', cleanup_error=str(error))
            raise
        finally:
            self.times['cleanup'] += time.perf_counter()-start
            self.publish('released' if self.closed else 'cleanup_failed')
