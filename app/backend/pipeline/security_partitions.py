"""Strict evaluation partitions. Issue/clone groups and repositories are indivisible.

Historical assignments are retained. A merge of incompatible old roles is
quarantined instead of laundering training data into an untouched test set.
"""
from __future__ import annotations
import datetime
import hashlib
from collections import Counter

SCHEMA = 'security-partitions/1'


def assign(records, *, previous=None, temporal_cutoff=None):
    rows = sorted(records, key=lambda r: r['record_id'])
    parent = {r['record_id']: r['record_id'] for r in rows}
    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]; k = parent[k]
        return k
    anchors = {}
    for row in rows:
        rid = row['record_id']
        for key in [('issue', row.get('issue_group_id')), ('repository', row.get('repository_identity'))]:
            if not key[1]: continue
            if key in anchors:
                a, b = root(rid), root(anchors[key]); parent[max(a, b)] = min(a, b)
            else: anchors[key] = rid
    groups = {}
    for row in rows: groups.setdefault(root(row['record_id']), []).append(row)
    assigned, temporal = {}, {}
    cutoff = datetime.date.fromisoformat(temporal_cutoff) if temporal_cutoff else None
    for gid, members in sorted(groups.items()):
        digest = hashlib.sha256(('security-split/1:' + gid).encode()).digest()
        bucket = int.from_bytes(digest[:8], 'big') / (1 << 64)
        split, role = ('test', 'untouched_test') if bucket < .1 else ('val', 'calibration') if bucket < .15 else ('val', 'selection') if bucket < .25 else ('train', None)
        known = {(previous[r['record_id']]['split'], previous[r['record_id']].get('role')) for r in members if r['record_id'] in (previous or {})}
        reason = 'repository_and_issue_holdout'
        if known:
            split, role = next(iter(known)) if len(known) == 1 else ('quarantine', None)
            reason = 'inherited' if len(known) == 1 else 'conflicting_historical_assignments'
        dates = []
        for row in members:
            try: dates.append(datetime.date.fromisoformat(str(row.get('advisory_first_published_at') or '')[:10]))
            except ValueError: dates.append(None)
        temporal_split = 'quarantine' if not cutoff or None in dates else 'test' if max(dates) >= cutoff else 'train'
        for row in members:
            assigned[row['record_id']] = {'split': split, 'role': role, 'component': gid, 'reason': reason}
            temporal[row['record_id']] = temporal_split
    return {'schema': SCHEMA, 'records': assigned, 'counts': dict(Counter(v['split'] for v in assigned.values())),
            'independent_components': len(groups), 'test_is_untouched': True,
            'temporal': {'cutoff': temporal_cutoff, 'status': 'configured' if cutoff else 'not_configured', 'records': temporal},
            'policy': 'A no-finding result is limited to its reviewed scope.'}
