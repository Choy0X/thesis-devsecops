"""Assemble the grounded security corpus for security_chat_v3 (security-trial-dataset/3, tag grounded_v35).

Same partition, closure, cap and publication rules as grounded_dataset (v34); what is new is that the training partition is balanced so that the answer
cannot be predicted from the form of the prompt or from the class named in the scope:

* the share of findings inside each scope form (any class, one class, a set) of the training discovery views is within BALANCE_FORM,
* the share of findings among the views that name a class in their scope is within BALANCE_CLASS for every class, and every scope class has at least
  MIN_SCOPE_POSITIVES training units that really have a flow of that class,
* the development, transfer and blind partitions keep every view (they are measurements, not training mix).

Dropped views are chosen by a stable hash, from the largest group first, so that no kind of negative disappears.
"""
from __future__ import annotations

import collections
import hashlib
import json
import re

from app.backend.pipeline import grounded_dataset as base
from app.backend.pipeline import grounded_recovery
from app.backend.pipeline import grounded_views as views_lib
from app.backend.pipeline import grounded_views_v3 as v3

TAG = 'grounded_v35'
CLAIM_GATE_FROM = 38                       # the dataset version from which the claim gate is strict (publication field claim_gate)


def _tag_version(tag):
    match = re.search(r'(\d+)$', tag or '')
    return int(match.group(1)) if match else 0


SCHEMA = base.SCHEMA
FORMS = ('open', 'single', 'set')
MIN_SCOPE_POSITIVES = 3
REAL_ORIGINS = ('upstream_fix', 'recovered_D', 'recovered_L')        # real fixes; the synthetic twins are sanitiser_removal and unsafe_substitution
RECOVERY_LIMITATIONS = (
    'Units recovered without a flow proof (origin recovered_D and recovered_L) are supported by the fix diff, the advisory class and either a family template or an LLM answer '
    'that was validated against the shown code; the LLM-authored text is marked in the lineage. They are weaker evidence than a proved flow and train at the reviewer weights.',
    'Unknown-licence code is admitted at full weight by the owner decision of 2026-11-06 and recorded per unit; the dataset is for local research training and is not redistributed.')
BALANCE_FORM = (0.45, 0.55)           # builder target; grounded_verify accepts a wider band so the token filter cannot fail a build
BALANCE_CLASS = (0.30, 0.70)
VERIFY_BAND = {'form_share': [0.40, 0.60], 'class_share': [0.25, 0.75], 'class_min_each': 3}
LIMITATIONS = base.LIMITATIONS + (
    'security_chat_v3: the explanation of a finding is made of names and line numbers of the shown code; the weakness class is chosen from the closed catalogue in the '
    'prompt. A catalogue class without training positives (SSRF, code injection, and others) can be answered but was never taught.',
    'The training views are balanced by thinning negatives (or, where negatives are scarce, findings) with a stable hash; the development, transfer and blind partitions are complete.')


def _row(view, unit, role, weight, tier, tag=TAG):
    row = base._row(view, unit, role, weight, tier)
    row['origin_corpus'] = tag
    row['mechanism'] = v3.label_of(unit) or 'unclassified'
    row['scope_form'] = view.get('form') or ''
    classes = sorted(view.get('scope_classes') or [])
    row['view_key'] = f"{unit['unit_id']}|{view['kind']}|{view.get('form') or ''}|{','.join(classes)}"            # the same case in every dataset version
    row['content_sha'] = hashlib.sha256((view['context_text'] + '\n' + row['response_json']).encode('utf-8')).hexdigest()
    row['scope_classes_json'] = json.dumps(view.get('scope_classes') or [])
    row['expected_class'] = view.get('expected_class') or ''
    row['lineage_json'] = json.dumps({**json.loads(row['lineage_json']), 'origin': tag}, sort_keys=True)
    return row


def _status(row):
    return json.loads(row['response_json']).get('status')


def _stable(row):
    return hashlib.sha256(row['view_id'].encode()).hexdigest()


