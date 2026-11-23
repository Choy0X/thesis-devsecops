"""Persistent compute subprocess, owned and released through the model gateway."""
import atexit
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import json
import os
import pathlib
import subprocess
import threading
import time
import uuid

import numpy as np
from .metrics import current


class Worker:
    def __init__(self, python, *, device='cuda', lease=True, timeout=120, should_stop=lambda: False):
        self.lock, self.timeout = threading.RLock(), timeout
        self.should_stop = should_stop
        self.process, self.executor, self.owner = None, None, None
        self.closed = False
        started = time.perf_counter()
        try:
            if device == 'cuda' and lease:
                from app.backend.pipeline.gateway import client
                self.owner = 'campaign-compute:' + str(os.getpid()) + ':' + uuid.uuid4().hex[:8]
                client.begin_exclusive(self.owner, pid=os.getpid(), wait=0, timeout=10)
            root = pathlib.Path(__file__).resolve().parents[4]
            from .processes import spawn
            self.process = spawn([str(python), '-u', '-m', 'app.backend.pipeline.acceleration.worker', device],
                cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                env={**os.environ, 'PYTHONPATH': str(root), **{name:'1' for name in
                    ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','BLIS_NUM_THREADS','NUMEXPR_NUM_THREADS')}})
            self.executor = ThreadPoolExecutor(1, thread_name_prefix='cuda-worker')
            ready = self._receive()
            if not ready.get('ready'):
                raise RuntimeError('Compute worker did not initialize: ' + str(ready.get('error', 'unknown startup error'))[:700])
            from .cuda_driver import runtime_name
            expected = runtime_name(device)
            if expected == 'driver' and (ready.get('numeric_runtime') != 'driver' or ready.get('torch_imported') is not False):
                raise RuntimeError('Compute worker did not initialize the selected Torch-free driver runtime')
            self.ready = ready
            current.count('worker_startup_seconds', time.perf_counter()-started)
        except BaseException:
            self.close()
            raise
        atexit.register(self.close)

    def _receive(self):
        future = self.executor.submit(self.process.stdout.readline, (32 << 20) + 1)
        deadline = time.monotonic() + self.timeout
        try:
            from app.backend.pipeline import stopping
            while True:
                stopping.check(self.should_stop)
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                try:
                    line = future.result(timeout=min(.25, remaining))
                    break
                except TimeoutError:
                    continue
            if not line or len(line) > 32 << 20:
                raise RuntimeError('Compute worker exited or exceeded the response limit')
            return json.loads(line)
        except (TimeoutError, ValueError):
            raise RuntimeError('Compute worker failed or timed out') from None

    def request(self, message):
        warmup = message.get('operation') == 'warmup'
        with self.lock, current.timer('cuda_warmup_ipc' if warmup else 'cuda_end_to_end'):
            encoded = json.dumps(message, separators=(',', ':')).encode() + b'\n'
            if len(encoded) > 32 << 20:
                raise ValueError('Compute batch exceeds 32 MiB; use smaller batches')
            try:
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
                result = self._receive()
                if result.get('error'):
                    raise RuntimeError(result['error'])
                self.last_metrics = result.get('metrics', {})
                if not warmup and self.ready.get('device') == 'cuda':
                    current.count('cuda_items', self.last_metrics.get('items', 0))
                    current.count('cuda_ipc_bytes', len(encoded))
                    current.count('cuda_worker_seconds', self.last_metrics.get('worker_seconds', 0))
                    operation = message.get('operation', 'unknown')
                    current.count('cuda_' + operation + '_items', self.last_metrics.get('items', 0))
                    current.count('gpu_timed_requests' if self.last_metrics.get('gpu_timing_coverage') == 'complete'
                                  else 'gpu_untimed_requests')
                    for name in ('gpu_kernel_seconds', 'gpu_transfer_seconds'):
                        if name in self.last_metrics:
                            current.count(name, self.last_metrics[name])
                current.maximum('gpu_peak_allocated_bytes', self.last_metrics.get('peak_vram_bytes', 0))
                if 'packed_result' in result:
                    from .wire import unpack
                    return unpack(result['packed_result'])
                return result['result']
            except BaseException:
                self.close()
                raise

    def warmup(self):
        from app.backend.pipeline import leakage
        from .wire import pack
        return self.request({'operation': 'warmup', 'a': pack(leakage._A), 'b': pack(leakage._B), 'powers': pack(leakage._P)})

    def signatures(self, rows, a, b, powers=None):
        result = np.full((len(rows), len(a)), np.iinfo(np.uint64).max, dtype=np.uint64)
        pending, indices, size = [], [], 0
        capacity = 8 << 20
        def flush():
            nonlocal pending, indices, size
            if pending:
                result[indices] = self._signature_batch(pending, a, b, powers)
                pending, indices, size = [], [], 0
        for i, row in enumerate(rows):
            if row.nbytes > capacity:
                flush()
                width = capacity // 8
                overlap = len(powers)-1 if powers is not None else 0
                for start in range(0, len(row), width-overlap):
                    partial = self._signature_batch([row[start:start+width]], a, b, powers)[0]
                    result[i] = np.minimum(result[i], partial)
            else:
                if size + row.nbytes > capacity or len(pending) >= 4096:
                    flush()
                pending.append(row)
                indices.append(i)
                size += row.nbytes
        flush()
        return result

    def _signature_batch(self, rows, a, b, powers=None):
        from .wire import pack, pack_rows
        return np.asarray(self.request({'operation': 'minhash', 'packed_rows': pack_rows(rows),
            'a': pack(a), 'b': pack(b), **({'powers': pack(powers)} if powers is not None else {})}), dtype=np.uint64).reshape(len(rows), len(a))

    def overlaps(self, pairs):
        return self.request({'operation': 'overlap', 'pairs': pairs})

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            confirmed = False
            try:
                if self.process:
                    from .processes import terminate
                    terminate(self.process)
                confirmed = True
                if self.executor:
                    self.executor.shutdown(wait=True, cancel_futures=True)
                if self.process:
                    self.process.stdin.close()
                    self.process.stdout.close()
            finally:
                if not confirmed:
                    self.closed = False  # A failed termination must remain retryable, also with a campaign-owned lease.
                elif self.owner:
                    from app.backend.pipeline.gateway import client
                    owner, self.owner = self.owner, None
                    client.end_exclusive(owner)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
