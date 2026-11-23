"""Operational dataset coverage. Counts never turn model accuracy into a claim."""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict

from training.finetune.quality_gate import QUALIFICATION_MECHANISMS, qualification_summary


def _object(value):
    if isinstance(value, str):
        try: value = json.loads(value)
        except ValueError: return {}
    return value if isinstance(value, dict) else {}


def _active(view):
    weight = view.get('weight', 1)
    return (view.get('eligible', True) is True and type(weight) in (int, float)
            and math.isfinite(weight) and weight > 0)


def _role(view):
    split = view.get('split')
    return view.get('val_role') if split == 'val' else 'untouched_test' if split == 'test' else split


def _identities(value):
    if isinstance(value, str):
        try: value = json.loads(value)
        except ValueError: value = [value]
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, (list, tuple)) else []


def qualification(records, views, groups, *, languages=None, validator_development_ids=(), assignment_roles=None):
    """Validate both sides and require their real active views before counting.

    Existing components and historical roles are never reassigned. Any connected
    family exposed in conflicting roles contributes zero, rather than being moved.
    Optional assignment roles are normalized roles from the authoritative partition
    manifest. They classify viewless records but never replace required views.
    """
    from . import funcsplit, security_quality, source_fingerprints
    from .appended_evidence import _declared_fix
    from .negatives import _sid

    rows = {row['record_id']: row for row in records if isinstance(row.get('record_id'), str)}
    parent = {rid: rid for rid in rows}
    family_parent = dict(parent)
    def root(rid, parents=None):
        parent = parents if parents is not None else family_parent
        while parent[rid] != rid:
            parent[rid] = parent[parent[rid]]; rid = parent[rid]
        return rid
    anchors, family_anchors = {}, {}
    def anchor(rid, kind, value, *, family=False):
        if not isinstance(value, str) or not value.strip(): return
        key = (kind, value)
        targets = [(parent, anchors), (family_parent, family_anchors)] if family else [(parent, anchors)]
        for parents, known in targets:
            if key in known:
                a, b = root(rid, parents), root(known[key], parents); parents[max(a, b)] = min(a, b)
            else: known[key] = rid
    code_keys, before_keys = {}, defaultdict(set)
    from .validation_exposure import RECORD_IDS
    development = set(validator_development_ids or ()) | RECORD_IDS
    for rid, row in rows.items():
        # Existing issue IDs already include repository edges. They isolate
        # source groups, but cannot define individual vulnerability/fix families.
        for kind, value in [('partition', groups.get(rid)), ('issue', row.get('issue_group_id')),
                            ('repository', row.get('repository_identity'))]:
            anchor(rid, kind, value)
        for kind, field in (('fix_family', 'fix_family_id'), ('fix', 'fix_commit'), ('pair', 'raw_pair_key')):
            anchor(rid, kind, row.get(field), family=True)
        repository = row.get('repository_identity') or ''
        repository_url = repository if '://' in repository else 'https://' + repository
        anchor(rid, 'declared_fix', _declared_fix(repository_url.rstrip('/') + '/commit/' + (row.get('fix_commit') or '')), family=True)
        for url in _identities(row.get('source_urls')):
            anchor(rid, 'declared_fix', _declared_fix(url), family=True)
        for field in ('raw_record_keys', 'advisory_aliases'):
            for value in _identities(row.get(field)):
                anchor(rid, field, value.upper() if field == 'advisory_aliases' else value, family=True)
        for field in ('parent_record_id', 'derived_from_record_id'):
            ancestor = row.get(field)
            if ancestor in rows:
                anchor(rid, 'ancestor', ancestor, family=True); anchor(ancestor, 'ancestor', ancestor, family=True)
        for field in ('vulnerable_code', 'fixed_code'):
            code = row.get(field)
            if isinstance(code, str) and code.strip():
                tokens = funcsplit.tokens(row.get('language'), code)
                if not source_fingerprints.meaningful(tokens):
                    continue
                key = hashlib.sha256('\x1f'.join(tokens).encode()).hexdigest()
                code_keys[(rid, field)] = key
                anchor(rid, 'code', key, family=True)
                if field == 'vulnerable_code': before_keys[key].add(rid)
        receipt = _object(row.get('admission_provenance'))
        if any(obj.get('validator_development') is True or obj.get('validator_development_case') is True
               or obj.get('evaluation_exposure') == 'validator_development' for obj in (row, receipt)):
            development.add(rid)
    components = {rid: root(rid, parent) for rid in rows}
    family_ids = {rid: root(rid) for rid in rows}
    development_groups = {components[rid] for rid in development if rid in components}
    by_record, roles, historical_splits = defaultdict(list), defaultdict(set), defaultdict(set)
    invalid_roles = set()
    assignment_roles = assignment_roles if assignment_roles is not None else {}
    def valid_role(split, role):
        if split == 'val':
            return role in ('selection', 'calibration')
        return role in ('train', 'untouched_test') and role == ('untouched_test' if split == 'test' else split)
    for rid, row in rows.items():
        split = row.get('split')
        group = components[rid]
        if rid in assignment_roles:
            role = assignment_roles[rid]
            if not valid_role(split, role):
                invalid_roles.add(group)
            else:
                roles[group].add(role)
        if split:
            historical_splits[group].add(split)
            if split != 'val':
                roles[group].add('untouched_test' if split == 'test' else split)
            elif row.get('val_role'):
                if valid_role(split, row['val_role']):
                    roles[group].add(row['val_role'])
                else:
                    invalid_roles.add(group)
        if row.get('val_role') is not None and not valid_role(split, row['val_role']):
            invalid_roles.add(group)
    for view in views:
        rid = view.get('record_id')
        if rid in rows and _active(view):
            by_record[rid].append(view)
            role = _role(view)
            if valid_role(view.get('split'), role):
                roles[components[rid]].add(role)
            else:
                invalid_roles.add(components[rid])
            if (
                    view.get('val_role') is not None and not valid_role(view.get('split'), view['val_role'])):
                invalid_roles.add(components[rid])
    families, excluded, chosen = [], Counter(), set()
    for rid, row in sorted(rows.items()):
        group = components[rid]
        family_id = family_ids[rid]
        if family_id in chosen: continue
        if (group in invalid_roles or len(historical_splits[group]) > 1 or len(roles[group]) != 1
                or next(iter(roles[group]), None) not in ('train', 'selection', 'calibration', 'untouched_test')):
            excluded['conflicting_or_missing_partition'] += 1; continue
        role = next(iter(roles[group]))
        if role == 'untouched_test' and group in development_groups:
            excluded['validator_development_exposure'] += 1; continue
        receipt = _object(row.get('admission_provenance'))
        if (row.get('admission_outcome') != 'admitted' or row.get('label_state') != 'vulnerability_supported'
                or receipt.get('repair_valid') is not True or receipt.get('mechanism') not in QUALIFICATION_MECHANISMS
                or str(row.get('ecosystem') or '').lower() != 'npm'
                or str(row.get('language') or '').lower() not in ('javascript', 'typescript', 'js', 'ts', 'jsx', 'tsx')
                or not (groups.get(rid) or row.get('issue_group_id'))
                or security_quality.admission_problems(row, languages=languages, admission_policy=row.get('admission_policy') or 'automatic-evidence/2')
                or security_quality.admission_problems(row, fixed_twin=True, languages=languages, admission_policy=row.get('admission_policy') or 'automatic-evidence/2')):
            excluded['unvalidated_or_unsupported_pair'] += 1; continue
        before, after = code_keys.get((rid, 'vulnerable_code')), code_keys.get((rid, 'fixed_code'))
        if not before or not after or before == after or before_keys[after] - {rid}:
            excluded['identical_or_chained_fixed_source'] += 1; continue
        available = {(v.get('task'), v.get('pool'), v.get('subject_id')) for v in by_record[rid]
                     if (not row.get('split') or v.get('split') == row['split'])}
        needed = {('discovery', 'supported', _sid(rid, 'vulnerable')),
                  ('discovery', 'scoped_negative', _sid(rid, 'fixed_twin')),
                  ('repair', 'supported', _sid(rid, 'vulnerable'))}
        if not needed <= available:
            excluded['missing_usable_vulnerable_fixed_or_repair_view'] += 1; continue
        chosen.add(family_id)
        families.append(dict(family_id=family_id, source_group=group, record_id=rid, mechanism=receipt['mechanism'], partition=role))
    return {**qualification_summary(families), 'excluded_records': dict(excluded)}
