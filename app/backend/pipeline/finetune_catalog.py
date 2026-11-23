"""Incremental training summaries. Listing never loads weights or verifies detailed artifacts."""
from __future__ import annotations

import json
import pathlib
import sqlite3
import hashlib
import re
import uuid
from contextlib import contextmanager

from app.backend.pipeline import util

_SEEN_ROOTS = set()


@contextmanager
def connect(root):
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    store = root / '.catalog'
    store.mkdir(exist_ok=True)
    if store.is_symlink() or getattr(store.lstat(), 'st_file_attributes', 0) & 0x400:
        raise ValueError('A linked catalog storage directory is protected')
    conn = sqlite3.connect(store / 'runs.sqlite3', timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS runs(name TEXT PRIMARY KEY, signature TEXT, summary TEXT NOT NULL, deletion_error TEXT);
        CREATE TABLE IF NOT EXISTS leases(job TEXT NOT NULL, name TEXT NOT NULL, pid INTEGER NOT NULL, PRIMARY KEY(job,name));
        CREATE TABLE IF NOT EXISTS previews(token TEXT PRIMARY KEY, document TEXT NOT NULL, created REAL NOT NULL, job TEXT, finished INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS deleting(name TEXT PRIMARY KEY, token TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS run_identities(name TEXT PRIMARY KEY COLLATE NOCASE, run_id TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS reservations(request_id TEXT PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE NOT NULL,
            run_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL, job_id TEXT);
        CREATE INDEX IF NOT EXISTS runs_invalidated ON runs(signature) WHERE signature IS NULL;
        CREATE INDEX IF NOT EXISTS active_previews ON previews(finished) WHERE job IS NOT NULL AND finished=0;
        CREATE INDEX IF NOT EXISTS leases_by_name ON leases(name);
    ''')
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


class RunConflict(ValueError):
    """A new-run request cannot overwrite or adopt an existing run."""


def reserve_run(root, *, request_id, params, name=None):
    """Reserve a monotonic display name and stable identity before any job starts.

    Reservations survive failures and deletion. A request token retries the same
    creation, while refresh/resume continue to address an existing run by name.
    """
    from app.backend.pipeline.finetune_prep import check_run_name
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', request_id):
        raise ValueError('Invalid run creation request ID')
    requested = check_run_name(name) if name else None
    fingerprint = hashlib.sha256(json.dumps({'name': requested, 'params': params}, sort_keys=True,
                                           separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    root = pathlib.Path(root)
    with connect(root) as conn:
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT name,run_id,fingerprint FROM reservations WHERE request_id=?', (request_id,)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                raise RunConflict('This creation request belongs to different settings')
            return {'name': old['name'], 'run_id': old['run_id'], 'request_id': request_id}
        occupied = {path.name.casefold() for path in root.iterdir()}
        for table in ('runs', 'run_identities', 'reservations'):
            occupied.update(row[0].casefold() for row in conn.execute(f'SELECT name FROM {table}'))
        marker = conn.execute("SELECT value FROM metadata WHERE key='run_high_water'").fetchone()
        highest = max([int(marker[0]) if marker else 0] +
                      [int(match[1]) for value in occupied if (match := re.fullmatch(r'run([0-9]+)', value))])
        if requested:
            if requested.casefold() in occupied:
                raise RunConflict(f'Run {requested} already exists; choose a new name or resume it explicitly')
            name = requested
        else:
            name = f'run{highest + 1}'
        match = re.fullmatch(r'run([0-9]+)', name, re.I)
        highest = max(highest, int(match[1]) if match else 0)
        identity = uuid.uuid4().hex
        conn.execute('INSERT INTO run_identities(name,run_id) VALUES(?,?)', (name, identity))
        conn.execute('INSERT INTO reservations(request_id,name,run_id,fingerprint) VALUES(?,?,?,?)',
                     (request_id, name, identity, fingerprint))
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('run_high_water',?)", (str(highest),))
    return {'name': name, 'run_id': identity, 'request_id': request_id}


def run_identity(root, name):
    """Legacy identity lives in the catalog, without editing frozen run artifacts."""
    from app.backend.pipeline.finetune_prep import check_run_name
    check_run_name(name)
    with connect(root) as conn:
        conn.execute('BEGIN IMMEDIATE')
        folder = pathlib.Path(root) / name
        if folder.is_symlink() or (folder.exists() and getattr(folder.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Linked run storage is protected')
        prepared = util.read_json(folder / 'prepared.json') or {}
        saved = prepared.get('run_id') if isinstance(prepared, dict) else None
        if saved is not None and not re.fullmatch(r'[0-9a-f]{32}', str(saved)):
            raise ValueError('The saved run identity is malformed')
        old = conn.execute('SELECT run_id FROM run_identities WHERE name=?', (name,)).fetchone()
        if old:
            if saved and saved != old[0]:
                raise RunConflict('The saved run has a different reserved identity')
            return old[0]
        identity = saved or uuid.uuid4().hex
        if conn.execute('SELECT 1 FROM run_identities WHERE run_id=?', (identity,)).fetchone():
            raise RunConflict('The saved run identity belongs to another run')
        conn.execute('INSERT INTO run_identities(name,run_id) VALUES(?,?)', (name, identity))
        return identity


def validate_creation(root, *, request_id, name, run_id, params):
    """Authorize only the original immutable request at its reserved target."""
    if not request_id or not run_id:
        raise RunConflict('A new export requires its creation reservation and parent identity')
    with connect(root) as conn:
        row = conn.execute('SELECT name,run_id,fingerprint FROM reservations WHERE request_id=?', (request_id,)).fetchone()
    if not row or row['name'] != name or row['run_id'] != run_id:
        raise RunConflict('Run creation reservation identity or target mismatch')
    # Automatic and custom-name requests have distinct fingerprints. The name
    # itself remains bound to the reserved database row in either case.
    possible = {hashlib.sha256(json.dumps({'name': requested, 'params': params}, sort_keys=True,
                  separators=(',', ':'), allow_nan=False).encode()).hexdigest() for requested in (None, name)}
    if row['fingerprint'] not in possible:
        raise RunConflict('Run creation reservation belongs to different inputs')


def _stat(path):
    try:
        st = path.stat()
        return [st.st_mtime_ns, st.st_size, st.st_ino]
    except FileNotFoundError:
        return None


def _signature(folder):
    # Imports/evaluations publish a new directory; job completion also invalidates explicitly.
    return json.dumps([4, *[_stat(folder / n) for n in ('prepared.json', 'cpu_validation.json', 'imports', 'eval_imports', 'trial_imports', 'bundle.zip')]])


def _summary(folder):
    prepared = util.read_json(folder / 'prepared.json') or {}
    if not isinstance(prepared, dict):
        prepared = {}
    state = 'configured'
    validation = util.read_json(folder / 'cpu_validation.json') or {}
    if not isinstance(validation, dict):
        validation = {}
    if validation.get('passed') and validation.get('bundle_sha256') == (prepared.get('bundle') or {}).get('sha256'):
        state = 'cpu_validated'
    from app.backend.pipeline import finetune_prep as fp
    manifest_sha = fp._current_bundle_manifest_sha(folder)
    trained_adapters = set()
    imports = folder / 'imports'
    for path in imports.glob('*/import.json') if imports.is_dir() else ():
        if prepared.get('mode') == 'trial':
            break
        info = util.read_json(path) or {}
        if not isinstance(info, dict):
            continue
        if not manifest_sha or info.get('bundle_manifest_sha256') != manifest_sha:
            continue
        if info.get('type') in ('gpu_smoke', 'tpu_smoke') and state in ('configured', 'cpu_validated'):
            state = 'gpu_smoke_tested'
        if info.get('type') == 'trained':
            state = 'trained'
            trained_adapters.add((info.get('adapter') or {}).get('sha256'))
    for path in (folder / 'eval_imports').glob('*/import.json'):
        info = util.read_json(path) or {}
        if not isinstance(info, dict):
            continue
        if (info.get('counts_as_evaluation') is True and info.get('adapter_sha256') in trained_adapters
                and info.get('bundle_manifest_sha256', manifest_sha) == manifest_sha):
            state = 'evaluated'
    return {'name': folder.name, 'run_id': prepared.get('run_id'), 'state': state, 'model': prepared.get('model'),
            'mode': prepared.get('mode', 'full'), 'total_budget_minutes': prepared.get('total_budget_minutes'),
            'trial_coverage': (prepared.get('trial_data') or {}).get('coverage'),
            'evaluation_availability': prepared.get('evaluation_availability'),
            'dataset': prepared.get('dataset'), 'model_revision': prepared.get('model_revision'),
            'stage': prepared.get('stage'), 'prepared_at': prepared.get('prepared_at'),
            'purpose': prepared.get('purpose'), 'profile': prepared.get('profile'),
            'bundle': {**{k: v for k, v in (prepared.get('bundle') or {}).items() if k in ('bytes', 'files', 'sha256')},
                       'warnings': (prepared.get('bundle') or {}).get('warnings') or []},
            'overrides': len(prepared.get('overrides') or {}), 'blocked': []}


def refresh(root, *, force=False):
    """Reconcile once per process/root change; writer invalidations update only named rows."""
    root = pathlib.Path(root)
    key = str(root.resolve())
    initial = key not in _SEEN_ROOTS
    with connect(root) as conn:
        conn.execute('BEGIN IMMEDIATE')
        stamp = str(root.stat().st_mtime_ns)
        marker = conn.execute("SELECT value FROM metadata WHERE key='root_mtime'").fetchone()
        reconcile = force or initial or not marker or marker['value'] != stamp
        if reconcile:
            cached = {r['name']: dict(r) for r in conn.execute('SELECT name,signature,deletion_error FROM runs')}
            present = set()
            for folder in root.iterdir():
                if folder.name.startswith('.') or folder.is_symlink() or not folder.is_dir():
                    continue
                previous = cached.get(folder.name)
                if previous and not force and not initial and previous['signature'] is not None:
                    present.add(folder.name)
                    continue
                if not (folder / 'prepared.json').is_file() and not (previous or {}).get('deletion_error'):
                    continue
                present.add(folder.name)
                signature = _signature(folder)
                if not previous or previous['signature'] != signature:
                    if not (folder / 'prepared.json').is_file() and (previous or {}).get('deletion_error'):
                        conn.execute('UPDATE runs SET signature=? WHERE name=?', (signature, folder.name))
                    else:
                        conn.execute('INSERT INTO runs(name,signature,summary) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET signature=excluded.signature,summary=excluded.summary',
                                     (folder.name, signature, json.dumps(_summary(folder))))
            for name in cached.keys() - present:
                conn.execute('DELETE FROM runs WHERE name=?', (name,))
            conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('root_mtime',?)", (stamp,))
        else:
            for row in list(conn.execute('SELECT name,deletion_error FROM runs WHERE signature IS NULL')):
                folder = root / row['name']
                if not folder.is_dir() or folder.is_symlink():
                    conn.execute('DELETE FROM runs WHERE name=?', (row['name'],))
                elif not (folder / 'prepared.json').is_file() and row['deletion_error']:
                    conn.execute('UPDATE runs SET signature=? WHERE name=?', (_signature(folder), row['name']))
                else:
                    conn.execute('UPDATE runs SET signature=?,summary=? WHERE name=?',
                                 (_signature(folder), json.dumps(_summary(folder)), row['name']))
    _SEEN_ROOTS.add(key)


def invalidate(root, name):
    with connect(root) as conn:
        changed = conn.execute('UPDATE runs SET signature=NULL WHERE name=?', (name,)).rowcount
        if not changed:
            folder = pathlib.Path(root) / name
            if folder.is_dir() and not folder.is_symlink() and (folder / 'prepared.json').is_file():
                # A page may discover the directory before preparation publishes
                # its metadata. Do not require another root-directory change.
                conn.execute('INSERT INTO runs(name,signature,summary) VALUES(?,NULL,?)',
                             (name, json.dumps(_summary(folder))))


def page(root, *, limit=100, offset=0, search='', state=''):
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError('limit must be 1 to 500 and offset must be nonnegative')
    from app.backend.pipeline import finetune_deletion
    finetune_deletion.recover(root)
    refresh(root)
    clauses, args = [], []
    if search:
        clauses.append("instr(lower(name || ' ' || coalesce(json_extract(summary,'$.model'),'') || ' ' || coalesce(json_extract(summary,'$.dataset.tag'),'')), lower(?)) > 0")
        args.append(search.strip())
    if state:
        clauses.append("json_extract(summary,'$.state')=?")
        args.append(state)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    with connect(root) as conn:
        conn.execute('BEGIN')
        total = conn.execute('SELECT count(*) FROM runs' + where, args).fetchone()[0]
        rows = conn.execute('SELECT summary,deletion_error FROM runs' + where + ' ORDER BY name LIMIT ? OFFSET ?', [*args, limit, offset])
        runs = [{**json.loads(r['summary']), **({'deletion_error': r['deletion_error']} if r['deletion_error'] else {})} for r in rows]
    return {'runs': runs, 'root': pathlib.Path(root).as_posix(), 'total': total, 'limit': limit, 'offset': offset}


def names(root, *, search='', state=''):
    refresh(root)
    clauses, args = [], []
    if search:
        clauses.append("instr(lower(name || ' ' || coalesce(json_extract(summary,'$.model'),'') || ' ' || coalesce(json_extract(summary,'$.dataset.tag'),'')), lower(?)) > 0")
        args.append(search.strip())
    if state:
        clauses.append("json_extract(summary,'$.state')=?")
        args.append(state)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    with connect(root) as conn:
        return [r['name'] for r in conn.execute('SELECT name FROM runs' + where + ' ORDER BY name', args)]
