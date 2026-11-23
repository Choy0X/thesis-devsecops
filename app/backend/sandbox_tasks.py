"""Fenced execution ownership for interactive sandboxes.

The durable task receipts belong to backend chat execution, never browser tabs.
Stopping the last owner's container also ends its remaining process tree. Volumes,
files, images and the container itself are preserved for the next task.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import threading
import time
import uuid

import psutil

from app.backend import sandbox_runtime
from app.backend.pipeline import oslock

_LOCK = threading.RLock()
_CURRENT = contextvars.ContextVar('sandbox_execution', default=None)
_LIVE = set()


class TaskRequired(PermissionError):
    pass


@contextlib.contextmanager
def _locked():
    with _LOCK:
        path = sandbox_runtime.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = oslock.FileLock(str(path) + '.tasks.lock', note=False)
        lock.acquire(wait=30)
        try:
            yield
        finally:
            lock.release()


def _alive(task):
    try:
        if abs(psutil.Process(task['pid']).create_time() - task['created']) >= .01:
            return False
        if task['pid'] == os.getpid():
            from app.backend.pipeline import agent_sessions
            return task.get('token') in _LIVE and agent_sessions.is_busy(task['project_id'], task['session_id'])
        return time.time() - task.get('heartbeat', task.get('started', 0)) <= 30
    except (psutil.Error, KeyError, TypeError):
        return False


def _tasks(state):
    return state.setdefault('interactive_tasks', {})


def _containers(state):
    return state.setdefault('interactive_containers', {})


def acquire(project_id, session_id):
    from app.backend.pipeline import agent_sessions
    if not agent_sessions.is_busy(project_id, session_id):
        raise TaskRequired('An executing chat task is required to start an interactive sandbox.')
    token = uuid.uuid4().hex
    with _locked():
        sandbox_runtime.update(lambda state: _tasks(state).update({token: {
            'token': token, 'project_id': project_id, 'session_id': session_id, 'pid': os.getpid(),
            'created': psutil.Process().create_time(), 'started': time.time(), 'heartbeat': time.time()}}))
        _LIVE.add(token)
    return token


def require_current(project_id, session_id=None):
    token = _CURRENT.get()
    task = _tasks(sandbox_runtime.state()).get(token)
    from app.backend.pipeline import agent_sessions
    if (not task or not _alive(task) or task['project_id'] != project_id
            or (session_id and task['session_id'] != session_id)
            or not agent_sessions.is_busy(project_id, task['session_id'])):
        raise TaskRequired('An executing chat task is required to start an interactive sandbox.')
    return token


def bind(token, container_id):
    with _locked():
        def save(state):
            task = _tasks(state).get(token)
            if not task or not _alive(task):
                raise TaskRequired('The chat execution owner has ended.')
            task['container_id'] = container_id
            _containers(state)[container_id] = {'project_id': task['project_id'], 'generation': token}
        sandbox_runtime.update(save)


@contextlib.contextmanager
def provision(project_id, session_id=None):
    """Fence container creation/start against last-owner cleanup."""
    with _locked():
        token = require_current(project_id, session_id)
        def attach(container_id):
            def save(state):
                task = _tasks(state).get(token)
                if not task:
                    raise TaskRequired('The chat execution owner has ended.')
                task['container_id'] = container_id
                _containers(state)[container_id] = {'project_id': project_id, 'generation': token}
            sandbox_runtime.update(save)
        yield attach


def container_state(container_id):
    state = sandbox_runtime.state()
    owners = [task for task in _tasks(state).values() if task.get('container_id') == container_id and _alive(task)]
    return {'active': bool(owners), 'owners': len(owners),
            'cleanup_error': _containers(state).get(container_id, {}).get('cleanup_error')}


def require_container(container_id):
    if not container_state(container_id)['active']:
        raise TaskRequired('This sandbox is available only while an executing chat task needs it.')


def _stop(container_id):
    from app.backend import sandbox, sandbox_history
    from docker.errors import NotFound
    try:
        ct = sandbox._ours(container_id)
        ct.reload()
    except NotFound:
        return
    if ct.status == 'paused':
        ct.unpause()
    if ct.status in ('running', 'restarting', 'paused'):
        # The trusted shell writes termination receipts for unfinished detached
        # jobs before Docker stops all children. No target source is executed.
        script = ('for d in /work/.agent/jobs/*; do [ -d "$d" ] || continue; '
                  'p=$(cat "$d/pid" 2>/dev/null); case "$p" in ""|*[!0-9]*) '
                  '[ -f "$d/rc" ] || printf "killed\\n" > "$d/rc";; *) '
                  'if kill -0 -- -"$p" 2>/dev/null || [ ! -f "$d/rc" ]; then '
                  'printf "killed\\n" > "$d/rc"; fi;; esac; done')
        try:
            ct.exec_run(['timeout', '-k', '1', '5', 'bash', '-c', script], user='10001:10001')
        except Exception:
            # ignored: receipt creation is best effort; Docker's confirmed
            # process-tree stop below remains authoritative for cleanup.
            pass
        ct.stop(timeout=10)
    ct.reload()
    if ct.status in ('running', 'restarting', 'paused'):
        raise RuntimeError('Docker has not confirmed that the task sandbox stopped.')
    sandbox_history.record(ct.name, 'stopped', {'reason': 'chat task ended', 'children': 'container process tree stopped'})


def _cleanup(container_id):
    # Called under the cross-process task lock. A new owner cannot bind or start
    # while Docker confirms this stop, and an obsolete release has no authority.
    state = sandbox_runtime.state()
    generation = _containers(state).get(container_id, {}).get('generation')
    if any(task.get('container_id') == container_id and _alive(task) for task in _tasks(state).values()):
        return
    problem = None
    try:
        _stop(container_id)
    except Exception as error:
        problem = str(error)[:500]
    def save(document):
        entry = _containers(document).get(container_id)
        if entry is None or entry.get('generation') != generation:
            return
        if problem:
            entry['cleanup_error'] = problem
        else:
            entry.pop('cleanup_error', None)
    sandbox_runtime.update(save)


def release(token):
    with _locked():
        _LIVE.discard(token)
        removed = []
        def save(state):
            task = _tasks(state).pop(token, None)
            if task:
                removed.append(task)
        sandbox_runtime.update(save)
        if removed:
            if removed[0].get('container_id'):
                _cleanup(removed[0]['container_id'])
            _cleanup_proxy()


def reconcile():
    with _locked():
        def save(state):
            tasks = _tasks(state)
            for token, task in list(tasks.items()):
                if not _alive(task):
                    tasks.pop(token, None)
        sandbox_runtime.update(save)
        for container_id in list(_containers(sandbox_runtime.state())):
            _cleanup(container_id)
        _cleanup_proxy()


@contextlib.contextmanager
def execution(project_id, session_id):
    token = acquire(project_id, session_id)
    current = _CURRENT.set(token)
    stopped = threading.Event()
    def heartbeat():
        while not stopped.wait(5):
            try:
                def beat(state):
                    if token in _tasks(state):
                        _tasks(state)[token]['heartbeat'] = time.time()
                sandbox_runtime.update(beat)
            except (OSError, ValueError):
                pass  # Expiring ownership prevents unbounded orphan execution.
    worker = threading.Thread(target=heartbeat, name='sandbox-task-heartbeat', daemon=True)
    worker.start()
    try:
        yield token
    finally:
        stopped.set()
        worker.join(timeout=1)
        try:
            release(token)
        finally:
            _CURRENT.reset(current)


def _cleanup_proxy():
    state = sandbox_runtime.state()
    if any(task.get('container_id') and _alive(task) for task in _tasks(state).values()):
        return
    # The proxy is a shared supporting service, not a verifier container. Keep
    # its networks and files; ensure_proxy starts it on the next owned task.
    from app.backend import sandbox
    from app.backend.pipeline.scan import agent_network
    if not any(item.get('name') == agent_network.PROXY_NAME for item in state['containers'].values()):
        sandbox_runtime.update(lambda document: document.pop('interactive_proxy_error', None))
        return
    problem = None
    try:
        client = sandbox._client()
        from docker.errors import NotFound
        try:
            proxy = client.containers.get(agent_network.PROXY_NAME)
        except NotFound:
            proxy = None
        if proxy is not None:
            from app.backend.sandbox_cleanup import owned
            if owned(proxy, installation_id=state['installation_id'], known_ids=set(state['containers'])):
                proxy.reload()
                if proxy.status == 'paused':
                    proxy.unpause()
                if proxy.status in ('running', 'restarting', 'paused'):
                    proxy.stop(timeout=10)
                proxy.reload()
                if proxy.status in ('running', 'restarting', 'paused'):
                    raise RuntimeError('Docker has not confirmed that the chat egress proxy stopped.')
    except Exception as error:
        problem = str(error)[:500]
    def save(document):
        if problem:
            document['interactive_proxy_error'] = problem
        else:
            document.pop('interactive_proxy_error', None)
    sandbox_runtime.update(save)