def balance(rows, *, form_band=BALANCE_FORM, class_band=BALANCE_CLASS, max_rounds=None):
    """(kept rows, report). `rows` are the training rows of one build; non-discovery rows pass through.

    One view is dropped per round until every band holds. `max_rounds` defaults to the number of rows, which can never be reached by a loop that drops one row per round, so the
    result no longer depends on a round limit (a limit of 400 stopped the v37 builds, which need about twice that, with the forms still unbalanced). The per-row facts (answer,
    scope classes, stable hash) are read once, and the counts are updated as views are dropped."""
    low, high = form_band
    kept = list(rows)
    dropped = collections.Counter()
    max_rounds = len(kept) if max_rounds is None else max_rounds
    facts = {}
    for row in kept:
        if row['task'] == 'discovery':
            facts[row['view_id']] = (_status(row) == 'finding', json.loads(row['scope_classes_json']), _stable(row))
    forms, classes = collections.defaultdict(lambda: [0, 0]), collections.defaultdict(lambda: [0, 0])
    for row in kept:
        if row['task'] == 'discovery':
            finding, scope, _ = facts[row['view_id']]
            slot = 0 if finding else 1
            forms[row['scope_form']][slot] += 1
            for cwe in scope:
                classes[cwe][slot] += 1

    def drop_from_largest_kind(candidates):
        by_kind = collections.defaultdict(list)
        for row in candidates:
            by_kind[row['kind']].append(row)
        kind = max(sorted(by_kind), key=lambda k: len(by_kind[k]))
        return max(by_kind[kind], key=lambda r: facts[r['view_id']][2])

    for _ in range(max_rounds):
        action = None
        for form in FORMS:
            findings, others = forms.get(form, [0, 0])
            total = findings + others
            if total and findings / total < low:
                pool = [r for r in kept if r['task'] == 'discovery' and r['scope_form'] == form and not facts[r['view_id']][0]]
                action = ('negative', drop_from_largest_kind(pool)) if pool else None
            elif total and findings / total > high:
                pool = [r for r in kept if r['task'] == 'discovery' and r['scope_form'] == form and facts[r['view_id']][0]]
                action = ('finding', max(pool, key=lambda r: facts[r['view_id']][2])) if pool else None
            if action:
                break
        if not action:
            for cwe in sorted(classes):
                findings, others = classes[cwe]
                if findings + others and findings / (findings + others) < class_band[0]:
                    pool = [r for r in kept if r['task'] == 'discovery' and not facts[r['view_id']][0] and cwe in facts[r['view_id']][1]]
                    if pool:
                        action = ('negative', drop_from_largest_kind(pool))
                        break
        if not action:
            break
        gone = action[1]
        dropped[action[0]] += 1
        kept = [r for r in kept if r['view_id'] != gone['view_id']]
        finding, scope, _ = facts[gone['view_id']]
        slot = 0 if finding else 1
        forms[gone['scope_form']][slot] -= 1
        for cwe in scope:
            classes[cwe][slot] -= 1
    return kept, {'form_band': list(form_band), 'class_band': list(class_band), 'dropped': dict(dropped),
                  'forms': {form: {'finding': forms[form][0], 'no_finding': forms[form][1]} for form in FORMS},
                  'classes': {cwe: {'finding': f, 'no_finding': n} for cwe, (f, n) in sorted(classes.items()) if f + n}}


RELEASE_RATIO = 5          # v37: a package whose closure would remove more than this many training units per transfer unit gives up its transfer units instead


def plan_closure(units, *, release_ratio=None):
    """The transfer closure: partitions are disjoint by package and by issue group, so every non-transfer unit that shares a package or an issue group with a transfer unit
    (CWE-918, CWE-94) has to go. With `release_ratio` a package whose closure would remove more than release_ratio units per transfer unit is RELEASED: its transfer
    units are given up (they are never trained on anyway) and its other units stay. Returns {'released_packages', 'dropped_transfer_ids', 'closed_ids'}."""
    transfer = [u for u in units if base.assign_partition(u) == 'transfer']
    others = [u for u in units if base.assign_partition(u) != 'transfer']
    transfer_count = collections.Counter(u.get('package') for u in transfer)
    other_count = collections.Counter(u.get('package') for u in others)
    released = sorted((p for p in transfer_count if release_ratio is not None and other_count[p] > release_ratio * transfer_count[p]), key=str)
    gone = set(released)
    dropped = {u['unit_id'] for u in transfer if u.get('package') in gone}
    staying = [u for u in transfer if u.get('package') not in gone]
    groups, packages = {u.get('issue_group_id') for u in staying}, {u.get('package') for u in staying}
    closed = {u['unit_id'] for u in others if u.get('issue_group_id') in groups or u.get('package') in packages}
    return {'released_packages': released, 'dropped_transfer_ids': dropped, 'closed_ids': closed}


