"""Opt-in bounded diff-tree pipes for two fixed metadata output grammars.

One I/O worker drains both pipes. Stderr is synchronously inspected at the
stdout boundary; there is no background stderr collector publication race.
"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import threading
import time
import uuid

from . import operations
from .processes import spawn, terminate
from .metrics import current

PRESETS = {
    'raw': ('--raw', '-z', '--no-renames'),
    'combined': ('--raw', '--numstat', '-p', '-z', '-M', '--unified=3', '--src-prefix=a/', '--dst-prefix=b/'),
}
_HEADER = re.compile(rb':([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}) ([0-9a-f]{40}) ([AMDRT])([0-9]*)\Z')
_REVISION = re.compile(r'[0-9a-f]{40}\Z')


_source_cache = OrderedDict()
_source_guard = threading.RLock()


def _environment():
    return {key: value for key, value in os.environ.items() if key.startswith('GIT_') or key in
            ('HOME', 'XDG_CONFIG_HOME', 'PATH', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'PROGRAMDATA')}


def _source_paths(repository):
    """Ask the selected Git for effective paths, including disabled/missing state.

    Discovery is bounded and cached by executable/environment identity. File
    contents are never cached: interpretation() hashes them on every request.
    """
    executable = shutil.which('git')
    if not executable:
        raise OSError('Git interpretation source executable is unavailable')
    binary = Path(executable).resolve()
    stat = binary.stat()
    identity = (str(binary), stat.st_size, stat.st_mtime_ns,
                hashlib.sha256(json.dumps(_environment(), sort_keys=True).encode()).hexdigest())
    with operations.acquire(_source_guard):
        cached = _source_cache.get(identity)
        if cached is not None:
            _source_cache.move_to_end(identity)
            return cached
        from .cache import default
        from .processes import spawn as source_spawn
        directory = (default().root / 'git-source-discovery' / uuid.uuid4().hex).resolve()
        directory.mkdir(parents=True, exist_ok=False)
        sources = {}
        try:
            # The empty directory and ceiling prevent unrelated local repository
            # settings from influencing global/system path discovery.
            environment = {**os.environ, 'GIT_CEILING_DIRECTORIES': str(directory),
                           'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0',
                           'GCM_INTERACTIVE': 'never', 'GIT_ASKPASS': '', 'SSH_ASKPASS': ''}
            for name in ('GIT_CONFIG_SYSTEM', 'GIT_CONFIG_GLOBAL', 'GIT_ATTR_SYSTEM', 'GIT_ATTR_GLOBAL'):
                process = source_spawn([str(binary), '-C', str(directory), 'var', name],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                try:
                    failure = None
                    with operations.resource(lambda: terminate(process, timeout=3)):
                        try:
                            out, error = process.communicate(timeout=operations.timeout(5))
                        except subprocess.TimeoutExpired as exc:
                            operations.check()
                            failure = OSError('Git interpretation source discovery timed out')
                            terminate(process, timeout=3)
                    if failure is not None:
                        raise failure
                    if process.returncode not in (0, 1) or error or len(out) > 16384 or (process.returncode == 1 and out):
                        raise OSError('Git interpretation source discovery is unsupported')
                    try:
                        names = out.decode('utf-8').splitlines()
                    except UnicodeError as exc:
                        raise OSError('Git interpretation source path is not UTF-8') from exc
                    values = tuple(Path(value) for value in names if value)
                    if any(not value.is_absolute() for value in values):
                        raise OSError('Git interpretation source path is not absolute')
                    sources[name] = values
                    current.count('git_diff_source_discovery_commands')
                    operations.check()
                finally:
                    terminate(process, timeout=3)
                    process.stdout.close()
                    process.stderr.close()
        finally:
            # This instance created an empty UUID directory; rmdir is deliberately
            # nonrecursive and fails visibly if anything unexpected appeared.
            directory.rmdir()
        _source_cache[identity] = sources
        while len(_source_cache) > 8:
            _source_cache.popitem(last=False)
        return sources


def interpretation(repository):
    """Restart on mutable interpretation changes; unsupported cases use the CLI."""
    overrides = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_REPLACE_REF_BASE')
    if any(os.environ.get(name) for name in overrides):
        raise OSError('Git repository or object environment override requires reference diff')
    if os.environ.get('GIT_CONFIG_PARAMETERS') or os.environ.get('GIT_CONFIG_COUNT', '0') not in ('', '0'):
        raise OSError('Injected Git configuration requires reference diff interpretation')
    root = Path(repository).resolve()
    if (root/'.git').exists() or not (root/'HEAD').is_file() or not (root/'objects').is_dir():
        raise OSError('Persistent diff requires an owned bare repository')
    sources = _source_paths(root)
    try:
        return _fingerprint(root, sources)
    except BaseException:
        with _source_guard:
            for key, cached in list(_source_cache.items()):
                if cached is sources:
                    _source_cache.pop(key, None)
        raise


def _fingerprint(root, sources):
    configs = {root/'config', root/'config.worktree', *sources['GIT_CONFIG_SYSTEM'], *sources['GIT_CONFIG_GLOBAL']}
    watched = {root/'HEAD', root/'packed-refs', root/'info/attributes',
               *sources['GIT_ATTR_SYSTEM'], *sources['GIT_ATTR_GLOBAL']}
    head = (root/'HEAD').read_text(encoding='ascii').strip()
    if head.startswith('ref: '):
        ref = head[5:]
        if not re.fullmatch(r'refs/[A-Za-z0-9/._-]+', ref) or '..' in ref:
            raise OSError('Unsupported repository HEAD reference')
        watched.add(root/ref)
    elif not _REVISION.fullmatch(head):
        raise OSError('Unsupported repository HEAD')
    blobs = []
    for path in sorted(configs, key=str):
        if not path.exists():
            blobs.append((str(path), None))
            continue
        if path.stat().st_size > 1 << 20:
            raise OSError('Git configuration exceeds interpretation bound')
        content = path.read_bytes()
        try:
            text = content.decode('utf-8-sig')
        except UnicodeError as error:
            raise OSError('Git configuration encoding requires reference diff') from error
        if re.search(r'(?im)^\s*\[\s*(?:include|includeIf)(?:\s|\]|\")|attributesfile', text):
            raise OSError('Git include or attributes configuration requires reference interpretation')
        if path == root/'config':
            try:
                config = configparser.RawConfigParser(strict=False)
                config.read_string(text)
                if not config.getboolean('core', 'bare'):
                    raise ValueError('Not bare')
            except (configparser.Error, ValueError) as error:
                raise OSError('Unsupported bare Git configuration') from error
        blobs.append((str(path), hashlib.sha256(content).hexdigest()))
    for path in sorted(watched, key=str):
        if path.exists():
            if path.stat().st_size > 4 << 20:
                raise OSError('Git reference or attributes metadata exceeds interpretation bound')
            blobs.append((str(path), hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            blobs.append((str(path), None))
    return hashlib.sha256(json.dumps([blobs, _environment()], sort_keys=True).encode()).hexdigest()


class Frame:
    """Markers are recognized outside NUL paths and only at patch line starts.

    Git quotes control characters in patch metadata; every source line has a
    space, plus or minus prefix. A bare codex-frame marker cannot be source.
    """
    def __init__(self, preset, marker, limit=64 << 20):
        if preset not in PRESETS or not re.fullmatch(rb'codex-frame-[A-Za-z0-9-]+', marker):
            raise ValueError('Unsupported diff framing request')
        self.preset, self.marker, self.limit = preset, marker + b'\n', limit
        self.data = bytearray()
        self.offset = self.paths = self.records = self.stats = self.stat_paths = 0
        self.stage, self.scan, self.patch_start = 'raw', 0, 0
        self.complete = False

    def _token(self):
        end = self.data.find(b'\0', self.offset)
        if end < 0:
            return None
        value = bytes(self.data[self.offset:end])
        self.offset = end + 1
        return value

    def _finish(self, offset):
        available = bytes(self.data[offset:])
        if len(available) < len(self.marker) and self.marker.startswith(available):
            return None
        if available != self.marker:
            raise OSError('Invalid Git diff response boundary')
        self.complete = True
        return bytes(self.data[:offset])

    def feed(self, data):
        if self.complete:
            raise OSError('Unexpected data after Git diff boundary')
        self.data.extend(data)
        if len(self.data) > self.limit:
            raise OSError('Git diff output exceeds bounded read limit')
        while True:
            if self.stage == 'raw':
                if self.paths:
                    token = self._token()
                    if token is None:
                        return None
                    if not token:
                        raise OSError('Empty Git raw path')
                    self.paths -= 1
                    continue
                if self.offset == len(self.data):
                    return None
                if self.data[self.offset] == ord(':'):
                    token = self._token()
                    if token is None:
                        return None
                    match = _HEADER.fullmatch(token)
                    if match is None:
                        raise OSError('Unsupported Git raw framing')
                    self.records += 1
                    self.paths = 2 if match[5] == b'R' else 1
                    continue
                if self.preset == 'raw' or self.records == 0:
                    return self._finish(self.offset)
                self.stage, self.stats = 'stats', self.records
            if self.stage == 'stats':
                if self.stat_paths:
                    token = self._token()
                    if token is None:
                        return None
                    if not token:
                        raise OSError('Empty Git numstat path')
                    self.stat_paths -= 1
                    if self.stat_paths == 0:
                        self.stats -= 1
                    continue
                if self.stats:
                    token = self._token()
                    if token is None:
                        return None
                    fields = token.split(b'\t', 2)
                    if len(fields) != 3 or not all(value.isdigit() or value == b'-' for value in fields[:2]):
                        raise OSError('Unsupported Git numstat framing')
                    if fields[2]:
                        self.stats -= 1
                    else:
                        self.stat_paths = 2
                    continue
                if self.offset == len(self.data):
                    return None
                if self.data[self.offset] != 0:
                    raise OSError('Git diff patch separator is missing')
                self.offset += 1
                self.stage, self.patch_start, self.scan = 'patch', self.offset, self.offset
            if self.stage == 'patch':
                while True:
                    found = self.data.find(self.marker, self.scan)
                    if found < 0:
                        self.scan = max(self.patch_start, len(self.data) - len(self.marker))
                        return None
                    self.scan = found + 1
                    if found == self.patch_start or self.data[found - 1] == ord('\n'):
                        return self._finish(found)


def available(pipe):
    """Readiness for an unbuffered pipe, with one and only one consumer."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        function = ctypes.WinDLL('kernel32', use_last_error=True).PeekNamedPipe
        function.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        function.restype = wintypes.BOOL
        count = wintypes.DWORD()
        if not function(msvcrt.get_osfhandle(pipe.fileno()), None, 0, None, ctypes.byref(count), None):
            code = ctypes.get_last_error()
            if code in (109, 232):
                return 0
            raise ctypes.WinError(code)
        return count.value
    readable, _, _ = select.select([pipe], [], [], 0)
    return 65536 if readable else 0


