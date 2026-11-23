"""Bounded, resumable upstream npm evidence acquisition, never label admission.

Full pinned files and notices are retained. Imported labels and regression files
are discovery/context evidence only; nothing here inserts pairs or executes tests.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import math
import os
import pathlib
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter, OrderedDict
from contextlib import contextmanager, nullcontext
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit

POLICY = 'npm-targeted-sources/2'
SCHEMA = 'npm-exact-source-evidence/1'
MECHANISMS = ('prototype_pollution', 'path_traversal', 'command_injection')
_REF = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?\Z')
_TEST = re.compile(r'(^|/)(?:test|tests|__tests__|spec|specs)(?:/|$)|(?:\.test|\.spec)\.[cm]?[jt]sx?$', re.I)
_NOTICES = ('LICENSE', 'LICENSE.md', 'LICENSE.txt', 'LICENCE', 'COPYING')
MAX_ARTIFACT_BYTES = 32 << 20
_PARSED_ARTIFACTS, _PARSED_GUARD = OrderedDict(), threading.RLock()
_WAVE_BUDGET_CODES = frozenset(('source_time_budget_exceeded', 'source_request_budget_exceeded', 'source_wave_byte_budget_exceeded'))
_LEGACY_WAVE_ERRORS = tuple('SourceBudgetExceeded: ' + code for code in sorted(_WAVE_BUDGET_CODES))
_GENUINE_FAILURE = "a.state='failed' AND (CASE WHEN json_valid(a.result) THEN json_extract(a.result,'$.error') ELSE '' END) NOT IN (?,?,?)"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _object(value, fallback=None):
    if isinstance(value, str):
        try: return json.loads(value)
        except ValueError: return fallback
    return value if value is not None else fallback


def source_catalog():
    return [
        {'source_id': 'npm-upstream-exact-snapshots', 'url': 'https://github.com/',
         'license': 'per_exact_repository_notice', 'code_acquisition_allowed': 'after_pinned_permission_check',
         'labels_are': 'discovery_hints_only', 'training_pairs': 0,
         'use': 'Exact before/after production, manifest and resolved dependency evidence for existing records.'},
        {'source_id': 'npm-upstream-regression-files', 'url': 'https://github.com/',
         'license': 'per_exact_repository_notice', 'code_acquisition_allowed': 'after_pinned_permission_check',
         'labels_are': 'discovery_hints_only', 'training_pairs': 0,
         'use': 'Pinned upstream regression source retained separately; never a trusted execution oracle.'},
        {'source_id': 'secbench-js-discovery', 'url': 'https://github.com/cristianstaicu/SecBench.js',
         'metadata_url': 'https://api.github.com/repos/cristianstaicu/SecBench.js',
         'license': None, 'code_acquisition_allowed': False, 'labels_are': 'discovery_hints_only',
         'training_pairs': 0, 'use': 'Optional advisory/package discovery pointers only. No exploit code or benchmark labels admitted.'},
    ]


def _root(root=None):
    if root is None:
        from . import versions
        root = versions.PROC
    path = pathlib.Path(root).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _connect(root=None):
    db = sqlite3.connect(_root(root) / '.npm_evidence_sources.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=30000')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS source_work (
            source_id TEXT PRIMARY KEY, identity TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL, priority INTEGER NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
            lease_until REAL, claim_token TEXT, last_error TEXT, artifact_sha256 TEXT);
        CREATE INDEX IF NOT EXISTS source_work_state_error ON source_work(state,last_error);
        CREATE TABLE IF NOT EXISTS source_records (
            source_id TEXT NOT NULL, record_id TEXT NOT NULL, fingerprint TEXT NOT NULL, body TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(source_id,record_id));
        CREATE TABLE IF NOT EXISTS source_attempts (
            attempt_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            state TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL, result TEXT);
        CREATE INDEX IF NOT EXISTS source_attempt_disposition ON source_attempts(source_id,fingerprint,state);
        CREATE TABLE IF NOT EXISTS exact_source_files (
            repository TEXT NOT NULL, revision TEXT NOT NULL, path TEXT NOT NULL,
            artifact_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
            source_id TEXT NOT NULL, PRIMARY KEY(repository,revision,path));
    ''')
    if 'metadata' not in {row[1] for row in db.execute('PRAGMA table_info(source_records)')}:
        db.execute("ALTER TABLE source_records ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
    columns = {row[1] for row in db.execute('PRAGMA table_info(source_work)')}
    for name, kind in (('waiting_code', 'TEXT'), ('next_retry_at', 'REAL')):
        if name not in columns:
            db.execute('ALTER TABLE source_work ADD COLUMN ' + name + ' ' + kind)
    if 'acquisition_policy' not in columns:
        db.execute('ALTER TABLE source_work ADD COLUMN acquisition_policy TEXT')
        # One bounded metadata migration, not a corpus/body rescan per transfer.
        for item in db.execute('SELECT source_id,identity FROM source_work').fetchall():
            identity = json.loads(item['identity'])
            policy = POLICY if item['source_id'] == _digest({'policy': POLICY, **identity}) else 'legacy'
            db.execute('UPDATE source_work SET acquisition_policy=? WHERE source_id=?', (policy, item['source_id']))
    db.execute('CREATE INDEX IF NOT EXISTS source_work_policy_state ON source_work(acquisition_policy,state)')
    # Old workers recorded these exact aggregate limits as failures. Requeue
    # their work without changing the original append-only attempt history.
    if db.execute("SELECT 1 FROM source_work WHERE state='failed' AND last_error IN (?,?,?) LIMIT 1", _LEGACY_WAVE_ERRORS).fetchone():
        db.execute("UPDATE source_work SET state='pending' WHERE state='failed' AND last_error IN (?,?,?)", _LEGACY_WAVE_ERRORS)
    db.commit()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback(); raise
    finally:
        db.close()


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 1024 or value.startswith('/')
            or any(c in value for c in '\\:\0\r\n') or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError('unsafe_exact_source_path')
    return value


def _repository(value):
    if not isinstance(value, str): raise ValueError('missing_upstream_repository')
    parsed = urlsplit(value if '://' in value else 'https://' + value)
    parts = parsed.path.strip('/').removesuffix('.git').split('/')
    if (parsed.scheme != 'https' or parsed.hostname != 'github.com' or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment or len(parts) != 2
            or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) or p in ('.', '..') for p in parts)):
        raise ValueError('unsupported_public_upstream_repository')
    if '/'.join(parts).lower() == 'cristianstaicu/secbench.js':
        raise ValueError('benchmark_code_permission_not_established')
    return 'https://github.com/' + '/'.join(parts)


def _hints(record):
    provenance = _object(record.get('admission_provenance'), {})
    mechanism = provenance.get('mechanism') if isinstance(provenance, dict) else None
    if mechanism in MECHANISMS: return [mechanism]
    codes = {int(x) for x in re.findall(r'(?:CWE-)?\b(\d+)\b', str(record.get('cwe_ids') or ''))}
    return [m for m, cwes in [('prototype_pollution', {1321}), ('path_traversal', {22, 23}),
                             ('command_injection', {78, 88})] if codes & cwes]


def _shortages(qualification):
    qualification = qualification if isinstance(qualification, dict) else {}
    coverage = qualification.get('partition_mechanism_groups') or {}
    coverage = coverage if isinstance(coverage, dict) else {}
    result = dict.fromkeys(MECHANISMS, 0)
    for role in ('train', 'selection', 'calibration', 'untouched_test'):
        detail = coverage.get(role) or {}
        detail = detail if isinstance(detail, dict) else {}
        for mechanism in MECHANISMS:
            actual = detail.get(mechanism)
            actual = actual if type(actual) is int and actual >= 0 else 0
            result[mechanism] += max(0, (50 if role == 'train' else 10) - actual)
    return result


def _task(db, row, *, include_records=True):
    identity = json.loads(row['identity'])
    if not include_records:
        members = list(db.execute('SELECT record_id,metadata FROM source_records WHERE source_id=? ORDER BY record_id', (row['source_id'],)))
        hints = set()
        for member in members:
            metadata = _object(member['metadata'], {})
            if isinstance(metadata, dict): hints.update(metadata.get('mechanism_hints') or [])
        failures = db.execute('SELECT COUNT(*) FROM source_attempts a WHERE source_id=? AND fingerprint=? AND ' + _GENUINE_FAILURE,
            (row['source_id'], row['input_fingerprint'], *_LEGACY_WAVE_ERRORS)).fetchone()[0]
        return {**{key: row[key] for key in ('source_id', 'input_fingerprint', 'state', 'priority', 'attempt_count', 'last_error', 'artifact_sha256', 'waiting_code', 'next_retry_at')},
                **identity, 'record_ids': [r['record_id'] for r in members], 'record_count': len(members), 'mechanism_hints': sorted(hints),
                'failed_attempt_count': failures,
                'retry_after_seconds': max(0, math.ceil((row['next_retry_at'] or 0) - time.time()))}
    records = [json.loads(r['body']) for r in db.execute(
        'SELECT body FROM source_records WHERE source_id=? ORDER BY record_id', (row['source_id'],))]
    return {**dict(row), **identity, 'records': records, 'record_ids': [r['record_id'] for r in records],
            'mechanism_hints': sorted({hint for record in records for hint in _hints(record)})}


def _summary(db):
    states = dict(Counter(row[0] for row in db.execute('SELECT state FROM source_work')))
    remaining = states.get('pending', 0) + states.get('running', 0) + states.get('waiting', 0)
    waiting = db.execute("SELECT waiting_code,next_retry_at FROM source_work WHERE state='waiting' "
        'ORDER BY CASE WHEN next_retry_at>? THEN 0 ELSE 1 END,next_retry_at LIMIT 1', (time.time(),)).fetchone()
    return {'schema': 'npm-source-work/1', 'policy': POLICY, 'total': sum(states.values()),
            'states': states, 'remaining': remaining, 'failed_remaining': states.get('failed', 0),
            'exhausted': remaining == 0, 'training_pairs_created': 0, 'waiting': states.get('waiting', 0),
            'state': 'waiting' if waiting else 'done' if remaining == 0 else 'pending',
            'code': waiting['waiting_code'] if waiting else None,
            'waiting_code': waiting['waiting_code'] if waiting else None,
            'next_retry_at': waiting['next_retry_at'] if waiting else None,
            'retry_after_seconds': max(0, math.ceil((waiting['next_retry_at'] or 0) - time.time())) if waiting else 0}


def _retire_obsolete_policy(db):
    """Fence old queue work on explicit planning/acquisition, retaining history."""
    stale = "SELECT source_id FROM source_work WHERE acquisition_policy IS NULL OR acquisition_policy!=?"
    db.execute("UPDATE source_attempts SET state='superseded',finished_at=? WHERE state='running' AND source_id IN (" + stale + ")",
               (time.time(), POLICY))
    db.execute("UPDATE source_work SET state='superseded',claim_token=NULL,lease_until=NULL,waiting_code=NULL,next_retry_at=NULL "
               "WHERE state IN ('pending','running','waiting','failed') AND (acquisition_policy IS NULL OR acquisition_policy!=?)", (POLICY,))


def plan(records, *, root=None, qualification=None, should_stop=lambda: False):
    """Stream an existing corpus into the durable queue; no campaign or fetch.

    All corpus candidates remain queued. Acquisition limits apply to each wave,
    never to the lifetime target or the number of records that can be planned.
    """
    shortages = _shortages(qualification)
    scanned = inserted = skipped = 0
    cancelled = False
    with _connect(root) as db:
        _retire_obsolete_policy(db)
        iterator = iter(records)
        while True:
            if should_stop(): cancelled = True; break
            try: original = next(iterator)
            except StopIteration: break
            scanned += 1
            record = copy.deepcopy(original)
            if not isinstance(record, dict) or str(record.get('ecosystem') or '').lower() != 'npm':
                skipped += 1; continue
            hints = _hints(record)
            if (record.get('admission_outcome') == 'admitted' and not record.get('missing_facts')
                    and not any(shortages[m] for m in hints)):
                skipped += 1; continue
            rid = record.get('record_id')
            if not isinstance(rid, str) or not rid:
                skipped += 1; continue
            error = None
            try:
                identity = {'repository_url': _repository(record.get('repository_identity') or record.get('repo_url')),
                    'before_revision': record.get('relevant_parent_commit'), 'after_revision': record.get('fix_commit'),
                    'before_path': _path(record.get('file_path_before')), 'after_path': _path(record.get('file_path_after'))}
                if not all(isinstance(identity[key], str) and _REF.fullmatch(identity[key]) for key in ('before_revision', 'after_revision')):
                    raise ValueError('exact_before_after_revisions_required')
                if identity['before_revision'] == identity['after_revision']:
                    raise ValueError('distinct_before_after_revisions_required')
                if len(_json(record).encode()) > 2 << 20:
                    raise ValueError('record_source_budget_exceeded')
            except ValueError as exc:
                identity = {'blocked_record_id': rid, 'source_pointer_sha256': _digest(record)}
                error = str(exc)
            source_id = _digest({'policy': POLICY, **identity})
            priority = 1000 + sum(shortages[m] for m in hints)
            added = db.execute('INSERT OR IGNORE INTO source_work(source_id,identity,input_fingerprint,state,priority,last_error,acquisition_policy) VALUES (?,?,?,?,?,?,?)',
                (source_id, _json(identity), '', 'blocked' if error else 'pending', priority, error, POLICY)).rowcount
            inserted += added
            fingerprint = _digest(record)
            old = db.execute('SELECT fingerprint FROM source_records WHERE source_id=? AND record_id=?', (source_id, rid)).fetchone()
            if old is None or old['fingerprint'] != fingerprint:
                db.execute('INSERT OR REPLACE INTO source_records(source_id,record_id,fingerprint,body,metadata) VALUES (?,?,?,?,?)',
                    (source_id, rid, fingerprint, _json(record), _json({'mechanism_hints': hints})))
                bindings = [(r['record_id'], r['fingerprint']) for r in db.execute(
                    'SELECT record_id,fingerprint FROM source_records WHERE source_id=? ORDER BY record_id', (source_id,))]
                fingerprint = _digest({'policy': POLICY, 'source_id': source_id, 'records': bindings})
                # Commit members and their fence in the same transaction, even
                # when a large planning scan checkpoints before it finishes.
                db.execute("UPDATE source_work SET input_fingerprint=?, state=CASE WHEN state IN ('acquired','failed') THEN 'pending' ELSE state END WHERE source_id=?",
                           (fingerprint, source_id))
            db.execute('UPDATE source_work SET priority=MAX(priority,?) WHERE source_id=?', (priority, source_id))
            if scanned % 128 == 0: db.commit()
        return {**_summary(db), 'records_scanned': scanned, 'inserted': inserted, 'skipped': skipped,
                'cancelled': cancelled, 'input_exhausted': not cancelled, 'qualification_shortages': shortages}


def page(*, root=None, limit=100, offset=0):
    if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
        raise ValueError('invalid_source_page_bounds')
    with _connect(root) as db:
        rows = db.execute('SELECT * FROM source_work ORDER BY priority DESC,source_id LIMIT ? OFFSET ?', (limit, offset))
        return {**_summary(db), 'items': [_task(db, row, include_records=False) for row in rows], 'limit': limit, 'offset': offset}


def evidence(source_id, *, root=None):
    with _connect(root) as db:
        row = db.execute('SELECT artifact_sha256 FROM source_work WHERE source_id=?', (source_id,)).fetchone()
    if not row or not row['artifact_sha256']: return None
    path = _root(root) / '.npm_evidence_sources' / (row['artifact_sha256'] + '.json')
    try: raw = path.read_bytes()
    except OSError: return None
    if hashlib.sha256(raw).hexdigest() != row['artifact_sha256']:
        raise ValueError('source_evidence_artifact_hash_mismatch')
    return json.loads(raw)


def _artifact(root, digest):
    """Always verify bytes, caching only the parse of small immutable artifacts."""
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest): return None
    path = _root(root) / '.npm_evidence_sources' / (digest + '.json')
    try:
        if path.stat().st_size > MAX_ARTIFACT_BYTES: return None
        raw = path.read_bytes()
        if len(raw) > MAX_ARTIFACT_BYTES or hashlib.sha256(raw).hexdigest() != digest: return None
        with _PARSED_GUARD:
            parsed = _PARSED_ARTIFACTS.pop(digest, None)
            if parsed is None: parsed = json.loads(raw)
            if len(raw) <= 4 << 20:
                _PARSED_ARTIFACTS[digest] = parsed
                while len(_PARSED_ARTIFACTS) > 2: _PARSED_ARTIFACTS.popitem(last=False)
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _exact_identity(repository, revision, path=None):
    try:
        repository = _repository(repository).removeprefix('https://').lower()
        if not isinstance(revision, str) or not _REF.fullmatch(revision): return None
        if path is not None: _path(path)
        return repository
    except (ValueError, TypeError):
        return None


def read_exact(repository, revision, path, *, root=None):
    """Read only backend-committed, permission-checked evidence with exact hashes.

    This is context evidence, never a trusted execution result or a security label.
    Arbitrary JSON files and uncommitted/stale worker output have no index entry.
    """
    repository = _exact_identity(repository, revision, path)
    if repository is None or not (_root(root) / '.npm_evidence_sources.sqlite3').is_file(): return None
    with _connect(root) as db:
        row = db.execute('SELECT * FROM exact_source_files WHERE repository=? AND revision=? AND path=?', (repository, revision, path)).fetchone()
    if row is None or type(row['bytes']) is not int or row['bytes'] < 0 or row['bytes'] > MAX_ARTIFACT_BYTES: return None
    artifact = _artifact(root, row['artifact_sha256'])
    if (artifact is None or artifact.get('schema') != 'npm-source-artifact/1'
            or artifact.get('policy') != POLICY or artifact.get('source_id') != row['source_id']
            or artifact.get('admission_status') != 'not_evaluated'):
        return None
    payload = artifact.get('payload') or {}
    if _exact_identity(payload.get('repository_url'), revision) != repository: return None
    for side in ('before', 'after'):
        snapshot = (payload.get('snapshots') or {}).get(side) or {}
        if payload.get(side + '_revision') != revision or snapshot.get('revision') != revision: continue
        text = (snapshot.get('files') or {}).get(path)
        metadata = (artifact.get('files') or {}).get(side, {}).get(path)
        if not isinstance(text, str) or not isinstance(metadata, dict): return None
        raw = text.encode()
        if (len(raw) == row['bytes'] == metadata.get('bytes')
                and hashlib.sha256(raw).hexdigest() == row['content_sha256'] == metadata.get('sha256')):
            return text
    return None


def exact_paths(repository, revision, *, root=None, limit=4096):
    """A bounded filename-only index; an over-budget result is never truncated."""
    if type(limit) is not int or not 1 <= limit <= 65536: raise ValueError('invalid_source_path_limit')
    repository = _exact_identity(repository, revision)
    if repository is None or not (_root(root) / '.npm_evidence_sources.sqlite3').is_file(): return ()
    with _connect(root) as db:
        rows = list(db.execute('SELECT path FROM exact_source_files WHERE repository=? AND revision=? ORDER BY path LIMIT ?', (repository, revision, limit + 1)))
    if len(rows) > limit: raise ValueError('source_path_index_budget_exceeded')
    return tuple(row['path'] for row in rows)


def attempt_page(source_id, *, root=None, limit=100, offset=0):
    if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
        raise ValueError('invalid_source_page_bounds')
    with _connect(root) as db:
        total = db.execute('SELECT COUNT(*) FROM source_attempts WHERE source_id=?', (source_id,)).fetchone()[0]
        rows = db.execute('SELECT * FROM source_attempts WHERE source_id=? ORDER BY started_at,attempt_id LIMIT ? OFFSET ?', (source_id, limit, offset))
        return {'items': [{**dict(row), 'result': _object(row['result'])} for row in rows], 'total': total, 'limit': limit, 'offset': offset}


class SourceBudgetExceeded(ValueError):
    pass


def _wave_deferral(error):
    return isinstance(error, SourceBudgetExceeded) and str(error) in _WAVE_BUDGET_CODES


class SourceCancelled(Exception):
    pass


class SourceWaiting(Exception):
    """A durable capability wait, not a source-proof or admission failure."""
    def __init__(self, code, detail, *, retry_after_seconds=60, next_retry_at=None):
        if code not in ('source_quota_unavailable', 'source_disk_unavailable', 'source_memory_unavailable'):
            raise ValueError('unknown_source_waiting_code')
        super().__init__(detail)
        self.code = code
        delay = retry_after_seconds if type(retry_after_seconds) in (int, float) and math.isfinite(retry_after_seconds) else 60
        retry = next_retry_at if type(next_retry_at) in (int, float) and math.isfinite(next_retry_at) else time.time() + delay
        self.next_retry_at = max(time.time() + 1, retry)


def _waiting_for_error(error):
    from .ingest.codemine import RateLimited
    from .acceleration.pipeline_spool import DiskPressure
    if isinstance(error, SourceWaiting): return error
    if isinstance(error, RateLimited):
        return SourceWaiting('source_quota_unavailable', str(error), next_retry_at=error.reset)
    if isinstance(error, MemoryError) or type(error) is RuntimeError and str(error) == "can't start new thread":
        return SourceWaiting('source_memory_unavailable', str(error))
    if isinstance(error, DiskPressure) or isinstance(error, OSError) and (
            error.errno == errno.ENOSPC or getattr(error, 'winerror', None) == 112):
        return SourceWaiting('source_disk_unavailable', str(error))
    if isinstance(error, OSError) and error.errno == errno.ENOMEM:
        return SourceWaiting('source_memory_unavailable', str(error))
    reason = getattr(error, 'reason', None)
    reason = reason if isinstance(reason, dict) else {}
    code = {'pipeline_disk_pressure': 'source_disk_unavailable', 'pipeline_memory_pressure': 'source_memory_unavailable',
            'mining_memory_pressure': 'source_memory_unavailable'}.get(reason.get('code'))
    if code:
        return SourceWaiting(code, reason.get('detail') or str(error),
            retry_after_seconds=reason.get('retry_after_seconds', 60), next_retry_at=reason.get('next_retry_at'))
    return None


def _http_waiting(response):
    headers = response.headers
    quota = response.status_code == 429 or response.status_code == 403 and (
        headers.get('X-RateLimit-Remaining') == '0' or headers.get('Retry-After'))
    if not quota: return None
    now, deadlines = time.time(), []
    for value, is_epoch in ((headers.get('Retry-After'), False), (headers.get('X-RateLimit-Reset'), True)):
        if value is None: continue
        try:
            number = float(value)
            if math.isfinite(number) and number >= 0: deadlines.append(number if is_epoch else now + number)
        except (TypeError, ValueError):
            if not is_epoch:
                try: deadlines.append(parsedate_to_datetime(value).timestamp())
                except (TypeError, ValueError, OverflowError): pass
    return SourceWaiting('source_quota_unavailable', 'Public upstream quota or rate limit is unavailable',
        next_retry_at=max(deadlines, default=now + 60))


def _check(bounds):
    if bounds['should_stop'](): raise SourceCancelled('source_acquisition_cancelled')
    if time.monotonic() >= bounds['deadline']: raise SourceBudgetExceeded('source_time_budget_exceeded')


def _validate(task, payload, bounds):
    from .ingest import licenses
    _check(bounds)
    if (not isinstance(payload, dict) or payload.get('schema') != SCHEMA
            or payload.get('repository_url') != task['repository_url']
            or payload.get('before_revision') != task['before_revision']
            or payload.get('after_revision') != task['after_revision']
            or payload.get('parent_revision') != task['before_revision']):
        raise ValueError('exact_source_revision_or_parent_mismatch')
    snapshots, permissions = payload.get('snapshots'), payload.get('permissions')
    if not isinstance(snapshots, dict) or not isinstance(permissions, dict):
        raise ValueError('missing_exact_snapshots_or_permissions')
    total_files = 0
    for side in ('before', 'after'):
        snapshot, permission = snapshots.get(side), permissions.get(side)
        if (not isinstance(snapshot, dict) or snapshot.get('revision') != task[side + '_revision']
                or not isinstance(snapshot.get('files'), dict)):
            raise ValueError('exact_snapshot_revision_mismatch:' + side)
        files = snapshot['files']; total_files += len(files)
        for path, text in files.items():
            _path(path)
            if not isinstance(text, str): raise ValueError('source_blob_must_be_complete_utf8')
            if len(text.encode()) > bounds['max_blob_bytes']:
                raise SourceBudgetExceeded('source_blob_byte_budget_exceeded:' + path)
            declarations = re.findall(r'SPDX-License-Identifier:\s*([^\r\n*]+)', text)
            if any(not licenses.redistributable(declared.strip()) for declared in declarations):
                raise ValueError('source_file_permission_not_established:' + path)
        if (not isinstance(permission, dict) or not licenses.redistributable(permission.get('spdx'))
                or permission.get('usage') != 'permissive'
                or not files.get(permission.get('notice_path'))):
            raise ValueError('pinned_source_permission_not_established:' + side)
        verified_permission = _license(files)
        if (permission['spdx'].lower() != verified_permission['spdx'].lower()
                or permission['notice_path'] != verified_permission['notice_path']):
            raise ValueError('pinned_source_license_claim_mismatch:' + side)
        for record in task['records']:
            code = record.get('vulnerable_code' if side == 'before' else 'fixed_code')
            if not isinstance(code, str) or not code.strip() or code.strip() not in files.get(task[side + '_path'], ''):
                raise ValueError('target_source_mismatch:' + side + ':' + record['record_id'])
    if total_files > bounds['max_files']: raise SourceBudgetExceeded('source_file_count_budget_exceeded')
    if not isinstance(payload.get('omissions'), list): raise ValueError('source_omissions_required')
    regressions = payload.get('regression_paths', [])
    if not isinstance(regressions, list) or len(regressions) > bounds['max_regressions']:
        raise SourceBudgetExceeded('regression_file_count_budget_exceeded')
    if any(not isinstance(path, str) or not _TEST.search(path) or path not in snapshots['after']['files'] for path in regressions):
        raise ValueError('regression_source_not_in_exact_after_snapshot')
    network = payload.get('network_bytes', 0)
    if type(network) is not int or network < 0: raise ValueError('invalid_network_byte_count')
    payload_bytes = len(_json(payload).encode())
    if payload_bytes > bounds['wave_bytes']:
        raise SourceBudgetExceeded('source_artifact_capacity_budget_exceeded')
    if max(payload_bytes, network) > bounds['remaining_bytes']:
        raise SourceBudgetExceeded('source_wave_byte_budget_exceeded')


def _bounded_get(session, url, bounds, *, maximum=None):
    """Stream at most the admitted bytes; no redirect or unbounded cache read."""
    from .acceleration.http_cache import host_slot, observe
    from .acceleration import operations
    host = urlsplit(url).hostname
    transport = 'github_api' if host == 'api.github.com' else 'github_raw'
    if host not in ('api.github.com', 'raw.githubusercontent.com'):
        raise ValueError('unsupported_source_transport_host')
    _check(bounds); operations.check(disk=False)
    individual_limit = maximum or bounds['max_blob_bytes']
    allowed = min(individual_limit, bounds['remaining_bytes'] - bounds['network_bytes'])
    if allowed <= 0: raise SourceBudgetExceeded('source_wave_byte_budget_exceeded')
    cache = _root(bounds['root']) / '.npm_evidence_sources' / 'http'
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()
    cached, metadata = cache / (key + '.bin'), cache / (key + '.json')
    try:
        info = json.loads(metadata.read_text(encoding='utf-8'))
        if info == {'url': url, 'status': 404}:
            bounds['last_transport'] = {'transport': transport, 'source_url': url, 'cache_hit': True}
            return None
        if cached.stat().st_size <= allowed:
            data = cached.read_bytes()
            if info == {'url': url, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}:
                bounds['last_transport'] = {'transport': transport, 'source_url': url, 'cache_hit': True}
                return data
    except (OSError, ValueError, TypeError):
        pass
    if bounds['requests'] >= bounds['max_requests']: raise SourceBudgetExceeded('source_request_budget_exceeded')
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'qualified-npm-evidence/1', 'Accept-Encoding': 'identity'}
    token = os.environ.get('GITHUB_TOKEN') if host == 'api.github.com' else None
    if token: headers['Authorization'] = 'Bearer ' + token
    started = time.monotonic()
    with host_slot((host, hashlib.sha256((token or '').encode()).hexdigest())) as state:
        bounds['requests'] += 1
        response = session.get(url, stream=True, allow_redirects=False, headers=headers,
            timeout=(min(10, max(.1, bounds['deadline'] - time.monotonic())), min(15, max(.1, bounds['deadline'] - time.monotonic()))))
        try:
            observe(state, response, time.monotonic() - started)
            wait = _http_waiting(response)
            if wait is not None: raise wait
            if response.status_code == 404:
                temporary = cache / (uuid.uuid4().hex + '.tmp')
                temporary.write_text(_json({'url': url, 'status': 404}), encoding='utf-8')
                os.replace(temporary, metadata)
                bounds['last_transport'] = {'transport': transport, 'source_url': url, 'cache_hit': False}
                return None
            if response.status_code != 200: raise OSError('upstream_http_status:' + str(response.status_code))
            length = response.headers.get('Content-Length')
            if response.headers.get('Content-Encoding', 'identity') not in ('', 'identity'):
                raise ValueError('source_compressed_transfer_not_supported')
            if length and length.isdigit() and int(length) > allowed:
                code = ('source_response_byte_budget_exceeded' if int(length) > individual_limit else
                        'source_blob_wave_capacity_budget_exceeded' if int(length) > bounds['wave_bytes'] else 'source_wave_byte_budget_exceeded')
                raise SourceBudgetExceeded(code)
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=min(16384, allowed + 1)):
                _check(bounds); operations.check(disk=False)
                size += len(chunk); bounds['network_bytes'] += len(chunk)
                if size > allowed or bounds['network_bytes'] > bounds['remaining_bytes']:
                    raise SourceBudgetExceeded('source_response_byte_budget_exceeded' if size > individual_limit else 'source_wave_byte_budget_exceeded')
                chunks.append(chunk)
            if length and length.isdigit() and size != int(length):
                raise ValueError('source_response_length_mismatch')
            data = b''.join(chunks)
            temporary = cache / (uuid.uuid4().hex + '.tmp')
            temporary.write_bytes(data); os.replace(temporary, cached)
            temporary = cache / (uuid.uuid4().hex + '.tmp')
            temporary.write_text(_json({'url': url, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}), encoding='utf-8')
            os.replace(temporary, metadata)
            bounds['last_transport'] = {'transport': transport, 'source_url': url, 'cache_hit': False}
            return data
        finally:
            response.close()


def _license(files):
    from .ingest import licenses
    manifest = _object(files.get('package.json'), {})
    spdx = manifest.get('license') if isinstance(manifest, dict) else None
    if isinstance(spdx, dict): spdx = spdx.get('type')
    if spdx is not None and (not isinstance(spdx, str) or not licenses.redistributable(spdx)):
        raise ValueError('pinned_source_manifest_permission_not_established')
    notice_paths = [p for p in _NOTICES if files.get(p)]
    identified = []
    for notice_path in notice_paths:
        text = files[notice_path]
        notice = ' '.join(text.split())
        declarations = re.findall(r'SPDX-License-Identifier:\s*([^\r\n*]+)', text)
        found = None
        if declarations:
            claims = {claim.strip().lower() for claim in declarations}
            if len(claims) == 1 and licenses.redistributable(next(iter(claims))):
                found = declarations[0].strip()
        elif ('Permission is hereby granted, free of charge, to any person obtaining a copy' in notice
                and ('MIT License' in notice or isinstance(spdx, str) and spdx.lower() == 'mit')):
            found = 'MIT'
        elif 'Apache License' in notice and 'Version 2.0, January 2004' in notice:
            found = 'Apache-2.0'
        elif ('Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted' in notice):
            found = 'ISC'
        elif ('Redistribution and use in source and binary forms, with or without' in notice
                and isinstance(spdx, str) and spdx.lower() in ('bsd-2-clause', 'bsd-3-clause')):
            found = spdx
        if found is None:
            raise ValueError('pinned_source_notice_permission_not_established:' + notice_path)
        identified.append(found)
    if not identified:
        raise ValueError('pinned_source_permission_not_established')
    if len({claim.lower() for claim in [*identified, *([spdx] if spdx else [])]}) != 1:
        raise ValueError('conflicting_pinned_source_permission_claims')
    return {'spdx': identified[0], 'usage': 'permissive', 'notice_path': notice_paths[0],
            'manifest_path': 'package.json' if files.get('package.json') else None}


def _cached_commit(repository_url, revision):
    from . import security_snapshots
    from .acceleration.repository_service import service, CompatibilityFallback
    path = security_snapshots.repository_path({'repo_url': repository_url})
    if path is None or not path.is_dir(): raise CompatibilityFallback('local_repository_unavailable')
    reader = service(path)
    with reader.reader_lease(): values = reader.metadata(revision)
    if not isinstance(values, dict) or not isinstance(values.get('parents'), (list, tuple)):
        raise ValueError('invalid_cached_git_metadata')
    return {'sha': values.get('sha'), 'parents': [{'sha': parent} for parent in values['parents']],
            'tree': {'sha': values.get('tree')}}, {'transport': 'git_snapshot', 'revision': revision,
            'source_url': repository_url + '/commit/' + revision}


def _git_snapshot(snapshot, revision):
    from . import security_snapshots
    original = getattr(snapshot, 'original', snapshot)
    return original if isinstance(original, security_snapshots.GitSnapshot) and original.available and original.revision == revision else None


def _cached_blob(snapshot, revision, path):
    from . import security_snapshots
    git = _git_snapshot(snapshot, revision)
    if git is not None:
        reader = getattr(git, 'service', None)
        with reader.reader_lease() if reader is not None else nullcontext(): value = git.read(path)
        if value is not None: return value, {'transport': 'git_snapshot', 'revision': revision, 'path': path}
        if path not in git.paths: return None, {'transport': 'git_snapshot', 'absent': True}
    if isinstance(snapshot, security_snapshots.AcquiredSnapshot):
        value = read_exact(snapshot.repository, revision, path, root=snapshot.root)
        if value is not None: return value, {'transport': 'acquired_artifact', 'revision': revision, 'path': path}
    elif git is None and snapshot.available and snapshot.revision == revision:
        value = snapshot.read(path)
        if value is not None: return value, {'transport': 'snapshot_provider', 'revision': revision, 'path': path}
    return None, None


def fetch_upstream(task, bounds):
    """Bounded exact GitHub metadata and raw files with shared host quota control."""
    import requests
    from . import security_snapshots
    repository = task['repository_url'].removeprefix('https://github.com/')
    api = 'https://api.github.com/repos/' + repository
    snapshots, permissions, trees, tree_available, omissions = {}, {}, {}, {}, []
    provenance = {'metadata': {}, 'blobs': {'before': {}, 'after': {}}, 'trees': {}}
    with requests.Session() as session:
        def metadata(url):
            data = _bounded_get(session, url, bounds, maximum=bounds['max_metadata_bytes'])
            if data is None: raise ValueError('exact_source_metadata_unavailable')
            value = json.loads(data)
            if not isinstance(value, dict): raise ValueError('invalid_source_metadata')
            return value
        from .acceleration.repository_service import CompatibilityFallback
        commits = {}
        for side in ('before', 'after'):
            _check(bounds)
            try:
                commits[side], provenance['metadata'][side] = _cached_commit(task['repository_url'], task[side + '_revision'])
            except (CompatibilityFallback, OSError) as exc:
                if _waiting_for_error(exc) is not None: raise
                commits[side] = metadata(api + '/git/commits/' + task[side + '_revision'])
                provenance['metadata'][side] = {**bounds['last_transport'], 'revision': task[side + '_revision'], 'fallback_reason': str(exc)}
        if (any(commits[side].get('sha') != task[side + '_revision'] for side in commits)
                or task['before_revision'] not in [p.get('sha') for p in commits['after'].get('parents', [])]):
            raise ValueError('exact_source_revision_or_parent_mismatch')
        for side in ('before', 'after'):
            revision = task[side + '_revision']
            files = {}; snapshots[side] = {'revision': revision, 'files': files}
            snapshot = security_snapshots.snapshot_for({'repo_url': task['repository_url']}, revision)
            raw_base = 'https://raw.githubusercontent.com/' + repository + '/' + revision + '/'
            def read(path):
                _check(bounds)
                _path(path)
                if path in files: return files[path]
                if sum(len(s['files']) for s in snapshots.values()) >= bounds['max_files']:
                    raise SourceBudgetExceeded('source_file_count_budget_exceeded')
                value, origin = _cached_blob(snapshot, revision, path)
                if value is not None:
                    if len(value.encode()) > bounds['max_blob_bytes']: raise SourceBudgetExceeded('source_blob_byte_budget_exceeded')
                    files[path] = value; provenance['blobs'][side][path] = origin
                elif not origin or not origin.get('absent'):
                    data = _bounded_get(session, raw_base + quote(path, safe='/'), bounds)
                    if data is not None:
                        files[path] = data.decode('utf-8', errors='strict')
                        provenance['blobs'][side][path] = dict(bounds['last_transport'])
                return files.get(path)
            # Public license/manifest metadata is acquired before any production
            # or regression code. A permissive stored classification alone is
            # insufficient to replace the pinned notice in this output.
            read('package.json')
            for path in _NOTICES:
                read(path)
            permissions[side] = _license(files)
            # Local exact-object cache reads use the existing no-network service.
            source = read
            source(task[side + '_path'])
            # The bounded recursive tree is only a filename discovery hint. A
            # truncated/oversized tree is never silently used as a full scan.
            tree_id = (commits[side].get('tree') or {}).get('sha')
            trees[side], tree_available[side] = {}, False
            try:
                if not isinstance(tree_id, str) or not _REF.fullmatch(tree_id): raise ValueError('exact_tree_identity_missing')
                git = _git_snapshot(snapshot, revision)
                if git is not None:
                    if len(git.paths) > bounds['max_tree_entries']: raise SourceBudgetExceeded('source_tree_scan_budget_exceeded')
                    trees[side] = dict.fromkeys(git.paths)
                    provenance['trees'][side] = {'transport': 'git_snapshot', 'revision': revision, 'tree_revision': tree_id}
                else:
                    tree = metadata(api + '/git/trees/' + tree_id + '?recursive=1')
                    if tree.get('sha') != tree_id or tree.get('truncated') is not False:
                        raise SourceBudgetExceeded('source_tree_truncated_or_mismatched')
                    entries = tree.get('tree')
                    if not isinstance(entries, list) or len(entries) > bounds['max_tree_entries']:
                        raise SourceBudgetExceeded('source_tree_scan_budget_exceeded')
                    trees[side] = {item['path']: item.get('sha') for item in entries if isinstance(item, dict)
                                  and item.get('type') == 'blob' and item.get('mode') in ('100644', '100755') and isinstance(item.get('path'), str)}
                    provenance['trees'][side] = dict(bounds['last_transport'])
                tree_available[side] = True
            except (OSError, ValueError) as exc:
                if _wave_deferral(exc): raise
                omissions.append({'reason': 'optional_tree_discovery_unavailable', 'side': side, 'tree_revision': tree_id,
                                  'detail': type(exc).__name__ + ': ' + str(exc)})
            from . import context_builder
            pending, seen = [(task[side + '_path'], 0)], set()
            while pending:
                _check(bounds)
                path, depth = pending.pop(0)
                if path in seen: continue
                seen.add(path)
                text = files.get(path) or ''
                specs = {m.group(1) for pattern in (context_builder._JS_IMPORT, context_builder._JS_REQUIRE) for m in pattern.finditer(text)}
                for spec in sorted(specs):
                    _check(bounds)
                    if not spec.startswith('.'):
                        if spec not in ('path', 'node:path', 'child_process', 'node:child_process', 'util', 'node:util'):
                            omissions.append({'reason': 'external_dependency_not_acquired', 'specifier': spec, 'path': path})
                        continue
                    if depth >= 4 or len(seen) + len(pending) >= bounds['max_dependencies'] + 1:
                        omissions.append({'reason': 'relative_dependency_budget', 'specifier': spec, 'path': path}); continue
                    candidates = context_builder._js_candidates(path, spec)
                    if tree_available[side]:
                        matches = [candidate for candidate in candidates if candidate in trees[side]]
                    else:
                        matches = []
                        # Exact relative imports have a bounded candidate list.
                        # No partial tree is used to infer that a path exists.
                        for candidate in candidates:
                            _check(bounds)
                            try:
                                if source(candidate) is not None: matches.append(candidate)
                            except (OSError, ValueError) as exc:
                                if _wave_deferral(exc): raise
                                omissions.append({'reason': 'relative_dependency_source_unavailable', 'path': candidate,
                                                  'detail': type(exc).__name__ + ': ' + str(exc)})
                    if len(matches) != 1:
                        omissions.append({'reason': 'relative_dependency_missing_or_ambiguous', 'specifier': spec, 'path': path}); continue
                    candidate = matches[0]
                    if candidate in seen: continue
                    if depth >= 4 or len(seen) + len(pending) >= bounds['max_dependencies'] + 1:
                        omissions.append({'reason': 'relative_dependency_budget', 'path': candidate}); continue
                    source(candidate); pending.append((candidate, depth + 1))
            # Exact relative dependencies named in the existing context remain
            # candidates; this module does not interpret them as mechanism proof.
            for record in task['records']:
                context = _object(record.get('context_' + side), {})
                for item in context.get('items', []) if isinstance(context, dict) else []:
                    path = item.get('path') if isinstance(item, dict) else None
                    if path and (not tree_available[side] or path in trees[side]): source(path)
            if side == 'after':
                candidates = sorted((path for path in trees[side] if _TEST.search(path) and path.endswith(('.js', '.ts', '.mjs', '.cjs'))),
                    key=lambda path: (trees['before'].get(path) == trees['after'][path], path))
                selected = candidates[:bounds['max_regressions']]
                if len(candidates) > len(selected): omissions.append({'reason': 'regression_file_selection_budget', 'omitted': len(candidates) - len(selected)})
                for path in selected: source(path)
                regressions = [path for path in selected if path in files]
                omissions.extend({'reason': 'exact_regression_blob_unavailable', 'path': path} for path in selected if path not in files)
    return {'schema': SCHEMA, 'repository_url': task['repository_url'],
        'before_revision': task['before_revision'], 'after_revision': task['after_revision'],
        'parent_revision': task['before_revision'], 'snapshots': snapshots, 'permissions': permissions,
        'regression_paths': regressions, 'omissions': omissions, 'network_bytes': bounds['network_bytes'], 'provenance': provenance}


def acquire(*, root=None, limit=25, max_bytes=16 << 20, max_blob_bytes=1 << 20,
            max_files=64, max_regressions=8, max_tree_entries=4096, max_requests=40,
            max_seconds=120, max_attempts=3, max_dependencies=8, fetch=None, should_stop=lambda: False, retry_failed=False):
    """Acquire one finite wave. Interrupted work and failed attempts remain durable."""
    for value in (limit, max_bytes, max_blob_bytes, max_files, max_regressions, max_tree_entries, max_requests, max_attempts, max_dependencies):
        if type(value) is not int or value <= 0: raise ValueError('invalid_source_acquisition_bounds')
    if type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError('invalid_source_acquisition_bounds')
    started = time.monotonic()
    counts = Counter(); cancelled = False; used = requests_used = 0; deferred_code = None
    for _ in range(limit):
        if should_stop(): cancelled = True; break
        if time.monotonic() - started >= max_seconds or used >= max_bytes or requests_used >= max_requests: break
        with _connect(root) as db:
            db.execute('BEGIN IMMEDIATE')
            _retire_obsolete_policy(db)
            if db.execute("SELECT 1 FROM source_work WHERE state='waiting' AND next_retry_at>? LIMIT 1", (time.time(),)).fetchone():
                break
            stale = [row[0] for row in db.execute("SELECT claim_token FROM source_work WHERE state='running' AND lease_until<?", (time.time(),))]
            for token in stale:
                db.execute("UPDATE source_attempts SET state='interrupted',finished_at=? WHERE attempt_id=? AND state='running'", (time.time(), token))
            db.execute("UPDATE source_work SET state='pending',claim_token=NULL,lease_until=NULL WHERE state='running' AND lease_until<?", (time.time(),))
            states = ('pending', 'waiting', 'failed') if retry_failed else ('pending', 'waiting')
            row = db.execute('SELECT * FROM source_work WHERE state IN (' + ','.join('?' for _ in states) + ') '
                "AND (state!='waiting' OR next_retry_at<=?) "
                'AND (SELECT COUNT(*) FROM source_attempts a WHERE a.source_id=source_work.source_id AND a.fingerprint=source_work.input_fingerprint AND ' + _GENUINE_FAILURE + ')<? '
                'ORDER BY priority DESC,source_id LIMIT 1', (*states, time.time(), *_LEGACY_WAVE_ERRORS, max_attempts)).fetchone()
            if row is None: break
            task = _task(db, row)
            token = uuid.uuid4().hex
            db.execute("UPDATE source_work SET state='running',attempt_count=attempt_count+1,claim_token=?,lease_until=?,waiting_code=NULL,next_retry_at=NULL WHERE source_id=?", (token, time.time()+max_seconds+30, task['source_id']))
            db.execute('INSERT INTO source_attempts VALUES (?,?,?,?,?,?,?)', (token, task['source_id'], task['input_fingerprint'], 'running', time.time(), None, None))
        bounds = {'remaining_bytes': max_bytes-used, 'max_blob_bytes': max_blob_bytes, 'max_files': max_files,
            'wave_bytes': max_bytes,
            'max_regressions': max_regressions, 'max_tree_entries': max_tree_entries, 'max_requests': max_requests-requests_used,
            'max_metadata_bytes': 1 << 20, 'deadline': started+max_seconds,
            'max_dependencies': max_dependencies,
            'should_stop': should_stop, 'requests': 0, 'network_bytes': 0, 'root': str(_root(root))}
        counts['attempted'] += 1
        state, error, artifact_sha, wait, deferred = 'failed', None, None, None, False
        try:
            previous = evidence(task['source_id'], root=root)
            payload = previous['payload'] if previous else (fetch or fetch_upstream)(task, bounds)
            _validate(task, payload, bounds)
            _check(bounds)
            envelope = {'schema': 'npm-source-artifact/1', 'policy': POLICY, 'source_id': task['source_id'],
                'input_fingerprint': task['input_fingerprint'], 'admission_status': 'not_evaluated',
                'labels_are': 'discovery_hints_only', 'training_pairs_created': 0, 'payload': payload,
                'files': {side: {path: {'sha256': hashlib.sha256(text.encode()).hexdigest(), 'bytes': len(text.encode()),
                    'acquisition': copy.deepcopy((payload.get('provenance') or {}).get('blobs', {}).get(side, {}).get(path) or {'transport': 'callback_provider'}),
                    'source_url': 'https://raw.githubusercontent.com/' + task['repository_url'].removeprefix('https://github.com/') + '/' + task[side + '_revision'] + '/' + quote(path, safe='/')}
                    for path, text in payload['snapshots'][side]['files'].items()} for side in ('before', 'after')}}
            raw = _json(envelope).encode(); artifact_sha = hashlib.sha256(raw).hexdigest()
            if len(raw) > MAX_ARTIFACT_BYTES: raise SourceBudgetExceeded('source_artifact_size_budget_exceeded')
            if len(raw) > bounds['wave_bytes']: raise SourceBudgetExceeded('source_artifact_capacity_budget_exceeded')
            if len(raw) > bounds['remaining_bytes']: raise SourceBudgetExceeded('source_wave_byte_budget_exceeded')
            directory = _root(root) / '.npm_evidence_sources'; directory.mkdir(exist_ok=True)
            path = directory / (artifact_sha + '.json')
            if not path.exists():
                temporary = directory / (token + '.tmp'); temporary.write_bytes(raw); os.replace(temporary, path)
            used += max(len(raw), payload.get('network_bytes', 0)); state = 'acquired'
        except SourceCancelled:
            state, cancelled = 'pending', True
        except Exception as exc:
            from . import stopping
            wait = _waiting_for_error(exc)
            if _wave_deferral(exc):
                state, error, deferred = 'pending', str(exc), True
                deferred_code = str(exc)
                counts['deferred'] += 1
            elif wait is not None:
                state, error = 'waiting', str(wait)
                counts['waiting_attempts'] += 1
            elif isinstance(exc, stopping.Paused):
                state, cancelled = 'pending', True
            else:
                error = type(exc).__name__ + ': ' + str(exc)
                counts['failed'] += 1
            used += bounds['network_bytes']
        requests_used += bounds['requests']
        with _connect(root) as db:
            current = db.execute('SELECT input_fingerprint,claim_token FROM source_work WHERE source_id=?', (task['source_id'],)).fetchone()
            superseded = False
            if current and current['claim_token'] == token:
                if current['input_fingerprint'] != task['input_fingerprint']:
                    state, error, artifact_sha = 'pending', 'source_input_changed_during_acquisition', None
                    superseded = True
                if state == 'acquired':
                    repository = _exact_identity(task['repository_url'], task['before_revision'])
                    indexed = [(repository, task[side + '_revision'], path, artifact_sha, detail['sha256'], detail['bytes'], task['source_id'])
                               for side in ('before', 'after') for path, detail in envelope['files'][side].items()]
                    for item in indexed:
                        known = db.execute('SELECT content_sha256 FROM exact_source_files WHERE repository=? AND revision=? AND path=?', item[:3]).fetchone()
                        if known and known['content_sha256'] != item[4]:
                            state, error, artifact_sha = 'failed', 'conflicting_exact_source_content', None
                            counts['failed'] += 1
                            break
                    if state == 'acquired':
                        db.executemany('INSERT OR REPLACE INTO exact_source_files VALUES (?,?,?,?,?,?,?)', indexed)
                db.execute('UPDATE source_work SET state=?,last_error=?,artifact_sha256=COALESCE(?,artifact_sha256),claim_token=NULL,lease_until=NULL,waiting_code=?,next_retry_at=? WHERE source_id=? AND claim_token=?',
                    (state, error, artifact_sha, wait.code if wait and not superseded else None,
                     wait.next_retry_at if wait and not superseded else None, task['source_id'], token))
            else:
                superseded, error, artifact_sha = True, 'source_worker_fence_replaced', None
            db.execute('UPDATE source_attempts SET state=?,finished_at=?,result=? WHERE attempt_id=?',
                ('cancelled' if cancelled else 'superseded' if superseded else 'deferred' if deferred else state, time.time(), _json({'error': error, 'artifact_sha256': artifact_sha,
                    'network_bytes': bounds['network_bytes'], 'waiting_code': wait.code if wait else None,
                    'next_retry_at': wait.next_retry_at if wait else None}), token))
            if superseded: counts['superseded'] += 1
            elif state == 'acquired': counts['acquired'] += 1
        if cancelled or state == 'waiting' or deferred: break
    with _connect(root) as db:
        summary = _summary(db)
    return {**summary, **dict.fromkeys(('attempted', 'acquired', 'failed', 'deferred'), 0), **dict(counts),
            'cancelled': cancelled, 'bytes_used': used, 'requests_used': requests_used, 'wave_limit': limit,
            'deferred_code': deferred_code,
            'wave_budget_exhausted': deferred_code is not None or used >= max_bytes or requests_used >= max_requests or time.monotonic()-started >= max_seconds}
