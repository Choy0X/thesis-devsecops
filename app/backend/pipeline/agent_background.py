"""Backend-owned chat execution with a bounded, disposable disk event spool.

Disconnecting a browser closes its reader, not the task. Session transcripts and
the agent journal remain the durable history; this spool exists only while a
task or reader is active, with no unbounded in-memory event queue.
"""
from __future__ import annotations

import json
import pathlib
import os
import re
import threading
import time
import uuid
import psutil

_LOCK = threading.RLock()
_ACTIVE = {}
MAX_BYTES = 64 * 1024 * 1024
MAX_ACTIVE = 8


def lines(factory, *, root=None, cancel=lambda: None, max_bytes=MAX_BYTES):
    from app.backend import paths, sandbox_runtime
    root = pathlib.Path(root) if root is not None else paths.interim_dir() / 'agent_streams'
    root = root.resolve()
    identifier = uuid.uuid4().hex
    done, disconnected, changed = threading.Event(), threading.Event(), threading.Event()
    path = root / f'{os.getpid()}-{int(psutil.Process().create_time() * 1000000)}-{identifier}.ndjson'

    def remove():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # A reader may still hold the file on Windows; it retries on close.

    def worker():
        stream = None
        try:
            with path.open('w', encoding='utf-8', newline='\n') as output:
                written = 0
                try:
                    stream = iter(factory())
                    for event in stream:
                        line = json.dumps(event, ensure_ascii=False) + '\n'
                        size = len(line.encode('utf-8'))
                        if written + size + 1024 > max_bytes:
                            raise RuntimeError('Chat event spool reached its bounded size; the task was stopped. The saved conversation is preserved.')
                        output.write(line); output.flush()
                        written += size
                        changed.set()
                except Exception as error:
                    # Do not expose arbitrary provider exception details or keys.
                    reason = str(error) if isinstance(error, RuntimeError) and str(error).startswith('Chat event spool') else 'Chat execution failed. Inspect the saved conversation and retry.'
                    output.write(json.dumps({'type': 'error', 'message': reason}) + '\n')
                    output.flush()
                finally:
                    if stream is not None and callable(getattr(stream, 'close', None)):
                        stream.close()
        finally:
            done.set(); changed.set()
            if disconnected.is_set():
                remove()
            with _LOCK:
                _ACTIVE.pop(identifier, None)

    rejected = None
    with sandbox_runtime.creation_guard():
        with _LOCK:
            if len(_ACTIVE) >= MAX_ACTIVE:
                rejected = 'Too many chat tasks are executing. Stop one or wait for it to finish.'
            else:
                root.mkdir(parents=True, exist_ok=True)
                _prune_orphans(root)
                active_paths = {item['path'] for item in _ACTIVE.values()}
                buffered = sum(item['max_bytes'] for item in _ACTIVE.values())
                for item in root.glob('*.ndjson'):
                    if item not in active_paths:
                        try:
                            buffered += item.stat().st_size
                        except FileNotFoundError:
                            pass  # Another reader just completed cleanup.
                if buffered > MAX_ACTIVE * MAX_BYTES - max_bytes:
                    rejected = 'Chat event storage is busy. Wait for active tasks or retry cleanup after restarting the app.'
                else:
                    path.touch(exist_ok=False)
                    thread = threading.Thread(target=worker, daemon=True, name='agent-execution-' + identifier[:8])
                    _ACTIVE[identifier] = {'thread': thread, 'cancel': cancel, 'path': path, 'max_bytes': max_bytes}
                    try:
                        thread.start()
                    except RuntimeError:
                        _ACTIVE.pop(identifier, None)
                        remove()
                        rejected = 'Chat execution could not start. Retry when local resources are available.'
    if rejected:
        yield json.dumps({'type': 'error', 'message': rejected}) + '\n'
        return
    try:
        with path.open('rb') as source:
            pending = b''
            while True:
                line = source.readline()
                if line:
                    pending += line
                    if pending.endswith(b'\n'):
                        yield pending.decode('utf-8')
                        pending = b''
                    continue
                if done.is_set():
                    break
                changed.wait(.1); changed.clear()
    finally:
        disconnected.set()
        if done.is_set():
            remove()


def wait_idle(timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _LOCK:
            if not _ACTIVE:
                return True
        time.sleep(.01)
    with _LOCK:
        return not _ACTIVE


def _prune_orphans(root):
    for path in root.glob('*.ndjson'):
        match = re.fullmatch(r'(\d+)-(\d+)-[0-9a-f]{32}\.ndjson', path.name)
        if not match:
            continue
        try:
            alive = abs(psutil.Process(int(match[1])).create_time() - int(match[2]) / 1000000) < .01
        except psutil.NoSuchProcess:
            alive = False
        except psutil.Error:
            continue
        if not alive:
            path.unlink(missing_ok=True)


def stop_all(timeout=10):
    deadline = time.monotonic() + timeout
    count = 0
    while True:
        with _LOCK:
            active = list(_ACTIVE.values())
        if not active:
            return f'{count} chat executions stopped'
        count = max(count, len(active))
        for item in active:
            try:
                item['cancel']()
            except Exception:  # ignored: continue cancelling other owners; an unconfirmed exit fails below.
                pass
        if time.monotonic() >= deadline:
            raise RuntimeError(f'{len(active)} chat executions did not confirm cancellation before shutdown')
        for item in active:
            item['thread'].join(min(.05, max(0, deadline - time.monotonic())))
