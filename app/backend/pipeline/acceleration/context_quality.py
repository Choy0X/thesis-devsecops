"""Bounded metadata-only context assessment. Never a security approval.

Source ratings reuse the shared explanation classifier. Draft checks describe
historical model drafts without controlling optional explanation selection.
No code bodies, model calls or source mutations are needed for these filters.
"""
from collections import OrderedDict
import json
import threading

from app.backend.pipeline.db import mappers
from app.backend.pipeline.explanation_quality import VERSION, context_suggestions
from app.backend.pipeline.ingest.draft_quality import FIELDS, check_draft

_cache = OrderedDict()
_lock = threading.RLock()
_prepared = OrderedDict()


def prepare(db):
    """Track source edits across connections without retaining source bodies."""
    conn = db._connection()
    revision = conn.execute('PRAGMA schema_version').fetchone()[0]
    with _lock:
        if _prepared.get(db) == revision:
            return
        with db.transaction():
            conn.execute('CREATE TABLE IF NOT EXISTS _context_quality_epoch (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)')
            conn.execute('INSERT OR IGNORE INTO _context_quality_epoch VALUES (1,0)')
            for name in ('code_pairs', 'fix_commits', 'fix_links', 'advisories', 'pair_enrichments'):
                table = db[name]._table
                for event in ('INSERT', 'UPDATE', 'DELETE'):
                    conn.execute(f'CREATE TRIGGER IF NOT EXISTS _context_quality_{name}_{event} AFTER {event} ON {table} BEGIN '
                                 'UPDATE _context_quality_epoch SET revision=revision+1 WHERE id=1; END')
        _prepared[db] = conn.execute('PRAGMA schema_version').fetchone()[0]
        while len(_prepared) > 2:
            _prepared.popitem(last=False)


def assess(row, summary=None):
    source = context_suggestions(row)
    draft = {key: row.get(key + '_llm') for key in FIELDS}
    present = sum(isinstance(value, str) and bool(value.strip()) for value in draft.values())
    problem = check_draft(draft, {'summary': summary}) if present else 'No model-written draft'
    draft_level = 'missing' if not present else 'partial' if present < len(FIELDS) else 'weak' if problem else 'complete'
    labels = {'missing_explanation': 'No explanation is available.',
              'placeholder_explanation': 'Replace the placeholder explanation.',
              'malformed_explanation': 'Repair malformed explanation text.',
              'stale_explanation': 'Refresh the explicitly stale explanation.',
              'unrelated_explanation': 'Replace the unrelated explanation.'}
    return {'context_quality': source['quality'], 'context_missing': source['reasons'],
            'context_suggestions': [labels.get(reason, reason) for reason in source['reasons']],
            'draft_quality': draft_level, 'draft_problem': problem,
            **{key: source[key] for key in ('explanation_state', 'explanation_reasons',
                                           'generation_eligible', 'evidence_state', 'evidence_reasons')}}


