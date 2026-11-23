"""Source-bound enrichment of identical legacy units, without label authority.

Only the backend collector's committed unit ledger can supply replacement source
bindings. Historical quality gates and partition assignments remain in force.
The original record remains in its immutable parent and is bound by a digest.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import contextmanager

from . import appended_evidence, npm_case_collector as collector

SCHEMA = 'source-supersession/1'
_FIELDS = ('package_identity', 'before_tree_hash', 'after_tree_hash', 'collector_version',
           'repository_license', 'data_usage_status', 'byte_span_before', 'byte_span_after',
           'line_span_before', 'line_span_after', 'file_blob_hashes', 'context_before', 'context_after',
           'extraction_version', 'parser_version', 'context_policy_version')
_HEX = collector._HEX


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def reference(row):
    if row.get('collector_version') != collector.POLICY:
        return None
    try:
        snapshot = json.loads(row['context_before'])['snapshot']
        artifact = snapshot['collector_artifact_sha256']
        if not isinstance(artifact, str) or not _HEX.fullmatch(artifact):
            return None
        return {'record_id': row['record_id'], 'source_sha256': collector._source_identity(row),
                'artifact_sha256': artifact}
    except (KeyError, TypeError, ValueError):
        return None


def _registered(ref, root):
    """Read one bounded committed shard; a supplied reference grants no trust."""
    if (not isinstance(ref, dict) or set(ref) != {'record_id', 'source_sha256', 'artifact_sha256'}
            or any(not isinstance(ref.get(k), str) or not _HEX.fullmatch(ref[k])
                   for k in ('source_sha256', 'artifact_sha256'))):
        return None
    with collector._connect(root) as db:
        item = db.execute('SELECT s.path,s.bytes,u.shard_sha FROM source_units u JOIN shards s ON s.sha=u.shard_sha '
            'WHERE u.record_id=? AND u.source_sha=? AND u.artifact_sha=? AND u.policy=?',
            (ref['record_id'], ref['source_sha256'], ref['artifact_sha256'], collector.POLICY)).fetchone()
    if item is None or item['path'] != '.npm_case_collector/shards/' + item['shard_sha'] + '.jsonl':
        return None
    path = root / item['path']
    if (not path.resolve().is_relative_to(root) or not path.is_file() or path.stat().st_size != item['bytes']
            or item['bytes'] > collector.MAX_SHARD_BYTES or collector._hash(path) != item['shard_sha']):
        return None
    matches = []
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if row.get('record_id') == ref['record_id'] and collector._source_identity(row) == ref['source_sha256']:
                matches.append(row)
    if len(matches) != 1 or reference(matches[0]) != ref:
        return None
    return matches[0] if collector.source_binding_for(matches[0], root=root) else None


@contextmanager
def _connect(root):
    db = sqlite3.connect(root / '.evidence_source_supersession.sqlite3', timeout=30)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS revisions (id TEXT PRIMARY KEY,record_id TEXT NOT NULL,body TEXT NOT NULL)')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def enrich(original, ref, *, root=None):
    if original.get('provenance_status') not in ('verified', 'unknown'):
        return original
    if original.get('collector_version') == collector.POLICY and reference(original) == ref:
        return original
    root = collector._root(root)
    candidate = _registered(ref, root) if ref else None
    if candidate is None or appended_evidence.identity(original) != appended_evidence.identity(candidate):
        return original
    # Context is never silently expanded or shrunk by a metadata refresh.
    for side in ('before', 'after'):
        old, fresh = (json.loads(row.get('context_' + side) or '{}') for row in (original, candidate))
        if old.get('budget') is not None and old.get('budget') != fresh.get('budget'):
            return original
    out = copy.deepcopy(original)
    out.update({key: copy.deepcopy(candidate.get(key)) for key in _FIELDS if key in candidate})
    out['provenance_status'] = 'unknown'  # Exact source authenticity does not establish an advisory fix.
    if all(out.get(key) == original.get(key) for key in (*_FIELDS, 'provenance_status')):
        return original
    receipt = {'schema': SCHEMA, 'record_id': original['record_id'], 'security_verdict': None,
        'previous_record_sha256': _digest(original), 'previous': {key: original.get(key) for key in
            ('collector_version', 'provenance_status', 'admission_policy', 'admission_outcome',
             'admission_reasons', 'data_quality_status', 'quarantine_reasons', 'exclusion_reasons')},
        'candidate': ref, 'identity_sha256': appended_evidence.identity(original),
        'replaced_fields': sorted(key for key in (*_FIELDS, 'provenance_status') if out.get(key) != original.get(key)),
        'source_sha256': collector._source_identity(out)}
    receipt['id'] = _digest(receipt)
    with _connect(root) as db:
        db.execute('INSERT OR IGNORE INTO revisions VALUES (?,?,?)', (receipt['id'], out['record_id'], _json(receipt)))
    out['source_revalidation'] = _json(receipt)
    return out


def validate(row, *, root=None):
    """Check persisted backend authority, not a self-consistent imported receipt."""
    if not row.get('source_revalidation'):
        return []
    root = collector._root(root)
    try:
        receipt = json.loads(row['source_revalidation'])
        body = {k: v for k, v in receipt.items() if k != 'id'}
        if (receipt.get('schema') != SCHEMA or receipt.get('id') != _digest(body)
                or receipt.get('record_id') != row.get('record_id')
                or receipt.get('identity_sha256') != appended_evidence.identity(row)
                or receipt.get('source_sha256') != collector._source_identity(row)):
            return ['source_supersession_binding_mismatch']
        path = root / '.evidence_source_supersession.sqlite3'
        if not path.is_file():
            return ['source_supersession_not_backend_issued']
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            stored = db.execute('SELECT body FROM revisions WHERE id=? AND record_id=?',
                                (receipt['id'], row['record_id'])).fetchone()
        finally:
            db.close()
        if stored is None or stored[0] != _json(receipt):
            return ['source_supersession_not_backend_issued']
        if not collector.source_binding_for(row, root=root):
            return ['source_supersession_source_unavailable']
        return []
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return ['source_supersession_invalid']
