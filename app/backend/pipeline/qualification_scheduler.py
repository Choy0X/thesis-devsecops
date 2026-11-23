"""Resume only explicitly created evidence workflows while this app is running."""
from __future__ import annotations

import pathlib
import threading
import time

_SHUTTING_DOWN = threading.Event()
_ADMISSION_LOCK = threading.RLock()


def _eligible(record):
    return (bool(record) and not record.get('cancel_requested')
            and record['state'] in ('queued', 'running', 'interrupted', 'waiting')
            and (record.get('next_retry_at') or 0) <= time.time())


def _active_job(root):
    from app.backend import jobs
    from . import npm_qualification_workflow as workflow
    return next((job for job in jobs.running('qualify_npm_dataset')
                 if pathlib.Path(job.params.get('build_root') or workflow._root()).resolve() == root), None)


def ensure_job(record, *, root=None):
    from app.backend import jobs, lifecycle
    from . import npm_qualification_workflow as workflow, oslock
    admission = lifecycle.admission_token()
    if _SHUTTING_DOWN.is_set() or admission is None or not _eligible(record):
        return None
    root = pathlib.Path(root or workflow._root()).resolve()
    try:
        with _ADMISSION_LOCK:
            lock = oslock.FileLock(root / '.npm_qualification' / '.admit.lock', note=False)
            lock.acquire(wait=0)
            try:
                if _SHUTTING_DOWN.is_set() or lifecycle.admission_token() != admission:
                    return None
                current = workflow.get(record['id'], root=root)
                if not _eligible(current):
                    return None
                existing = _active_job(root)
                if existing:
                    return existing if existing.params.get('workflow_id') == current['id'] else None
                if oslock.held_elsewhere(workflow.lock_path(root)):
                    return None
                return jobs.start('qualify_npm_dataset', {'workflow_id': current['id'], 'build_root': str(root)})
            finally:
                lock.release()
    except (jobs.Blocked, jobs.TooManyJobs, oslock.Busy):
        # ignored: durable requests remain queued while policy, capacity, or a
        # different admission owner prevents starting a worker on this tick.
        return None


def tick(*, root=None):
    from app.backend import lifecycle
    from . import npm_qualification_workflow as workflow
    root = pathlib.Path(root or workflow._root()).resolve()
    if (_SHUTTING_DOWN.is_set() or lifecycle.admission_token() is None
            or not workflow.database_path(root).is_file() or _active_job(root) is not None):
        return []
    offset = 0
    while not _SHUTTING_DOWN.is_set():
        rows = workflow.recover_pending(root=root, limit=100, offset=offset)
        if not rows['items']:
            return []
        for record in rows['items']:
            job = ensure_job(record, root=root)
            if job is not None:
                return [job.id]  # One logical evidence workflow for this root.
        offset += len(rows['items'])
        if offset >= rows['total']:
            return []
    return []


def start():
    _SHUTTING_DOWN.clear()
    stop = threading.Event()
    def watch():
        while not stop.is_set():
            try:
                tick()
            except Exception as error:
                print('Evidence workflow reconciliation unavailable: ' + type(error).__name__ + ': ' + str(error)[:300], flush=True)
            stop.wait(30)
    worker = threading.Thread(target=watch, name='npm-qualification-recovery', daemon=True)
    worker.start()
    def close():
        _SHUTTING_DOWN.set()
        stop.set()
        # Drain any start already admitted before shutdown. Future direct HTTP
        # calls also see the closing flag, not just this scheduler's thread.
        with _ADMISSION_LOCK:
            pass
        worker.join(timeout=3)
    return close
