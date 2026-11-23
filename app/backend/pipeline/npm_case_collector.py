"""Pinned npm advisory discovery and source-only candidate collection.

Discovery is not a security verdict. The collector never runs package code,
installs dependencies, touches the campaign store, or changes a frozen dataset.
Only exact single-parent GitHub fixes with matching package and pinned permissive
notices become unreviewed records. Security admission belongs to the builder.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote
import uuid
import zipfile

from . import oslock

POLICY = 'npm-targeted-case-collector/2'
SOURCE_BINDING = 'npm-exact-source-binding/2'
SCHEMA = 'npm-case-shards/1'
_SHA = re.compile(r'[0-9a-f]{40}\Z')
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_CWES = {'CWE-1321', 'CWE-78', 'CWE-77', 'CWE-22', 'CWE-23'}
MAX_SHARD_BYTES = 8 << 20
MAX_SHARD_RECORDS = 256


class UnsupportedCollection(ValueError):
    """Source attribution is outside this collector's proven mapping subset."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _hash(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''): result.update(chunk)
    return result.hexdigest()


@lru_cache(maxsize=8)
def _immutable_dump_hash(path, size, modified, changed, inode):
    result = _hash(path)
    stat = Path(path).stat()
    if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino) != (size, modified, changed, inode):
        raise ValueError('npm_discovery_snapshot_changed_while_hashing')
    return result


def _dump_hash(path):
    stat = Path(path).stat()
    return _immutable_dump_hash(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)


def _dump_directory(path):
    # The standard library's bounded EOCD reader handles ZIP64 without loading
    # the central directory. Check its declared size before ZipFile allocates
    # one Python object per member. Supported Python runtimes ship this helper.
    with Path(path).open('rb') as stream:
        record = zipfile._EndRecData(stream)
    if record is None: raise ValueError('npm_discovery_snapshot_not_a_zip')
    count, central_bytes = record[zipfile._ECD_ENTRIES_TOTAL], record[zipfile._ECD_SIZE]
    if count > 500000 or central_bytes > 64 << 20 or count * 512 + central_bytes > 320 << 20:
        raise ValueError('npm_discovery_central_directory_budget_exceeded')
    return count, central_bytes


def configured_snapshot(*, root=None):
    """Resolve a pinned configured snapshot, preserving legacy cached sources."""
    root = _root(root)
    config = root / '.npm_case_collector/configured-source.json'
    if config.exists():
        if config.stat().st_size > 64 << 10: raise ValueError('npm_discovery_snapshot_descriptor_budget_exceeded')
        descriptor = json.loads(config.read_text(encoding='utf-8'))
        logical = descriptor.get('path') if isinstance(descriptor, dict) else None
        if (not isinstance(logical, str) or Path(logical).is_absolute() or '\\' in logical
                or any(part in ('', '.', '..') for part in logical.split('/'))):
            raise ValueError('npm_discovery_snapshot_path_invalid')
        path = (root / logical).resolve()
        if (not path.is_relative_to(root) or descriptor.get('schema') != 'npm-discovery-snapshot/1'
                or not _HEX.fullmatch(str(descriptor.get('sha256')))
                or type(descriptor.get('bytes')) is not int or not 0 < descriptor['bytes'] <= 1 << 30
                or not path.is_file() or path.stat().st_size != descriptor['bytes'] or _dump_hash(path) != descriptor['sha256']):
            raise ValueError('npm_discovery_snapshot_hash_or_size_mismatch')
        return {'state': 'ready', 'path': str(path), 'descriptor': descriptor, 'sha256': descriptor['sha256']}
    from .ingest import osv
    path = (osv.RAW / 'osv_npm.zip').resolve()
    if not path.is_file():
        return {'state': 'waiting', 'code': 'npm_discovery_dump_unavailable', 'reason': 'A pinned npm OSV snapshot is required.'}
    if path.stat().st_size > 1 << 30: raise ValueError('npm_discovery_snapshot_size_budget_exceeded')
    sha = _dump_hash(path)
    return {'state': 'ready', 'path': str(path), 'sha256': sha,
            'descriptor': {'schema': 'npm-discovery-snapshot/1', 'path': str(path), 'sha256': sha,
                           'bytes': path.stat().st_size, 'source': 'existing_cached_osv_npm_snapshot'}}


def _root(root=None):
    if root is None:
        from . import versions
        root = versions.PROC
    result = Path(root).resolve()
    result.mkdir(parents=True, exist_ok=True)
    return result


def _repository_identity(value):
    from . import ecosystems
    if not isinstance(value, str): return None
    return ecosystems.repository_identity(value if '://' in value else 'https://' + value)


