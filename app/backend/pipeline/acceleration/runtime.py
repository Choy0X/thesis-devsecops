"""Campaign-scoped compute policy and authenticated broker for parser subprocesses."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import threading
import time
import uuid

from .metrics import current
from .numeric import overlap_cpu, signatures_cpu
from .policy import choose

_active = None


class CudaUnavailable(RuntimeError):
    """Auto can use the exact CPU reference when a GPU owner would add a wait."""


class Runtime:
    def __init__(self, policy, should_stop=lambda: False, progress=lambda **kw: None):
        self.policy, self.should_stop, self.progress = policy, should_stop, progress
        self.worker, self.lock = None, threading.RLock()
        self.benchmarks = self._benchmarks()

    def _benchmarks(self):
        from .cache import default
        try:
            data = json.loads((default().root.parent / 'calibration.json').read_text())
            from app.backend import compute
            # A calibration belongs to one exact runtime, not every machine copying its data.
            from .policy import identity
            return data['operations'] if data.get('identity') == identity() else {}
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def selected(self, operation, count, work_units=None):
        evidence = self.benchmarks.get(operation) or {}
        if self.policy.get('mode', 'auto') == 'auto':
            from app.backend import compute
            if not compute.gpu_enabled():
                return 'cpu'
        if self.policy.get('mode', 'auto') == 'auto' and work_units is not None and work_units < evidence.get('minimum_shingles', 0):
            return 'cpu'
        return choose(self.policy.get('mode', 'auto'), operation, count, self.benchmarks)

    def _worker(self):
        from app.backend import compute
        from app.backend.pipeline import stopping
        from app.backend.pipeline.gateway import client
        from .worker_client import Worker
        if self.worker and not self.worker.closed:
            return self.worker
        python = compute.gpu_python()
        if not compute.gpu_enabled() or not python:
            raise RuntimeError('CUDA required: enable GPU compute and select a CUDA Python runtime in Settings')
        while True:
            stopping.check(self.should_stop)
            status = client.status()
            if not status:
                if self.policy.get('mode', 'auto') == 'auto':
                    raise CudaUnavailable('The GPU scheduler is unavailable')
                raise RuntimeError('CUDA required: the GPU resource scheduler is unavailable')
            exclusive = status.get('exclusive')
            busy = exclusive or next(({'owner': role, 'model': slot.get('model')} for role, slot in status.get('slots', {}).items()
                                     if slot.get('in_flight') or slot.get('state') in ('loading', 'draining', 'unloading')), None)
            if not busy:
                busy = next((c for c in status.get('consumers', []) if (status.get('slots', {}).get(c['role']) or {}).get('gpu_bytes')), None)
            if busy:
                if self.policy.get('mode', 'auto') == 'auto':
                    raise CudaUnavailable('GPU is being used by ' + str(busy.get('owner', 'another service')))
                self.progress(state='waiting_gpu', label='GPU is being used by ' + str(busy.get('owner', 'another service')),
                              gpu_owner=busy.get('owner'))
                current.count('gpu_wait_seconds', 1)
                time.sleep(1)
                continue
            try:
                self.worker = Worker(python)
                return self.worker
            except Exception as error:
                if getattr(error, 'code', '') in ('gpu_exclusive', 'busy'):
                    continue
                raise

    def execute(self, operation, data):
        with self.lock:
            count = len(data.get('rows', data.get('pairs', data.get('left', []))))
            work_units = sum(len(row) for row in data['rows']) if operation == 'minhash' else None
            if self.selected(operation, count, work_units) == 'cuda':
                while True:
                    try:
                        self.progress(state='cuda', label='CUDA: ' + operation, processed=count)
                        if operation == 'minhash':
                            import numpy as np
                            return self._worker().signatures([np.asarray(row, dtype=np.uint64) for row in data['rows']],
                                np.asarray(data['a'], dtype=np.uint64), np.asarray(data['b'], dtype=np.uint64),
                                powers=np.asarray(data['powers'], dtype=np.uint64) if data.get('powers') is not None else None)
                        return self._worker().request({'operation': operation, **data})
                    except CudaUnavailable as error:
                        current.count('auto_cpu_resource_fallbacks')
                        self.progress(state='cpu', label=str(error) + '; using the CPU reference')
                        break
                    except RuntimeError as error:
                        if 'OutOfMemoryError' not in str(error):
                            raise
                        if self.policy.get('mode', 'auto') == 'auto':
                            current.count('auto_cpu_resource_fallbacks')
                            self.progress(state='cpu', label='CUDA memory pressure; using the CPU reference')
                            break
                        from app.backend.pipeline import stopping
                        stopping.check(self.should_stop)
                        self.progress(state='waiting_gpu', label='CUDA memory is full; waiting for memory to become available')
                        current.count('gpu_wait_seconds', 1)
                        time.sleep(1)
            if operation == 'overlap':
                return overlap_cpu(data['pairs'])
            import numpy as np
            if operation == 'compare':
                return np.sum(np.asarray(data['left']) == np.asarray(data['right']), axis=1).tolist()
            return signatures_cpu([np.asarray(r, dtype=np.uint64) for r in data['rows']],
                                  np.asarray(data['a'], dtype=np.uint64), np.asarray(data['b'], dtype=np.uint64),
                                  powers=np.asarray(data['powers'], dtype=np.uint64) if data.get('powers') is not None else None)

    def close(self):
        if self.worker:
            self.worker.close()


@contextmanager
def session(policy=None, *, should_stop=lambda: False, progress=lambda **kw: None, campaign_id=None, on_update=lambda state: None):
    global _active
    previous, old_env = _active, os.environ.get('ZTSA_ACCEL_BROKER')
    if campaign_id is not None:
        from .campaign_session import CampaignRuntime
        runtime = CampaignRuntime(policy, should_stop, progress, campaign_id, on_update)
    else:
        runtime = Runtime(policy or {'mode': 'auto'}, should_stop, progress)
    runtime.batcher = None
    if runtime.policy.get('mode') == 'cuda' and (runtime.policy.get('mining') or {}).get('version') in (3, 4):
        from .numeric_batcher import Batcher
        runtime.batcher = Batcher(runtime.execute)
    token = uuid.uuid4().hex
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 32 << 20:
                    raise ValueError('Invalid compute payload size')
                request = json.loads(self.rfile.read(length))
                execute = runtime.batcher.submit if runtime.batcher else runtime.execute
                value = execute(request['operation'], request['data'])
                result = {'result': value.tolist() if hasattr(value, 'tolist') else value}
            except Exception as error:
                from app.backend.pipeline import stopping
                required = runtime.policy.get('version') == 2 and runtime.policy.get('mode') == 'cuda'
                result = {'error': type(error).__name__ + ': ' + str(error)[:500], 'paused': required or isinstance(error, stopping.Paused)}
            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
    need_broker = runtime.policy.get('mode') == 'cuda' or runtime.selected('overlap', 10**9) == 'cuda'
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler) if need_broker else None
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.05), daemon=True) if server else None
    if thread:
        thread.start()
    _active = runtime
    if server:
        os.environ['ZTSA_ACCEL_BROKER'] = json.dumps([server.server_port, token])
    else:
        os.environ.pop('ZTSA_ACCEL_BROKER', None)
    try:
        yield runtime
    finally:
        cleanup_error = None
        if server:
            server.shutdown()
        if runtime.batcher:
            try:runtime.batcher.close()
            except Exception as error:cleanup_error=error
        if server:
            server.server_close()
            thread.join(timeout=5)
        try:
            runtime.close()
        finally:
            _active = previous
            if old_env is None:
                os.environ.pop('ZTSA_ACCEL_BROKER', None)
            else:
                os.environ['ZTSA_ACCEL_BROKER'] = old_env
            from .git_objects import close_all
            close_all()
            if cleanup_error:raise cleanup_error


def _execute(operation, data):
    if _active:
        batcher=getattr(_active,'batcher',None)
        return batcher.submit(operation,data) if batcher else _active.execute(operation,data)
    broker = os.environ.get('ZTSA_ACCEL_BROKER')
    if broker:
        import requests
        try:
            port, token = json.loads(broker)
            response = requests.post(f'http://127.0.0.1:{int(port)}/',
                headers={'Authorization': 'Bearer ' + token}, json={'operation': operation,
                    'data': json.loads(json.dumps(data, default=lambda value: value.tolist()))}, timeout=(5, 3600))
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError) as error:
            from .campaign_session import CudaPaused
            raise CudaPaused('CUDA broker request failed: ' + str(error), 'cuda_broker') from error
        if result.get('error'):
            if result.get('paused'):
                from .campaign_session import CudaPaused
                raise CudaPaused(result['error'])
            raise RuntimeError(result['error'])
        return result['result']
    return None


def overlaps(pairs):
    result = _execute('overlap', {'pairs': pairs})
    return overlap_cpu(pairs) if result is None else result


def current_policy():
    return _active.policy if _active else {}


def accelerates(operation, count):
    return _active is not None and _active.selected(operation, count) == 'cuda'


def signatures(rows, a, b, powers=None):
    import numpy as np
    def calculate():
        result = _execute('minhash', {'rows': rows, 'a': a, 'b': b, **({'powers': powers} if powers is not None else {})})
        return signatures_cpu(rows, a, b, powers=powers) if result is None else np.asarray(result, dtype=np.uint64).reshape(len(rows), len(a))
    if _active is None and not os.environ.get('ZTSA_ACCEL_BROKER'):
        return calculate()
    import hashlib
    from pathlib import Path
    from .cache import default
    from .wire import pack, unpack
    digest = hashlib.sha256(b'numeric-minhash-v2')
    for name in ('numeric.py', 'cuda_kernels.py', 'wire.py', 'worker.py'):
        digest.update((Path(__file__).parent / name).read_bytes())
    digest.update(b'no-powers' if powers is None else b'with-powers')
    for value in [a, b, powers if powers is not None else np.empty(0, dtype=np.uint64), *rows]:
        array = np.asarray(value, dtype='<u8')
        digest.update(len(array).to_bytes(8, 'little'))
        digest.update(array.tobytes())
    value = default().get_or_compute('numeric-minhash-v2', digest.hexdigest(), lambda: pack(calculate()))
    return unpack(value).reshape(len(rows), len(a))


def comparisons(left, right):
    import numpy as np
    if not len(left):
        return []
    result = _execute('compare', {'left': np.asarray(left).tolist(), 'right': np.asarray(right).tolist()})
    return np.sum(np.asarray(left) == np.asarray(right), axis=1).tolist() if result is None else result


def batch_size(operation):
    return max(128, min(8192, int((_active.benchmarks.get(operation) or {}).get('minimum_items', 128)))) if _active else 128