def assemble(units, *, view_filter=None, weights=None, tiers=None, provenance=None, held_out_packages=None, tag=TAG, protocol='security_chat_v3', release_ratio=None, extra_scopes=()):
    """Like grounded_dataset.assemble; returns {'views', 'units', 'manifest'}. `protocol` selects the prompt protocol and its class catalogue. `extra_scopes`: classes another build already teaches (v40 adds rows to the v39 rows), so a set scope may name them too. `release_ratio` (None: off, as in
    v35 and v36) lets a package with many training units give up its few transfer units instead of losing the training units to the closure (see plan_closure)."""
    if protocol != v3.proto.PROTOCOL:
        with v3.protocol_scope(protocol):
            return assemble(units, view_filter=view_filter, weights=weights, tiers=tiers, provenance=provenance, held_out_packages=held_out_packages, tag=tag, protocol=protocol,
                            release_ratio=release_ratio, extra_scopes=extra_scopes)
    weights, tiers, held = weights or {}, tiers or {}, set(held_out_packages or ())
    deduped, duplicates = base.dedupe(units)
    weighted = [u for u in deduped if weights.get(u['unit_id'], 1.0) > 0]
    dropped_by_weight, deduped = len(deduped) - len(weighted), weighted
    release = None
    if release_ratio is not None:
        release = plan_closure(deduped, release_ratio=release_ratio)
        deduped = [u for u in deduped if u['unit_id'] not in release['dropped_transfer_ids']]
    roles = {u['unit_id']: ('development' if u.get('package') in held and base.assign_partition(u) != 'transfer' else base.assign_partition(u)) for u in deduped}
    transfer_groups = {u.get('issue_group_id') for u in deduped if roles[u['unit_id']] == 'transfer'}
    transfer_packages = {u.get('package') for u in deduped if roles[u['unit_id']] == 'transfer'}
    closure = [u for u in deduped if roles[u['unit_id']] != 'transfer' and (u.get('issue_group_id') in transfer_groups or u.get('package') in transfer_packages)]
    package_closed = [u for u in closure if u.get('package') in transfer_packages and u.get('issue_group_id') not in transfer_groups]
    closed = {u['unit_id'] for u in closure}
    pool = [u for u in deduped if u['unit_id'] not in closed]
    train = [u for u in pool if roles[u['unit_id']] == 'train']
    other = [u for u in pool if roles[u['unit_id']] != 'train']
    train, cap_report = base.apply_caps(train, **base.CAPS)

    # pass 1: which units give training views at all, and which classes then have enough positives to be named in a scope
    probe = {u['unit_id']: v3.build_unit_views(u, eligible_scopes=()) for u in train}
    productive = [u for u in train if any(v['pool'] == 'supported' for v in probe[u['unit_id']])]
    train, class_dropped = base.cap_class_share(productive)
    positives = collections.Counter(v3.label_of(u) for u in train)
    eligible = tuple(sorted({*(c for c, n in positives.items() if c and n >= MIN_SCOPE_POSITIVES and c not in v3.HELD_OUT), *extra_scopes}, key=lambda c: int(c.split('-')[1])))

    # pass 2: every kept unit with the scope pool
    kept = train + other
    rows, per_role_units, token_dropped, claim_dropped = [], {}, 0, 0
    claim_gate = _tag_version(tag) >= CLAIM_GATE_FROM            # v38 on: no answer that talks about the fix or names code the model is not shown (the v37 defect)
    for unit in kept:
        role = roles[unit['unit_id']]
        label = v3.label_of(unit)
        built = v3.build_unit_views(unit, eligible_scopes=eligible, name_class=(role != 'train' or label in eligible))
        if not any(v['pool'] == 'supported' for v in built):
            continue
        made = [_row(v, unit, role, weights.get(unit['unit_id'], 1.0) if role == 'train' else 1.0, tiers.get(unit['unit_id']), tag) for v in built]
        if claim_gate:
            from training.finetune import grounded_claim_gate
            sound = [row for row in made if grounded_claim_gate.gate(json.loads(row['response_json']), row['context_text'])[0]]
            claim_dropped += len(made) - len(sound)
            made = sound
            if not any(r['pool'] == 'supported' for r in made):
                continue
        if view_filter is not None:
            fitting = [row for row in made if view_filter(row)]
            token_dropped += len(made) - len(fitting)
            made = fitting
            if not any(r['pool'] == 'supported' for r in made):
                continue
        per_role_units.setdefault(role, []).append(unit)
        rows.extend(made)
    train_rows = [r for r in rows if r['partition_role'] == 'train']
    balanced, balance_report = balance(train_rows)
    keep_ids = {r['view_id'] for r in balanced}
    rows = [r for r in rows if r['partition_role'] != 'train' or r['view_id'] in keep_ids]
    live = {r['record_id'] for r in rows}
    per_role_units = {role: [u for u in group if u['unit_id'] in live] for role, group in per_role_units.items()}
    by_role = collections.Counter(r['partition_role'] for r in rows)
    kept_units = [u for group in per_role_units.values() for u in group]
    unit_origins = collections.Counter(u.get('origin', 'upstream_fix') for u in kept_units)
    origin_of = {u['unit_id']: u.get('origin', 'upstream_fix') for u in kept_units}
    manifest = {
        'schema': SCHEMA, 'tag': tag, 'dataset_status': 'frozen', 'publication_role': 'main', 'protocol': v3.proto.PROTOCOL,
        'origin': 'mined_grounded', 'label_authority': 'unreviewed_upstream_fix_evidence', 'human_review': False, 'independent_evaluation': False,
        'synthetic': any(origin not in REAL_ORIGINS for origin in unit_origins), 'generated_code_execution': 'never', 'policy': v3.POLICY,
        'seed': base.SEED, 'transfer_cwes': list(base.TRANSFER_CWES), 'caps': cap_report['caps'],
        'token_fit': 'checked_with_pinned_tokenizer' if view_filter is not None else 'not_checked',
        'catalogue': [list(item) for item in v3.proto.CATALOGUE], 'eligible_scopes': list(eligible), 'min_scope_positives': MIN_SCOPE_POSITIVES,
        'balance_policy': VERIFY_BAND, 'balance': balance_report, 'claim_gate': 'strict' if claim_gate else 'off',
        'counts': {'units_in': len(units), 'duplicates': duplicates, 'group_closure_dropped': len(closure), 'package_closure_dropped': len(package_closed),
                   'dropped_by_cap': cap_report['dropped_by_cap'], 'dropped_by_class_cap': class_dropped, 'dropped_by_weight': dropped_by_weight,
                   'dropped_by_token_fit': token_dropped, 'dropped_by_claim_gate': claim_dropped, 'dropped_by_balance': sum(balance_report['dropped'].values()),
                   'units_kept': sum(len(v) for v in per_role_units.values()), 'views': len(rows), 'views_by_role': dict(by_role),
                   'units_by_role': {k: len(v) for k, v in per_role_units.items()},
                   'packages_by_role': {k: len({u.get('package') for u in v}) for k, v in per_role_units.items()}},
        'unit_origin_counts': dict(unit_origins),
        'view_origin_counts': dict(collections.Counter(origin_of.get(r['record_id'], 'upstream_fix') for r in rows)),
        'unit_tier_counts': dict(collections.Counter(tiers.get(u['unit_id'], 'unreviewed') for u in kept_units)),
        'held_out_packages': sorted(held), 'provenance': dict(provenance or {}),
        'task_pool_counts': dict(collections.Counter(f"{r['task']}/{r['pool']}" for r in rows)), 'kind_counts': dict(collections.Counter(r['kind'] for r in rows)),
        'limitations': list(LIMITATIONS), 'not_covered': list(base.NOT_COVERED)}
    if release is not None:
        manifest['counts']['transfer_released'] = len(release['dropped_transfer_ids'])
        manifest['counts']['released_packages'] = len(release['released_packages'])
        manifest['release_ratio'] = release_ratio
    if any(str(u.get('origin', '')).startswith('recovered_') or u.get('admission') == 'licence_policy' for u in kept_units):
        manifest['licence_policy'] = grounded_recovery.LICENCE_POLICY
        manifest['licence_counts'] = dict(collections.Counter(u.get('licence_status') or 'unrecorded' for u in kept_units))
        manifest['tier_counts'] = dict(collections.Counter(u.get('tier') or 'none' for u in kept_units))
        manifest['admission_counts'] = dict(collections.Counter(u.get('admission') or 'attribution' for u in kept_units))
        manifest['llm_authored_units'] = sum(1 for u in kept_units if u.get('llm_attribution'))
        manifest['limitations'] = manifest['limitations'] + list(RECOVERY_LIMITATIONS)
    return {'views': rows, 'units': kept, 'manifest': manifest}


write = base.write
verify = base.verify
