"""Pinned candidate streams and derived leakage groups for append-only builds.

This module never fetches source, executes candidates, or trusts imported labels.
Original assignments remain auditable; a new bridge quarantines its whole derived
component rather than leaving previously admitted train/test relatives usable.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import pathlib
import re
import shutil
import sqlite3
from collections import Counter, defaultdict
from urllib.parse import urlsplit

from . import funcsplit, leakage, source_fingerprints, versions
from .schemas import reviewed

SCHEMA = 'appended-source-components/3'
MAX_ROW_BYTES = 8 << 20
MAX_SHARD_BYTES = 64 << 20
MERGE_FIELDS = ('raw_record_keys', 'advisory_aliases', 'source_record_hashes', 'source_urls')
IDENTITY_FIELDS = ('record_id', 'raw_pair_key', 'repository_identity',
    'language', 'ecosystem', 'vulnerable_code', 'fixed_code', 'relevant_parent_commit', 'fix_commit',
    'file_path_before', 'file_path_after')


def identity(row):
    return hashlib.sha256(json.dumps({key: row.get(key) for key in IDENTITY_FIELDS},
        sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def _declared_fix(url):
    """Conservative leakage identity only; a reference is never label evidence."""
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in ('http', 'https') or parsed.netloc.lower() != 'github.com':
        return None
    match = re.fullmatch(r'/([^/]+)/([^/]+)/commit/([0-9a-fA-F]{40})/?', parsed.path)
    if match is None:
        return None
    owner, repository, revision = match.groups()
    return 'github.com/' + owner.lower() + '/' + repository.removesuffix('.git').lower() + '@' + revision.lower()


def _references(captured, root):
    root = pathlib.Path(root).resolve()
    for category in ('artifacts', 'discoveries'):
        entries = captured.get(category, [])
        if not isinstance(entries, list):
            raise ValueError('invalid candidate provenance reference list')
        seen = set()
        for entry in entries:
            sha = entry.get('sha256')
            relative = '.npm_case_collector/' + category + '/' + str(sha) + '.json'
            path = root / relative
            if (not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha) or sha in seen
                    or entry.get('path') != relative or type(entry.get('bytes')) is not int
                    or not 0 <= entry['bytes'] <= MAX_SHARD_BYTES
                    or path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink()
                    or not path.resolve().is_relative_to(root) or not path.is_file()
                    or path.stat().st_size != entry['bytes'] or versions._sha256(path) != sha):
                raise ValueError('candidate provenance artifact reference hash or path mismatch')
            seen.add(sha)
            yield category, entry, path


def candidate_records(captured, *, root):
    if not captured:
        return
    if captured.get('schema') != 'npm-case-shards/1' or not isinstance(captured.get('shards'), list):
        raise ValueError('invalid candidate shard capture')
    expected = hashlib.sha256(json.dumps({key: value for key, value in captured.items() if key != 'sha256'},
        sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    if captured.get('sha256') != expected:
        raise ValueError('candidate capture hash mismatch')
    provenance = {'artifacts': {}, 'discoveries': {}}
    for category, entry, _ in _references(captured, root):
        provenance[category][entry['sha256']] = entry
    root = pathlib.Path(root).resolve()
    expected_root = root / '.npm_case_collector' / 'shards'
    seen = set()
    for entry in captured['shards']:
        sha = entry.get('sha256')
        relative = entry.get('path')
        if (not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha)
                or relative != '.npm_case_collector/shards/' + sha + '.jsonl'
                or sha in seen or type(entry.get('records')) is not int or entry['records'] < 0
                or type(entry.get('bytes')) is not int or not 0 <= entry['bytes'] <= MAX_SHARD_BYTES):
            raise ValueError('invalid candidate shard identity or bounds')
        seen.add(sha)
        path = root / relative
        if (not path.resolve().is_relative_to(expected_root.resolve()) or path.is_symlink()
                or expected_root.is_symlink() or expected_root.parent.is_symlink()
                or not path.is_file() or path.stat().st_size != entry['bytes'] or versions._sha256(path) != sha):
            raise ValueError('candidate shard hash or path mismatch')
        count, hasher = 0, hashlib.sha256()
        with path.open('rb') as stream:
            while raw := stream.readline(MAX_ROW_BYTES + 1):
                if len(raw) > MAX_ROW_BYTES:
                    raise ValueError('candidate row exceeds source budget')
                hasher.update(raw)
                if not raw.strip():
                    raise ValueError('empty candidate shard record')
                row = json.loads(raw)
                if (not isinstance(row, dict) or not isinstance(row.get('raw_pair_key'), str)
                        or row.get('record_id') != reviewed.record_id_of(row['raw_pair_key'])
                        or reviewed.validate({**row, 'split': row.get('split') or 'quarantine'})):
                    raise ValueError('invalid candidate record identity or schema')
                discoveries = row.get('source_record_hashes')
                if (not isinstance(discoveries, list) or not discoveries
                        or any(not isinstance(sha, str) or sha not in provenance['discoveries'] for sha in discoveries)):
                    raise ValueError('candidate discovery reference missing from pinned capture')
                artifacts = set()
                for side in ('before', 'after'):
                    context = json.loads(row.get('context_' + side) or '{}')
                    snapshot = context.get('snapshot') or {}
                    artifact_sha = snapshot.get('collector_artifact_sha256')
                    descriptor = provenance['artifacts'].get(artifact_sha)
                    if (descriptor is None or snapshot.get('collector_artifact_path') != descriptor['path']
                            or snapshot.get('discovery_artifact_sha256s') != sorted(set(discoveries))):
                        raise ValueError('candidate snapshot reference missing or inconsistent with pinned capture')
                    artifacts.add(artifact_sha)
                if len(artifacts) != 1:
                    raise ValueError('candidate snapshot references disagree across source pair')
                count += 1
                yield row
        if count != entry['records'] or hasher.hexdigest() != sha:
            raise ValueError('candidate shard record count or hash changed')


def prepare(records, historical, *, workspace, progress=None):
    """Consume one source stream, retaining only metadata and SSD code signatures."""
    import numpy as np
    workspace = pathlib.Path(workspace)
    db = sqlite3.connect(workspace / 'append-source-signatures.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS codes (rid TEXT,side TEXT,language TEXT,n INTEGER,shingles BLOB,PRIMARY KEY(rid,side))')
    db.execute('CREATE INDEX IF NOT EXISTS code_lengths ON codes(language,n)')
    # Owned resumed staging is rebuilt; stale signatures must not introduce links.
    db.execute('DELETE FROM codes')
    parents, anchors, metadata, edges = {}, {}, {}, []
    added = set()
    empty_fingerprints = 0
    punctuation_fingerprints = 0
    excluded_fingerprints = []

    def find(rid):
        while parents[rid] != rid:
            parents[rid] = parents[parents[rid]]
            rid = parents[rid]
        return rid

    def join(left, right, kind, evidence):
        a, b = find(left), find(right)
        if a != b:
            parents[max(a, b)] = min(a, b)
            edges.append({'src': left, 'dst': right, 'type': kind, 'confidence': 1.0,
                'evidence_json': json.dumps(evidence, sort_keys=True), 'review_only': False})

    def anchor(rid, kind, value):
        if not isinstance(value, str) or not value.strip():
            return
        key = (kind, value)
        if key in anchors:
            join(anchors[key], rid, kind, {'identity': value})
        else:
            anchors[key] = rid

    try:
        for number, row in enumerate(records, 1):
            rid = row['record_id']
            if rid in parents:
                raise ValueError('duplicate append source record identity')
            parents[rid] = rid
            old = historical.get(rid)
            if old is not None and old['split'] != row['split']:
                raise ValueError('historical source/partition mismatch')
            if old is None:
                added.add(rid)
            metadata[rid] = {'split': row.get('split'), 'old': copy.deepcopy(old)}
            for kind, value in (('inherited_component', (old or {}).get('component')),
                    ('inherited_issue', row.get('issue_group_id')), ('same_repository', row.get('repository_identity')),
                    ('fix_family', row.get('fix_commit')), ('raw_pair', row.get('raw_pair_key'))):
                anchor(rid, kind, value)
            repository = row.get('repository_identity') or ''
            repository_url = repository if '://' in repository else 'https://' + repository
            anchor(rid, 'declared_fix', _declared_fix(repository_url.rstrip('/') + '/commit/' + (row.get('fix_commit') or '')))
            for url in row.get('source_urls') or []:
                anchor(rid, 'declared_fix', _declared_fix(url))
            for field in ('raw_record_keys', 'advisory_aliases'):
                for value in row.get(field) or []:
                    anchor(rid, 'alias', value.upper() if isinstance(value, str) else value)
            for field in ('parent_record_id', 'derived_from_record_id'):
                if row.get(field):
                    anchor(rid, 'derivative', row[field])
            anchor(rid, 'derivative', rid)
            for side, field in (('before', 'vulnerable_code'), ('after', 'fixed_code')):
                code = row.get(field)
                if not isinstance(code, str) or not code.strip():
                    continue
                tokens = funcsplit.tokens(row.get('language'), code)
                if not source_fingerprints.meaningful(tokens):
                    excluded_fingerprints.append({'record_id': rid, 'side': side, 'language': row.get('language'),
                        'source_sha256': hashlib.sha256(code.encode()).hexdigest(),
                        'token_sha256': hashlib.sha256('\x1f'.join(tokens).encode()).hexdigest(),
                        'token_count': len(tokens), 'reason': 'empty_tokens' if not tokens else 'punctuation_only_tokens'})
                if not tokens:
                    # An unavailable/empty token stream cannot establish source
                    # identity. All empty streams previously shared sha256("").
                    empty_fingerprints += 1
                    continue
                if not source_fingerprints.meaningful(tokens):
                    punctuation_fingerprints += 1
                    continue
                key = hashlib.sha256('\x1f'.join(tokens).encode()).hexdigest()
                anchor(rid, 'exact_hash', key)
                shingles = leakage._shingles(tokens) if len(tokens) >= leakage.MIN_TOKENS else np.array([], dtype=np.uint64)
                db.execute('INSERT INTO codes VALUES (?,?,?,?,?)', (rid, side, row.get('language'), len(shingles), shingles.tobytes()))
            if number % 128 == 0:
                db.commit()
                if progress:
                    progress(state='append_groups', label='Indexing source relationships', processed=number)
        if not set(historical) <= set(parents):
            raise ValueError('immutable parent source coverage changed')
        db.commit()
        # Compare every potentially similar signature involving an appended unit.
        # Length bounds are exact Jaccard bounds, not probabilistic LSH sampling.
        threshold = leakage.NEAR_CLONE_JACCARD
        for number, rid in enumerate(sorted(added), 1):
            for _, side, language, length, raw in db.execute('SELECT * FROM codes WHERE rid=? AND n>0', (rid,)).fetchall():
                current = np.frombuffer(raw, dtype=np.uint64)
                lo, hi = math.ceil(length * threshold), math.floor(length / threshold)
                for other, other_side, _, other_length, other_raw in db.execute(
                        'SELECT * FROM codes WHERE language=? AND n BETWEEN ? AND ? AND rid!=?', (language, lo, hi, rid)):
                    if find(other) == find(rid):
                        continue
                    previous = np.frombuffer(other_raw, dtype=np.uint64)
                    common = len(np.intersect1d(current, previous, assume_unique=True))
                    similarity = common / (length + other_length - common)
                    if similarity >= threshold:
                        join(other, rid, 'near_clone', {'source_side': other_side, 'target_side': side, 'jaccard': similarity})
            if progress and number % 32 == 0:
                progress(state='append_groups', label='Checking appended source clone relationships', processed=number, total=len(added))
    finally:
        db.close()
    components = defaultdict(list)
    for rid in sorted(parents):
        components[find(rid)].append(rid)
    assignments, conflicts, groups = {}, [], {}
    for component, members in sorted(components.items()):
        known = {(metadata[rid]['old']['split'], metadata[rid]['old'].get('role')) for rid in members if metadata[rid]['old'] is not None}
        conflict = len(known) > 1 or any(split == 'quarantine' for split, _ in known)
        if known:
            split, role = next(iter(known)) if not conflict else ('quarantine', None)
        else:
            bucket = int.from_bytes(hashlib.sha256(('security-split/1:' + component).encode()).digest()[:8], 'big') / (1 << 64)
            split, role = ('test', 'untouched_test') if bucket < .1 else ('val', 'calibration') if bucket < .15 else ('val', 'selection') if bucket < .25 else ('train', None)
        if conflict:
            conflicts.append(component)
        for rid in members:
            old = metadata[rid]['old']
            assignments[rid] = {**(old or {}), 'split': old['split'] if old else split,
                'role': old.get('role') if old else role, 'component': component,
                'quarantined': conflict, 'reason': 'append_source_cross_partition_conflict' if conflict else 'inherited' if old else 'append_source_component',
                **({'historical_component': old.get('component')} if old else {})}
        groups[component] = {'members': members, 'split': 'mixed' if conflict else split,
            'role': role, 'fold': None, 'keys': [], 'overrides': {rid: assignments[rid]['split'] for rid in members} if conflict else {},
            'reason': 'append_source_cross_partition_conflict' if conflict else 'append_source_component'}
    return {'schema': SCHEMA, 'records': assignments, 'groups': groups, 'edges': edges,
        'ignored_empty_fingerprints': empty_fingerprints,
        'ignored_punctuation_fingerprints': punctuation_fingerprints,
        'fingerprint_policy': source_fingerprints.SCHEMA,
        'fingerprint_policy_sha256': hashlib.sha256(pathlib.Path(source_fingerprints.__file__).read_bytes()).hexdigest(),
        'excluded_fingerprint_evidence': excluded_fingerprints,
        'added_records': len(added), 'conflicted_components': conflicts, 'counts': dict(Counter(row['split'] for row in assignments.values()))}


def write_graph(plan, parent, staging):
    from . import build_reviewed as br
    for name in ('edges.parquet', 'groups.parquet', 'split_manifest.json', 'security_partitions.json'):
        shutil.copyfile(parent / name, staging / ('historical_' + name))
    rows = [{'node_id': rid, 'group_id': assignment['component'], 'split': assignment['split'], 'role': assignment.get('role'),
             'fold': None, 'reason': assignment['reason'], 'group_size': len(plan['groups'][assignment['component']]['members'])}
            for rid, assignment in sorted(plan['records'].items())]
    fingerprint_policy = {key: plan[key] for key in ('fingerprint_policy', 'fingerprint_policy_sha256',
        'ignored_empty_fingerprints', 'ignored_punctuation_fingerprints')}
    (staging / 'source_fingerprint_audit.json').write_bytes(br._json_bytes({
        'schema': 'source-fingerprint-exclusions/1', **fingerprint_policy,
        'scope': 'Derived identity exclusions only; source authenticity is bound by the frozen build capture.',
        'records': plan['excluded_fingerprint_evidence']}))
    (staging / 'edges.parquet').write_bytes(br._table_bytes(plan['edges'], br._EDGE_SCHEMA))
    (staging / 'groups.parquet').write_bytes(br._table_bytes(rows, br._GROUP_SCHEMA))
    (staging / 'split_manifest.json').write_bytes(br._json_bytes({'schema': 'split-manifest/4', 'groups': plan['groups'],
        'nodes': {rid: value['component'] for rid, value in plan['records'].items()}, 'append_source': {
            'schema': SCHEMA, 'added_records': plan['added_records'], 'conflicted_components': plan['conflicted_components'],
            **fingerprint_policy,
            'historical_assignments': 'preserved; conflicting components excluded from all eligible views'}}))
    (staging / 'security_partitions.json').write_bytes(br._json_bytes({'schema': 'security-partitions/1',
        'records': plan['records'], 'counts': plan['counts'], 'independent_components': len(plan['groups']),
        'test_is_untouched': True, 'append_source': {'schema': SCHEMA, 'conflicted_components': plan['conflicted_components'],
            **fingerprint_policy}}))


def write_provenance(captured, *, root, destination):
    """Retain actual permission notices plus immutable full-source references."""
    from .npm_evidence_sources import _NOTICES
    discovery_refs = {entry['sha256']: entry for entry in captured.get('discoveries', [])}
    with pathlib.Path(destination).open('w', encoding='utf-8', newline='\n') as stream:
        for category, descriptor, path in _references(captured, root):
            if category != 'artifacts':
                continue
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != descriptor['sha256']:
                raise ValueError('candidate provenance artifact changed during read')
            artifact = json.loads(raw)
            permissions = artifact.get('permissions') or {}
            snapshots = (artifact.get('payload') or {}).get('snapshots') or {}
            notices = {}
            for side in ('before', 'after'):
                permission = permissions.get(side) or {}
                files = (snapshots.get(side) or {}).get('files') or {}
                if not isinstance(files.get(permission.get('notice_path')), str):
                    raise ValueError('candidate source permission notice missing')
                names = {*_NOTICES, permission.get('notice_path'), permission.get('manifest_path')}
                notices[side] = {name: files[name] for name in sorted(name for name in names if isinstance(name, str)) if name in files}
            linked = sorted({entry.get('advisory_sha256') for entry in artifact.get('discoveries', [])
                             if isinstance(entry, dict) and isinstance(entry.get('advisory_sha256'), str)})
            if any(sha not in discovery_refs for sha in linked):
                raise ValueError('candidate source discovery reference missing from capture')
            stream.write(json.dumps({'schema': 'candidate-provenance/1', 'artifact': descriptor,
                'discoveries': [discovery_refs[sha] for sha in linked], 'permissions': permissions, 'notices': notices},
                sort_keys=True, ensure_ascii=True) + '\n')
