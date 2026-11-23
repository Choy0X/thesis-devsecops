"""Scoped, conservative verification admission, not a hard cap of a shared VM.

All production scopes share a SQLite ledger. Host roots are deduplicated and
Docker/WSL memory is charged as a shared upper envelope, never exact attribution.
Container caps are additionally reserved until Docker confirms their absence.
Unknown telemetry refuses execution; low physical RAM alone does not refuse it.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import os
import re
import sqlite3
import sys
import time
import uuid

from app.backend.pipeline.acceleration.resource_control import CAP_BYTES, system_commit_available

PREFLIGHT_BYTES = 128 << 20
CASE_BYTES = 256 << 20
_CURRENT = ContextVar('verification_resource_scope', default=None)
_VM_NAMES = {'vmmem', 'vmmemwsl', 'vmwp.exe', 'dockerdesktopvm.exe'}
_DOCKER_NAMES = {'docker desktop.exe', 'com.docker.backend.exe', 'com.docker.proxy.exe', 'dockerd.exe'}


class ResourceWait(RuntimeError):
    def __init__(self, code, reason, resources=None):
        super().__init__(reason)
        self.code, self.reason, self.resources = code, reason, resources or {}


def current():
    return _CURRENT.get()


def default_directory():
    from app.backend import paths
    return paths.interim_dir() / 'verification-resources'


def _unknown(reason):
    return {'runtime_overhead_bytes': None, 'telemetry_known': False,
            'overhead_basis': 'unavailable', 'accounting_reason': reason}


def _observer_capable():
    """Refuse Windows sandbox/impersonation tokens before shared-ledger mutation.

    TokenIsAppContainer alone does not cover restricted or low-integrity tokens.
    An unknown token state is not authority to retire another host process.
    """
    if sys.platform != 'win32':
        return True
    import ctypes
    from ctypes import wintypes
    token = wintypes.HANDLE()
    kernel = None
    try:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        security = ctypes.WinDLL('advapi32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetCurrentThread.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        security.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE)]
        security.OpenThreadToken.restype = wintypes.BOOL
        security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        security.OpenProcessToken.restype = wintypes.BOOL
        security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        security.GetTokenInformation.restype = wintypes.BOOL
        security.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        security.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        security.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        security.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
        if not security.OpenThreadToken(kernel.GetCurrentThread(), 0x0008, True, ctypes.byref(token)):
            if ctypes.get_last_error() != 1008:  # ERROR_NO_TOKEN only permits process-token fallback.
                return False
            if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
                return False
        length = wintypes.DWORD()
        for information, wanted in ((8, 1), (29, 0)):  # TokenType, TokenIsAppContainer.
            value = wintypes.DWORD()
            if (not security.GetTokenInformation(token, information, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(length))
                    or length.value != ctypes.sizeof(value) or value.value != wanted):
                return False
        buffers = {}
        # TokenHasRestrictions means ever filtered, including ordinary UAC host
        # tokens. Query the current restricting SID set (TOKEN_GROUPS) instead.
        for information, minimum, maximum in ((11, 4, 65536), (25, ctypes.sizeof(ctypes.c_void_p) + 4, 4096)):
            length.value = 0
            ctypes.set_last_error(0)
            sizing_ok = security.GetTokenInformation(token, information, None, 0, ctypes.byref(length))
            if sizing_ok or ctypes.get_last_error() != 122 or not minimum <= length.value <= maximum:
                return False
            buffer = ctypes.create_string_buffer(length.value)
            if (not security.GetTokenInformation(token, information, buffer, len(buffer), ctypes.byref(length))
                    or not minimum <= length.value <= len(buffer)):
                return False
            buffers[information] = buffer
        if wintypes.DWORD.from_buffer(buffers[11]).value != 0:
            return False
        buffer = buffers[25]
        sid = ctypes.c_void_p.from_buffer(buffer).value
        count = security.GetSidSubAuthorityCount(sid) if sid else None
        if not count or not count[0]:
            return False
        authority = security.GetSidSubAuthority(sid, count[0] - 1)
        return bool(authority and authority[0] >= 0x2000)  # SECURITY_MANDATORY_MEDIUM_RID.
    except (OSError, AttributeError, ValueError, ctypes.ArgumentError):
        return False
    finally:
        if kernel is not None and token.value:
            kernel.CloseHandle(token)


def runtime_overhead(*, excluded=()):
    """Charge whole visible Windows shared runtime envelope, without VM changes.

    RSS/private maxima intentionally overcount shared host pages. A missing VM,
    unsupported backend or denied process observation cannot establish a zero
    startup allowance. No arbitrary cached cold-start estimate is invented.
    """
    import psutil
    if sys.platform != 'win32':
        return _unknown('This backend has no validated local Docker runtime overhead collector.')
    total, vm_seen, observations = 0, False, []
    try:
        for process in psutil.process_iter(['name']):
            name = str(process.info.get('name') or '').lower()
            if name not in _VM_NAMES | _DOCKER_NAMES:
                continue
            vm_seen |= name in _VM_NAMES
            if process.pid in excluded:
                continue
            try:
                info = process.memory_info()
                value = max(info.rss, getattr(info, 'private', 0))
                total += value
                observations.append({'pid': process.pid, 'name': name, 'bytes': value})
            except psutil.NoSuchProcess:
                return _unknown('The shared runtime changed during observation; retry accounting.')
    except (psutil.Error, OSError):
        return _unknown('Windows denied complete shared Docker/WSL memory observation.')
    if not vm_seen:
        return _unknown('No observable Docker/WSL VM envelope or validated cold-start overhead profile is available.')
    return {'runtime_overhead_bytes': total, 'telemetry_known': True,
            'overhead_basis': 'shared-runtime-upper-envelope', 'runtime_processes': observations}


def _host_telemetry(owners):
    import psutil
    processes, seen = [], set()
    try:
        for pid, created in owners:
            try:
                root = psutil.Process(pid)
                if abs(root.create_time() - created) >= .01:
                    continue
                processes.extend([root, *root.children(recursive=True)])
            except psutil.NoSuchProcess:
                continue
        host = 0
        for process in processes:
            if process.pid in seen:
                continue
            seen.add(process.pid)
            try:
                info = process.memory_info()
                private = getattr(info, 'private', None)
                if private is None:
                    private = getattr(process.memory_full_info(), 'uss', None)
                if private is None:
                    return {'host_bytes': None, **_unknown('Host committed-memory accounting is unavailable.')}
                host += private
            except psutil.NoSuchProcess:
                continue
        return {'host_bytes': host, **runtime_overhead(excluded=seen),
                'commit_available_bytes': system_commit_available(),
                'available_bytes': psutil.virtual_memory().available}
    except (psutil.Error, OSError):
        return {'host_bytes': None, **_unknown('Host process-tree accounting is unavailable.')}


class Scope:
    def __init__(self, group_id, *, directory=None, sample=None, progress=None):
        if not isinstance(group_id, str) or not group_id or len(group_id) > 500:
            raise ValueError('A bounded verification resource group identity is required.')
        explicit_directory = directory is not None
        if directory is None:
            directory = default_directory()
        self.directory, self.group_id = Path(directory), group_id
        self.sample, self.progress = sample, progress
        self.id = uuid.uuid4().hex
        self.last = {}
        self._owner = None
        # Injected telemetry is only a fixture escape in an explicit, separate ledger.
        self._synthetic = (sample is not None and explicit_directory
                           and self.directory.resolve() != default_directory().resolve())

    @contextmanager
    def _db(self):
        if not self._synthetic and not _observer_capable():
            raise ResourceWait('verification_resource_accounting_unavailable',
                'This observer cannot authoritatively account for or mutate the shared host resource ledger.', self.last)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.directory / 'budget.sqlite3', timeout=5)
        except (OSError, sqlite3.Error) as error:
            raise ResourceWait('verification_resource_accounting_unavailable', 'The scoped resource ledger is unavailable: ' + str(error)[:200], self.last) from error
        try:
            db.execute('CREATE TABLE IF NOT EXISTS scopes (id TEXT PRIMARY KEY, group_id TEXT, pid INTEGER, created REAL, active INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, scope_id TEXT, kind TEXT, bytes INTEGER, container_id TEXT, bound INTEGER)')
            if 'blocked' not in {row[1] for row in db.execute('PRAGMA table_info(tickets)')}:
                db.execute('ALTER TABLE tickets ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0')
            for name in ('installation_id', 'session_id'):
                if name not in {row[1] for row in db.execute('PRAGMA table_info(tickets)')}:
                    db.execute('ALTER TABLE tickets ADD COLUMN ' + name + ' TEXT')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except (OSError, sqlite3.Error) as error:
            db.rollback()
            raise ResourceWait('verification_resource_accounting_unavailable', 'The scoped resource ledger cannot safely admit work: ' + str(error)[:200], self.last) from error
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def enter(self):
        import psutil
        owner = (os.getpid(), psutil.Process().create_time())
        with self._db() as db:
            db.execute('INSERT INTO scopes VALUES (?,?,?,?,1)', (self.id, self.group_id, *owner))
        self._owner = owner

    def close(self):
        with self._db() as db:
            if self._owner and self._owner[0] == os.getpid():
                db.execute('UPDATE scopes SET active=0 WHERE id=? AND group_id=? AND pid=? AND created=?',
                           (self.id, self.group_id, *self._owner))
            db.execute('DELETE FROM scopes WHERE active=0 AND id NOT IN (SELECT scope_id FROM tickets)')

    def _require_owner(self, db):
        import psutil
        row = db.execute('SELECT group_id,pid,created,active FROM scopes WHERE id=?', (self.id,)).fetchone()
        try:
            current = (os.getpid(), psutil.Process().create_time())
        except (psutil.Error, OSError) as error:
            raise ResourceWait('verification_resource_accounting_unavailable',
                'The current resource-scope owner cannot be authenticated.', self.last) from error
        if self._owner != current or row != (self.group_id, *current, 1):
            raise ResourceWait('verification_resource_accounting_unavailable',
                'The current resource scope is missing, inactive or belongs to another process identity.', self.last)

    def _state(self, db, *, require_runtime=True):
        self._retire_dead(db)
        owners = db.execute('SELECT DISTINCT pid,created FROM scopes WHERE active=1 OR id IN (SELECT scope_id FROM tickets)').fetchall()
        telemetry = dict(self.sample() if self.sample else _host_telemetry(owners))
        reserved = db.execute('SELECT COALESCE(SUM(bytes),0) FROM tickets').fetchone()[0]
        host, overhead = telemetry.get('host_bytes'), telemetry.get('runtime_overhead_bytes')
        known = telemetry.get('telemetry_known') is True and type(overhead) is int and overhead >= 0
        combined = host + overhead + reserved if type(host) is int and known else None
        accounted = combined if require_runtime else host + reserved if type(host) is int else None
        runtime_charge = overhead if require_runtime and known else 0
        state = {**telemetry, 'schema': 'verification-resources/1', 'group_id': self.group_id,
                 'cap_bytes': CAP_BYTES, 'reserved_container_bytes': reserved,
                 'accounted_bytes': accounted, 'combined_observed_bytes': combined,
                 'accounting_mode': 'combined' if require_runtime else 'host_only',
                 'runtime_envelope_excluded': not require_runtime,
                 'host_limit_bytes': max(0, CAP_BYTES - runtime_charge - reserved),
                 'attribution': ('conservative_shared_upper_envelope' if known else 'unknown') if require_runtime else 'owned_host_tree_only',
                 'enforcement': 'sampled_admission', 'observed_at': time.time()}
        self.last = state
        if self.progress:
            self.progress(state)
        if type(host) is not int or host < 0 or require_runtime and not known:
            raise ResourceWait('verification_resource_accounting_unavailable', telemetry.get('accounting_reason') or 'Complete verification memory telemetry is unavailable.', state)
        if (accounted is not None and accounted > CAP_BYTES) or host + reserved > CAP_BYTES:
            raise ResourceWait('verification_memory_waiting', 'The scoped host, runtime envelope and container reservations exceed the verification budget.', state)
        return state

    def _retire_dead(self, db):
        import psutil
        self._require_owner(db)
        for identity, pid, created in db.execute('SELECT id,pid,created FROM scopes WHERE active=1').fetchall():
            if identity == self.id:
                continue
            try:
                dead = abs(psutil.Process(pid).create_time() - created) >= .01
            except psutil.NoSuchProcess:
                dead = True
            except psutil.Error:
                # ignored: denied liveness cannot authorize releasing a charge.
                continue
            if dead:
                db.execute('UPDATE scopes SET active=0 WHERE id=?', (identity,))

    def reconcile(self, client):
        """Reconcile charges and remove only positively bound inactive job containers."""
        from docker.errors import NotFound
        with self._db() as db:
            self._retire_dead(db)
            rows = db.execute('SELECT t.id,t.container_id,t.bound,s.active,s.group_id,t.kind,t.installation_id,t.session_id '
                'FROM tickets t JOIN scopes s ON s.id=t.scope_id WHERE s.active=0 OR t.blocked=1').fetchall()
            for identity, container_id, bound, active, group, kind, installation, session in rows:
                try:
                    if bound:
                        try:
                            container = client.containers.get(container_id)
                        except NotFound:
                            from app.backend import sandbox_runtime
                            sandbox_runtime.forget(container_id)
                            db.execute('DELETE FROM tickets WHERE id=?', (identity,))
                        else:
                            if not active:
                                self._remove_orphan(db, client, container, identity=identity, container_id=container_id,
                                    group=group, kind=kind, installation=installation, session=session)
                    else:
                        matches = client.containers.list(all=True, filters={'label': ['ztsa.verification.reservation=' + identity]})
                        if len(matches) == 1:
                            db.execute('UPDATE tickets SET container_id=?,bound=1,blocked=1 WHERE id=?', (matches[0].id, identity))
                        # An API timeout may complete after a negative list.
                        # Absence cannot prove an uncertain create never ran.
                except Exception as error:
                    raise ResourceWait('verification_cleanup_waiting', 'Docker could not reconcile prior scoped reservations: ' + str(error)[:200], self.last) from error

    def _remove_orphan(self, db, client, container, *, identity, container_id, group, kind, installation, session):
        from app.backend import sandbox_runtime
        from docker.errors import NotFound
        expected_kind = 'verification-preflight' if kind == 'preflight' else 'verification' if kind in ('case', 'mechanism_case') else None
        if (not re.fullmatch(r'[0-9a-f]{64}', str(container_id or '')) or container.id != container_id
                or not installation or not session or expected_kind is None):
            raise ResourceWait('verification_cleanup_waiting', 'Orphan cleanup needs a full container identity and recorded verification ownership labels.', self.last)
        if db.execute('SELECT 1 FROM tickets t JOIN scopes s ON s.id=t.scope_id '
                      'WHERE t.container_id=? AND s.active=1 LIMIT 1', (container_id,)).fetchone():
            raise ResourceWait('verification_cleanup_waiting', 'A live resource scope has adopted this container; orphan cleanup is refused.', self.last)

        def remove_owned(state):
            # Use the same cross-process ownership lock as register/adoption.
            # A new app owner cannot adopt between this check and removal.
            registered = state['containers'].get(container_id)
            if (state['installation_id'] != installation
                    or registered is not None and registered.get('session_id') != session):
                raise ResourceWait('verification_cleanup_waiting', 'Container installation/session ownership changed; orphan cleanup is refused.', self.last)
            container.reload()
            labels = (container.attrs.get('Config') or {}).get('Labels') or {}
            expected = {'aivdf.sandbox': '1', sandbox_runtime.INSTALL_LABEL: installation,
                sandbox_runtime.SESSION_LABEL: session, 'aivdf.kind': expected_kind,
                'ztsa.verification.resource-group': group, 'ztsa.verification.reservation': identity}
            if any(labels.get(key) != value for key, value in expected.items()):
                raise ResourceWait('verification_cleanup_waiting', 'Container labels do not prove this inactive verification reservation; cleanup is refused.', self.last)
            container.remove(force=True, v=False)
            try:
                client.containers.get(container_id)
            except NotFound:
                state['containers'].pop(container_id, None)
            else:
                raise ResourceWait('verification_cleanup_waiting', 'Docker has not confirmed orphan container removal; its charge is retained.', self.last)

        sandbox_runtime.update(remove_owned)
        db.execute('DELETE FROM tickets WHERE id=?', (identity,))

    def check(self, *, require_runtime=True):
        with self._db() as db:
            return self._state(db, require_runtime=require_runtime)

    def engine(self, client):
        endpoint = str(getattr(getattr(client, 'api', None), 'base_url', ''))
        if endpoint not in ('http+docker://localnpipe', 'http+docker://localhost'):
            raise ResourceWait('verification_resource_accounting_unavailable', 'Local Docker/WSL observations cannot account for this Docker endpoint.', self.last)

    def admit(self, kind, amount):
        if type(amount) is not int or amount < 0 or amount > CAP_BYTES:
            raise ValueError('Invalid verification reservation size.')
        with self._db() as db:
            state = self._state(db)
            unresolved = db.execute('SELECT 1 FROM tickets t JOIN scopes s ON s.id=t.scope_id WHERE s.active=0 OR t.blocked=1 LIMIT 1').fetchone()
            if unresolved:
                raise ResourceWait('verification_cleanup_waiting', 'An earlier verification reservation requires confirmed cleanup before new execution.', state)
            if state['accounted_bytes'] + amount > CAP_BYTES:
                raise ResourceWait('verification_memory_waiting', 'The next bounded verification operation does not fit the combined memory budget.', state)
            commit = state.get('commit_available_bytes')
            if type(commit) is int and commit < amount:
                raise ResourceWait('verification_memory_waiting', 'Committed-memory backing cannot admit the next bounded verification operation.', state)
            if amount == 0 and db.execute("SELECT 1 FROM tickets WHERE bytes=0 LIMIT 1").fetchone():
                raise ResourceWait('verification_runtime_busy', 'Another scoped runtime startup or image acquisition is active.', state)
            identity = uuid.uuid4().hex
            db.execute('INSERT INTO tickets (id,scope_id,kind,bytes,container_id,bound) VALUES (?,?,?,?,NULL,0)', (identity, self.id, str(kind)[:100], amount))
        return Reservation(self, identity)

    @contextmanager
    def operation(self, kind):
        ticket = self.admit(kind, 0)
        try:
            yield self
            self.check()
        finally:
            ticket.cancel_before_create()


class Reservation:
    def __init__(self, scope, identity):
        self.scope, self.id = scope, identity

    def labels(self):
        from app.backend import sandbox_runtime
        origin = sandbox_runtime.labels()
        installation, session = origin[sandbox_runtime.INSTALL_LABEL], origin[sandbox_runtime.SESSION_LABEL]
        with self.scope._db() as db:
            row = db.execute('SELECT installation_id,session_id FROM tickets WHERE id=?', (self.id,)).fetchone()
            if row is None or row[0] is not None and row != (installation, session):
                raise ResourceWait('verification_cleanup_waiting', 'A verification reservation cannot be adopted by another installation or session.', self.scope.last)
            db.execute('UPDATE tickets SET installation_id=?,session_id=? WHERE id=?', (installation, session, self.id))
        return {'ztsa.verification.resource-group': self.scope.group_id, 'ztsa.verification.reservation': self.id}

    def retain(self):
        """Fence retries after uncertain creation or failed container cleanup."""
        with self.scope._db() as db:
            db.execute('UPDATE tickets SET blocked=1 WHERE id=?', (self.id,))

    def bind(self, container):
        identity = str(container.id)
        if not identity:
            raise ValueError('A created container identity is required.')
        with self.scope._db() as db:
            if db.execute('UPDATE tickets SET container_id=?,bound=1 WHERE id=? AND bound=0', (identity, self.id)).rowcount != 1:
                raise RuntimeError('Verification reservation was already bound or released.')

    def cancel_before_create(self):
        with self.scope._db() as db:
            row = db.execute('SELECT bound,blocked FROM tickets WHERE id=?', (self.id,)).fetchone()
            if row and (row[0] or row[1]):
                raise ResourceWait('verification_cleanup_waiting', 'A created verification container must be confirmed absent before releasing memory.', self.scope.last)
            db.execute('DELETE FROM tickets WHERE id=?', (self.id,))

    def confirm_removed(self, client):
        from docker.errors import NotFound
        with self.scope._db() as db:
            row = db.execute('SELECT container_id,bound FROM tickets WHERE id=?', (self.id,)).fetchone()
            if row is None:
                return
            if not row[1]:
                raise ResourceWait('verification_cleanup_waiting', 'Container creation has not been reconciled; its reservation is retained.', self.scope.last)
            try:
                client.containers.get(row[0])
            except NotFound:
                db.execute('DELETE FROM tickets WHERE id=?', (self.id,))
            except Exception as error:
                raise ResourceWait('verification_cleanup_waiting', 'Docker could not confirm container removal: ' + str(error)[:200], self.scope.last) from error
            else:
                raise ResourceWait('verification_cleanup_waiting', 'Docker still reports the verification container; its memory reservation is retained.', self.scope.last)


@contextmanager
def scope(group_id, **kwargs):
    budget = Scope(group_id, **kwargs)
    budget.enter()
    token = _CURRENT.set(budget)
    try:
        yield budget
    finally:
        _CURRENT.reset(token)
        budget.close()
