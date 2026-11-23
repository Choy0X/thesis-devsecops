"""Load only explicitly selected source records, without dataset-building scans.

Owners and evidence use the same document mappers as pair review. Full advisory
text is read through the selected commits' exact links, never a package-name join.
"""
from app.backend.pipeline.db import legacy, mappers
from app.backend.pipeline.ingest.context import advisory_text


def rows_for_ids(ids):
    db = legacy.handle()
    wanted = sorted(set(ids))
    for start in range(0, len(wanted), 128):
        batch = wanted[start:start + 128]
        docs = list(db['code_pairs'].find({'_id': {'$in': batch}}).sort('_id', 1))
        codes = {r['_id']: r for r in db['pair_code'].find({'_id': {'$in': batch}})}
        drafts = {r['_id']: r for r in db['pair_enrichments'].find({'_id': {'$in': batch}})}
        commits, advisories = legacy.owners_of(db, sorted({p['commit_key'] for p in docs if p.get('commit_key')}))
        for pair in docs:
            key = pair.get('commit_key')
            linked = advisories.get(key, []) if key else legacy.linked_by_subject(db, pair)
            raw = mappers.pair_row(pair, codes.get(pair['_id']), drafts.get(pair['_id']), commits.get(key), linked)
            # Dataset-level deduplication is unrelated to an explicit selection.
            # Prefer the first active mined advisory, in the existing ID order.
            eligible = [a for a in linked if a.get('kind') == 'vulnerability' and 'withdrawn_at' not in a
                        and (a.get('mining') or {}).get('code_status') in ('mined', 'suspect')]
            advisory = next(iter(eligible), None)
            summary, details = (advisory or {}).get('summary'), (advisory or {}).get('details')
            yield {**raw, 'pair_id': raw['id'], 'summary': summary, 'details': details,
                   'advisory': advisory_text(summary, details, fallback=raw.get('explanation')),
                   '_legacy_advisory': raw.get('explanation') or ''}