class DiffReader:
    def __init__(self, repository, preset):
        if preset not in PRESETS:
            raise ValueError('Unsupported persistent diff preset')
        self.signature = interpretation(repository)
        self.preset = preset
        self.lock, self.close_guard = threading.RLock(), threading.Lock()
        self.stopping, self.closed = threading.Event(), False
        from .memory_profile import current as memory_profile
        args = ['git', '-c', 'credential.helper=', '-c', 'core.askPass=', '-c', 'credential.interactive=false',
                '-c', 'pack.threads=1', *memory_profile().git_flags,
                '-C', str(repository), '-c', 'protocol.allow=never',
                '-c', 'diff.algorithm=myers', '-c', 'diff.indentHeuristic=true', '-c', 'core.quotePath=true',
                'diff-tree', '--root', '-r', '--no-commit-id', '--no-ext-diff', '--no-textconv',
                '--no-abbrev', '--full-index', *PRESETS[preset], '--stdin']
        env = {**os.environ, 'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0',
               'GCM_INTERACTIVE': 'never', 'GIT_ASKPASS': '', 'SSH_ASKPASS': ''}
        self.process = spawn(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=env, bufsize=0, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            self.executor = ThreadPoolExecutor(1, thread_name_prefix='git-diff-command')
        except BaseException:
            terminate(self.process, timeout=3)
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                pipe.close()
            raise

    def _request(self, commit, parent, limit, deadline):
        marker = ('codex-frame-' + uuid.uuid4().hex).encode('ascii')
        frame = Frame(self.preset, marker, limit)
        request = (commit + (' ' + parent if parent else '') + '\n').encode() + marker + b'\n'
        pending = memoryview(request)
        while pending:
            written = self.process.stdin.write(pending)
            if not written:
                raise OSError('Git diff request pipe closed')
            pending = pending[written:]
        errors = bytearray()
        def drain_errors():
            while count := available(self.process.stderr):
                value = os.read(self.process.stderr.fileno(), min(count, 65536))
                if not value:
                    break
                errors.extend(value)
                if len(errors) > 65536:
                    raise OSError('Git diff stderr exceeds bounded limit')
            if errors:
                raise OSError('Git diff stderr: ' + errors.decode('utf-8', 'replace')[:2000])
        while not self.stopping.is_set():
            if time.monotonic() >= deadline:
                raise TimeoutError('Git diff response deadline exceeded')
            drain_errors()
            count = available(self.process.stdout)
            if count:
                chunk = os.read(self.process.stdout.fileno(), min(count, 65536))
                if chunk:
                    result = frame.feed(chunk)
                    if result is not None:
                        # Marker output is flushed after Git's preceding error writes.
                        # Synchronous drain here cannot race another stderr consumer.
                        drain_errors()
                        if self.process.poll() is not None:
                            raise OSError('Git diff process exited at response boundary')
                        return result
            if self.process.poll() is not None:
                drain_errors()
                raise OSError('Git diff exited before a complete response')
            self.stopping.wait(.005)
        raise OSError('Git diff reader stopped')

    def exchange(self, commit, parent=None, limit=64 << 20):
        if not isinstance(commit, str) or not _REVISION.fullmatch(commit) or (parent is not None and (not isinstance(parent, str) or not _REVISION.fullmatch(parent))):
            raise ValueError('Persistent diff requires exact SHA-1 revisions')
        if not 0 < limit <= 64 << 20:
            raise ValueError('Invalid persistent diff output bound')
        with operations.acquire(self.lock):
            if self.closed or self.stopping.is_set():
                raise OSError('Git diff reader is closed')
            deadline = time.monotonic() + operations.timeout(30)
            try:
                failure = None
                with operations.resource(self.close):
                    from .submission import submit_owned
                    future = submit_owned(self.executor,self._request,commit,parent,limit,deadline)
                    while True:
                        operations.check()
                        left = deadline - time.monotonic()
                        if left <= 0:
                            failure = OSError('Git diff response deadline exceeded')
                            break
                        try:
                            result = future.result(timeout=min(.1, left))
                        except TimeoutError:
                            if not future.done():
                                continue
                            try:
                                result = future.result()
                            except OSError as error:
                                failure = error
                                break
                        except OSError as error:
                            failure = error
                            break
                        operations.check()
                        break
                    if failure is not None:
                        # Expected transport failure is recoverable. Close while
                        # registered so a cleanup failure still cancels/records
                        # the operation, then propagate after normal scope exit.
                        operations.check()
                        self.close()
                if failure is not None:
                    raise failure
                return result
            except BaseException:
                self.close()
                raise

    def close(self):
        # Cancellation may run while exchange owns self.lock. Never take it here.
        with self.close_guard:
            if self.closed:
                return
            self.stopping.set()
            terminate(self.process, timeout=3)
            self.executor.shutdown(wait=True, cancel_futures=True)
            for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                pipe.close()
            self.closed = True
