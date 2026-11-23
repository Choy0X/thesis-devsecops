"""Frozen deletion previews, durable reservations and bounded owned-directory deletion."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import secrets
import shutil
import time
import functools
import inspect
from contextlib import contextmanager

from app.backend.pipeline import finetune_catalog as catalog

RUN_STAGES = frozenset(('export_training_bundle', 'finetune_validate', 'import_finetune_outputs',
                        'import_trial_outputs', 'import_finetune_eval', 'export_full_model', 'gpu_smoke', 'gpu_eval', 'train_local'))


class Refused(ValueError):
    pass


def _pid_alive(pid):
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except (ImportError, ValueError, TypeError):
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError, TypeError):
            return False


def _owned(root, name):
    from app.backend.pipeline.finetune_prep import check_run_name
    check_run_name(name)
    original_root = pathlib.Path(root)
    if original_root.is_symlink() or getattr(original_root.lstat(), 'st_file_attributes', 0) & 0x400:
        raise Refused('A linked training root is protected')
    root = original_root.resolve()
    folder = root / name
    if (folder.is_symlink() or (folder.exists() and getattr(folder.lstat(), 'st_file_attributes', 0) & 0x400)
            or folder.resolve().parent != root):
        raise Refused('The run folder is outside its owned training root')
    if not folder.is_dir():
        raise Refused(f'Run {name} no longer exists')
    return folder


def _inventory(folder):
    if folder.is_symlink() or getattr(folder.lstat(), 'st_file_attributes', 0) & 0x400:
        raise Refused('A linked run folder is protected')
    rows = []
    for base, dirs, files in os.walk(folder, followlinks=False):
        for name in [*dirs, *files]:
            path = pathlib.Path(base) / name
            st = path.lstat()
            # Windows junctions are reparse points even on Python versions without is_junction.
            if path.is_symlink() or getattr(st, 'st_file_attributes', 0) & 0x400:
                raise Refused('A linked artifact is protected; remove its reference before deleting the run')
            rows.append((path.relative_to(folder).as_posix(), st.st_size if path.is_file() else 0, st.st_mtime_ns, st.st_ino))
    rows.sort()
    identity = folder.stat()
    fingerprint = hashlib.sha256(json.dumps([identity.st_ino, rows]).encode()).hexdigest()
    return rows, fingerprint


def dependencies(name):
    from app.backend import jobs
    from app.backend.pipeline import finetune_prep as fp
    from app.backend.pipeline.gateway import client
    folder = pathlib.Path(fp.RUNS).resolve() / name
    out = [{'kind': 'job', 'id': j.id, 'stage': j.stage} for j in jobs.running()
           if j.stage in RUN_STAGES and (j.params or {}).get('run') == name]
    status = client.status(timeout=1.0)
    status = status or {}
    artifacts = status.get('running_artifacts')
    if artifacts is None:
        artifacts = [{'role': role, 'path': slot[key]} for role, slot in status.get('slots', {}).items()
                     if slot.get('state') in ('loading', 'ready', 'draining')
                     for key in ('model', 'adapter') if slot.get(key)]
    out.extend({'kind': 'serving', **artifact} for artifact in artifacts
               if pathlib.Path(artifact['path']).resolve().is_relative_to(folder))
    # Configured enabled aliases also reserve artifacts, including an adapter that has not loaded yet.
    from app.backend.pipeline import model_access
    for row in model_access.default().read()['models'].values():
        if row.get('enabled') and any(row.get(k) and pathlib.Path(row[k]).resolve().is_relative_to(folder) for k in ('model', 'adapter')):
            out.append({'kind': 'serving_alias', 'id': row.get('id')})
    return out


def _live_leases(conn, name):
    rows = list(conn.execute('SELECT * FROM leases WHERE name=?', (name,)))
    for row in rows:
        if not _pid_alive(row['pid']):
            conn.execute('DELETE FROM leases WHERE job=? AND name=?', (row['job'], name))
    return [{'kind': 'job', 'id': row['job']} for row in rows if _pid_alive(row['pid'])]


def admit(root, name, job):
    from app.backend.pipeline.finetune_prep import check_run_name
    check_run_name(name)
    with catalog.connect(root) as conn:
        conn.execute('BEGIN IMMEDIATE')
        _recover(conn, root)
        if conn.execute('SELECT 1 FROM deleting WHERE name=?', (name,)).fetchone():
            raise Refused(f'Run {name} is reserved for deletion')
        conn.execute('INSERT OR REPLACE INTO leases(job,name,pid) VALUES(?,?,?)', (job, name, os.getpid()))


def release(root, job):
    with catalog.connect(root) as conn:
        conn.execute('DELETE FROM leases WHERE job=?', (job,))


def acquire_paths(spec):
    """Reserve any run-owned serving model/adapter for the lifetime of a gateway slot."""
    from app.backend.pipeline import finetune_prep as fp
    root = pathlib.Path(fp.RUNS).resolve()
    names = set()
    for key in ('model', 'adapter'):
        value = spec.get(key)
        if value:
            path = pathlib.Path(value).resolve()
            if path.is_relative_to(root) and len(path.relative_to(root).parts) >= 2:
                names.add(path.relative_to(root).parts[0])
    if not names:
        return None
    lease = 'serving-' + secrets.token_hex(12)
    try:
        for name in names:
            admit(root, name, lease)
        return lease
    except BaseException:
        release(root, lease)
        raise


def release_paths(lease):
    from app.backend.pipeline import finetune_prep as fp
    if lease:
        release(fp.RUNS, lease)


@contextmanager
def serving_source(spec):
    """Fence a run-owned source while Ollama reads and imports it."""
    lease = acquire_paths(spec)
    try:
        yield
    finally:
        release_paths(lease)


@contextmanager
def operation(run_dir):
    folder = pathlib.Path(run_dir)
    lease = 'operation-' + secrets.token_hex(12)
    admit(folder.parent, folder.name, lease)
    try:
        yield
    finally:
        catalog.invalidate(folder.parent, folder.name)
        release(folder.parent, lease)


def guarded(function):
    """Fence standalone pipeline calls as well as calls admitted through the jobs API."""
    signature = inspect.signature(function)
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        folder = signature.bind(*args, **kwargs).arguments['run_dir']
        with operation(folder):
            return function(*args, **kwargs)
    return wrapped


def _quarantine(root, token, name):
    suffix = hashlib.sha256((token + '/' + name).encode()).hexdigest()[:24]
    return pathlib.Path(root).resolve() / ('.delete-' + suffix)


def _recover(conn, root):
    """A killed worker keeps data recoverable and must not fence the run forever."""
    for row in list(conn.execute('SELECT token,document FROM previews WHERE job IS NOT NULL AND finished=0')):
        doc = json.loads(row['document'])
        owner = doc.get('owner_pid')
        if not owner or _pid_alive(owner):
            continue
        for name in doc['names']:
            folder = pathlib.Path(root).resolve() / name
            quarantine = _quarantine(root, row['token'], name)
            if quarantine.is_dir() and not folder.exists():
                if quarantine.is_symlink() or getattr(quarantine.lstat(), 'st_file_attributes', 0) & 0x400:
                    raise Refused('Interrupted deletion has a linked recovery path; manual recovery is required')
                quarantine.rename(folder)
            conn.execute('UPDATE runs SET deletion_error=? WHERE name=?', ('Deletion was interrupted; preview the remaining artifacts and retry.', name))
        conn.execute('DELETE FROM deleting WHERE token=?', (row['token'],))
        conn.execute('UPDATE previews SET finished=1 WHERE token=?', (row['token'],))


def recover(root):
    with catalog.connect(root) as conn:
        conn.execute('BEGIN IMMEDIATE')
        _recover(conn, root)


def preview(root, *, names=None, select_all=False, search='', state='', dependencies=dependencies):
    if bool(names) == bool(select_all):
        raise ValueError('Choose explicit run names or select_all with the current filters')
    recover(root)
    catalog.refresh(root)
    selected = catalog.names(root, search=search, state=state) if select_all else sorted(set(names))
    if not selected:
        raise ValueError('Select at least one run')
    artifacts, blockers, snapshots, total_bytes = [], [], {}, 0
    with catalog.connect(root) as conn:
        conn.execute('BEGIN IMMEDIATE')
        for name in selected:
            folder = _owned(root, name)
            if not conn.execute('SELECT 1 FROM runs WHERE name=?', (name,)).fetchone():
                raise Refused(f'{name} is not an owned prepared training run')
            rows, fingerprint = _inventory(folder)
            snapshots[name] = fingerprint
            size = sum(row[1] for row in rows)
            total_bytes += size
            groups = {}
            for relative, file_bytes, *_ in rows:
                category = relative.split('/')[0]
                group = groups.setdefault(category, {'path': category, 'bytes': 0, 'entries': 0})
                group['bytes'] += file_bytes
                group['entries'] += 1
            artifacts.append({'name': name, 'bytes': size, 'files': len(rows), 'path': str(folder), 'groups': list(groups.values())})
            using = [*dependencies(name), *_live_leases(conn, name)]
            if conn.execute('SELECT 1 FROM deleting WHERE name=?', (name,)).fetchone():
                using.append({'kind': 'deletion', 'reason': 'A deletion is already queued'})
            if using:
                blockers.append({'name': name, 'dependencies': using})
        token = secrets.token_urlsafe(32)
        result = {'preview_token': token, 'names': selected, 'total': len(selected), 'bytes': total_bytes,
                  'artifacts': artifacts, 'dependencies': blockers, 'blocked': bool(blockers),
                  'permanent': True, 'protected': ['shared model cache', 'source datasets', 'source repositories'],
                  'expires_at': time.time() + 900}
        doc = {**result, 'snapshots': snapshots}
        conn.execute('INSERT INTO previews(token,document,created) VALUES(?,?,?)', (token, json.dumps(doc), time.time()))
    return result


def reserve(root, token, job, *, dependencies=dependencies):
    with catalog.connect(root) as conn:
        conn.execute('BEGIN IMMEDIATE')
        _recover(conn, root)
        row = conn.execute('SELECT * FROM previews WHERE token=?', (token,)).fetchone()
        if not row or row['finished'] or row['job']:
            raise Refused('The deletion preview is invalid or already used')
        doc = json.loads(row['document'])
        if time.time() > doc['expires_at']:
            raise Refused('The deletion preview expired; preview the selection again')
        for name in doc['names']:
            folder = _owned(root, name)
            if _inventory(folder)[1] != doc['snapshots'][name]:
                raise Refused(f'Run {name} changed; preview it again')
            if dependencies(name) or _live_leases(conn, name):
                raise Refused(f'Run {name} is in use')
            if conn.execute('SELECT 1 FROM deleting WHERE name=?', (name,)).fetchone():
                raise Refused(f'Run {name} is already reserved for deletion')
            conn.execute('INSERT INTO deleting(name,token) VALUES(?,?)', (name, token))
        doc['owner_pid'] = os.getpid()
        conn.execute('UPDATE previews SET job=?,document=? WHERE token=?', (job, json.dumps(doc), token))
    return doc


def unreserve(root, token):
    with catalog.connect(root) as conn:
        conn.execute('BEGIN IMMEDIATE')
        _recover(conn, root)
        conn.execute('DELETE FROM deleting WHERE token=?', (token,))
        conn.execute('UPDATE previews SET job=NULL WHERE token=? AND finished=0', (token,))


def execute(root, token, *, dependencies=dependencies, progress=lambda **_: None):
    outcomes = []
    with catalog.connect(root) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM previews WHERE token=?', (token,)).fetchone()
        if not row or not row['job'] or row['finished']:
            raise Refused('No confirmed deletion exists')
        doc = json.loads(row['document'])
        doc['owner_pid'] = os.getpid()
        conn.execute('UPDATE previews SET document=? WHERE token=?', (json.dumps(doc), token))
    for name in doc['names']:
        try:
            with catalog.connect(root) as conn:
                conn.execute('BEGIN IMMEDIATE')
                reserved = conn.execute('SELECT token FROM deleting WHERE name=?', (name,)).fetchone()
                if not reserved or reserved['token'] != token:
                    raise Refused('The deletion reservation is no longer held')
                folder = _owned(root, name)
                if dependencies(name) or _live_leases(conn, name):
                    raise Refused('The run acquired an active dependency after preview')
                if _inventory(folder)[1] != doc['snapshots'][name]:
                    raise Refused('The run changed after confirmation; preview it again')
                # Rename atomically while admission is fenced. Only the root-owned quarantined tree is removed.
                quarantine = _quarantine(root, token, name)
                if quarantine.exists():
                    raise Refused('A recovery folder already exists; recover it before retrying')
                folder.rename(quarantine)
                try:
                    if quarantine.resolve().parent != pathlib.Path(root).resolve():
                        raise Refused('Deletion path escaped its training root')
                    if _inventory(quarantine)[1] != doc['snapshots'][name]:
                        raise Refused('The run directory changed during deletion admission; preview it again')
                    shutil.rmtree(quarantine)
                except BaseException:
                    if quarantine.exists() and not folder.exists():
                        quarantine.rename(folder)
                    raise
                conn.execute('DELETE FROM runs WHERE name=?', (name,))
                outcomes.append({'name': name, 'status': 'deleted'})
        except Exception as problem:
            message = f'{type(problem).__name__}: {problem}'
            outcomes.append({'name': name, 'status': 'failed', 'error': message, 'retryable': True})
            with catalog.connect(root) as conn:
                conn.execute('UPDATE runs SET deletion_error=? WHERE name=?', (message, name))
        finally:
            with catalog.connect(root) as conn:
                conn.execute('DELETE FROM deleting WHERE name=? AND token=?', (name, token))
            progress(state='deleting', processed=len(outcomes), total=doc['total'], outcomes=list(outcomes))
    with catalog.connect(root) as conn:
        doc['outcomes'] = outcomes
        conn.execute('UPDATE previews SET finished=1,document=? WHERE token=?', (json.dumps(doc), token))
    return {'outcomes': outcomes, 'total': len(outcomes), 'failed': sum(r['status'] == 'failed' for r in outcomes)}
