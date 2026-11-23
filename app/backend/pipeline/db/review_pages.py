"""Indexed review read model. Raw evidence is never a table-page dependency.

The authoritative Python gate populates metadata in bounded batches once per
source revision. SQLite filters, counts and pages subsequent reads. Triggers
also catch writes that bypass application change counters. Decisions and exact
snapshot freshness are evaluated before selecting the projection revision.
"""
import hashlib
import json
from pathlib import Path

from . import legacy, pipeline_mongo


def source_rows(db):
    batch = []
    for doc in db['code_pairs'].find({'flags.available': True}).sort('_id', 1):
        batch.append(doc)
        if len(batch) == 1000:
            yield from pipeline_mongo._review_rows(db, batch)
            batch = []
    if batch:
        yield from pipeline_mongo._review_rows(db, batch)


def _schema(db):
    conn = db._connection()
    conn.execute('CREATE TABLE IF NOT EXISTS _review_page_epoch (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)')
    conn.execute('INSERT OR IGNORE INTO _review_page_epoch VALUES (1,0)')
    conn.execute('CREATE TABLE IF NOT EXISTS _review_page_state (id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL)')
    conn.execute('''CREATE TABLE IF NOT EXISTS _review_page_rows (
        pair_key TEXT PRIMARY KEY, ecosystem TEXT, candidate INTEGER NOT NULL,
        waiting INTEGER NOT NULL, ai INTEGER NOT NULL, rank INTEGER NOT NULL,
        score REAL NOT NULL, key_hash TEXT NOT NULL, body TEXT NOT NULL)''')
    for name, columns in [('candidate', 'candidate,rank,score,key_hash'),
                          ('eco', 'ecosystem,candidate,rank,score,key_hash'),
                          ('waiting', 'waiting,candidate,rank,score,key_hash'),
                          ('ai', 'ai,candidate,rank,score,key_hash')]:
        conn.execute(f'CREATE INDEX IF NOT EXISTS _review_page_{name} ON _review_page_rows ({columns})')
    for name in ('code_pairs', 'fix_commits', 'advisories', 'pair_enrichments', 'fix_links'):
        table = db[name]._table
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            trigger = f'_review_epoch_{name}_{event}'
            conn.execute(f'CREATE TRIGGER IF NOT EXISTS {trigger} AFTER {event} ON {table} BEGIN '
                         'UPDATE _review_page_epoch SET revision=revision+1 WHERE id=1; END')


def page(decisions, *, limit, offset, ecosystem=None, include_failed=False, review_state='waiting'):
    from app.backend.pipeline import review
    from app.backend.pipeline.schemas import reviewed
    db = legacy.handle()
    with db.transaction():
        _schema(db)
        conn = db._connection()
        revision = conn.execute('SELECT revision FROM _review_page_epoch WHERE id=1').fetchone()[0]
        schema_revision = conn.execute('PRAGMA schema_version').fetchone()[0]
        decision_identity = {rid: {key: decision.get(key) for key in ('decision_id', 'review_status', 'label_state')} |
                             {'completion': (decision.get('fields') or {}).get('_review_exchange')}
                             for rid, decision in decisions.items()}
        policy = [review.RUBRIC_VERSION, review.LINK_EVIDENCE_OK, review.PARSE_OK,
                  review.ATTRIBUTED, review.EXPLANATION_OK, review.gate.__code__.co_code.hex(),
                  Path(review.__file__).stat().st_mtime_ns]
        identity = hashlib.sha256(json.dumps([revision, schema_revision, policy, decision_identity], sort_keys=True, default=str).encode()).hexdigest()
        existing = conn.execute('SELECT fingerprint FROM _review_page_state WHERE id=1').fetchone()
        if not existing or existing[0] != identity:
            conn.execute('DELETE FROM _review_page_rows')
            batch = []
            for row in source_rows(db):
                result = review.gate(row)
                decision = decisions.get(reviewed.record_id_of(row['pair_key']))
                ai = bool(decision and decision.get('review_status') == 'llm_adjudicated')
                completed = bool(decision and (decision.get('review_status') == 'human_reviewed' or
                    ai and ((decision.get('fields') or {}).get('_review_exchange') or {}).get('complete') is True))
                batch.append((row['pair_key'], row.get('ecosystem'), int(result.candidate), int(not completed), int(ai),
                    {'strong': 0, 'moderate': 1}.get(row.get('evidence_level'), 2), -(row.get('attribution_score') or 0.0),
                    review._hash(row['pair_key']), json.dumps(review._item(row, result, decision), separators=(',', ':'))))
                if len(batch) >= 1000:
                    conn.executemany('INSERT INTO _review_page_rows VALUES (?,?,?,?,?,?,?,?,?)', batch); batch = []
            if batch:
                conn.executemany('INSERT INTO _review_page_rows VALUES (?,?,?,?,?,?,?,?,?)', batch)
            conn.execute('INSERT OR REPLACE INTO _review_page_state VALUES (1,?)', (identity,))
        clauses, values = [], []
        if ecosystem:
            clauses.append('ecosystem=?'); values.append(ecosystem)
        if not include_failed:
            clauses.append('candidate=1')
        if review_state == 'waiting':
            clauses.append('waiting=1')
        elif review_state == 'ai_adjudicated':
            clauses.append('ai=1')
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        total = conn.execute('SELECT count(*) FROM _review_page_rows' + where, values).fetchone()[0]
        rows = conn.execute('SELECT body FROM _review_page_rows' + where +
                            ' ORDER BY rank,score,key_hash LIMIT ? OFFSET ?', [*values, limit, offset]).fetchall()
    return {'total': total, 'limit': limit, 'offset': offset, 'items': [json.loads(row[0]) for row in rows]}
