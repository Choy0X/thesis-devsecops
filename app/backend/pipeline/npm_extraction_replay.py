"""Replay saved exact-source cases when extraction changes, without acquisition.

Old artifacts and rows remain immutable. Only new unreviewed units are added to
an atomically published union shard under the collector and workflow fences.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import time

POLICY = 'npm-unit-replay/2'
_QUALITY = ('data_quality_status', 'quarantine_reasons', 'exclusion_reasons', 'production_relevance')


def _unit_source(row):
    from . import npm_case_collector as collector
    return collector._source_identity({**row, 'record_id':'unit', 'raw_pair_key':'unit'})


def fingerprint():
    from . import funcsplit, npm_case_collector as collector, npm_package_scope, npm_evidence_sources
    from .ingest import codemine, splitquality
    from .acceleration import analysis_cache, parser_tasks
    return collector._digest({'policy': POLICY, 'parser': funcsplit.parser_fingerprint(),
        'stack': analysis_cache.stack_fingerprint(), 'extraction': codemine.EXTRACTION_VERSION,
        'modules': {module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                    for module in (collector, parser_tasks, splitquality, npm_package_scope, npm_evidence_sources)},
        'implementation': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})


def _schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS extraction_revisions (
        work_id TEXT NOT NULL, signature TEXT NOT NULL, artifact_sha TEXT NOT NULL,
        previous_shard TEXT NOT NULL, result_shard TEXT NOT NULL, additional_records INTEGER NOT NULL,
        completed REAL NOT NULL, PRIMARY KEY(work_id,signature,artifact_sha))''')
    db.execute('''CREATE TABLE IF NOT EXISTS extraction_failures (
        work_id TEXT NOT NULL, signature TEXT NOT NULL, artifact_sha TEXT NOT NULL,
        previous_shard TEXT NOT NULL, status TEXT NOT NULL,reason TEXT NOT NULL,
        recorded REAL NOT NULL, PRIMARY KEY(work_id,signature,artifact_sha))''')


def _read_shard(root, row, collector):
    if not collector._HEX.fullmatch(str(row.get('shard_sha') or '')):
        raise ValueError('replay_invalid_shard_identity')
    with collector._connect(root) as db:
        shard = db.execute('SELECT * FROM shards WHERE sha=?', (row['shard_sha'],)).fetchone()
    if shard is None or shard['path'] != '.npm_case_collector/shards/' + row['shard_sha'] + '.jsonl':
        raise ValueError('replay_registered_shard_missing')
    path = root / shard['path']
    if (not path.resolve().is_relative_to(root) or not path.is_file() or path.stat().st_size != shard['bytes']
            or path.stat().st_size > collector.MAX_SHARD_BYTES or collector._hash(path) != row['shard_sha']):
        raise ValueError('replay_original_shard_corrupted')
    rows = {}
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            record = json.loads(line)
            identity = record['record_id']
            if identity in rows:
                raise ValueError('replay_duplicate_original_unit')
            rows[identity] = record
    if len(rows) != shard['records'] or len(rows) > collector.MAX_SHARD_RECORDS:
        raise ValueError('replay_original_shard_count_mismatch')
    return rows


def execute(*, root=None, limit=25, cancelled=None, progress=None, work_ids=None):
    from . import npm_case_collector as collector, npm_evidence_sources as sources, oslock, fsio
    from .verify import resource_budget
    if type(limit) is not int or not 0 < limit <= 25:
        raise ValueError('invalid_extraction_replay_limit')
    if work_ids is not None and (not isinstance(work_ids, (tuple, list)) or len(work_ids) > 25
            or any(not isinstance(value, str) or not collector._HEX.fullmatch(value) for value in work_ids)
            or len(set(work_ids)) != len(work_ids)):
        raise ValueError('invalid_replay_selection')
    root = collector._root(root)
    stop = cancelled or (lambda: False)
    guard = getattr(cancelled, 'publication_guard', None) or nullcontext
    signature = fingerprint()
    out = {'schema': POLICY, 'state': 'done', 'signature': signature, 'attempted': 0,
           'replayed': 0, 'unsupported':0, 'additional_records': 0, 'network_requests': 0}
    if work_ids == [] or work_ids == ():
        return {**out, 'remaining': 0}
    selection = ' AND w.id IN (' + ','.join('?' for _ in work_ids) + ')' if work_ids is not None else ''
    selected = tuple(work_ids or ())
    lock = oslock.FileLock(root / '.npm_case_collector.acquire.lock', note=False)
    lock.acquire(wait=0)
    try:
        with collector._connect(root) as db:
            _schema(db)
            if work_ids is not None:
                count = db.execute("SELECT count(*) FROM work w WHERE w.collection_policy=? AND w.state='collected'" + selection,
                                   (collector.POLICY, *selected)).fetchone()[0]
                if count != len(selected):
                    raise ValueError('unknown_replay_selection')
            targets = [dict(row) for row in db.execute('''SELECT w.* FROM work w
                WHERE w.collection_policy=? AND w.state='collected' AND w.shard_sha IS NOT NULL
                AND w.artifact_sha IS NOT NULL AND NOT EXISTS (
                  SELECT 1 FROM extraction_revisions r WHERE r.work_id=w.id AND r.signature=? AND r.artifact_sha=w.artifact_sha)
                AND NOT EXISTS (SELECT 1 FROM extraction_failures f WHERE f.work_id=w.id AND f.signature=? AND f.artifact_sha=w.artifact_sha)
                ''' + selection + ' ORDER BY w.id LIMIT ?', (collector.POLICY, signature, signature, *selected, limit))]
        for row in targets:
            if stop():
                out['state'] = 'cancelled'
                break
            if resource_budget.current() is not None:
                resource_budget.current().check(require_runtime=False)
            out['attempted'] += 1
            if not collector._HEX.fullmatch(str(row.get('artifact_sha') or '')):
                raise ValueError('replay_invalid_artifact_identity')
            artifact_path = root / '.npm_case_collector/artifacts' / (row['artifact_sha'] + '.json')
            stat = artifact_path.stat()
            try:
                artifact = collector._binding_artifact(str(artifact_path), row['artifact_sha'], stat.st_size,
                    stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
            except collector.UnsupportedCollection as error:
                # An old artifact can fall outside a newer package/notice proof
                # subset. Keep every original byte and pointer, record the
                # unresolved mapping, and let independent cases continue. The
                # source foundation still refuses this artifact for admission.
                with guard():
                    with collector._connect(root) as db:
                        db.execute('BEGIN IMMEDIATE')
                        active = db.execute('SELECT state,shard_sha,artifact_sha FROM work WHERE id=?',(row['id'],)).fetchone()
                        if (stop() or active is None or active['state'] != 'collected'
                                or active['shard_sha'] != row['shard_sha'] or active['artifact_sha'] != row['artifact_sha']):
                            raise sources.SourceCancelled('extraction_replay_fenced')
                        db.execute('INSERT OR IGNORE INTO extraction_failures VALUES (?,?,?,?,?,?,?)',
                            (row['id'],signature,row['artifact_sha'],row['shard_sha'],'unsupported',str(error)[:1000],time.time()))
                out['unsupported'] += 1
                if progress:
                    progress(label='Source mapping unresolved; original evidence retained',
                        processed=out['replayed']+out['unsupported'], total=len(targets), counts=dict(out),
                        item_id=row['id'], reason=str(error)[:1000])
                continue
            originals = _read_shard(root, row, collector)
            budgets, parents = {}, {}
            for original in originals.values():
                for side in ('before', 'after'):
                    context = json.loads(original.get('context_' + side) or '{}')
                    key = (side, original.get('file_path_' + side))
                    budgets.setdefault(key, set()).add(collector._json(context.get('budget') or {}))
                    if original.get('granularity') == 'file':
                        parents[key] = original['record_id']
            bounds = {'root': root, 'should_stop': stop, 'deadline': time.monotonic() + 600,
                'remaining_bytes': collector.MAX_SHARD_BYTES, 'max_files': 32, 'max_tree_entries': 20000,
                'max_blob_bytes': 1 << 20, 'max_changed_files': 20, 'max_metadata_bytes': 4 << 20,
                'max_requests': 0, 'requests': 0, 'network_bytes': 0}
            changes, permissions = collector._validate(artifact['task'], artifact['payload'], bounds)
            added = {}
            previous_units = {(_unit_source(old), collector._json({key:old.get(key) for key in _QUALITY}))
                              for old in originals.values()}
            for candidate in collector._records(artifact['task'], artifact['discoveries'], artifact['payload'],
                    changes, permissions, bounds, row['artifact_sha']):
                rid = candidate['record_id']
                revised_parent = None
                if rid in originals:
                    if collector._source_identity(candidate) != collector._source_identity(originals[rid]):
                        raise ValueError('replay_source_identity_collision')
                    unit = (_unit_source(candidate), collector._json({key:candidate.get(key) for key in _QUALITY}))
                    if (unit in previous_units or candidate.get('granularity') != 'function'
                            or candidate.get('data_quality_status') != 'ok'
                            or candidate.get('production_relevance') != 'production'
                            or set(originals[rid].get('quarantine_reasons') or []) - {
                                'low_match_confidence', 'parse_errors_in_unit', 'residual_region'}
                            or set(originals[rid].get('exclusion_reasons') or []) - {'tangled'}):
                        continue
                    from .schemas import reviewed
                    revised_parent = rid
                    candidate['raw_pair_key'] += '#unit-replay:' + signature
                    rid = candidate['record_id'] = reviewed.record_id_of(candidate['raw_pair_key'])
                    previous_units.add(unit)
                if rid in added:
                    raise ValueError('replay_duplicate_new_unit')
                for side in ('before', 'after'):
                    key = (side, candidate.get('file_path_' + side))
                    budget = collector._json(json.loads(candidate['context_' + side]).get('budget') or {})
                    if budgets.get(key) != {budget}:
                        raise ValueError('replay_context_budget_changed')
                candidate['augmentation_policy_version'] = POLICY
                candidate['lineage_parent_ids'] = sorted({parents[key] for key in (
                    ('before', candidate.get('file_path_before')), ('after', candidate.get('file_path_after'))) if key in parents})
                if revised_parent:
                    candidate['lineage_parent_ids'] = sorted(set(candidate['lineage_parent_ids']) | {revised_parent})
                added[rid] = candidate
                if len(originals) + len(added) > collector.MAX_SHARD_RECORDS:
                    raise ValueError('replay_union_record_budget_exceeded')
            if stop():
                out['state'] = 'cancelled'
                break
            merged = {**originals, **added}
            content = b''.join((collector._json(record) + '\n').encode() for record in merged.values())
            if len(content) > collector.MAX_SHARD_BYTES:
                raise ValueError('replay_union_shard_budget_exceeded')
            sha = hashlib.sha256(content).hexdigest() if added else row['shard_sha']
            relative = '.npm_case_collector/shards/' + sha + '.jsonl'
            with guard():
                with collector._connect(root) as db:
                    db.execute('BEGIN IMMEDIATE')
                    active = db.execute('SELECT state,shard_sha,artifact_sha FROM work WHERE id=?', (row['id'],)).fetchone()
                    if (stop() or active is None or active['state'] != 'collected'
                            or active['shard_sha'] != row['shard_sha'] or active['artifact_sha'] != row['artifact_sha']):
                        raise sources.SourceCancelled('extraction_replay_fenced')
                    if added:
                        path = root / relative
                        if path.exists():
                            if collector._hash(path) != sha:
                                raise ValueError('replay_shard_collision')
                        else:
                            fsio.write_bytes(path, content)
                        db.execute('INSERT OR IGNORE INTO shards VALUES (?,?,?,?)', (sha, relative, len(merged), len(content)))
                        db.executemany('INSERT OR IGNORE INTO source_units VALUES (?,?,?,?,?)', [
                            (record['record_id'], collector._source_identity(record), row['artifact_sha'], sha, collector.POLICY)
                            for record in added.values()])
                        if stop():
                            raise sources.SourceCancelled('extraction_replay_cancelled')
                        db.execute('UPDATE work SET shard_sha=? WHERE id=? AND shard_sha=? AND artifact_sha=?',
                                   (sha, row['id'], row['shard_sha'], row['artifact_sha']))
                    db.execute('INSERT INTO extraction_revisions VALUES (?,?,?,?,?,?,?)',
                        (row['id'], signature, row['artifact_sha'], row['shard_sha'], sha, len(added), time.time()))
            out['replayed'] += 1
            out['additional_records'] += len(added)
            if progress:
                progress(label='Recovering saved npm function units', processed=out['replayed']+out['unsupported'],
                         total=len(targets), counts=dict(out))
        with collector._connect(root) as db:
            out['remaining'] = db.execute('''SELECT count(*) FROM work w WHERE w.collection_policy=?
                AND w.state='collected' AND w.shard_sha IS NOT NULL AND w.artifact_sha IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM extraction_revisions r
                  WHERE r.work_id=w.id AND r.signature=? AND r.artifact_sha=w.artifact_sha)
                AND NOT EXISTS (SELECT 1 FROM extraction_failures f
                  WHERE f.work_id=w.id AND f.signature=? AND f.artifact_sha=w.artifact_sha)''' + selection,
                (collector.POLICY, signature, signature, *selected)).fetchone()[0]
        return out
    except sources.SourceCancelled:
        return {**out, 'state': 'cancelled'}
    finally:
        lock.release()
