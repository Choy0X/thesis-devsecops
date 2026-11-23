"""Bounded, persistent Git object reads. No implicit network fetches."""
import atexit
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import os
import pathlib
import subprocess
import threading
import time
import logging

from .metrics import current
from . import operations


class GitObjects:
    def __init__(self, repository, timeout=30):
        self.timeout = timeout
        self.lock = threading.RLock()
        self.close_lock = threading.RLock()
        from .processes import spawn
        from .memory_profile import current as memory_profile
        self.process = spawn(['git', *memory_profile().git_flags, '-C', str(repository), 'cat-file', '--batch'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, 'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0'},
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.executor = ThreadPoolExecutor(1, thread_name_prefix='git-objects')
        self.closed = False

    def _read(self, ref, limit):
        p = self.process
        p.stdin.write(ref.encode('utf-8') + b'\n')
        p.stdin.flush()
        header = p.stdout.readline(4096)
        if header.endswith(b' missing\n') or header.endswith(b' ambiguous\n'):
            return None
        fields = header.rstrip(b'\n').split()
        if len(fields) != 3 or not fields[2].isdigit():
            raise OSError('Invalid Git batch response')
        size = int(fields[2])
        keep = size <= limit and fields[1] == b'blob'
        remaining, parts = size, []
        while remaining:
            chunk = p.stdout.read(min(remaining, 65536))
            if not chunk:
                raise OSError('Truncated Git batch response')
            remaining -= len(chunk)
            if keep:
                parts.append(chunk)
        if p.stdout.read(1) != b'\n':
            raise OSError('Invalid Git object terminator')
        current.count('git_blob_bytes', size)
        return b''.join(parts) if keep else None

    def read(self, revision, path, *, limit):
        from app.backend.pipeline.security_snapshots import logical_path
        path = logical_path(path)
        p = pathlib.PurePosixPath(path)
        if (not path or p.is_absolute() or '..' in p.parts or any(c in path for c in '\\\0\r\n')
                or not revision or any(c in revision for c in '\0\r\n:')):
            raise ValueError('Invalid Git object path or revision')
        with operations.acquire(self.lock), current.timer('snapshot_read'):
            if self.closed:
                raise OSError('Git object reader is closed')
            try:
                with operations.resource(self.close):
                    from .submission import submit_owned
                    future = submit_owned(self.executor,self._read,f'{revision}:{path}',limit)
                    deadline = time.monotonic()+operations.timeout(self.timeout)
                    while True:
                        operations.check()
                        left = deadline-time.monotonic()
                        if left <= 0: raise TimeoutError()
                        try: return future.result(timeout=min(.2, left))
                        except TimeoutError: continue
            except (TimeoutError, OSError, BrokenPipeError):
                self.close()
                operations.check()
                raise OSError('Git object read failed or timed out') from None
            except BaseException:
                self.close()
                raise

    def close(self):
        with self.close_lock:
            if self.closed:
                return
            from .processes import terminate
            terminate(self.process)
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.process.stdin.close()
            self.process.stdout.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


_readers = OrderedDict()
_lock = threading.RLock()
_reaper, _reaper_stop = None, threading.Event()
_cleanup_pending = []


def _evict_locked():
    now = time.monotonic()
    idle = [(key, reader) for key, reader in _readers.items() if not reader.users]
    evicted = []
    excess = max(0, len(idle)-2)
    for index, (key, reader) in enumerate(idle):
        if index < excess or now-reader.last_used >= 30:
            del _readers[key]
            evicted.append(reader)
    return evicted


def _close_readers(readers):
    with _lock:
        retry = list(_cleanup_pending)
        _cleanup_pending.clear()
    for reader in [*retry, *readers]:
        try:
            reader.close()
        except (OSError, subprocess.SubprocessError) as error:
            with _lock: _cleanup_pending.append(reader)
            current.count('git_cleanup_failures')
            logging.getLogger(__name__).warning('Git reader cleanup remains pending: %s', error)


def _reap():
    while not _reaper_stop.wait(5):
        with _lock:
            evicted = _evict_locked()
        try:
            _close_readers(evicted)
        except (OSError, subprocess.SubprocessError) as error:
            logging.getLogger(__name__).warning('Idle Git reader cleanup failed: %s', error)


def read(repository, revision, path, *, limit):
    global _reaper
    key = str(pathlib.Path(repository).resolve())
    with _lock:
        reader = _readers.pop(key, None)
        if reader is None or reader.closed:
            reader = GitObjects(key)
            reader.users, reader.last_used = 0, time.monotonic()
        _readers[key] = reader
        reader.users += 1
        evicted = _evict_locked()
        if _reaper is None or not _reaper.is_alive():
            _reaper_stop.clear()
            _reaper = threading.Thread(target=_reap, name='git-reader-reaper', daemon=True)
            _reaper.start()
    _close_readers(evicted)
    # Pin under the cache lock, then wait outside it. Another repository must
    # never wait behind two callers of a busy repository.
    try:
        with operations.acquire(reader.lock):
            return reader.read(revision, path, limit=limit)
    finally:
        with _lock:
            reader.users -= 1
            reader.last_used = time.monotonic()
            evicted = _evict_locked()
        _close_readers(evicted)


def close_all():
    global _reaper
    _reaper_stop.set()
    if _reaper is not None and _reaper is not threading.current_thread():
        _reaper.join(timeout=6)
    if _reaper is not None and _reaper.is_alive():
        raise OSError('Git reader reaper has not stopped')
    _reaper = None
    with _lock:
        readers = list(_readers.values())
        _readers.clear()
    _close_readers(readers)
    with _lock:
        if _cleanup_pending:
            raise OSError('Owned Git readers still need cleanup')


atexit.register(close_all)