@contextmanager
def _connect(root):
    db = sqlite3.connect(_root(root) / '.npm_case_collector.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE IF NOT EXISTS dumps (sha TEXT PRIMARY KEY,path TEXT,position INTEGER NOT NULL, members INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS existing_fixes (repository TEXT,revision TEXT,PRIMARY KEY(repository,revision));
      CREATE TABLE IF NOT EXISTS parent_indexes (identity TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS known_complete_indexes (identity TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS known_complete_fixes (identity TEXT,repository TEXT,revision TEXT,
        PRIMARY KEY(identity,repository,revision));
      CREATE TABLE IF NOT EXISTS discovery_scans (dump_sha TEXT,index_identity TEXT,path TEXT,
        position INTEGER NOT NULL,members INTEGER NOT NULL,PRIMARY KEY(dump_sha,index_identity));
      CREATE TABLE IF NOT EXISTS work (id TEXT PRIMARY KEY,body TEXT NOT NULL,state TEXT NOT NULL,
        failure_count INTEGER NOT NULL DEFAULT 0,last_error TEXT,next_retry_at REAL,token TEXT);
      CREATE INDEX IF NOT EXISTS collector_state ON work(state,next_retry_at);
      CREATE TABLE IF NOT EXISTS shards (sha TEXT PRIMARY KEY,path TEXT NOT NULL,records INTEGER NOT NULL,bytes INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS discoveries (work_id TEXT,advisory_sha TEXT,body TEXT NOT NULL,
        PRIMARY KEY(work_id,advisory_sha));
      CREATE TABLE IF NOT EXISTS exact_files (repository TEXT,revision TEXT,path TEXT,artifact_sha TEXT,
        content_sha TEXT,PRIMARY KEY(repository,revision,path));
      CREATE TABLE IF NOT EXISTS source_units (record_id TEXT,source_sha TEXT,artifact_sha TEXT,
        shard_sha TEXT,policy TEXT,PRIMARY KEY(record_id,source_sha,artifact_sha));
      CREATE TABLE IF NOT EXISTS package_scope_revisions (work_id TEXT,scope_policy TEXT,
        previous_state TEXT,previous_error TEXT,next_state TEXT,changed_at REAL,
        PRIMARY KEY(work_id,scope_policy));
    ''')
    for name in ('shard_sha', 'artifact_sha', 'collection_policy', 'scope_policy'):
        if name not in {row[1] for row in db.execute('PRAGMA table_info(work)')}:
            db.execute('ALTER TABLE work ADD COLUMN ' + name + ' TEXT')
    # Earlier policy work remains auditable, but cannot inherit the new source
    # basis or suppress newly permitted discovery hints.
    db.execute("UPDATE work SET collection_policy='npm-targeted-case-collector/1' WHERE collection_policy IS NULL")
    db.commit()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _check(bounds):
    from .npm_evidence_sources import SourceCancelled, SourceBudgetExceeded
    if bounds['should_stop'](): raise SourceCancelled('case_collection_cancelled')
    if time.monotonic() >= bounds['deadline']: raise SourceBudgetExceeded('source_time_budget_exceeded')


def _advisory(record, raw, dump_sha):
    from .ingest import fixrefs
    from .npm_evidence_sources import _repository
    if not isinstance(record, dict) or record.get('withdrawn'): return []
    affected = [item for item in record.get('affected', []) if isinstance(item, dict)
                and (item.get('package') or {}).get('ecosystem') == 'npm'
                and isinstance((item.get('package') or {}).get('name'), str)]
    if not affected: return []
    cwes = (record.get('database_specific') or {}).get('cwe_ids') or []
    text = (str(record.get('summary', '')) + ' ' + str(record.get('details', ''))).lower()
    # Selection hints only. The unit's labels remain unreviewed.
    if not set(cwes) & _CWES and not any(term in text for term in ('prototype pollution', 'path traversal', 'command injection', 'shell injection')):
        return []
    safe = {**record, 'affected': affected,
            'references': [r for r in record.get('references', []) if isinstance(r, dict) and r.get('type') in ('FIX', 'WEB')]}
    pointers = []
    for pointer in fixrefs.from_osv_record(safe):
        if pointer['host'] != 'github.com' or not _SHA.fullmatch(pointer['sha']): continue
        try: repository = _repository('https://github.com/' + pointer['owner'] + '/' + pointer['repo']).lower()
        except ValueError:
            # ignored: unsupported repositories remain discovery hints and
            # never enter the public source acquisition queue.
            continue
        pointers.append((pointer, repository))
    source_urls = sorted({repository + '/commit/' + pointer['sha'] for pointer, repository in pointers})
    output = []
    for pointer, repository in pointers:
        references = sorted({r['type'] for r in safe['references']
            if (parsed := fixrefs.parse_commit_url(r.get('url', ''))) is not None
            and parsed['sha'] == pointer['sha'] and _repository_identity(parsed['repo_url']) == _repository_identity(repository)})
        if pointer['via'] == 'osv_git_range': references = sorted(set(references) | {'GIT_FIXED_EVENT'})
        reference_kind = 'GIT_FIXED_EVENT' if 'GIT_FIXED_EVENT' in references else 'FIX' if 'FIX' in references else 'WEB'
        candidate = {'repository_url': repository, 'after_revision': pointer['sha'], 'commit_key': repository.removeprefix('https://') + '@' + pointer['sha'],
            'advisory_id': record.get('id'), 'aliases': sorted(set([str(record.get('id'))] + [str(a) for a in record.get('aliases', [])])),
            'advisory_sha256': hashlib.sha256(raw).hexdigest(), 'dump_sha256': dump_sha,
            'packages': sorted({item['package']['name'] for item in affected}), 'affected': affected,
            'summary': record.get('summary'), 'details': record.get('details'), 'cwe_ids': list(cwes),
            'published': record.get('published'), 'modified': record.get('modified'), 'via': pointer['via'],
            'source_urls': source_urls, 'reference_kind': reference_kind, 'reference_kinds': references,
            'reference_strength': 'declared' if reference_kind in ('FIX', 'GIT_FIXED_EVENT') else 'mentioned',
            'security_claims_created': False, 'advisory_fix_status': 'unknown'}
        if len(_json(candidate).encode()) <= 64 << 10:
            output.append(candidate)
    return output


def plan(dump_path=None, *, root=None, parent_records=(), parent_identity=None, limit=500, should_stop=lambda: False, progress=None):
    """Advance a pinned local npm OSV dump by at most limit members.

    The dump is an explicit cached source, not a campaign restart. New snapshots
    have separate cursors. Historical fixes are streamed into a disk-backed set.
    No advisory text is stored in workflow metadata; the collector owns its
    bounded per-advisory discovery records with exact raw hashes.
    """
    if type(limit) is not int or not 0 < limit <= 5000: raise ValueError('invalid_discovery_limit')
    if parent_identity is not None and (not isinstance(parent_identity, str) or not 0 < len(parent_identity) <= 512):
        raise ValueError('invalid_complete_index_identity')
    if dump_path is None:
        from .ingest import osv
        dump_path = osv.RAW / 'osv_npm.zip'
    path, root = Path(dump_path).resolve(), _root(root)
    if not path.is_file():
        return {'state': 'waiting', 'code': 'npm_discovery_dump_unavailable', 'reason': 'A pinned local npm OSV snapshot is required.', 'exhausted': False, 'queued': 0}
    if path.stat().st_size > 1 << 30: raise ValueError('npm_discovery_snapshot_size_budget_exceeded')
    identity = _dump_hash(path)
    member_count, _ = _dump_directory(path)
    queued = present = processed = 0
    lock = oslock.FileLock(root / '.npm_case_collector.discovery.lock', note=False)
    lock.acquire(wait=0)
    try:
        with _connect(root) as db, zipfile.ZipFile(path) as archive:
            # Legacy global indexes/cursors remain readable only for the legacy
            # None namespace. A production policy identity gets an independent
            # skip set and scan, including when an earlier policy finished.
            index_needed = parent_identity is None or not db.execute('SELECT 1 FROM known_complete_indexes WHERE identity=?', (parent_identity,)).fetchone()
            for row in parent_records if index_needed else ():
                if should_stop(): return {'state': 'cancelled', 'queued': 0, 'exhausted': False}
                repository, revision = _repository_identity(row.get('repository_identity')), row.get('fix_commit')
                if repository and revision:
                    if parent_identity is None:
                        db.execute('INSERT OR IGNORE INTO existing_fixes VALUES (?,?)', (repository.rstrip('/'), revision))
                    else:
                        db.execute('INSERT OR IGNORE INTO known_complete_fixes VALUES (?,?,?)', (parent_identity, repository.rstrip('/'), revision))
            if parent_identity is not None: db.execute('INSERT OR IGNORE INTO known_complete_indexes VALUES (?)', (parent_identity,))
            members = archive.infolist()
            if len(members) != member_count or len(members) > 500000: raise ValueError('npm_discovery_member_budget_exceeded')
            if parent_identity is None:
                saved = db.execute('SELECT position,members FROM dumps WHERE sha=?', (identity,)).fetchone()
                db.execute('INSERT OR IGNORE INTO dumps VALUES (?,?,0,?)', (identity, str(path), len(members)))
            else:
                saved = db.execute('SELECT position,members FROM discovery_scans WHERE dump_sha=? AND index_identity=?', (identity, parent_identity)).fetchone()
                db.execute('INSERT OR IGNORE INTO discovery_scans VALUES (?,?,?,0,?)', (identity, parent_identity, str(path), len(members)))
            position = saved['position'] if saved else 0
            last_checkpoint = time.monotonic()
            for index in range(position, min(len(members), position + limit)):
                if should_stop(): break
                member = members[index]
                if member.file_size > 2 << 20: raise ValueError('npm_advisory_member_budget_exceeded')
                raw = archive.read(member)
                if not member.is_dir() and member.filename.endswith('.json'):
                    record = json.loads(raw)
                    for task in _advisory(record, raw, identity):
                        from . import fsio
                        known = (db.execute('SELECT 1 FROM existing_fixes WHERE repository=? AND revision=?', (_repository_identity(task['repository_url']), task['after_revision'])).fetchone()
                            if parent_identity is None else db.execute('SELECT 1 FROM known_complete_fixes WHERE identity=? AND repository=? AND revision=?',
                                (parent_identity, _repository_identity(task['repository_url']), task['after_revision'])).fetchone())
                        if known:
                            present += 1; continue
                        discovery_path = root / '.npm_case_collector/discoveries' / (task['advisory_sha256'] + '.json')
                        if discovery_path.exists():
                            if _hash(discovery_path) != task['advisory_sha256']: raise ValueError('collector_discovery_artifact_corrupted')
                        else:
                            discovery_path.parent.mkdir(parents=True, exist_ok=True)
                            fsio.write_bytes(discovery_path, raw)
                        work_id = _digest({'policy': POLICY, 'repository': task['repository_url'], 'revision': task['after_revision']})
                        cursor = db.execute("INSERT OR IGNORE INTO work(id,body,state,collection_policy) VALUES (?,?,'pending',?)", (work_id, _json(task), POLICY))
                        queued += cursor.rowcount
                        inserted = db.execute('INSERT OR IGNORE INTO discoveries VALUES (?,?,?)', (work_id, task['advisory_sha256'], _json(task))).rowcount
                        if inserted:
                            db.execute("UPDATE work SET state='pending',failure_count=0,last_error=NULL WHERE id=? AND state='collected'", (work_id,))
                processed += 1; position = index + 1
                if parent_identity is None:
                    db.execute('UPDATE dumps SET position=? WHERE sha=?', (position, identity))
                else:
                    db.execute('UPDATE discovery_scans SET position=? WHERE dump_sha=? AND index_identity=?', (position, identity, parent_identity))
                # Commit the cursor and queue together, without one durable
                # flush per mostly irrelevant advisory. A cut replays at most
                # this bounded prefix; immutable artifact writes are idempotent.
                if processed % 100 == 0 or time.monotonic() - last_checkpoint >= 1:
                    db.commit()
                    last_checkpoint = time.monotonic()
                if progress: progress(label='Discovering declared npm fix references', processed=position, total=len(members))
            return {'state': 'cancelled' if should_stop() else 'done', 'policy': POLICY, 'dump_sha256': identity,
                    'processed': processed, 'position': position, 'total': len(members), 'queued': queued,
                    'already_present': present, 'exhausted': position == len(members), 'scope': 'configured_pinned_npm_osv_snapshot',
                    'index_identity': parent_identity}
    finally:
        lock.release()


def page(*, root=None, limit=50, offset=0):
    if type(limit) is not int or not 0 < limit <= 1000 or type(offset) is not int or offset < 0: raise ValueError('invalid_page')
    with _connect(root) as db:
        total = db.execute('SELECT count(*) FROM work').fetchone()[0]
        rows = db.execute('SELECT id,state,failure_count,last_error,next_retry_at,collection_policy FROM work ORDER BY id LIMIT ? OFFSET ?', (limit, offset)).fetchall()
    return {'items': [dict(row) for row in rows], 'total': total, 'limit': limit, 'offset': offset}


def queue_summary(*, root=None):
    now = time.time()
    with _connect(root) as db:
        states = dict(db.execute('SELECT state,count(*) FROM work WHERE collection_policy=? GROUP BY state', (POLICY,)))
        remaining = db.execute("SELECT count(*) FROM work WHERE collection_policy=? AND (state IN ('pending','running','waiting') OR state='failed' AND failure_count<3)", (POLICY,)).fetchone()[0]
        eligible = db.execute("SELECT count(*) FROM work WHERE collection_policy=? AND (state='pending' OR state='waiting' AND next_retry_at<=? OR state='failed' AND failure_count<3)", (POLICY, now)).fetchone()[0]
        retry = db.execute("SELECT min(next_retry_at) FROM work WHERE collection_policy=? AND state='waiting' AND next_retry_at>?", (POLICY, now)).fetchone()[0]
    return {'remaining': remaining, 'eligible': eligible, 'exhausted': remaining == 0, 'total': sum(states.values()),
            'states': states, **({'next_retry_at': retry} if retry else {})}


def discovery_summary(*, root=None, parent_identity=None, dump_sha256=None):
    """Read only the exact configured dump and admission-index scan cursor."""
    if parent_identity is not None and (not isinstance(parent_identity, str) or not 0 < len(parent_identity) <= 512):
        raise ValueError('invalid_complete_index_identity')
    if dump_sha256 is None:
        configured = configured_snapshot(root=root)
        if configured.get('state') != 'ready': return {**configured, 'exhausted': False}
        dump_sha256 = configured['sha256']
    if not _HEX.fullmatch(str(dump_sha256)): raise ValueError('invalid_discovery_dump_identity')
    with _connect(root) as db:
        row = (db.execute('SELECT position,members FROM dumps WHERE sha=?', (dump_sha256,)).fetchone()
            if parent_identity is None else db.execute('SELECT position,members FROM discovery_scans WHERE dump_sha=? AND index_identity=?',
                (dump_sha256, parent_identity)).fetchone())
    position, total = (row['position'], row['members']) if row else (0, None)
    return {'dump_sha256': dump_sha256, 'index_identity': parent_identity, 'position': position, 'total': total,
            'exhausted': row is not None and position == total, 'scope': 'configured_pinned_npm_osv_snapshot'}


def _blob(text):
    raw = text.encode('utf-8')
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def _production(path):
    from . import ecosystems
    from .ingest.codemine import language_of
    return path.endswith(('.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx')) and ecosystems.categorize_path(path, 'npm', language_of(path)) == 'source'


def _root_package(task, files, tree, boundaries=()):
    package = json.loads(files.get('package.json', '{}'))
    if not isinstance(package, dict) or package.get('name') not in task['packages']:
        if 'package.json' in boundaries or any(path.endswith('/package.json') for path in (*tree, *boundaries)):
            raise UnsupportedCollection('collector_nested_package_mapping_unsupported:root_package_not_bound')
        raise ValueError('collector_package_repository_attribution_mismatch')


def _unit_package(path, tree, boundaries=()):
    parts = path.split('/')[:-1]
    if any('/'.join(parts[:length]) + '/package.json' in tree or '/'.join(parts[:length]) + '/package.json' in boundaries
           for length in range(1, len(parts) + 1)):
        raise UnsupportedCollection('collector_nested_package_mapping_unsupported:' + path)


def _validate(task, payload, bounds):
    from .npm_evidence_sources import _license, _path
    from . import npm_package_scope
    from .ingest import licenses
    _check(bounds)
    if not isinstance(payload, dict) or payload.get('repository_url') != task['repository_url'] or payload.get('after_revision') != task['after_revision']:
        raise ValueError('collector_repository_or_revision_mismatch')
    before = payload.get('before_revision')
    if not isinstance(before, str) or not _SHA.fullmatch(before) or payload.get('parents') != [before] or before == task['after_revision']:
        raise ValueError('collector_exact_single_parent_required')
    if len(_json(payload).encode()) > bounds['remaining_bytes']: raise ValueError('collector_artifact_byte_budget_exceeded')
    permissions = {}
    for side, revision in (('before', before), ('after', task['after_revision'])):
        snap = payload.get('snapshots', {}).get(side)
        tree = payload.get('trees', {}).get(side)
        if not isinstance(snap, dict) or snap.get('revision') != revision or not isinstance(snap.get('files'), dict): raise ValueError('collector_snapshot_revision_mismatch')
        if not isinstance(tree, dict) or not _SHA.fullmatch(str(tree.get('sha', ''))) or not isinstance(tree.get('files'), dict): raise ValueError('collector_tree_identity_missing')
        if len(snap['files']) > bounds['max_files'] or len(tree['files']) > bounds['max_tree_entries']: raise ValueError('collector_file_budget_exceeded')
        for path, text in snap['files'].items():
            _path(path)
            if not isinstance(text, str) or len(text.encode()) > bounds['max_blob_bytes']: raise ValueError('collector_source_blob_budget_exceeded')
            if tree['files'].get(path) != _blob(text): raise ValueError('collector_source_blob_hash_mismatch')
            declarations = re.findall(r'SPDX-License-Identifier:\s*([^\r\n*]+)', text)
            if any(not licenses.redistributable(declared.strip()) for declared in declarations):
                raise ValueError('source_file_permission_not_established:' + path)
    before_tree, after_tree = payload['trees']['before']['files'], payload['trees']['after']['files']
    changes = [path for path in before_tree.keys() & after_tree.keys()
               if before_tree[path] != after_tree[path] and _production(path)]
    if not changes: raise ValueError('collector_no_changed_production_source')
    if len(changes) > bounds['max_changed_files']: raise ValueError('collector_changed_file_budget_exceeded')
    scopes = {}
    for side in ('before', 'after'):
        files, tree = payload['snapshots'][side]['files'], payload['trees'][side]
        if any(path not in files for path in changes): raise ValueError('collector_changed_source_missing')
        try:
            scopes[side] = npm_package_scope.resolve(task, files, tree['files'], changes, tree.get('package_boundaries', ()), tree.get('notice_boundaries', ()))
        except npm_package_scope.UnsupportedPackageScope as error:
            raise UnsupportedCollection('collector_nested_package_mapping_unsupported:' + str(error)) from error
        permissions[side] = scopes[side]['permission']
        for path in changes:
            declarations = re.findall(r'SPDX-License-Identifier:\s*([^\r\n*]+)', files[path])
            if any(declared.strip().lower() != permissions[side]['spdx'].lower() for declared in declarations):
                raise ValueError('source_file_license_scope_conflict:' + path)
    if any(scopes['before'][key] != scopes['after'][key] for key in ('package', 'manifest_path', 'prefix')):
        raise ValueError('collector_package_owner_changed_between_snapshots')
    return sorted(changes), permissions


class _GitFiles(dict):
    def __init__(self):
        super().__init__()
        self.package_boundaries = set()
        self.notice_boundaries = set()


def _cached_tree(repository, revision, *, bounds=None):
    from collections import deque
    from . import security_snapshots
    from .npm_evidence_sources import _NOTICES
    from .acceleration.repository_service import service, CompatibilityFallback
    path = security_snapshots.repository_path({'repo_url': repository})
    if path is None or not path.is_dir(): raise CompatibilityFallback('local_repository_unavailable')
    reader = service(path)
    with reader.reader_lease(), reader.lock:
        if bounds is not None: _check(bounds)
        # The ordinary tree API omits symlinks. A package.json symlink is still
        # an ownership boundary, so retain its presence without following it.
        # Reuse the persistent exact-object reader with an incremental bound.
        metadata = reader.metadata(revision)
        tree_sha = metadata.get('tree')
        if not _SHA.fullmatch(str(tree_sha)): raise ValueError('collector_cached_tree_identity_missing')
        pending, result, entries = deque([('', tree_sha)]), _GitFiles(), 0
        while pending:
            if bounds is not None: _check(bounds)
            prefix, identity = pending.popleft()
            row = reader._object(identity, 1 << 20)
            if row is None or row[1] != 'tree': raise OSError('collector_cached_tree_object_unavailable')
            raw, offset = row[3], 0
            while offset < len(raw):
                space = raw.index(b' ', offset); end = raw.index(b'\0', space)
                mode, name, oid = raw[offset:space], raw[space + 1:end].decode('utf-8'), raw[end + 1:end + 21]
                if len(oid) != 20: raise ValueError('collector_cached_tree_object_truncated')
                offset = end + 21; entries += 1
                if bounds is not None and entries % 64 == 0: _check(bounds)
                if entries > 20000: raise ValueError('collector_cached_tree_budget_exceeded')
                path = security_snapshots.logical_path(prefix + name)
                if name == 'package.json': result.package_boundaries.add(path)
                if name in _NOTICES: result.notice_boundaries.add(path)
                if mode in (b'40000', b'040000'): pending.append((path + '/', oid.hex()))
                elif mode in (b'100644', b'100755'): result[path] = oid.hex()
        return result


def fetch_upstream(task, bounds):
    """Read exact cached Git objects first, then bounded public HTTPS fallback.

    The persistent repository reader handles only Git object metadata/blobs;
    no package code, dependency installer, lifecycle script, or test is run.
    """
    import requests
    from . import npm_evidence_sources as sources, security_snapshots
    from .acceleration.repository_service import CompatibilityFallback
    repository = task['repository_url'].removeprefix('https://github.com/')
    api = 'https://api.github.com/repos/' + repository
    provenance = {'metadata': {}, 'trees': {}, 'blobs': {'before': {}, 'after': {}}}
    with requests.Session() as session:
        def metadata(url):
            raw = sources._bounded_get(session, url, bounds, maximum=bounds['max_metadata_bytes'])
            if raw is None: raise ValueError('collector_git_metadata_unavailable')
            value = json.loads(raw)
            if not isinstance(value, dict): raise ValueError('collector_git_metadata_invalid')
            return value
        def commit(revision, side):
            _check(bounds)
            try:
                value, origin = sources._cached_commit(task['repository_url'], revision)
            except (CompatibilityFallback, OSError, ValueError) as error:
                if sources._waiting_for_error(error) is not None or isinstance(error, sources.SourceBudgetExceeded): raise
                value = metadata(api + '/git/commits/' + revision)
                origin = {**bounds.get('last_transport', {}), 'transport': 'github_api', 'revision': revision,
                          'fallback_reason': type(error).__name__ + ': ' + str(error)[:200]}
            provenance['metadata'][side] = origin
            return value
        after = commit(task['after_revision'], 'after')
        parents = [item.get('sha') for item in after.get('parents', [])]
        if after.get('sha') != task['after_revision'] or len(parents) != 1 or not _SHA.fullmatch(str(parents[0])):
            raise ValueError('collector_exact_single_parent_required')
        before = commit(parents[0], 'before')
        if before.get('sha') != parents[0]: raise ValueError('collector_parent_identity_mismatch')
        trees = {}
        for side, value in (('before', before), ('after', after)):
            tree_sha = (value.get('tree') or {}).get('sha')
            if not _SHA.fullmatch(str(tree_sha)): raise ValueError('collector_tree_identity_missing')
            revision = value['sha']
            try:
                files = _cached_tree(task['repository_url'], revision, bounds=bounds)
                boundaries = sorted(getattr(files, 'package_boundaries', ()))
                notice_boundaries = sorted(getattr(files, 'notice_boundaries', ()))
                provenance['trees'][side] = {'transport': 'git_snapshot', 'revision': revision, 'tree_revision': tree_sha}
            except (CompatibilityFallback, OSError, ValueError) as error:
                if sources._waiting_for_error(error) is not None or isinstance(error, sources.SourceBudgetExceeded): raise
                tree = metadata(api + '/git/trees/' + tree_sha + '?recursive=1')
                if tree.get('sha') != tree_sha or tree.get('truncated') is not False or not isinstance(tree.get('tree'), list) or len(tree['tree']) > bounds['max_tree_entries']:
                    raise ValueError('collector_full_tree_budget_exceeded')
                files = {item['path']: item['sha'] for item in tree['tree']
                    if isinstance(item, dict) and item.get('type') == 'blob' and item.get('mode') in ('100644', '100755')
                    and isinstance(item.get('path'), str) and _SHA.fullmatch(str(item.get('sha', '')))}
                boundaries = sorted({item['path'] for item in tree['tree'] if isinstance(item, dict)
                    and isinstance(item.get('path'), str) and item['path'].split('/')[-1] == 'package.json'})
                notice_boundaries = sorted({item['path'] for item in tree['tree'] if isinstance(item, dict)
                    and isinstance(item.get('path'), str) and item['path'].split('/')[-1] in sources._NOTICES})
                provenance['trees'][side] = {**bounds.get('last_transport', {}), 'transport': 'github_api', 'revision': revision,
                    'tree_revision': tree_sha, 'fallback_reason': type(error).__name__ + ': ' + str(error)[:200]}
            if len(files) > bounds['max_tree_entries'] or any(not _SHA.fullmatch(str(sha)) for sha in files.values()):
                raise ValueError('collector_full_tree_budget_exceeded')
            trees[side] = {'sha': tree_sha, 'files': files, 'package_boundaries': boundaries, 'notice_boundaries': notice_boundaries}
        changes = [path for path in trees['before']['files'].keys() & trees['after']['files'].keys()
                   if trees['before']['files'][path] != trees['after']['files'][path]
                   and _production(path)]
        if len(changes) > bounds['max_changed_files']: raise ValueError('collector_changed_file_budget_exceeded')
        snapshots = {}
        for side, revision in (('before', parents[0]), ('after', task['after_revision'])):
            files = {}; snapshots[side] = {'revision': revision, 'files': files}
            cached_snapshot = security_snapshots.snapshot_for({'repo_url': task['repository_url']}, revision)
            def read(path):
                sources._path(path)
                if path not in trees[side]['files']: return
                _check(bounds)
                fallback = None
                try:
                    text, origin = sources._cached_blob(cached_snapshot, revision, path)
                except (CompatibilityFallback, OSError, ValueError) as error:
                    if sources._waiting_for_error(error) is not None or isinstance(error, sources.SourceBudgetExceeded): raise
                    text, origin, fallback = None, None, type(error).__name__ + ': ' + str(error)[:200]
                if text is None:
                    raw = sources._bounded_get(session, 'https://raw.githubusercontent.com/' + repository + '/' + revision + '/' + quote(path, safe='/'), bounds)
                    if raw is None: raise ValueError('collector_pinned_blob_unavailable')
                    text = raw.decode('utf-8', errors='strict')
                    origin = {**bounds.get('last_transport', {}), 'transport': 'github_raw', 'revision': revision, 'path': path}
                    if fallback: origin['fallback_reason'] = fallback
                if len(text.encode()) > bounds['max_blob_bytes']: raise ValueError('collector_source_blob_budget_exceeded')
                if _blob(text) != trees[side]['files'][path]: raise ValueError('collector_source_blob_hash_mismatch')
                files[path] = text
                provenance['blobs'][side][path] = origin
            from . import npm_package_scope
            try:
                metadata_paths = npm_package_scope.required_paths(trees[side]['files'], changes, trees[side]['package_boundaries'], trees[side]['notice_boundaries'])
                for path in metadata_paths: read(path)
                npm_package_scope.resolve(task, files, trees[side]['files'], changes, trees[side]['package_boundaries'], trees[side]['notice_boundaries'])
            except npm_package_scope.UnsupportedPackageScope as error:
                raise UnsupportedCollection('collector_nested_package_mapping_unsupported:' + str(error)) from error
            for path in sorted(changes):
                read(path)
        return {'repository_url': task['repository_url'], 'before_revision': parents[0], 'after_revision': task['after_revision'],
                'parents': parents, 'trees': trees, 'snapshots': snapshots, 'message': after.get('message'),
                'date': (after.get('committer') or {}).get('date'), 'transport': 'cached_git_first_with_bounded_https_fallback',
                'provenance': provenance}


def _unit_bounds(pair, payload, side):
    path = (pair.get('old_path') if side == 'before' else None) or pair['file_path']
    source, code = payload['snapshots'][side]['files'][path], pair['code_' + side]
    raw = source.encode()
    meta = pair.get('extraction_meta') or {}
    if isinstance(meta, str): meta = json.loads(meta)
    declared = (meta.get('prov') or {}).get(side) or {}
    span = declared.get('bytes')
    if span is not None:
        if (not isinstance(span, (list, tuple)) or len(span) != 2 or any(type(value) is not int for value in span)
                or not 0 <= span[0] <= span[1] <= len(raw) or raw[span[0]:span[1]] != code.encode()):
            return None
        start, end = span
    elif source == code:
        start, end = 0, len(raw)
    elif source.count(code) == 1:
        offset = source.index(code)
        start = len(source[:offset].encode()); end = start + len(code.encode())
    else:
        return None
    return {'bytes': [start, end], 'lines': [raw[:start].count(b'\n') + 1, raw[:max(start, end - 1)].count(b'\n') + 1]}


def _source_identity(record):
    fields = ('record_id', 'raw_pair_key', 'repository_identity', 'package_identity', 'ecosystem', 'language',
        'fix_commit', 'relevant_parent_commit', 'before_tree_hash', 'after_tree_hash', 'file_path_before', 'file_path_after',
        'collector_version', 'repository_license', 'data_usage_status')
    identity = {name: record.get(name) for name in fields}
    for name in ('byte_span_before', 'byte_span_after', 'line_span_before', 'line_span_after', 'file_blob_hashes'):
        value = record.get(name)
        identity[name] = json.loads(value) if isinstance(value, str) else value
    for name in ('vulnerable_code', 'fixed_code'):
        text = record.get(name)
        if not isinstance(text, str) or not text: raise ValueError('collector_source_code_missing')
        identity[name + '_sha256'] = hashlib.sha256(text.encode()).hexdigest()
    return _digest(identity)


@lru_cache(maxsize=2)
def _binding_artifact(path, identity, size, modified, changed, inode):
    artifact = Path(path)
    if size > 64 << 20 or _hash(artifact) != identity: raise ValueError('collector_source_binding_artifact_corrupted')
    value = json.loads(artifact.read_text(encoding='utf-8'))
    if value.get('policy') != POLICY: raise ValueError('collector_source_binding_policy_mismatch')
    bounds = {'should_stop': lambda: False, 'deadline': time.monotonic() + 60, 'remaining_bytes': 64 << 20,
              'max_files': 32, 'max_tree_entries': 20000, 'max_blob_bytes': 1 << 20, 'max_changed_files': 20}
    _, permissions = _validate(value['task'], value['payload'], bounds)
    if value.get('permissions') != permissions: raise ValueError('collector_source_binding_permissions_mismatch')
    current = artifact.stat()
    if (current.st_size, current.st_mtime_ns, current.st_ctime_ns, current.st_ino) != (size, modified, changed, inode):
        raise ValueError('collector_source_binding_changed_while_reading')
    return value


def source_binding_for(record, *, root=None):
    """Authenticate committed exact source bytes, never an advisory fix claim.

    Imported snapshot status, reference kind and labels grant no authority. The
    backend unit index is populated only in the atomic shard publication path.
    This source foundation still requires independent security/repair proof.
    """
    if not isinstance(record, dict) or record.get('collector_version') != POLICY: return None
    try:
        root = _root(root)
        source_sha = _source_identity(record)
        contexts = [json.loads(record.get('context_' + side) or '{}') for side in ('before', 'after')]
        snapshots = [context.get('snapshot') or {} for context in contexts]
        artifact_sha = snapshots[0].get('collector_artifact_sha256')
        if (not _HEX.fullmatch(str(artifact_sha)) or any(snapshot.get('collector_artifact_sha256') != artifact_sha
                or snapshot.get('collector_artifact_path') != '.npm_case_collector/artifacts/' + artifact_sha + '.json' for snapshot in snapshots)):
            return None
        with _connect(root) as db:
            unit = db.execute('SELECT shard_sha FROM source_units WHERE record_id=? AND source_sha=? AND artifact_sha=? AND policy=?',
                (record.get('record_id'), source_sha, artifact_sha, POLICY)).fetchone()
            if unit is None: return None
            registered = db.execute('SELECT path,bytes FROM shards WHERE sha=?', (unit['shard_sha'],)).fetchone()
        if registered is None or registered['path'] != '.npm_case_collector/shards/' + unit['shard_sha'] + '.jsonl': return None
        shard = root / registered['path']
        if (not shard.resolve().is_relative_to(root) or not shard.is_file() or shard.stat().st_size != registered['bytes']
                or shard.stat().st_size > MAX_SHARD_BYTES or _hash(shard) != unit['shard_sha']): return None
        path = root / '.npm_case_collector/artifacts' / (artifact_sha + '.json')
        if not path.resolve().is_relative_to(root) or not path.is_file(): return None
        stat = path.stat()
        artifact = _binding_artifact(str(path), artifact_sha, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
        task, payload = artifact['task'], artifact['payload']
        package = ((artifact['permissions']['after'].get('package_scope') or {}).get('package')
                   or json.loads(payload['snapshots']['after']['files']['package.json'])['name'])
        if (_repository_identity(record.get('repository_identity')) != _repository_identity(task['repository_url'])
                or record.get('package_identity') != 'npm:' + package or record.get('ecosystem') != 'npm'
                or record.get('relevant_parent_commit') != payload['before_revision'] or record.get('fix_commit') != payload['after_revision']):
            return None
        discoveries = {item['advisory_sha256']: item for item in artifact['discoveries'] if package in item['packages']}
        # Advisory aliases may be conservatively enriched by the append layer.
        # Authenticate this artifact's own discovery history rather than making
        # mutable advisory metadata part of the immutable source-code identity.
        hashes = sorted(discoveries)
        if not hashes: return None
        for identity in hashes:
            raw = root / '.npm_case_collector/discoveries' / (identity + '.json')
            if not _HEX.fullmatch(identity) or not raw.resolve().is_relative_to(root) or not raw.is_file() or raw.stat().st_size > 2 << 20 or _hash(raw) != identity:
                return None
        blobs = json.loads(record['file_blob_hashes'])
        for side, code_key, revision in (('before', 'vulnerable_code', payload['before_revision']), ('after', 'fixed_code', payload['after_revision'])):
            logical, code = record['file_path_' + side], record[code_key]
            full = payload['snapshots'][side]['files'].get(logical)
            span = json.loads(record['byte_span_' + side])
            snapshot = snapshots[0 if side == 'before' else 1]
            if (not isinstance(full, str) or not isinstance(span, list) or len(span) != 2 or any(type(value) is not int for value in span)
                    or not 0 <= span[0] <= span[1] <= len(full.encode()) or full.encode()[span[0]:span[1]] != code.encode()
                    or blobs.get(side) != _blob(full) or record[side + '_tree_hash'] != payload['trees'][side]['sha']
                    or snapshot.get('revision') != revision or snapshot.get('side') != side or snapshot.get('path') != logical
                    or snapshot.get('target_sha256') != hashlib.sha256(code.encode()).hexdigest()
                    or snapshot.get('file_sha256') != hashlib.sha256(full.encode()).hexdigest()):
                return None
        fact = {'schema': SOURCE_BINDING, 'status': 'verified', 'basis': 'backend_exact_git_parent_tree_blob_package_permission_unit',
            'record_id': record['record_id'], 'artifact_sha256': artifact_sha, 'source_sha256': source_sha,
            'advisory_fix_status': 'unknown', 'reference_kinds': sorted({kind for identity in hashes for kind in discoveries[identity]['reference_kinds']})}
        return {**fact, 'binding_sha256': _digest(fact)}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        # ignored: absent, stale or malformed backend provenance cannot grant a
        # source foundation; the caller retains its original hard source gates.
        return None


def _records(task, discoveries, payload, changes, permissions, bounds, artifact_sha):
    from . import security_snapshots, ecosystems
    from .ingest import codemine
    from .schemas import reviewed
    class Loader:
        def content(self, revision, path):
            side = 'before' if revision == payload['before_revision'] else 'after'
            text = payload['snapshots'][side]['files'].get(path)
            return text.encode() if text is not None else None
    commit = codemine.Commit(task['after_revision'], payload['before_revision'], payload.get('message') or '', payload.get('date'),
        [codemine.FileChange(path, path, 'modified') for path in changes], Loader(), payload['parents'])
    package = ((permissions['after'].get('package_scope') or {}).get('package')
               or json.loads(payload['snapshots']['after']['files']['package.json'])['name'])
    discoveries = [item for item in discoveries if package in item['packages']]
    if not discoveries: raise ValueError('collector_no_package_bound_advisory')
    primary = discoveries[0]
    meta = {'ecosystem': 'npm', 'package': package, 'published': primary.get('published')}
    pairs = codemine.build_pairs({**task, 'repo_url': task['repository_url'], 'sources': '["osv"]'}, commit, meta,
                                 bounds['max_changed_files'], max_functions=40).pairs
    aliases = sorted({alias for item in discoveries for alias in item['aliases']})
    hashes = sorted({item['advisory_sha256'] for item in discoveries})
    keys = sorted({item['advisory_id'] for item in discoveries if item.get('advisory_id')})
    for pair in pairs:
        _check(bounds)
        if not pair.get('code_available') or pair.get('is_test') or pair.get('tangled') or pair.get('quarantine_reason'): continue
        if not pair.get('code_before') or not pair.get('code_after') or pair['code_before'] == pair['code_after']: continue
        spans = {side: _unit_bounds(pair, payload, side) for side in ('before', 'after')}
        if not all(spans.values()): continue
        def provider(row, revision):
            side = 'before' if revision == payload['before_revision'] else 'after'
            # Acquired files are complete; unacquired repository paths remain
            # explicit omissions, never inferred absent from a partial snapshot.
            return security_snapshots.Snapshot(revision, payload['snapshots'][side]['files'])
        bundles = security_snapshots.build_pair(pair, provider=provider)
        for bundle in bundles.values():
            bundle.omissions.append('collector_snapshot_limited_to_changed_files_and_pinned_notices')
            bundle.completeness = 'partial'
            bundle.snapshot.update(collector_artifact_sha256=artifact_sha,
                collector_artifact_path='.npm_case_collector/artifacts/' + artifact_sha + '.json',
                discovery_artifact_sha256s=hashes,
                permission=permissions['before' if bundle is bundles['before'] else 'after'],
                source_provenance={'schema': SOURCE_BINDING, 'status': 'verified',
                    'basis': 'backend_exact_git_parent_tree_blob_package_permission_unit', 'advisory_fix_status': 'unknown'})
        record = reviewed.new_record(raw_pair_key=pair['pair_key'], record_id=reviewed.record_id_of(pair['pair_key']),
            raw_record_keys=keys, advisory_aliases=aliases, repository_identity=ecosystems.repository_identity(task['repository_url']),
            package_identity='npm:' + package, ecosystem='npm', language=pair.get('language'),
            source_urls=sorted({url for item in discoveries for url in item['source_urls']} | {task['repository_url'] + '/commit/' + task['after_revision']}),
            source_record_hashes=hashes, repository_license=permissions['after']['spdx'], data_usage_status='usable',
            advisory_first_published_at=primary.get('published'), advisory_modified_at=primary.get('modified'),
            fix_commit=task['after_revision'], relevant_parent_commit=payload['before_revision'], parent_selection_reason='single_parent',
            before_tree_hash=payload['trees']['before']['sha'], after_tree_hash=payload['trees']['after']['sha'],
            file_blob_hashes=_json({'before': pair.get('before_blob_sha'), 'after': pair.get('after_blob_sha')}),
            collector_version=POLICY, extraction_version=codemine.EXTRACTION_VERSION,
            vulnerable_code=pair['code_before'], fixed_code=pair['code_after'], diff=pair.get('diff'),
            file_path_before=pair.get('old_path') or pair['file_path'], file_path_after=pair['file_path'], symbol=pair.get('function_name'),
            byte_span_before=_json(spans['before']['bytes']), byte_span_after=_json(spans['after']['bytes']),
            line_span_before=_json(spans['before']['lines']), line_span_after=_json(spans['after']['lines']),
            changed_lines=pair.get('changed_lines'), granularity=pair['granularity'],
            context_before=_json(bundles['before'].to_dict()), context_after=_json(bundles['after'].to_dict()),
            evidence_completeness='partial', omitted_context_reasons=sorted({reason for bundle in bundles.values() for reason in bundle.omissions}),
            dependency_versions=_json([affected for item in discoveries for affected in item['affected'] if affected['package']['name'] == package]), production_relevance='production',
            cwe_ids=sorted({cwe for item in discoveries for cwe in item['cwe_ids']}), cwe_source='osv',
            explanation=primary.get('details') or primary.get('summary'), explanation_source='advisory',
            evidence_summary=primary.get('summary'), evidence_summary_provenance='extractive',
            provenance_status='unknown', split='quarantine', eligible_tasks=[], verification_receipt_ids=[])
        errors = reviewed.validate(record)
        if errors: raise ValueError('collector_reviewed_schema_invalid:' + ';'.join(errors))
        yield record


def _refresh_package_scope(db):
    """Retry only the repaired mapping limitation, retaining its old disposition."""
    from . import npm_package_scope
    changed = 0
    rows = db.execute('SELECT id,state,last_error FROM work WHERE collection_policy=? AND (scope_policy IS NULL OR scope_policy!=?)',
                      (POLICY, npm_package_scope.POLICY)).fetchall()
    for row in rows:
        retry = row['state'] == 'unsupported' and (row['last_error'] or '').startswith('collector_nested_package_mapping_unsupported:')
        if retry:
            db.execute('INSERT OR IGNORE INTO package_scope_revisions VALUES (?,?,?,?,?,?)',
                (row['id'], npm_package_scope.POLICY, row['state'], row['last_error'], 'pending', time.time()))
            db.execute("UPDATE work SET state='pending',last_error=NULL,next_retry_at=NULL,token=NULL WHERE id=?", (row['id'],))
            changed += 1
        db.execute('UPDATE work SET scope_policy=? WHERE id=?', (npm_package_scope.POLICY, row['id']))
    return changed


def acquire(*, root=None, limit=5, max_bytes=16 << 20, max_seconds=120, max_requests=100,
            max_changed_files=20, fetch=None, should_stop=lambda: False, progress=None, exclude_ids=()):
    """Collect bounded ready fixes into immutable reviewed-record JSONL shards."""
    from . import npm_evidence_sources as sources
    if type(limit) is not int or not 0 < limit <= 25: raise ValueError('invalid_collection_limit')
    if (not isinstance(exclude_ids, (list, tuple, set, frozenset)) or len(exclude_ids) > 100000
            or any(not isinstance(value, str) or not _HEX.fullmatch(value) for value in exclude_ids)):
        raise ValueError('invalid_collection_excluded_ids')
    excluded = frozenset(exclude_ids)
    for name, value, maximum in (('max_bytes', max_bytes, 32 << 20), ('max_seconds', max_seconds, 600),
                                ('max_requests', max_requests, 200), ('max_changed_files', max_changed_files, 20)):
        if type(value) not in (int, float) or not 0 < value <= maximum: raise ValueError('invalid_collection_bound:' + name)
    root = _root(root)
    bounds = {'root': root, 'should_stop': should_stop, 'deadline': time.monotonic() + max_seconds,
        'max_requests': max_requests, 'requests': 0, 'network_bytes': 0, 'remaining_bytes': min(max_bytes, 32 << 20),
        'wave_bytes': min(max_bytes, 32 << 20),
        'max_blob_bytes': 1 << 20, 'max_metadata_bytes': 4 << 20, 'max_tree_entries': 20000,
        'max_changed_files': min(max_changed_files, 20), 'max_files': 32}
    lock = oslock.FileLock(root / '.npm_case_collector.acquire.lock', note=False)
    lock.acquire(wait=0)
    out = {'state': 'done', 'attempted': 0, 'collected': 0, 'failed': 0, 'unsupported': 0, 'records': 0}
    try:
        with _connect(root) as db:
            # The lock proves any prior running lease no longer has an owner.
            db.execute("UPDATE work SET state='pending',token=NULL WHERE state='running' AND collection_policy=?", (POLICY,))
            out['scope_requeued'] = _refresh_package_scope(db)
            ids = []
            for row in db.execute("SELECT id FROM work WHERE collection_policy=? AND (state='pending' OR state='waiting' AND next_retry_at<=? OR state='failed' AND failure_count<3) ORDER BY CASE state WHEN 'pending' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END,id", (POLICY, time.time())):
                if row[0] not in excluded:
                    ids.append(row[0])
                    if len(ids) == limit:
                        break
        for identity in ids:
            if should_stop(): out['state'] = 'cancelled'; break
            token = uuid.uuid4().hex
            with _connect(root) as db:
                row = db.execute('SELECT body FROM work WHERE id=?', (identity,)).fetchone()
                task = json.loads(row['body'])
                discovery_rows = db.execute('SELECT body FROM discoveries WHERE work_id=? ORDER BY advisory_sha LIMIT 33', (identity,)).fetchall()
                if len(discovery_rows) > 32:
                    db.execute("UPDATE work SET state='failed',failure_count=3,last_error='collector_per_fix_discovery_budget_exceeded' WHERE id=?", (identity,))
                    out['attempted'] += 1; out['failed'] += 1
                    continue
                discoveries = [json.loads(row[0]) for row in discovery_rows]
                task['packages'] = sorted({package for item in discoveries for package in item['packages']})
                db.execute("UPDATE work SET state='running',token=? WHERE id=?", (token, identity))
            out['attempted'] += 1
            try:
                _check(bounds)
                payload = (fetch or fetch_upstream)(task, bounds)
                changes, permissions = _validate(task, payload, bounds)
                artifact = (_json({'policy': POLICY, 'task': task, 'discoveries': discoveries,
                    'payload': payload, 'permissions': permissions}) + '\n').encode()
                artifact_sha = hashlib.sha256(artifact).hexdigest()
                rows, unit_keys = [], []
                size = 0
                for record in _records(task, discoveries, payload, changes, permissions, bounds, artifact_sha):
                    line = (_json(record) + '\n').encode()
                    size += len(line)
                    if len(rows) >= MAX_SHARD_RECORDS or size > MAX_SHARD_BYTES or size > bounds['remaining_bytes']:
                        raise ValueError('collector_candidate_shard_budget_exceeded')
                    rows.append(line)
                    unit_keys.append((record['record_id'], _source_identity(record)))
                if not rows: raise ValueError('collector_no_extractable_changed_units')
                content = b''.join(rows)
                sha = hashlib.sha256(content).hexdigest()
                relative = '.npm_case_collector/shards/' + sha + '.jsonl'
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                # Hold the collector journal transaction across the final
                # cancellation check and atomic shard publication.
                with _connect(root) as db:
                    db.execute('BEGIN IMMEDIATE')
                    _check(bounds)
                    active = db.execute("SELECT token FROM work WHERE id=? AND state='running'", (identity,)).fetchone()
                    if not active or active['token'] != token: raise sources.SourceCancelled('case_collection_fenced')
                    from . import fsio
                    artifact_path = root / '.npm_case_collector/artifacts' / (artifact_sha + '.json')
                    artifact_path.parent.mkdir(parents=True, exist_ok=True)
                    if artifact_path.exists():
                        if _hash(artifact_path) != artifact_sha: raise ValueError('collector_source_artifact_collision')
                    else: fsio.write_bytes(artifact_path, artifact)
                    if path.exists():
                        if _hash(path) != sha: raise ValueError('collector_shard_collision')
                    else:
                        fsio.write_bytes(path, content)
                    db.execute('INSERT OR IGNORE INTO shards VALUES (?,?,?,?)', (sha, relative, len(rows), len(content)))
                    db.executemany('INSERT OR IGNORE INTO source_units VALUES (?,?,?,?,?)',
                        [(rid, source_sha, artifact_sha, sha, POLICY) for rid, source_sha in unit_keys])
                    for side, revision in (('before', payload['before_revision']), ('after', payload['after_revision'])):
                        for logical, text in payload['snapshots'][side]['files'].items():
                            db.execute('INSERT OR REPLACE INTO exact_files VALUES (?,?,?,?,?)',
                                (task['repository_url'], revision, logical, artifact_sha, hashlib.sha256(text.encode()).hexdigest()))
                    db.execute("UPDATE work SET state='collected',token=NULL,last_error=NULL,next_retry_at=NULL,shard_sha=?,artifact_sha=? WHERE id=? AND token=?", (sha, artifact_sha, identity, token))
                out['collected'] += 1; out['records'] += len(rows)
                bounds['remaining_bytes'] -= len(content)
                if progress: progress(label='Collecting exact npm causal units', processed=out['attempted'], total=len(ids), counts=dict(out))
            except sources.SourceCancelled:
                with _connect(root) as db: db.execute("UPDATE work SET state='pending',token=NULL WHERE id=? AND token=?", (identity, token))
                out['state'] = 'cancelled'; break
            except Exception as error:
                if isinstance(error, UnsupportedCollection) or (isinstance(error, sources.SourceBudgetExceeded) and not sources._wave_deferral(error)):
                    with _connect(root) as db:
                        db.execute("UPDATE work SET state='unsupported',token=NULL,last_error=? WHERE id=? AND token=?", (str(error)[:500], identity, token))
                    out['unsupported'] += 1
                    continue
                waiting = error if isinstance(error, sources.SourceWaiting) else sources._waiting_for_error(error)
                if waiting or isinstance(error, sources.SourceBudgetExceeded):
                    code = waiting.code if waiting else str(error)
                    retry = waiting.next_retry_at if waiting else time.time() + 1
                    with _connect(root) as db: db.execute("UPDATE work SET state='waiting',token=NULL,last_error=?,next_retry_at=? WHERE id=? AND token=?", (code, retry, identity, token))
                    out.update(state='waiting', code=code, reason=str(error), next_retry_at=retry,
                        wait_scope='wave' if sources._wave_deferral(error) else 'capability',
                        deferred_ids=[identity] if sources._wave_deferral(error) else []); break
                with _connect(root) as db: db.execute("UPDATE work SET state='failed',failure_count=failure_count+1,token=NULL,last_error=? WHERE id=? AND token=?", (type(error).__name__ + ': ' + str(error)[:500], identity, token))
                out['failed'] += 1
        with _connect(root) as db:
            remaining = db.execute("SELECT count(*) FROM work WHERE collection_policy=? AND (state IN ('pending','waiting','running') OR state='failed' AND failure_count<3)", (POLICY,)).fetchone()[0]
            future = db.execute("SELECT last_error,next_retry_at FROM work WHERE collection_policy=? AND state='waiting' AND next_retry_at>? ORDER BY next_retry_at LIMIT 1", (POLICY, time.time())).fetchone()
            if not out['attempted'] and future:
                out.update(state='waiting', code=future['last_error'], next_retry_at=future['next_retry_at'])
        out.update(remaining=remaining, exhausted=remaining == 0, scope='configured_discovery_queue',
            bytes_used=bounds['network_bytes'], requests_used=bounds['requests'])
        return out
    finally:
        lock.release()


def capture(*, root=None):
    root = _root(root)
    with _connect(root) as db:
        # The active shard and its source/discovery references must come from
        # one journal snapshot even when another owner refreshes aliases.
        db.execute('BEGIN')
        shards = [{'sha256': row['sha'], 'path': row['path'], 'records': row['records'], 'bytes': row['bytes']}
                  for row in db.execute('SELECT DISTINCT s.* FROM shards s JOIN work w ON w.shard_sha=s.sha WHERE w.collection_policy=? ORDER BY s.sha', (POLICY,))]
        artifact_ids = [row[0] for row in db.execute('SELECT DISTINCT artifact_sha FROM work WHERE collection_policy=? AND shard_sha IS NOT NULL AND artifact_sha IS NOT NULL ORDER BY artifact_sha', (POLICY,))]
        discovery_ids = [row[0] for row in db.execute('SELECT DISTINCT d.advisory_sha FROM discoveries d JOIN work w ON d.work_id=w.id WHERE w.collection_policy=? AND w.shard_sha IS NOT NULL ORDER BY d.advisory_sha', (POLICY,))]
    def descriptors(kind, identities):
        result = []
        for identity in identities:
            relative = '.npm_case_collector/' + kind + '/' + identity + '.json'
            path = root / relative
            if not path.is_file(): raise ValueError('collector_' + kind + '_artifact_missing')
            result.append({'sha256': identity, 'path': relative, 'bytes': path.stat().st_size})
        return result
    result = {'schema': SCHEMA, 'shards': shards, 'artifacts': descriptors('artifacts', artifact_ids),
              'discoveries': descriptors('discoveries', discovery_ids)}
    return {**result, 'sha256': _digest(result)}


def iter_records(captured, *, root=None):
    from .schemas import reviewed
    root = _root(root)
    if not isinstance(captured, dict) or captured.get('schema') != SCHEMA or not isinstance(captured.get('shards'), list): raise ValueError('invalid_collector_shard_capture')
    identity = {key: captured[key] for key in ('schema', 'shards', 'artifacts', 'discoveries') if key in captured}
    if captured.get('sha256') != _digest(identity): raise ValueError('collector_shard_capture_hash_mismatch')
    for kind in ('artifacts', 'discoveries'):
        for descriptor in captured.get(kind, []):
            sha = descriptor.get('sha256')
            expected = '.npm_case_collector/' + kind + '/' + str(sha) + '.json'
            if not _HEX.fullmatch(str(sha)) or descriptor.get('path') != expected: raise ValueError('invalid_collector_source_artifact_path')
            path = root / expected
            if not path.resolve().is_relative_to(root) or not path.is_file() or path.stat().st_size != descriptor.get('bytes') or path.stat().st_size > 64 << 20 or _hash(path) != sha:
                raise ValueError('collector_source_artifact_hash_or_size_mismatch')
    artifacts = {entry['sha256']: entry['path'] for entry in captured.get('artifacts', [])}
    discoveries = {entry['sha256'] for entry in captured.get('discoveries', [])}
    for shard in captured['shards']:
        sha = shard.get('sha256')
        expected = '.npm_case_collector/shards/' + str(sha) + '.jsonl'
        if not _HEX.fullmatch(str(sha)) or shard.get('path') != expected: raise ValueError('invalid_collector_shard_path')
        path = root / expected
        if path.resolve().parent != (root / '.npm_case_collector/shards').resolve(): raise ValueError('collector_shard_path_escape')
        if not path.is_file() or path.stat().st_size != shard.get('bytes') or path.stat().st_size > MAX_SHARD_BYTES or _hash(path) != sha: raise ValueError('collector_shard_hash_or_size_mismatch')
        count = 0
        with path.open('r', encoding='utf-8') as stream:
            for line in stream:
                count += 1
                if count > MAX_SHARD_RECORDS: raise ValueError('collector_shard_record_budget_exceeded')
                row = json.loads(line)
                if reviewed.validate(row) or row.get('collector_version') != POLICY or row.get('review_status') != 'unreviewed' or row.get('label_state') != 'unreviewed' or row.get('eligible_tasks'):
                    raise ValueError('collector_shard_record_invalid_or_trusted')
                hashes = row.get('source_record_hashes') or []
                snapshots = [json.loads(row.get('context_' + side) or '{}').get('snapshot', {}) for side in ('before', 'after')]
                if (not hashes or not set(hashes) <= discoveries
                        or any(snapshot.get('collector_artifact_sha256') not in artifacts
                            or snapshot.get('collector_artifact_path') != artifacts.get(snapshot.get('collector_artifact_sha256'))
                            or snapshot.get('discovery_artifact_sha256s') != sorted(hashes) for snapshot in snapshots)
                        or snapshots[0]['collector_artifact_sha256'] != snapshots[1]['collector_artifact_sha256']):
                    raise ValueError('collector_candidate_provenance_not_captured')
                yield row
        if count != shard.get('records'): raise ValueError('collector_shard_record_count_mismatch')


def read_exact(repository, revision, path, *, root=None):
    """Read only a backend-indexed, hash-validated acquired full source file."""
    from .npm_evidence_sources import _repository, _path
    repository, path = _repository(repository).lower(), _path(path)
    if not isinstance(revision, str) or not _SHA.fullmatch(revision): raise ValueError('collector_exact_revision_required')
    root = _root(root)
    with _connect(root) as db:
        row = db.execute('SELECT artifact_sha,content_sha FROM exact_files WHERE repository=? AND revision=? AND path=?', (repository, revision, path)).fetchone()
    if not row: return None
    artifact = root / '.npm_case_collector/artifacts' / (row['artifact_sha'] + '.json')
    if not _HEX.fullmatch(row['artifact_sha']) or not artifact.is_file() or artifact.stat().st_size > 64 << 20 or _hash(artifact) != row['artifact_sha']:
        raise ValueError('collector_exact_source_artifact_corrupted')
    payload = json.loads(artifact.read_text(encoding='utf-8'))['payload']
    if payload['repository_url'] != repository: raise ValueError('collector_exact_source_repository_mismatch')
    side = 'before' if payload['before_revision'] == revision else 'after' if payload['after_revision'] == revision else None
    if side is None: raise ValueError('collector_exact_source_revision_mismatch')
    text = payload['snapshots'][side]['files'].get(path)
    if not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != row['content_sha']:
        raise ValueError('collector_exact_source_content_corrupted')
    return text


def exact_paths(repository, revision, *, root=None, limit=4096):
    from .npm_evidence_sources import _repository
    repository = _repository(repository).lower()
    if not isinstance(revision, str) or not _SHA.fullmatch(revision) or type(limit) is not int or not 0 < limit <= 4096:
        raise ValueError('invalid_collector_exact_paths_request')
    with _connect(root) as db:
        return [row[0] for row in db.execute('SELECT path FROM exact_files WHERE repository=? AND revision=? ORDER BY path LIMIT ?', (repository, revision, limit))]