def _batch(db, docs):
    enrichments = {row['_id']: row for row in db['pair_enrichments'].find({'_id': {'$in': [p['_id'] for p in docs]}})}
    keys = sorted({p['commit_key'] for p in docs if p.get('commit_key')})
    commits = {c['commit_key']: c for c in db['fix_commits'].find({'commit_key': {'$in': keys}}, {'commit_key': 1, 'git.message': 1})}
    links = list(db['fix_links'].find({'commit_key': {'$in': keys}}, {'commit_key': 1, 'advisory_id': 1}))
    aids = list({link['advisory_id'] for link in links})
    advisories = {a['_id']: a for a in db['advisories'].find({'_id': {'$in': aids}}, {'summary': 1, 'details': 1})} if aids else {}
    by_commit = {}
    for link in links:
        if link['advisory_id'] in advisories:
            by_commit.setdefault(link['commit_key'], []).append(advisories[link['advisory_id']])
    for pair in docs:
        key = pair.get('commit_key')
        linked = sorted(by_commit.get(key, []), key=lambda a: a['_id'])
        first = next((a for a in linked if mappers.advisory_text(a.get('summary'), a.get('details'))), {})
        own = {'explanation': mappers.advisory_text(first.get('summary'), first.get('details')),
               'commit_message': (commits.get(key, {}).get('git') or {}).get('message')}
        own.update(pair.get('overrides') or {})
        unit = pair.get('unit') or {}
        enr = enrichments.get(pair['_id'], {})
        row = {**own, 'file_path': unit.get('file'), 'function_name': unit.get('function'),
               'cwe': ';'.join((pair.get('subject') or {}).get('cwe') or []),
               'quarantine_reasons': (pair.get('flags') or {}).get('quarantine'),
               **{k + '_llm': enr.get(k) for k in FIELDS}}
        metadata = pair.get('explanation') or {}
        if isinstance(metadata, dict):
            row['explanation_stale'] = metadata.get('stale') is True or metadata.get('status') == 'stale'
            row['explanation_unrelated'] = metadata.get('unrelated') is True or metadata.get('status') == 'unrelated'
        if isinstance(metadata, dict) and metadata.get('source') == 'llm_fallback':
            row['explanation'] = mappers.advisory_text(first.get('summary'), first.get('details')) or enr.get('original_advisory_reference')
        yield pair['_id'], assess(row, first.get('summary'))


def page_assessments(db, ids):
    docs = list(db['code_pairs'].find({'_id': {'$in': ids}}, dict.fromkeys(
        ('commit_key', 'unit', 'subject', 'overrides', 'explanation', 'flags.quarantine'), 1))) if ids else []
    return dict(_batch(db, docs))


def select(db, pairs, selected, params, filters):
    """Filter before count/page/selection with the same read snapshot.

    Cache only small assessment records, not source text. A source epoch catches
    writes on any connection; schema revision catches collection recreation.
    Two snapshots at most are retained; edits invalidate even without touch().
    """
    if not filters.get('context_quality') and not filters.get('draft_quality'):
        return selected, params, {}
    from app.backend.pipeline.db.codec import id_key
    conn = db._connection()
    stamp = (VERSION, db, conn.execute('SELECT revision FROM _context_quality_epoch WHERE id=1').fetchone()[0],
             conn.execute('PRAGMA schema_version').fetchone()[0],
             selected, json.dumps(params, sort_keys=True))
    with _lock:
        ratings = _cache.get(stamp)
        if ratings is None:
            ratings = {}
            # Context receipts and extraction metadata can be large. Read only
            # the metadata branches used by this assessment, never source code.
            paths = ('_id', 'commit_key', 'unit', 'subject', 'overrides', 'explanation', 'flags')
            projection = ','.join(f"'{path}',json_extract(p.payload,'$.{path}')" for path in paths)
            cursor = conn.execute('SELECT json_object(' + projection + ')' + selected + ' ORDER BY pair.id_sort,pair.doc_key', params)
            while batch := cursor.fetchmany(256):
                docs = [json.loads(row[0]) for row in batch]
                ratings.update(_batch(db, docs))
            # Retain the source revision from the read snapshot. A concurrent
            # source writer must invalidate this result on the next request.
            _cache[stamp] = ratings
            while len(_cache) > 2:
                _cache.popitem(last=False)
        else:
            _cache.move_to_end(stamp)
    def matches(rating):
        for field in ('context_quality', 'draft_quality'):
            wanted = filters.get(field)
            if wanted == 'needs_work' and rating[field] in ('strong', 'complete'):
                return False
            if wanted and wanted != 'needs_work' and rating[field] != wanted:
                return False
        return True
    ids = [id_key(pid) for pid, rating in ratings.items() if matches(rating)]
    # Keep the commit-key lookup as the pair access path. Otherwise SQLite can
    # probe every qualifying ID for every source before checking commit_key.
    return selected + ' AND +pair.doc_key IN (SELECT value FROM json_each(:quality_ids))', {
        **params, 'quality_ids': json.dumps(ids)}, ratings
