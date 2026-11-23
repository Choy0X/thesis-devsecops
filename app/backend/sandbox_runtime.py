"""Installation/session ownership and the creation/shutdown interlock."""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import threading
import uuid

from app.backend import paths
from app.backend.pipeline import fsio

INSTALL_LABEL = 'aivdf.installation'
SESSION_LABEL = 'aivdf.session'
_SESSION = uuid.uuid4().hex
_LOCK = threading.RLock()
_STOPPING = False


def state_path():
    return paths.interim_dir() / 'sandbox_runtime.json'


def state():
    with _LOCK:
        path = state_path()
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            value = {'schema': 'sandbox-runtime/1', 'installation_id': uuid.uuid4().hex,
                     'containers': {}, 'pending_images': {}, 'cleanup_errors': []}
            path.parent.mkdir(parents=True, exist_ok=True)
            fsio.write_json(path, value)
        if value.get('schema') != 'sandbox-runtime/1' or not value.get('installation_id') or not isinstance(value.get('containers'), dict):
            raise ValueError('Invalid sandbox ownership record. Restore it before managing Docker resources.')
        return value


def update(fn):
    with _LOCK:
        from app.backend.pipeline import oslock
        state_path().parent.mkdir(parents=True, exist_ok=True)
        with oslock.FileLock(str(state_path()) + '.lock', note=False):
            value = state(); fn(value); fsio.write_json(state_path(), value)
            return value


def labels(kind='sandbox'):
    return {'aivdf.sandbox': '1', INSTALL_LABEL: state()['installation_id'], SESSION_LABEL: _SESSION, 'aivdf.kind': kind}


def register(ct):
    def save(value):
        value['containers'][ct.id] = {'session_id': _SESSION, 'name': ct.name}
    update(save)


def forget(cid):
    update(lambda value: value['containers'].pop(cid, None))


def run_container(client, *args, create_only=False, **kwargs):
    """Creation and ownership registration are atomic with the shutdown interlock."""
    with creation_guard():
        kwargs['labels'] = {**kwargs.get('labels', {}), **labels((kwargs.get('labels') or {}).get('aivdf.kind', 'sandbox'))}
        kwargs['restart_policy'] = {'Name': 'no'}
        ct = (client.containers.create if create_only else client.containers.run)(*args, **kwargs)
        try:
            register(ct)
        except BaseException:
            # Persisting ownership failed: never leave an untracked detached worker.
            ct.remove(force=True, v=False)
            raise
        return ct


def start_container(ct):
    with creation_guard():
        ct.start()
        register(ct)


def set_stopping(value):
    global _STOPPING
    with _LOCK:
        _STOPPING = bool(value)


@contextlib.contextmanager
def creation_guard():
    with _LOCK:
        if _STOPPING:
            raise RuntimeError('The app is shutting down; new sandboxes cannot be created.')
        yield


def cleanup_scope():
    value = state()
    return {'installation_id': value['installation_id'], 'known_ids': set(value['containers'])}


def stop_for_shutdown():
    from app.backend import sandbox, sandbox_cleanup
    set_stopping(True)
    scope = cleanup_scope()
    if not scope['known_ids']:
        return 'no registered sandbox containers; workspace volumes preserved'
    try:
        report = sandbox_cleanup.stop_owned(sandbox._client(), **scope)
    except Exception as error:
        report = {'ok': False, 'errors': [str(error)], 'stopped': []}
    update(lambda value: value.update(cleanup_errors=report.get('errors', [])))
    if not report['ok']:
        raise RuntimeError('Sandbox cleanup incomplete: ' + '; '.join(str(x) for x in report['errors']))
    return f"{len(report['stopped'])} sandbox containers confirmed stopped; workspace volumes preserved"


def bootstrap():
    """Called by an actual application entry point, never at module import."""
    import subprocess
    import sys
    import psutil
    if os.environ.get('ZTSA_SANDBOX_WATCHDOG') == '0':
        return
    owner_pid = int(os.environ.get('ZTSA_SANDBOX_OWNER_PID') or os.getpid())
    owner = {'pid': owner_pid, 'created': psutil.Process(owner_pid).create_time()}
    previous = state()
    update(lambda value: value.update(owner=owner, owner_session=_SESSION, watchdog_error=None, restart_handoff_until=None))
    try:
        import docker
        from app.backend import sandbox_cleanup
        client = docker.from_env(timeout=3)
        rows = client.containers.list(all=True, filters={'label': 'aivdf.sandbox=1'})
        candidates = [ct for ct in rows if (ct.labels or {}).get(INSTALL_LABEL) in (None, previous['installation_id'])]
        # A real startup after an unclean exit first stops orphaned work. Files
        # and volumes survive; no resource is removed during reconciliation.
        report = sandbox_cleanup.stop_owned(client, installation_id=previous['installation_id'],
                                             known_ids={ct.id for ct in candidates})
        for ct in candidates:
            if ct.id in report['stopped']:
                register(ct)
        update(lambda value: value.update(cleanup_errors=report['errors']))
        retry_images(client)
    except Exception as error:
        update(lambda value: value.update(cleanup_errors=[str(error)]))
    command = [sys.executable, '-m', 'app.backend.sandbox_watchdog', '--state', str(state_path().resolve()), '--session', _SESSION]
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB if os.name == 'nt' else 0
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   cwd=pathlib.Path(__file__).resolve().parents[2], creationflags=flags,
                                   start_new_session=os.name != 'nt')
        update(lambda value: value.update(watchdog_pid=process.pid))
    except OSError as error:
        update(lambda value: value.update(watchdog_error=str(error)))


def retry_images(client):
    from app.backend.sandbox_cleanup import cleanup_image
    for image_id in list(state().get('pending_images', {})):
        try:
            result = cleanup_image(client, image_id)
        except Exception as error:
            result = {'id': image_id, 'status': 'failed', 'reason': str(error)}
        if result['status'] == 'removed':
            update(lambda value: value['pending_images'].pop(image_id, None))
        else:
            update(lambda value: value['pending_images'].update({image_id: result}))


def reconcile(client):
    """Retry old-session cleanup after Docker reconnects, without stopping current work."""
    from app.backend.sandbox_cleanup import stop_owned
    value = state()
    pending = {cid for cid, item in value['containers'].items() if item.get('session_id') != _SESSION}
    if pending:
        report = stop_owned(client, installation_id=value['installation_id'], known_ids=pending, only_ids=pending)
        def save(document):
            document['cleanup_errors'] = report['errors']
            for cid in report['stopped']:
                if cid in document['containers']:
                    document['containers'][cid]['session_id'] = _SESSION
        update(save)
    retry_images(client)
    from app.backend import sandbox_tasks
    sandbox_tasks.reconcile()
    value = state()
    return {key: value.get(key) for key in ('cleanup_errors', 'pending_images', 'watchdog_error', 'interactive_containers', 'interactive_proxy_error')}
