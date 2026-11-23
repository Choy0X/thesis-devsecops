"""Small, thread-safe profiles. No GPU imports in the application process."""
from contextlib import contextmanager
from collections import deque
import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid


class Profile:
    def __init__(self):
        self.lock = threading.Lock()
        self.counts, self.timings = {}, {}
        self._execution = None
        self._pipeline = None
        self.started = time.perf_counter()
        self.operations, self.recent = {}, deque(maxlen=64)
        self._completion_times = deque()
        self.history_path = None
        self.peaks, self._stages, self.memory_profile = {}, [], None

    def start_operation(self, operation):
        with self.lock:
            self.operations[operation.identifier] = operation

    def finish_operation(self, operation):
        state = operation.state()
        with self.lock:
            self.operations.pop(operation.identifier, None)
            self.recent.append(state)
            self.counts['mining_commits_' + state['status']] = self.counts.get('mining_commits_' + state['status'],0)+1
            # Keep operation history/counters unchanged. Only explicitly tagged
            # top-level persistence confirmations feed the live completion rate.
            if state.get('kind') == 'commit':
                if state['status'] == 'done' and state.get('completion') in ('acknowledged', 'legacy'):
                    self._completion_times.append(time.monotonic())
                    self.counts['completed_commits'] = self.counts.get('completed_commits', 0)+1
                elif state['status'] == 'done' and state.get('completion') == 'reused':
                    self.counts['reused_commit_receipts'] = self.counts.get('reused_commit_receipts', 0)+1
                elif state['status'] == 'failed':
                    self.counts['failed_commits'] = self.counts.get('failed_commits', 0)+1
            self._prune_completions(time.monotonic())
            for name, seconds in state['phase_seconds'].items():
                row = self.timings.setdefault('commit_' + name, {'calls': 0, 'seconds': 0.0})
                row['calls'] += 1
                row['seconds'] += seconds
            if self.history_path is not None:
                try:
                    self.history_path.parent.mkdir(parents=True, exist_ok=True)
                    with self.history_path.open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(state, sort_keys=True)+'\n')
                except OSError as error:
                    self.counts['profile_write_failures'] = self.counts.get('profile_write_failures', 0)+1
                    logging.getLogger(__name__).warning('Commit history could not be saved: %s', error)

    def _prune_completions(self, now):
        # Called under self.lock. This independent rolling window is not capped
        # by the 64 operation-history entries and decays while the miner is idle.
        while self._completion_times and self._completion_times[0] <= now-60:
            self._completion_times.popleft()

    def count(self, name, value=1):
        with self.lock:
            self.counts[name] = self.counts.get(name, 0) + value

    def pipeline(self, state):
        with self.lock:
            self._pipeline = dict(state)

    def execution(self, state):
        with self.lock:
            self._execution = dict(state)

    def maximum(self, name, value):
        with self.lock:
            self.counts[name] = max(self.counts.get(name, 0), value)

    def merge(self, other):
        with self.lock:
            for name, value in other.get('counts', {}).items():
                self.counts[name] = self.counts.get(name, 0)+value
            for name, value in other.get('timings', {}).items():
                row = self.timings.setdefault(name, {'calls':0, 'seconds':0.0})
                row['calls'] += value['calls']
                row['seconds'] += value['seconds']

    def resume(self, path):
        self.history_path = Path(path).with_name('commit_history.jsonl')
        try:
            data = json.loads(Path(path).read_text())
            if data.get('schema') != 'campaign-performance/1':
                raise ValueError('Unknown performance schema')
        except (OSError, ValueError):
            data = {}
        with self.lock:
            self.counts = dict(data.get('counts') or {})
            legacy_items = self.counts.get('cuda_items', 0)
            if legacy_items and not any(k in self.counts for k in ('gpu_timed_requests', 'gpu_untimed_requests')):
                self.counts['gpu_historical_items_without_timing_coverage'] = legacy_items
            self.timings = dict(data.get('timings') or {})
            self.peaks = {k: dict(v) for k, v in (data.get('stage_peaks') or {}).items() if isinstance(v, dict)}
            self._execution = None
            self._pipeline = None
            self._completion_times.clear()
            self.started = time.perf_counter() - float(data.get('elapsed_seconds') or 0)

    def sample(self, path, *, stage=None):
        from app.backend import device
        data = {'at': time.time(), 'stage': stage, **self.snapshot(), 'gpu': device.gpus()}
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock, path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(data, sort_keys=True) + '\n')
        except OSError as error:
            self.count('profile_write_failures')
            logging.getLogger(__name__).warning('Resource history could not be saved: %s', error)

    def note_memory(self, private_bytes=None, rss_bytes=None, available_bytes=None):
        """Largest private memory, RSS and lowest system free memory seen while each stage ran (the stage on top of the timer stack)."""
        if private_bytes is None and rss_bytes is None and available_bytes is None:
            return
        with self.lock:
            row = self.peaks.setdefault(self._stages[-1] if self._stages else 'startup', {'private_mb': 0, 'rss_mb': 0, 'min_available_mb': None})
            if private_bytes is not None:
                row['private_mb'] = max(row['private_mb'], round(private_bytes / 1048576, 1))
            if rss_bytes is not None:
                row['rss_mb'] = max(row['rss_mb'], round(rss_bytes / 1048576, 1))
            if available_bytes is not None:
                free = round(available_bytes / 1048576, 1)
                row['min_available_mb'] = free if row['min_available_mb'] is None else min(row['min_available_mb'], free)

    def set_memory_profile(self, snapshot):
        with self.lock:
            self.memory_profile = dict(snapshot) if snapshot else None

    @contextmanager
    def timer(self, name):
        start = time.perf_counter()
        with self.lock:
            self._stages.append(name)
        try:
            yield
        finally:
            with self.lock:
                if name in self._stages:
                    self._stages.reverse(); self._stages.remove(name); self._stages.reverse()
                row = self.timings.setdefault(name, {'calls': 0, 'seconds': 0.0})
                row['calls'] += 1
                row['seconds'] += time.perf_counter() - start

    def snapshot(self):
        with self.lock:
            out = {'schema': 'campaign-performance/1', 'elapsed_seconds': time.perf_counter() - self.started,
                   'counts': dict(self.counts), 'timings': {k: dict(v) for k, v in self.timings.items()}}
            if self._pipeline is not None:
                out['pipeline'] = dict(self._pipeline)
            if self._execution is not None:
                out['execution'] = dict(self._execution)
            if self.peaks:
                out['stage_peaks'] = {k: dict(v) for k, v in self.peaks.items()}
            if self.memory_profile:
                out['memory_profile'] = dict(self.memory_profile)
            self._prune_completions(time.monotonic())
            out['recent_commits_per_minute'] = len(self._completion_times)
            out['recent_commits_window_seconds'] = 60
            operations, recent = list(self.operations.values()), list(self.recent)
        out['active_commits'] = [operation.state() for operation in operations]
        out['recent_commits'] = recent
        try:
            import psutil
            process = psutil.Process()
            out['rss_bytes'] = process.memory_info().rss
            out['system_available_bytes'] = psutil.virtual_memory().available
            out['cpu_seconds'] = sum(process.cpu_times()[:2])
        except (ImportError, OSError):
            pass
        return out

    def save(self, path):
        path = Path(path)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(self.snapshot(), sort_keys=True), encoding='utf-8')
            for attempt in range(5):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt==4:raise
                    time.sleep(.01*2**attempt)
        except OSError as error:
            self.count('profile_write_failures')
            logging.getLogger(__name__).warning('Performance profile could not be saved: %s', error)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


current = Profile()
