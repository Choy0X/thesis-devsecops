"""Assemble, partition, publish and verify the grounded security corpus (security-trial-dataset/3, tag grounded_v34).

Generalisation is what is measured, so partitions are disjoint by package (train, development, blind), a whole set of weakness classes is held out
as a transfer slice, units that share an issue group with a transfer unit are not trained on, identical vulnerable code is kept once, and
training is capped per package, per fix commit and per issue group. The blind partition is published in a separate directory with a commitment.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from app.backend.pipeline import grounded_policy
from app.backend.pipeline import grounded_views as views_lib
from training.finetune import grounded_verify

SCHEMA = 'security-trial-dataset/3'
TAG = 'grounded_v34'
SEED = 'grounded-v30'
TRANSFER_CWES = grounded_policy.HELD_OUT
DEV_PERCENT, BLIND_PERCENT = 10, 10
CAPS = {'per_package': 40, 'per_commit': 8, 'per_group': 12}
CLASS_SHARE_CAP = 0.35          # no weakness class may be more than this share of the training units (prototype-pollution guards are very common)
SPLIT_OF = {'train': 'train', 'development': 'val', 'blind_test': 'blind', 'transfer': 'transfer'}
LIMITATIONS = (
    'Labels are unreviewed upstream-fix evidence mined from npm advisories and GitHub fix commits, not proof of a vulnerability.',
    'Explanation text is extracted from the advisory, the fix commit message and the CWE catalogue; no text is model-written.',
    'No executable tests or oracle: repair correctness and test quality are not verified by running code.',
    'Negatives are scoped fixed twins and unchanged neighbouring functions; none is proven free of other weaknesses.',
    'The blind and transfer partitions are mined data from the same collection, not an independent benchmark.',
    'Units with origin sanitiser_removal are synthetic twins: a real function that sanitises or checks a flowing value, with one protection removed. Their label '
    'is proven by a static flow analysis, not by an advisory; the original function is the fixed side.')
NOT_COVERED = ('insufficient_context', 'executable_verification', 'near_miss_guard_negatives', 'caller_callee_context')


def _bucket(text):
    return int(hashlib.sha256(f'{SEED}|{text}'.encode()).hexdigest()[:8], 16) % 100


def _squash_whitespace(code):
    """Every whitespace run collapsed to one space: the dedup key of identical vulnerable code (not util.norm_code, which keeps line breaks)."""
    return ' '.join((code or '').split())


def assign_partition(unit):
    if any(cwe in TRANSFER_CWES for cwe in unit.get('cwe') or []):
        return 'transfer'
    bucket = _bucket(unit.get('package'))
    if bucket < DEV_PERCENT:
        return 'development'
    if bucket < DEV_PERCENT + BLIND_PERCENT:
        return 'blind_test'
    return 'train'


def dedupe(units):
    seen, kept, dropped = set(), [], 0
    for unit in units:
        key = hashlib.sha256(_squash_whitespace(unit['before']).encode()).hexdigest()
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(unit)
    return kept, dropped


def share_class(unit):
    """The class a unit counts under in the class-share cap: its flow-detector class; for a unit proved by its diff (plan v6, `proof` set) the class of that evidence;
    else 'unclassified' (units of the earlier builds keep exactly this rule)."""
    if unit.get('proof'):
        return (unit.get('diff_grounded') or {}).get('class') or 'unclassified'
    return views_lib.known_cwe(unit) or 'unclassified'


def cap_class_share(units, share=CLASS_SHARE_CAP):
    """Keep the units so that no weakness class is more than `share` of what is kept (a per-class ceiling found by shrinking until it holds; units chosen by
    a stable hash). With fewer classes than 1/share the bound cannot hold and nothing is dropped. Returns (kept units, number dropped)."""
    import collections
    classes = {u['unit_id']: share_class(u) for u in units}
    counts = collections.Counter(classes.values())
    if not units or len(counts) * share < 1:
        return units, 0
    ceiling = max(counts.values())
    while True:
        total = sum(min(n, ceiling) for n in counts.values())
        limit = max(3, int(share * total))
        if ceiling <= limit:
            break
        ceiling = limit
    taken, kept = collections.Counter(), []
    for unit in sorted(units, key=lambda u: hashlib.sha256(u['unit_id'].encode()).hexdigest()):
        cwe = classes[unit['unit_id']]
        if taken[cwe] >= ceiling:
            continue
        taken[cwe] += 1
        kept.append(unit)
    return kept, len(units) - len(kept)


def apply_caps(units, *, per_package, per_commit, per_group):
    import collections
    counts = {name: collections.Counter() for name in ('package', 'commit', 'group')}
    kept = []
    for unit in sorted(units, key=lambda u: hashlib.sha256(u['unit_id'].encode()).hexdigest()):
        keys = {'package': unit.get('package'), 'commit': unit.get('commit_key'), 'group': unit.get('issue_group_id')}
        if (counts['package'][keys['package']] >= per_package or counts['commit'][keys['commit']] >= per_commit
                or counts['group'][keys['group']] >= per_group):
            continue
        for name, key in keys.items():
            counts[name][key] += 1
        kept.append(unit)
    return kept, {'dropped_by_cap': len(units) - len(kept), 'caps': {'per_package': per_package, 'per_commit': per_commit, 'per_group': per_group}}


def _row(view, unit, role, weight=1.0, tier=None):
    cwe = views_lib.known_cwe(unit)
    return {
        'view_id': view['view_id'], 'record_id': unit['unit_id'], 'subject_id': unit['unit_id'], 'family_id': unit.get('issue_group_id') or unit['unit_id'],
        'issue_group_id': unit.get('issue_group_id') or unit['unit_id'], 'task': view['task'], 'pool': view['pool'], 'kind': view['kind'],
        'split': SPLIT_OF[role], 'partition_role': role, 'val_role': '', 'eligible': True, 'weight': float(weight),
        'context_text': view['context_text'], 'response_json': json.dumps(view['response'], sort_keys=True, ensure_ascii=False),
        'origin_corpus': TAG, 'mechanism': cwe or 'unclassified', 'language': unit['language'], 'package': unit.get('package') or '',
        'source_file': unit['file'], 'allowed_patch_paths': [unit['file']], 'cwe_json': json.dumps(list(unit.get('cwe') or [])),
        'lineage_json': json.dumps({'origin': TAG, 'unit_origin': unit.get('origin', 'upstream_fix'), 'unit': unit['unit_id'],
                                    'attribution': unit.get('attribution'), 'link': unit.get('link_level'),
                                    'tier': unit.get('tier'), 'review_tier': tier, 'commit': unit.get('commit_key'),
                                    **({'proof': unit['proof']} if unit.get('proof') else {})}, sort_keys=True)}


def assemble(units, *, view_filter=None, weights=None, tiers=None, provenance=None, held_out_packages=None):
    """Partition, cap and build the views. Returns {'views': rows (all partitions), 'units': kept units, 'manifest': dict}.

    ``view_filter(row) -> bool`` drops views that cannot be used (for example those that do not fit the pinned tokenizer's budget); the drops are counted.
    ``weights`` maps unit_id to the weight of its TRAINING rows (default 1.0; held-out rows always keep 1.0; 0 drops the unit everywhere), ``tiers`` maps unit_id to a review/execution tier label recorded
    in every row, ``held_out_packages`` moves whole packages to the development partition (never to training), ``provenance`` is copied into the manifest."""
    weights, tiers, held = weights or {}, tiers or {}, set(held_out_packages or ())
    deduped, duplicates = dedupe(units)
    weighted = [u for u in deduped if weights.get(u['unit_id'], 1.0) > 0]
    dropped_by_weight, deduped = len(deduped) - len(weighted), weighted
    roles = {u['unit_id']: ('development' if u.get('package') in held and assign_partition(u) != 'transfer' else assign_partition(u)) for u in deduped}
    transfer_groups = {u.get('issue_group_id') for u in deduped if roles[u['unit_id']] == 'transfer'}
    transfer_packages = {u.get('package') for u in deduped if roles[u['unit_id']] == 'transfer'}
    closure = [u for u in deduped if roles[u['unit_id']] != 'transfer'
               and (u.get('issue_group_id') in transfer_groups or u.get('package') in transfer_packages)]
    package_closed = [u for u in closure if u.get('package') in transfer_packages and u.get('issue_group_id') not in transfer_groups]
    closed = {u['unit_id'] for u in closure}
    pool = [u for u in deduped if u['unit_id'] not in closed]
    train = [u for u in pool if roles[u['unit_id']] == 'train']
    other = [u for u in pool if roles[u['unit_id']] != 'train']
    train, cap_report = apply_caps(train, **CAPS)
    views_of = {}

    def build(unit):
        if unit['unit_id'] not in views_of:
            views_of[unit['unit_id']] = views_lib.build_unit_views(unit)
        return views_of[unit['unit_id']]

    productive = [u for u in train if any(v['pool'] == 'supported' for v in build(u))]
    train, class_dropped = cap_class_share(productive)        # the share is of the units that really give training views
    kept = train + other
    rows, per_role_units, token_dropped = [], {}, 0
    for unit in kept:
        role = roles[unit['unit_id']]
        built = build(unit)
        if not any(v['pool'] == 'supported' for v in built):
            continue
        made = [_row(v, unit, role, weights.get(unit['unit_id'], 1.0) if role == 'train' else 1.0, tiers.get(unit['unit_id'])) for v in built]
        if view_filter is not None:
            fitting = [row for row in made if view_filter(row)]
            token_dropped += len(made) - len(fitting)
            made = fitting
            if not any(r['pool'] == 'supported' for r in made):
                continue                      # negatives alone are never kept
        per_role_units.setdefault(role, []).append(unit)
        rows.extend(made)
    import collections
    by_role = collections.Counter(r['partition_role'] for r in rows)
    task_pool = collections.Counter(f"{r['task']}/{r['pool']}" for r in rows)
    kinds = collections.Counter(r['kind'] for r in rows)
    kept_units = [u for group in per_role_units.values() for u in group]
    tier_counts = collections.Counter(tiers.get(u['unit_id'], 'unreviewed') for u in kept_units)
    unit_origins = collections.Counter(u.get('origin', 'upstream_fix') for u in kept_units)
    origin_of = {u['unit_id']: u.get('origin', 'upstream_fix') for u in kept_units}
    view_origins = collections.Counter(origin_of.get(r['record_id'], 'upstream_fix') for r in rows)
    manifest = {
        'schema': SCHEMA, 'tag': TAG, 'dataset_status': 'frozen', 'publication_role': 'main', 'protocol': 'security_chat_v2',
        'origin': 'mined_grounded', 'label_authority': 'unreviewed_upstream_fix_evidence', 'human_review': False,
        'independent_evaluation': False, 'synthetic': any(origin != 'upstream_fix' for origin in unit_origins), 'generated_code_execution': 'never', 'policy': views_lib.POLICY,
        'seed': SEED, 'transfer_cwes': list(TRANSFER_CWES), 'caps': cap_report['caps'],
        'token_fit': 'checked_with_pinned_tokenizer' if view_filter is not None else 'not_checked',
        'counts': {'units_in': len(units), 'duplicates': duplicates, 'group_closure_dropped': len(closure), 'package_closure_dropped': len(package_closed),
                   'dropped_by_cap': cap_report['dropped_by_cap'], 'dropped_by_class_cap': class_dropped, 'dropped_by_weight': dropped_by_weight, 'dropped_by_token_fit': token_dropped,
                   'units_kept': sum(len(v) for v in per_role_units.values()),
                   'views': len(rows), 'views_by_role': dict(by_role),
                   'units_by_role': {k: len(v) for k, v in per_role_units.items()},
                   'packages_by_role': {k: len({u.get('package') for u in v}) for k, v in per_role_units.items()}},
        'unit_origin_counts': dict(unit_origins), 'view_origin_counts': dict(view_origins), 'unit_tier_counts': dict(tier_counts),
        'held_out_packages': sorted(held), 'provenance': dict(provenance or {}),
        'task_pool_counts': dict(task_pool), 'kind_counts': dict(kinds), 'limitations': list(LIMITATIONS), 'not_covered': list(NOT_COVERED)}
    return {'views': rows, 'units': kept, 'manifest': manifest}


def _write_parquet(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path)


def _file_entry(path):
    return grounded_verify.file_entry(path)


def write(data_dir, blind_dir, built):
    data_dir, blind_dir = Path(data_dir), Path(blind_dir)
    if data_dir.exists() and any(data_dir.iterdir()):
        raise FileExistsError(f'refusing to write into a non-empty dataset directory: {data_dir}')
    main = [r for r in built['views'] if r['partition_role'] != 'blind_test']
    blind = [r for r in built['views'] if r['partition_role'] == 'blind_test']
    kept_ids = {r['record_id'] for r in main}
    records = [{'record_id': u['unit_id'], 'package': u.get('package') or '', 'cve_id': u.get('cve_id') or '', 'cwe_json': json.dumps(list(u.get('cwe') or [])),
                'repository_identity': u.get('repo_url') or u.get('package') or u['unit_id'],
                'file': u['file'], 'language': u['language'], 'commit_key': u.get('commit_key') or '', 'issue_group_id': u.get('issue_group_id') or '',
                'tier': u.get('tier') or '', 'attribution': u.get('attribution') or '', 'link_level': u.get('link_level') or ''}
               for u in built['units'] if u['unit_id'] in kept_ids]
    _write_parquet(data_dir / 'views.parquet', main)
    _write_parquet(data_dir / 'records.parquet', records)
    (data_dir / 'LIMITATIONS.txt').write_text('\n'.join(built['manifest']['limitations']) + '\n', encoding='utf-8', newline='\n')
    blind_ids = sorted(r['view_id'] for r in blind)
    commitment = {'tag': built['manifest'].get('tag', TAG) + '_blind', 'sealed': True, 'views': len(blind),
                  'view_ids_sha256': hashlib.sha256('\n'.join(blind_ids).encode()).hexdigest()}
    _write_parquet(blind_dir / 'views.parquet', blind)
    manifest = dict(built['manifest'])
    manifest['files'] = {name: _file_entry(data_dir / name) for name in ('views.parquet', 'records.parquet', 'LIMITATIONS.txt')}
    manifest['blind_commitment'] = commitment
    text = json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + '\n'
    (data_dir / 'dataset_manifest.json').write_text(text, encoding='utf-8', newline='\n')
    return {'manifest_sha256': hashlib.sha256(text.encode()).hexdigest(), 'views': len(main), 'blind_views': len(blind)}


def verify(data_dir):
    return grounded_verify.verify(data_dir)
