"""Selected frozen evidence recovery and source-bound explanation handoff.

Only metadata is indexed. Frozen artifacts are read-only; recovered contexts are
append-only overlays and require a separate immutable rebuild and admission.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import pathlib
import copy
import difflib
import uuid
from itertools import islice
from collections import Counter
from contextlib import contextmanager

from . import evidence_recovery as recovery
from .util import now_utc_precise as _now

SCHEMA = 'insufficient-context-repair/2'


@contextmanager
def connect(root=None):
    base = recovery._root(root)
    base.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(base / '.context_repairs.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript('''
      CREATE TABLE IF NOT EXISTS indexes(identity TEXT PRIMARY KEY, summary TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS candidates(identity TEXT, ordinal INTEGER, record_id TEXT, pair_key TEXT, eligible INTEGER, body TEXT, PRIMARY KEY(identity,record_id));
      CREATE INDEX IF NOT EXISTS context_candidates_page ON candidates(identity,ordinal);
      CREATE TABLE IF NOT EXISTS outcomes(identity TEXT, record_id TEXT, body TEXT, PRIMARY KEY(identity,record_id));
      CREATE TABLE IF NOT EXISTS workflow_heads(identity TEXT PRIMARY KEY, fence INTEGER NOT NULL, attempt_id TEXT);
      CREATE TABLE IF NOT EXISTS workflow_attempts(attempt_id TEXT PRIMARY KEY, identity TEXT NOT NULL, fence INTEGER NOT NULL, state TEXT NOT NULL, created TEXT NOT NULL, finished TEXT, implementation TEXT, total INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS workflow_attempts_page ON workflow_attempts(identity,created);
      CREATE TABLE IF NOT EXISTS workflow_items(attempt_id TEXT, record_id TEXT, ordinal INTEGER, body TEXT NOT NULL, artifact_id TEXT, PRIMARY KEY(attempt_id,record_id));
      CREATE INDEX IF NOT EXISTS workflow_items_page ON workflow_items(attempt_id,ordinal);
      CREATE TABLE IF NOT EXISTS workflow_latest(identity TEXT, record_id TEXT, attempt_id TEXT, body TEXT NOT NULL, artifact_id TEXT, PRIMARY KEY(identity,record_id));
      CREATE TABLE IF NOT EXISTS workflow_events(cursor INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL, record_id TEXT, kind TEXT NOT NULL, created TEXT NOT NULL, body TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS workflow_events_cursor ON workflow_events(attempt_id,cursor);
      CREATE TABLE IF NOT EXISTS workflow_artifacts(artifact_id TEXT PRIMARY KEY, body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS workflow_legacy(identity TEXT, legacy_identity TEXT, PRIMARY KEY(identity,legacy_identity));
    ''')
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _index(tag, root=None):
    import pyarrow.parquet as pq
    directory, identity, manifest = recovery._artifact(tag, root)
    with connect(root) as db:
        saved = db.execute('SELECT summary FROM indexes WHERE identity=?', (identity,)).fetchone()
        if saved:
            return directory, identity, json.loads(saved[0])
        parquet = pq.ParquetFile(directory / 'records.parquet')
        columns = [key for key in ('record_id', 'raw_pair_key', 'admission_outcome', 'label_state', 'missing_facts',
                                   'admission_reasons', 'context_suggestions') if key in parquet.schema_arrow.names]
        counts = Counter()
        db.execute('BEGIN IMMEDIATE')
        ordinal = 0
        for batch in parquet.iter_batches(batch_size=128, columns=columns):
            for row in batch.to_pylist():
                position, ordinal = ordinal, ordinal + 1
                if row.get('admission_outcome') != 'insufficient_context' and row.get('label_state') != 'insufficient_context':
                    continue
                suggestion = recovery._object(row.get('context_suggestions'), {}) or {}
                missing = recovery._object(row.get('missing_facts'), []) or []
                eligible = suggestion.get('generation_eligible') is True and not missing
                item = {'record_id': row['record_id'], 'pair_key': row.get('raw_pair_key'),
                        'missing_facts': missing, 'explanation_state': suggestion.get('explanation_state', 'not_assessed'),
                        'generation_eligible': eligible, 'reasons': recovery._object(row.get('admission_reasons'), []) or []}
                db.execute('INSERT OR REPLACE INTO candidates VALUES(?,?,?,?,?,?)',
                    (identity, position, row['record_id'], row.get('raw_pair_key'), int(eligible), recovery._json(item)))
                counts['total'] += 1
                counts['explanation_eligible'] += int(eligible)
                counts['source_gaps'] += bool(missing)
        summary = {'total': counts['total'], 'explanation_eligible': counts['explanation_eligible'],
                   'source_gaps': counts['source_gaps'], 'basis': 'frozen_dataset_metadata',
                   'source_manifest_sha256': identity, 'admission_policy': manifest.get('admission_policy')}
        db.execute('INSERT OR REPLACE INTO indexes VALUES(?,?)', (identity, recovery._json(summary)))
    return directory, identity, summary


def _outcome_filter(state):
    if state in ('', 'all'):
        return ''
    if state == 'failed':
        return " AND json_extract(o.body,'$.status')='failed'"
    if state == 'unresolved':
        return " AND (json_extract(o.body,'$.resolution')='unresolved' OR json_extract(o.body,'$.status') IN ('failed','unavailable','cancelled','interrupted'))"
    if state == 'repaired':
        return " AND json_extract(o.body,'$.status')='repaired'"
    raise ValueError('Unknown recovery outcome filter.')


def page(tag, *, root=None, limit=100, offset=0, state='all'):
    _, identity, summary = _index(tag, root)
    limit, offset = min(500, max(1, int(limit))), max(0, int(offset))
    with connect(root) as db:
        clause = " FROM candidates c LEFT JOIN workflow_latest o ON o.identity=? AND o.record_id=c.record_id WHERE c.identity=?" + _outcome_filter(state)
        _migrate_legacy(db, identity)
        args = (identity, identity)
        total = db.execute('SELECT count(*)'+clause, args).fetchone()[0]
        rows = db.execute('SELECT c.body,o.body'+clause+' ORDER BY c.ordinal LIMIT ? OFFSET ?', (*args, limit, offset))
        items = [{**json.loads(row[0]), 'recovery': json.loads(row[1]) if row[1] else None} for row in rows]
    return {'schema': SCHEMA, 'items': items, 'total': total, 'limit': limit, 'offset': offset, 'summary': summary}


def selected_ids(tag, *, root=None, state='all'):
    _, identity, summary = _index(tag, root)
    with connect(root) as db:
        clause = " FROM candidates c LEFT JOIN workflow_latest o ON o.identity=? AND o.record_id=c.record_id WHERE c.identity=?" + _outcome_filter(state)
        _migrate_legacy(db, identity)
        ids = [row[0] for row in db.execute('SELECT c.record_id'+clause+' ORDER BY c.ordinal', (identity, identity))]
    return {'ids': ids, 'total': len(ids), 'source_manifest_sha256': summary['source_manifest_sha256']}


def _selection(tag, ids, root=None):
    if not isinstance(ids, list) or not ids or any(not isinstance(rid, str) for rid in ids) or len(set(ids)) != len(ids):
        raise ValueError('Select distinct context item identities.')
    directory, identity, _ = _index(tag, root)
    with connect(root) as db:
        db.execute('CREATE TEMP TABLE selected(record_id TEXT PRIMARY KEY)')
        db.executemany('INSERT INTO selected VALUES(?)', ((rid,) for rid in ids))
        rows = db.execute('SELECT c.ordinal,c.record_id FROM candidates c JOIN selected s USING(record_id) WHERE c.identity=? ORDER BY ordinal', (identity,)).fetchall()
        if len(rows) != len(ids):
            raise ValueError('Unknown or ineligible context item identities.')
    return directory, identity, {row['ordinal']: row['record_id'] for row in rows}


def _rows(directory, selected):
    """Read selected row groups once, in bounded batches; never one file read per item."""
    import pyarrow.parquet as pq
    parquet = pq.ParquetFile(directory / 'records.parquet')
    start = 0
    positions = sorted(selected)
    for group in range(parquet.num_row_groups):
        end = start + parquet.metadata.row_group(group).num_rows
        if any(start <= position < end for position in positions):
            ordinal = start
            for batch in parquet.iter_batches(batch_size=16, row_groups=[group]):
                for row in batch.to_pylist():
                    if ordinal in selected:
                        if row.get('record_id') != selected[ordinal]:
                            raise ValueError('Frozen context source changed; select items again.')
                        yield row
                    ordinal += 1
        start = end


def resolve(tag, ids, *, method='source', root=None):
    directory, identity, selected = _selection(tag, ids, root)
    if method == 'source':
        return {'record_ids': list(selected.values()), 'source_manifest_sha256': identity}
    if method not in ('local', 'json', 'free_api'):
        raise ValueError('Unsupported context method.')
    from . import automatic_evidence, explanation_quality, context_sources
    from .db import legacy
    source = iter(_rows(directory, selected))
    numeric_ids, ineligible = [], 0
    while records := list(islice(source, 16)):
        eligible = [row for row in records if explanation_quality.assess_explanation(row)['generation_eligible']
                    and not automatic_evidence._decisive_missing(row)]
        ineligible += len(records) - len(eligible)
        if not eligible:
            continue
        pairs = {pair['pair_key']: pair['_id'] for pair in legacy.handle()['code_pairs'].find(
            {'pair_key': {'$in': [row['raw_pair_key'] for row in eligible]}}, {'_id': 1, 'pair_key': 1})}
        # Retain hashes instead of two copies of selected source bodies.
        binding = {'code_before': 'vulnerable_code', 'code_after': 'fixed_code', 'parent_commit': 'relevant_parent_commit',
                   'fix_commit': 'fix_commit', 'file_path': 'file_path_after'}
        expected = {record['raw_pair_key']: {key: hashlib.sha256(str(record.get(field) or '').encode()).hexdigest()
                    for key, field in binding.items()} for record in eligible}
        seen = set()
        for row in context_sources.rows_for_ids(list(pairs.values())):
            wanted = expected.get(row['pair_key'])
            if wanted is None or any(hashlib.sha256(str(row.get(key) or '').encode()).hexdigest() != value for key, value in wanted.items()):
                raise ValueError('Selected source is stale or unavailable. Rebuild from current evidence before generation.')
            if not explanation_quality.assess_explanation(row)['generation_eligible']:
                raise ValueError('An explanation is already usable. Refresh the selected items.')
            seen.add(row['pair_key'])
        if seen != set(expected):
            raise ValueError('Selected source is stale or unavailable. Rebuild from current evidence before generation.')
        numeric_ids.extend(pairs[row['raw_pair_key']] for row in eligible)
    if not numeric_ids:
        raise ValueError('No selected items need explanation generation. Recover missing source evidence instead.')
    if ineligible:
        raise ValueError('Selection includes usable explanations or unresolved source evidence. Select only applicable items.')
    return {'ids': numeric_ids, 'source_manifest_sha256': identity}


def _outcome_summary(body):
    allowed = {'record_id', 'attempt_id', 'status', 'retryable', 'resolution', 'admission',
               'remaining_missing_facts', 'reasons', 'error', 'input_sha256', 'artifact_id', 'repair_ids',
               'legacy_identity', 'legacy_binding', 'detail_unavailable', 'resumed_from_attempt_id',
               'source_manifest_sha256', 'implementation'}
    return {key: value for key, value in body.items() if key in allowed}


def _event(db, attempt_id, kind, record_id=None, **body):
    db.execute('INSERT INTO workflow_events(attempt_id,record_id,kind,created,body) VALUES(?,?,?,?,?)',
               (attempt_id, record_id, kind, _now(), recovery._json(body)))


def _migrate_legacy(db, identity):
    """Keep opaque implementation-keyed rows intact and expose their provenance.

    Old rows did not store a manifest binding. Their association is explicitly
    labelled as inferred from candidate membership rather than fabricated proof.
    """
    keys = db.execute('SELECT DISTINCT o.identity FROM outcomes o JOIN candidates c ON c.record_id=o.record_id WHERE c.identity=?', (identity,)).fetchall()
    for key in keys:
        legacy_key = key[0]
        if db.execute('SELECT 1 FROM workflow_legacy WHERE identity=? AND legacy_identity=?', (identity, legacy_key)).fetchone():
            continue
        rows = db.execute('SELECT o.record_id,o.body,c.ordinal FROM outcomes o JOIN candidates c ON c.record_id=o.record_id WHERE c.identity=? AND o.identity=? ORDER BY c.ordinal', (identity, legacy_key)).fetchall()
        aid = 'legacy-' + recovery._digest({'manifest': identity, 'legacy': legacy_key})
        db.execute('INSERT OR IGNORE INTO workflow_attempts VALUES(?,?,?,?,?,?,?,?)', (aid, identity, 0, 'legacy', _now(), None, legacy_key, len(rows)))
        for row in rows:
            body = {**_outcome_summary(json.loads(row['body'])), 'attempt_id': aid, 'legacy_identity': legacy_key,
                    'legacy_binding': 'inferred_candidate_record_id', 'detail_unavailable': True}
            db.execute('INSERT OR IGNORE INTO workflow_items VALUES(?,?,?,?,?)', (aid, row['record_id'], row['ordinal'], recovery._json(body), None))
            db.execute('INSERT OR IGNORE INTO workflow_latest VALUES(?,?,?,?,?)', (identity, row['record_id'], aid, recovery._json(body), None))
        db.execute('INSERT INTO workflow_legacy VALUES(?,?)', (identity, legacy_key))
        _event(db, aid, 'legacy_imported', count=len(rows), legacy_identity=legacy_key, binding='inferred_candidate_record_id')


def _finish_pending(db, identity, attempt_id, state, reason):
    rows = db.execute("SELECT record_id,body FROM workflow_items WHERE attempt_id=? AND json_extract(body,'$.status') IN ('pending','running')", (attempt_id,)).fetchall()
    for row in rows:
        prior = db.execute('SELECT body,artifact_id FROM workflow_latest WHERE identity=? AND record_id=?', (identity, row['record_id'])).fetchone()
        previous = json.loads(prior['body']) if prior else {}
        body = {**previous, 'record_id': row['record_id'], 'attempt_id': attempt_id, 'status': state,
                'retryable': True, 'resolution': 'unresolved', 'reasons': [reason]}
        artifact_id = prior['artifact_id'] if prior else None
        encoded = recovery._json(body)
        db.execute('UPDATE workflow_items SET body=?,artifact_id=? WHERE attempt_id=? AND record_id=?', (encoded, artifact_id, attempt_id, row['record_id']))
        db.execute('INSERT OR REPLACE INTO workflow_latest VALUES(?,?,?,?,?)', (identity, row['record_id'], attempt_id, encoded, artifact_id))
        _event(db, attempt_id, 'item_' + state, row['record_id'], reason=reason, retryable=True)


def begin_attempt(tag, ids, *, root=None):
    _, identity, selected = _selection(tag, ids, root)
    aid = str(uuid.uuid4())
    with connect(root) as db:
        db.execute('BEGIN IMMEDIATE')
        _migrate_legacy(db, identity)
        old = db.execute('SELECT fence,attempt_id FROM workflow_heads WHERE identity=?', (identity,)).fetchone()
        fence = old['fence'] + 1 if old else 1
        if old and old['attempt_id']:
            changed = db.execute("UPDATE workflow_attempts SET state='interrupted',finished=? WHERE attempt_id=? AND state='running'", (_now(), old['attempt_id'])).rowcount
            if changed:
                _finish_pending(db, identity, old['attempt_id'], 'interrupted', 'superseded_by_new_attempt')
                _event(db, old['attempt_id'], 'interrupted', reason='superseded_by_new_attempt')
        db.execute('INSERT OR REPLACE INTO workflow_heads VALUES(?,?,?)', (identity, fence, aid))
        db.execute('INSERT INTO workflow_attempts VALUES(?,?,?,?,?,?,?,?)', (aid, identity, fence, 'running', _now(), None, recovery._digest({'evidence': recovery._implementation_fingerprint(), 'workflow': hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(), 'schema': SCHEMA}), len(selected)))
        for ordinal, rid in selected.items():
            db.execute('INSERT INTO workflow_items VALUES(?,?,?,?,?)', (aid, rid, ordinal, recovery._json({'record_id': rid, 'attempt_id': aid, 'status': 'pending'}), None))
        _event(db, aid, 'attempt_started', total=len(selected), fence=fence)
    return {'attempt_id': aid, 'source_manifest_sha256': identity, 'fence': fence, 'state': 'running', 'total': len(selected)}


def _attempt(db, identity, aid):
    row = db.execute('SELECT * FROM workflow_attempts WHERE identity=? AND attempt_id=?', (identity, aid)).fetchone()
    if not row:
        raise ValueError('Unknown recovery attempt.')
    return row


def _fenced(db, identity, aid):
    attempt = _attempt(db, identity, aid)
    head = db.execute('SELECT * FROM workflow_heads WHERE identity=?', (identity,)).fetchone()
    if attempt['state'] != 'running' or not head or head['attempt_id'] != aid or head['fence'] != attempt['fence']:
        raise InterruptedError('Context recovery attempt is cancelled or superseded; stale writes were rejected.')


def cancel_attempt(tag, attempt_id, *, root=None):
    _, identity, _ = _index(tag, root)
    with connect(root) as db:
        db.execute('BEGIN IMMEDIATE')
        attempt = _attempt(db, identity, attempt_id)
        if attempt['state'] == 'running':
            db.execute("UPDATE workflow_attempts SET state='cancelled',finished=? WHERE attempt_id=?", (_now(), attempt_id))
            db.execute('UPDATE workflow_heads SET fence=fence+1 WHERE identity=? AND attempt_id=?', (identity, attempt_id))
            _finish_pending(db, identity, attempt_id, 'cancelled', 'context_recovery_cancelled')
            _event(db, attempt_id, 'cancelled', reason='requested')
    return {'attempt_id': attempt_id, 'state': 'cancelled' if attempt['state'] == 'running' else attempt['state']}


def _bounds(limit, offset=0):
    return min(500, max(1, int(limit))), max(0, int(offset))


def attempts(tag, *, root=None, limit=100, offset=0):
    _, identity, _ = _index(tag, root)
    limit, offset = _bounds(limit, offset)
    with connect(root) as db:
        _migrate_legacy(db, identity)
        total = db.execute('SELECT count(*) FROM workflow_attempts WHERE identity=?', (identity,)).fetchone()[0]
        rows = db.execute('SELECT * FROM workflow_attempts WHERE identity=? ORDER BY created DESC,attempt_id DESC LIMIT ? OFFSET ?', (identity, limit, offset)).fetchall()
        items = []
        for row in rows:
            counts = {item[0]: item[1] for item in db.execute("SELECT json_extract(body,'$.status'),count(*) FROM workflow_items WHERE attempt_id=? GROUP BY json_extract(body,'$.status')", (row['attempt_id'],))}
            items.append({**dict(row), 'source_manifest_sha256': identity, 'counts': counts})
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset}


def outcomes(tag, attempt_id, *, root=None, limit=100, offset=0):
    _, identity, _ = _index(tag, root)
    limit, offset = _bounds(limit, offset)
    with connect(root) as db:
        _attempt(db, identity, attempt_id)
        total = db.execute('SELECT count(*) FROM workflow_items WHERE attempt_id=?', (attempt_id,)).fetchone()[0]
        rows = db.execute('SELECT body FROM workflow_items WHERE attempt_id=? ORDER BY ordinal LIMIT ? OFFSET ?', (attempt_id, limit, offset))
        items = [json.loads(row[0]) for row in rows]
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset, 'attempt_id': attempt_id}


def events(tag, attempt_id, *, root=None, limit=100, after=0):
    _, identity, _ = _index(tag, root)
    limit, after = _bounds(limit, after)
    with connect(root) as db:
        _attempt(db, identity, attempt_id)
        rows = db.execute('SELECT * FROM workflow_events WHERE attempt_id=? AND cursor>? ORDER BY cursor LIMIT ?', (attempt_id, after, limit)).fetchall()
        items = [{**dict(row), 'body': json.loads(row['body'])} for row in rows]
        total = db.execute('SELECT count(*) FROM workflow_events WHERE attempt_id=? AND cursor>?', (attempt_id, after)).fetchone()[0]
    return {'items': items, 'total': total, 'limit': limit, 'next_cursor': items[-1]['cursor'] if items else after, 'has_more': total > len(items)}


def detail(tag, record_id, *, root=None, attempt_id=None):
    directory, identity, selected = _selection(tag, [record_id], root)
    with connect(root) as db:
        _migrate_legacy(db, identity)
        if attempt_id:
            _attempt(db, identity, attempt_id)
            row = db.execute('SELECT body,artifact_id FROM workflow_items WHERE attempt_id=? AND record_id=?', (attempt_id, record_id)).fetchone()
            if not row:
                raise ValueError('Item was not selected in this recovery attempt.')
        else:
            row = db.execute('SELECT body,artifact_id FROM workflow_latest WHERE identity=? AND record_id=?', (identity, record_id)).fetchone()
        if row and row['artifact_id']:
            artifact = json.loads(db.execute('SELECT body FROM workflow_artifacts WHERE artifact_id=?', (row['artifact_id'],)).fetchone()[0])
            return {**artifact, 'artifact_id': row['artifact_id'], 'outcome': json.loads(row['body'])}
    original = next(_rows(directory, selected))
    from .verify.automatic import source_binding
    return {'record_id': record_id, 'source_manifest_sha256': identity, 'original': _contexts(original),
            'repaired': None, 'diff': {}, 'source_binding': source_binding(original), 'repair_ids': [],
            'outcome': json.loads(row['body']) if row else None, 'detail_unavailable': bool(row)}


def _contexts(record):
    return {field: copy.deepcopy(record.get(field)) for field in ('context_before', 'context_after')}


def _context_text(value):
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, indent=2)


def _context_artifact(record, subject, repaired, identity, repair_ids):
    from .verify.automatic import source_binding
    original, final = _contexts(record), _contexts(repaired)
    diff = {field: '\n'.join(difflib.unified_diff(_context_text(original[field]).splitlines(),
            _context_text(final[field]).splitlines(), fromfile='original/' + field, tofile='repaired/' + field, lineterm=''))
            for field in original}
    return {'record_id': record['record_id'], 'source_manifest_sha256': identity, 'original': original,
            'input': _contexts(subject), 'repaired': final, 'diff': diff, 'source_binding': source_binding(record),
            'input_binding': source_binding(subject), 'repaired_binding': source_binding(repaired), 'repair_ids': repair_ids}


def execute(tag, ids, *, root=None, progress=None, cancelled=None, attempt_id=None):
    directory, manifest_id, selected = _selection(tag, ids, root)
    attempt_id = attempt_id or begin_attempt(tag, ids, root=root)['attempt_id']
    counts, resumed = Counter(), 0
    with connect(root) as db:
        _fenced(db, manifest_id, attempt_id)
        implementation = _attempt(db, manifest_id, attempt_id)['implementation']
        attempt_ids = {row[0] for row in db.execute('SELECT record_id FROM workflow_items WHERE attempt_id=?', (attempt_id,))}
        if attempt_ids != set(ids):
            raise ValueError('Recovery attempt selection does not match.')
    with recovery._connect(root) as db:
        overlays = recovery.repair_groups(json.loads(row[0]) for row in db.execute('SELECT body FROM repairs ORDER BY rowid'))
    try:
        for index, record in enumerate(_rows(directory, selected), 1):
            if cancelled and cancelled():
                cancel_attempt(tag, attempt_id, root=root)
                raise InterruptedError('Context recovery cancelled; completed outcomes are retained.')
            rid = record['record_id']
            subject = recovery.apply_repairs(record, overlays)
            from .verify.automatic import source_binding
            input_sha = recovery._digest(source_binding(subject))
            with connect(root) as db:
                db.execute('BEGIN IMMEDIATE')
                _fenced(db, manifest_id, attempt_id)
                prior = db.execute('SELECT body,artifact_id FROM workflow_latest WHERE identity=? AND record_id=?', (manifest_id, rid)).fetchone()
                _event(db, attempt_id, 'item_started', rid, processed=index - 1, total=len(selected))
                db.execute('UPDATE workflow_items SET body=? WHERE attempt_id=? AND record_id=?', (recovery._json({'record_id': rid, 'attempt_id': attempt_id, 'status': 'running'}), attempt_id, rid))
            result = json.loads(prior['body']) if prior else None
            artifact_id, changes, repaired, receipt = None, {}, subject, None
            if result and not result.get('retryable') and result.get('input_sha256') == input_sha and result.get('implementation') == implementation:
                resumed += 1
                artifact_id = prior['artifact_id']
                result = {**result, 'resumed_from_attempt_id': result.get('attempt_id')}
            else:
                try:
                    repaired, result = recovery.recover_record(subject)
                    if any(str(reason).startswith('exact_source_unavailable:') for reason in result.get('reasons', [])):
                        from .verify import automatic
                        facts = automatic.diagnose_case(subject).get('missing_facts', [])
                        acquired = recovery.acquire_missing_source(subject, facts)
                        if acquired.get('fetched'):
                            repaired, result = recovery.recover_record(subject)
                    changes = {field: repaired[field] for field in ('context_before', 'context_after') if repaired.get(field) != subject.get(field)}
                    from . import automatic_evidence
                    remaining = automatic_evidence._decisive_missing(repaired)
                    result = {**result, 'record_id': rid, 'admission': 'requires_revalidation_and_rebuild',
                              'remaining_missing_facts': remaining,
                              'resolution': 'unresolved' if remaining or result['status'] in ('unavailable', 'failed') else 'requires_revalidation'}
                    if result['resolution'] == 'unresolved':
                        result['retryable'] = True
                except Exception as error:
                    changes, repaired = {}, subject
                    result = {'record_id': rid, 'status': 'failed', 'retryable': True, 'resolution': 'unresolved', 'error': str(error)}
            result = {**_outcome_summary(result), 'input_sha256': input_sha, 'attempt_id': attempt_id, 'implementation': implementation}
            result.setdefault('retryable', result['status'] in ('failed', 'unavailable'))
            with connect(root) as db:
                db.execute('BEGIN IMMEDIATE')
                _fenced(db, manifest_id, attempt_id)
                # Hold the fence transaction through append-only overlay publication.
                # A cancellation committed during retrieval prevents this write.
                repair_ids = []
                if changes:
                    receipt = recovery.save_repair(subject, changes, root=root)
                    repair_ids.append(receipt['repair_id'])
                if artifact_id is None:
                    artifact = _context_artifact(record, subject, repaired, manifest_id, repair_ids)
                    artifact_id = recovery._digest(artifact)
                    db.execute('INSERT OR IGNORE INTO workflow_artifacts VALUES(?,?)', (artifact_id, recovery._json(artifact)))
                result['artifact_id'] = artifact_id
                result['repair_ids'] = repair_ids or result.get('repair_ids', [])
                body = recovery._json(result)
                db.execute('UPDATE workflow_items SET body=?,artifact_id=? WHERE attempt_id=? AND record_id=?', (body, artifact_id, attempt_id, rid))
                db.execute('INSERT OR REPLACE INTO workflow_latest VALUES(?,?,?,?,?)', (manifest_id, rid, attempt_id, body, artifact_id))
                _event(db, attempt_id, 'item_completed', rid, outcome=result)
            if receipt:
                overlays.setdefault(subject.get('raw_pair_key'), []).append(receipt)
            counts[result['status']] += 1
            if progress:
                progress(state='running', label='Recovering selected source evidence', attempt_id=attempt_id,
                         processed=index, total=len(selected), counts=dict(counts), resumed=resumed)
        with connect(root) as db:
            db.execute('BEGIN IMMEDIATE')
            _fenced(db, manifest_id, attempt_id)
            db.execute("UPDATE workflow_attempts SET state='done',finished=? WHERE attempt_id=?", (_now(), attempt_id))
            _event(db, attempt_id, 'attempt_completed', counts=dict(counts), resumed=resumed)
    except InterruptedError:
        with connect(root) as db:
            if db.execute("UPDATE workflow_attempts SET state='interrupted',finished=? WHERE attempt_id=? AND state='running'", (_now(), attempt_id)).rowcount:
                _finish_pending(db, manifest_id, attempt_id, 'interrupted', 'worker_interrupted')
                _event(db, attempt_id, 'interrupted', reason='worker_interrupted')
        raise
    except BaseException as error:
        with connect(root) as db:
            if db.execute("UPDATE workflow_attempts SET state='failed',finished=? WHERE attempt_id=? AND state='running'", (_now(), attempt_id)).rowcount:
                _finish_pending(db, manifest_id, attempt_id, 'failed', 'worker_failed:' + str(error))
                _event(db, attempt_id, 'attempt_failed', error=str(error))
        raise
    report = {'schema': SCHEMA, 'state': 'done', 'attempt_id': attempt_id, 'processed': len(selected), 'total': len(selected),
              'counts': dict(counts), 'resumed': resumed, 'source_manifest_sha256': manifest_id, 'requires_rebuild': True}
    if progress:
        progress(**report, label='Source recovery finished; rebuild to revalidate admission')
    return report

