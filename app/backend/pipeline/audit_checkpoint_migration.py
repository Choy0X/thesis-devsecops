"""One pinned storage-layout transition, never an admission/evidence exception.

Call only from the builder's normal locked, freshly captured source path. The
old checkpoint is read-only; current raw-row identity, registration, projection
and publication checks remain the builder's responsibility. No live audit or
checkpoint migration is started by importing this module.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
from contextlib import closing

SCHEMA = 'sft-output-layout-migration/1'
APPROVED_OLD = {
    'source_identity': 'f64f77abe75b3b98655e868837a79849d45a24859cb56170713d5e3367bc3121',
    'policy_sha256': 'f87ddc4991f9259ac0701416014574dd39e44e0eb33d23ca1ef0b4d1bad141e4',
    'checkpoint_sha256': '4614a3acccf1ed38c5d5237707148bbf1d73a94e7ecdc506a2a6da1734a7e7eb',
    'owner_sha256': '6ee9386cd7230030ce8d6005b7db0221108246aa472a526cd762a16c9b1f90e5',
    'builder_sha256': '0e4df08eb8624a93029e7a07826401e6b3d3febbd6699b1ef8179ce965812a3c',
    'cli_sha256': 'e7e89353c7b534d89fa3bf35aeb07eb79c9b88b46d5637c5d91b78aa1a102047',
    'tag': 'v26', 'owner': 'sft-84246809f6da4eb890200ff8e2d79f14', 'seed_count': 8927,
}
# Filled only with the root-approved final builder/CLI/writer byte freeze and
# exact layout identity. Any future implementation needs a new reviewed mapping.
# This module's own SHA is captured by the current policy, avoiding a hash cycle.
APPROVED_NEW = {
    'builder_sha256': '11fef8d3ac8f5beceea04c29c6025df7ca83fc32167bcd0328b54c9aa4b0aacb',
    'cli_sha256': '2010d7d82bebfa0cc4ed153c75e6d17aea53de2d9ef175d82d9ceb0a8c164c25',
    'writer_sha256': '21a11b2191fd37113a02f20847504ca346191a9633bb04a94670aa138204ba61',
    'output_layout': {'schema': 'bounded-parquet-output/1', 'max_rows': 64, 'max_arrow_bytes': 4194304,
                      'oversized_single_row': 'flush_previous_then_write_intact'},
}


def _implementation_paths():
    package = Path(__file__).resolve().parent
    return {'builder': package / 'sft_dataset_build.py',
            'cli': package.parents[2] / 'tools' / 'build_sft_dataset.py',
            'writer': package / 'bounded_parquet.py', 'migration': Path(__file__).resolve()}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def _fail(detail):
    raise ValueError('output-layout migration rejected: ' + detail)


def _pins():
    if (set(APPROVED_NEW) != {'builder_sha256', 'cli_sha256', 'writer_sha256', 'output_layout'}
            or any(not _hash(APPROVED_NEW.get(name + '_sha256')) for name in ('builder', 'cli', 'writer'))
            or not isinstance(APPROVED_NEW.get('output_layout'), dict)):
        _fail('exact new implementation mapping is not approved')
    if (any(not _hash(APPROVED_OLD.get(key)) for key in (
            'source_identity', 'policy_sha256', 'checkpoint_sha256', 'owner_sha256', 'builder_sha256', 'cli_sha256'))
            or type(APPROVED_OLD.get('seed_count')) is not int or APPROVED_OLD['seed_count'] <= 0):
        _fail('invalid fixed old checkpoint pins')


def _snapshot(root):
    from . import sft_dataset_build as builder

    workspace = builder._safe(root, root / '.sft_build' / APPROVED_OLD['source_identity'])
    owner_path = builder._safe(root, workspace / 'owner.json')
    database = builder._safe(root, workspace / 'checkpoints.sqlite3')
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = builder._safe(root, database.with_name(database.name + suffix))
        if sidecar.exists():
            _fail('old checkpoint is not the approved closed database')
    for path, key in ((owner_path, 'owner_sha256'), (database, 'checkpoint_sha256')):
        if not path.is_file() or _sha(path) != APPROVED_OLD[key]:
            _fail('old owner/checkpoint bytes changed')
    if owner_path.stat().st_size > 8 << 20:
        _fail('old owner metadata exceeds its bound')
    try:
        state = json.loads(owner_path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError):
        _fail('invalid old owner metadata')
    if (not isinstance(state, dict) or state.get('source_identity') != APPROVED_OLD['source_identity']
            or state.get('owner') != APPROVED_OLD['owner'] or state.get('tag') != APPROVED_OLD['tag']
            or state.get('complete') is not False or not isinstance(state.get('capture'), dict)
            or _digest(state['capture']) != APPROVED_OLD['source_identity']
            or _digest(state['capture'].get('policy')) != APPROVED_OLD['policy_sha256']):
        _fail('old owner/capture/policy identity changed')
    return workspace, database, state['capture']


def _policy(base, old):
    from . import sft_dataset_build as builder

    if (not isinstance(base, dict) or set(base) != {'schema', 'source', 'authority', 'policy'}
            or set(old) != set(base) or not isinstance(base.get('policy'), dict)):
        _fail('invalid current/old capture shape')
    paths = _implementation_paths()
    paths = {key: Path(value).absolute() for key, value in paths.items()}
    old_files = old['policy'].get('files')
    current_files = base['policy'].get('files')
    if not isinstance(old_files, dict) or not isinstance(current_files, dict):
        _fail('invalid policy implementation inventory')
    if (old_files.get(str(paths['builder'])) != APPROVED_OLD['builder_sha256']
            or old_files.get(str(paths['cli'])) != APPROVED_OLD['cli_sha256']
            or any(str(paths[name]) in old_files for name in ('writer', 'migration'))):
        _fail('old implementation inventory is not the approved transition')
    expected = dict(old_files)
    expected.update({str(paths[name]): APPROVED_NEW[name + '_sha256'] for name in ('builder', 'cli', 'writer')})
    migration_path = builder._safe(paths['migration'].parent, paths['migration'])
    if not migration_path.is_file():
        _fail('missing captured migration implementation')
    expected[str(paths['migration'])] = _sha(migration_path)
    if _json(current_files) != _json(expected):
        _fail('nonallowlisted implementation change')
    normalized = json.loads(_json(base))
    normalized['policy']['files'] = old_files
    layout = normalized['policy'].pop('output_layout', None)
    if _json(layout) != _json(APPROVED_NEW['output_layout']) or _json(normalized) != _json(old):
        _fail('source/authority/nonstorage policy changed')
    for name, expected_sha in current_files.items():
        if not isinstance(name, str) or not _hash(expected_sha):
            _fail('invalid implementation descriptor')
        path = Path(name)
        if not path.is_absolute():
            _fail('implementation paths must be exact absolute paths')
        builder._safe(path.parent, path)
        if not path.is_file() or _sha(path) != expected_sha:
            _fail('current captured implementation bytes changed')


def _rows(database):
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as prior:
        schema = prior.execute('PRAGMA table_info(items)').fetchall()
        if [(row[1], row[2].upper(), row[5]) for row in schema] != [
                ('rid', 'TEXT', 1), ('identity', 'TEXT', 0), ('disposition', 'TEXT', 0), ('views', 'TEXT', 0)]:
            _fail('unexpected checkpoint row schema')
        for row in prior.execute('SELECT rid,identity,disposition,views FROM items ORDER BY rid'):
            rid, identity, body, views = row
            if not all(isinstance(value, str) for value in row) or not rid or not _hash(identity):
                _fail('invalid checkpoint row identity/types')
            try:
                disposition, native_views = json.loads(body), json.loads(views)
                # Reserialization rejects imported NaN/Infinity without changing
                # the original strings that are copied into the new checkpoint.
                _json(disposition)
                _json(native_views)
            except (ValueError, TypeError, RecursionError):
                _fail('invalid checkpoint JSON payload')
            if (not isinstance(disposition, dict) or disposition.get('record_id') != rid
                    or not isinstance(native_views, list) or any(
                        not isinstance(view, dict) or view.get('record_id') != rid for view in native_views)):
                _fail('checkpoint payload record identity/types changed')
            yield row


def migration_seed(base, root):
    """Return the one approved immutable seed, rechecking known evidence twice.

    The builder calls this under its existing OS lock after fresh source and
    authority capture. Imported metadata cannot authorize another old stage.
    """
    from . import sft_dataset_build as builder

    _pins()
    root = Path(root).absolute()
    builder._safe(root, root)
    workspace, database, old = _snapshot(root)
    _policy(base, old)
    count = sum(1 for _ in _rows(database))
    if count != APPROVED_OLD['seed_count']:
        _fail('old checkpoint row count changed')
    if builder._unresolved(workspace, root, retry_unknown=False):
        _fail('known backend evidence changed; request a normal evidence retry')
    _snapshot(root)
    _policy(base, old)
    if builder._unresolved(workspace, root, retry_unknown=False):
        _fail('known backend evidence changed; request a normal evidence retry')
    _snapshot(root)
    _policy(base, old)
    return {'workspace': str(workspace), 'receipt': {
        'schema': SCHEMA, 'seed_count': count,
        'old': {key: APPROVED_OLD[key] for key in ('source_identity', 'policy_sha256', 'owner_sha256',
                                                 'checkpoint_sha256', 'tag', 'owner')},
        'new': {'source_identity': _digest(base), 'policy_sha256': _digest(base['policy']),
                'files': dict(base['policy']['files']), 'output_layout': dict(APPROVED_NEW['output_layout'])},
    }}


def seed_checkpoints(db, descriptor, base, root):
    """Copy all pinned cached decisions unchanged; rollback on any late drift."""
    current = migration_seed(base, root)
    if not isinstance(descriptor, dict) or _json(descriptor) != _json(current):
        _fail('seed descriptor is not the approved current transition')
    database = Path(current['workspace']) / 'checkpoints.sqlite3'
    db.execute('SAVEPOINT output_layout_seed')
    try:
        for row in _rows(database):
            existing = db.execute('SELECT rid,identity,disposition,views FROM items WHERE rid=?', (row[0],)).fetchone()
            if existing is None:
                db.execute('INSERT INTO items VALUES (?,?,?,?)', row)
            elif existing != row:
                _fail('existing checkpoint row differs from pinned seed')
        # Rehash the old closed DB/owner and all code; recheck backend artifacts
        # and registration revocation after copying, before committing imports.
        if _json(migration_seed(base, root)) != _json(current):
            _fail('seed changed during copy')
        db.execute('RELEASE output_layout_seed')
    except BaseException:
        db.execute('ROLLBACK TO output_layout_seed')
        db.execute('RELEASE output_layout_seed')
        raise
    db.commit()
    return current['receipt']
