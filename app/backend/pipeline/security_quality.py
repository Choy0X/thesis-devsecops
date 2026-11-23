"""Admission and readiness of security supervision, independent of model confidence.

G0 provenance and model-written drafts are candidates, never independent approval.
Historical candidate builds remain readable; a new strict build fails closed.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter

SCHEMA = 'security-readiness/1'
TARGETS = {'recall': .90, 'false_positive_rate': .01}


def unpack(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def evidence_counts(records, views):
    """Record outcomes and usable protected subjects have different denominators."""
    records, views = list(records), list(views)
    by_id = {row.get('record_id'): row for row in records}
    negatives = [view for view in views if view.get('pool') == 'scoped_negative'
                 and view.get('eligible', True) and view.get('weight', 1) > 0]
    subjects = {view.get('subject_id') or view.get('record_id') for view in negatives}
    fixed = {view.get('subject_id') or view.get('record_id') for view in negatives
             if by_id.get(view.get('record_id'), {}).get('label_state') == 'vulnerability_supported'}
    return {'supported': sum(row.get('admission_outcome') == 'admitted' and row.get('label_state') == 'vulnerability_supported' for row in records),
            'protected': len(subjects), 'protected_examples': len(subjects), 'fixed_twin_protected': len(fixed),
            'protected_records': sum(row.get('admission_outcome') == 'admitted' and row.get('label_state') == 'no_supported_finding_in_scope' for row in records),
            'protected_views': len(negatives)}


def admission_problems(row, *, fixed_twin=False, languages=None, admission_policy=None, check_receipt=True):
    problems = []
    allowed = set(languages) if languages is not None else {'javascript', 'typescript', 'js', 'ts', 'jsx', 'tsx'}
    if row.get('language') and str(row['language']).lower() not in allowed:
        problems.append('outside_initial_js_ts_validation_scope' if languages is None else 'outside_profile_language_scope')
    for side, ref in [('before', row.get('relevant_parent_commit')), ('after', row.get('fix_commit'))]:
        context = unpack(row.get('context_' + side), {})
        snap = context.get('snapshot') or {}
        if snap and (snap.get('revision') != ref or snap.get('side') != side):
            problems.append('mixed_snapshot:' + side)
        if 'target_source_mismatch' in context.get('omissions', []):
            problems.append('target_source_mismatch:' + side)
    # Only the receipt validator may set this transient field; it is never accepted
    # from an imported explanation or copied from a receipt-id string.
    policy = admission_policy or row.get('admission_policy')
    automatic = policy in ('automatic-evidence/1', 'automatic-evidence/2', 'automatic-evidence/3')
    if automatic:
        if policy == 'automatic-evidence/3':
            from app.backend.pipeline import automatic_evidence_v3 as automatic_admission
        elif policy == 'automatic-evidence/2':
            from app.backend.pipeline import automatic_evidence as automatic_admission
        else:
            from app.backend.pipeline import automatic_admission
        if check_receipt:
            problems.extend(automatic_admission.receipt_problems(row))
    else:
        independently_validated = row.get('review_status') == 'human_reviewed' or row.get('_validated_receipt') is True
        if not independently_validated:
            problems.append('security_label_not_independently_validated')
    if fixed_twin:
        decision = unpack(row.get('fixed_review'), {})
        if not (decision.get('scope') and decision.get('safeguard') and
                (automatic or decision.get('review_status') == 'human_reviewed')):
            problems.append('fixed_twin_requires_scoped_negative_evidence' if automatic else 'fixed_twin_requires_scoped_negative_review')
        quotes = [x.get('quote') for x in decision.get('evidence', []) if isinstance(x, dict)]
        if not quotes or any(not isinstance(q, str) or not q.strip() or q not in (row.get('fixed_code') or '') for q in quotes):
            problems.append('fixed_evidence_not_in_source')
        return problems
    state = row.get('label_state')
    if state == 'vulnerability_supported':
        if not row.get('violated_invariant') or not row.get('attacker_control') or not unpack(row.get('preconditions'), []):
            problems.append('missing_security_mechanism')
        evidence = unpack(row.get('evidence_spans'), [])
        quotes = [x.get('quote') for x in evidence if isinstance(x, dict)]
        if not quotes:
            problems.append('missing_code_grounded_evidence')
        elif any(not isinstance(q, str) or not q.strip() or q not in (row.get('vulnerable_code') or '') for q in quotes):
            problems.append('evidence_not_in_source')
    elif state == 'no_supported_finding_in_scope':
        if not row.get('condition_scope') or not row.get('decisive_safeguard'):
            problems.append('missing_scoped_negative_evidence')
        quotes = [span.get('quote') for span in unpack(row.get('evidence_spans'), []) if isinstance(span, dict)]
        if not quotes or any(not isinstance(quote, str) or not quote.strip() or quote not in (row.get('vulnerable_code') or '') for quote in quotes):
            problems.append('negative_evidence_not_in_source')
    elif state == 'insufficient_context':
        if not unpack(row.get('missing_facts'), []):
            problems.append('missing_explicit_context_omissions')
    else:
        problems.append('unknown_security_label')
    return problems


def readiness(records, views, *, languages=None, admission_policy=None, partition_groups=None,
              assignment_roles=None, validator_development_ids=()):
    records, views = list(records), list(views)
    policy = admission_policy or next((r.get('admission_policy') for r in records if r.get('admission_policy') in ('automatic-evidence/1', 'automatic-evidence/2', 'automatic-evidence/3')), None)
    automatic = policy in ('automatic-evidence/1', 'automatic-evidence/2', 'automatic-evidence/3')
    strict_policy = policy if policy in ('automatic-evidence/2', 'automatic-evidence/3') else None
    by_id = {r.get('record_id'): r for r in records}
    train = [v for v in views if v.get('split') == 'train' and v.get('eligible', True) and v.get('weight', 1) > 0]
    blockers = []
    if not train:
        blockers.append('missing_supervised_training_views')
    invalid = Counter()
    for v in train:
        row = by_id.get(v.get('record_id'), {})
        for problem in admission_problems(row, fixed_twin=v.get('pool') == 'scoped_negative' and row.get('label_state') == 'vulnerability_supported', languages=languages, admission_policy=strict_policy):
            invalid[problem] += 1
    blockers.extend(sorted(invalid))
    pools = Counter(v.get('pool') for v in train if v.get('task') == 'discovery')
    for pool in ['supported', 'scoped_negative', 'insufficient_context']:
        if not pools[pool]:
            blockers.append('missing_discovery_pool:' + pool)
    roles = Counter(v.get('val_role') for v in views if v.get('split') == 'val' and v.get('eligible',True) and v.get('weight',1)>0)
    if not roles['selection'] or not roles['calibration']:
        blockers.append('missing_selection_or_calibration_partition')
    if automatic:
        selection_pools = Counter(v.get('pool') for v in views if v.get('split') == 'val' and v.get('val_role') == 'selection'
                                  and v.get('task') == 'discovery' and v.get('eligible', True) and v.get('weight', 1) > 0)
        blockers.extend('missing_selection_pool:' + pool for pool in ('supported', 'scoped_negative') if not selection_pools[pool])
        if policy in ('automatic-evidence/2', 'automatic-evidence/3'):
            calibration_pools=Counter(v.get('pool') for v in views if v.get('split')=='val' and v.get('val_role')=='calibration'
                                      and v.get('task')=='discovery' and v.get('eligible',True) and v.get('weight',1)>0)
            blockers.extend('missing_calibration_pool:'+pool for pool in ('supported','scoped_negative') if not calibration_pools[pool])
    test = [v for v in views if v.get('split') == 'test']
    if not test:
        blockers.append('missing_untouched_test_partition')
    for task in ('discovery', 'condition_assessment', 'repair'):
        if not any(v.get('task') == task for v in train):
            blockers.append('missing_core_task:' + task)
    families = {name: {'reviewed_positive': 0, 'reviewed_negative': 0, 'insufficient_context': 0, 'candidates': 0}
                for name in ('injection', 'xss', 'path_traversal', 'prototype_pollution', 'redos', 'ssrf', 'access_control')}
    mapping = {79: 'xss', 80: 'xss', 89: 'injection', 78: 'injection', 94: 'injection', 22: 'path_traversal',
               23: 'path_traversal', 1321: 'prototype_pollution', 1333: 'redos', 400: 'redos', 918: 'ssrf',
               284: 'access_control', 285: 'access_control', 287: 'access_control', 862: 'access_control', 863: 'access_control', 639: 'access_control'}
    for row in records:
        names = {mapping[int(code)] for code in re.findall(r'(?:CWE-)?\b(\d{1,5})\b', str(row.get('cwe_ids') or '')) if int(code) in mapping}
        for name in names:
            families[name]['candidates'] += 1
            if not admission_problems(row, languages=languages, admission_policy=strict_policy):
                field = {'vulnerability_supported': 'reviewed_positive', 'no_supported_finding_in_scope': 'reviewed_negative', 'insufficient_context': 'insufficient_context'}.get(row.get('label_state'))
                if field: families[name][field] += 1
            if not admission_problems(row, fixed_twin=True, languages=languages, admission_policy=strict_policy): families[name]['reviewed_negative'] += 1
    result = {'schema': ('security-readiness/4' if policy in ('automatic-evidence/2', 'automatic-evidence/3') else 'security-readiness/2') if automatic else SCHEMA, 'training_ready': not blockers, 'pilot_ready': False,
            'blockers': blockers, 'supervision_failures': dict(invalid), 'training_views': len(train),
            'discovery_pools': dict(pools), 'held_out_test_views': len(test), 'targets': TARGETS,
            'family_readiness': families, 'validated_scope': sorted(languages) if languages is not None else ['javascript', 'typescript'],
            'pilot_blockers': ['requires_independent_held_out_evaluation_and_uncertainty_analysis']
                              + ([] if test else ['missing_held_out_test_partition']),
            'scope': 'No supported finding is scoped evidence, never a clean-code certification.'}
    if automatic:
        outcomes = Counter(r.get('admission_outcome') for r in records)
        result.update(admission_policy=policy,
            admission_counts={'automatically_eligible': outcomes['admitted'] + outcomes['insufficient_context'],
                              'supported': sum(r.get('admission_outcome') == 'admitted' and r.get('label_state') == 'vulnerability_supported' for r in records),
                              'insufficient_context': outcomes['insufficient_context'], 'excluded': outcomes['excluded']},
            supervision_provenance='automatically admitted evidence; independent security performance remains unmeasured')
        for family in result['family_readiness'].values():
            family['admitted_positive'] = family.pop('reviewed_positive')
            family['admitted_negative'] = family.pop('reviewed_negative')
        if policy in ('automatic-evidence/2', 'automatic-evidence/3'):
            result['admission_counts'].update(evidence_counts(records, views))
            proof_counts, mechanism_coverage, mechanism_records, invalid_proofs = Counter(), {}, {}, Counter()
            for row in records:
                if row.get('admission_outcome') != 'admitted':
                    continue
                problems = admission_problems(row, languages=languages, admission_policy=policy)
                if problems:
                    invalid_proofs.update(problems)
                    continue
                receipt = unpack(row.get('admission_provenance'), {})
                method = receipt.get('proof_method')
                if method:
                    proof_counts[method] += 1
                mechanism = receipt.get('mechanism')
                if mechanism:
                    mechanism_records.setdefault(mechanism, []).append(row)
                    coverage = mechanism_coverage.setdefault(mechanism, Counter())
                    coverage[row.get('admission_outcome') or 'excluded'] += 1
                    if row.get('admission_outcome') == 'admitted':
                        coverage['protected' if row.get('label_state') == 'no_supported_finding_in_scope' else 'supported'] += 1
            for mechanism, rows in mechanism_records.items():
                ids = {row.get('record_id') for row in rows}
                for field, value in evidence_counts(rows, [view for view in views if view.get('record_id') in ids]).items():
                    mechanism_coverage[mechanism][field] = value
            result.update(proof_counts=dict(proof_counts), mechanism_coverage={key: dict(value) for key, value in mechanism_coverage.items()},
                          invalid_proof_claims=dict(invalid_proofs))
            coverage,scale=_partition_coverage(records,views,partition_groups or {},languages=languages)
            result.update(partition_coverage=coverage,scale_assessment=scale)
            from .data_qualification import qualification
            qualified = qualification(records, views, partition_groups or {}, languages=languages,
                                      validator_development_ids=validator_development_ids, assignment_roles=assignment_roles)
            result.update(qualification_policy=qualified['policy'], data_qualification=qualified,
                          security_performance_design=scale)
            result['blockers'] = list(dict.fromkeys([*result['blockers'], *qualified['blockers']]))
            result['training_ready'] = not result['blockers']
    return result


def _partition_coverage(records,views,groups,*,languages=None):
    """Count independent source components, never multiple views as new cases.

    The zero-error calculation is a sampling-design floor, not a confidence
    interval for a model that has not been evaluated. Calibration is used for
    threshold choice; only an independent held-out evaluation can test claims.
    """
    by_id={r.get('record_id'):r for r in records}
    valid={rid:not admission_problems(row,languages=languages,admission_policy=row.get('admission_policy') or 'automatic-evidence/2')
           for rid,row in by_id.items() if row.get('admission_outcome') in ('admitted','insufficient_context')}
    names=('train','selection','calibration','untouched_test')
    examples={name:{} for name in names};components={name:{} for name in names};mechanisms={name:{} for name in names}
    for view in views:
        rid=view.get('record_id');row=by_id.get(rid,{})
        if not valid.get(rid) or view.get('task')!='discovery' or not view.get('eligible',True) or view.get('weight',1)<=0:
            continue
        name=view.get('val_role') if view.get('split')=='val' else 'untouched_test' if view.get('split')=='test' else view.get('split')
        if name not in examples:continue
        pool=view.get('pool')
        if pool not in ('supported','scoped_negative','insufficient_context'):continue
        # Fixed protection has its own evidence gate. A vulnerable-side receipt
        # alone cannot fill a negative calibration or evaluation cell.
        if pool=='scoped_negative' and row.get('label_state')=='vulnerability_supported' and admission_problems(row,fixed_twin=True,languages=languages,admission_policy=row.get('admission_policy') or 'automatic-evidence/2'):
            continue
        group=groups.get(rid) or row.get('fix_family_id') or row.get('issue_group_id') or rid
        examples[name].setdefault(pool,set()).add(view.get('subject_id') or rid)
        components[name].setdefault(pool,set()).add(group)
        mechanism=unpack(row.get('admission_provenance'),{}).get('mechanism')
        if mechanism:mechanisms[name].setdefault(mechanism,set()).add(group)
    coverage={name:{'examples':{pool:len(ids) for pool,ids in examples[name].items()},
                    'source_groups':{pool:len(ids) for pool,ids in components[name].items()},
                    'mechanism_groups':{mechanism:len(ids) for mechanism,ids in mechanisms[name].items()}}
              for name in names}
    alpha=.05
    needed={'supported_groups':math.ceil(math.log(alpha)/math.log(TARGETS['recall'])),
            'protected_groups':math.ceil(math.log(alpha)/math.log(1-TARGETS['false_positive_rate']))}
    limitations=[]
    for name in ('calibration','untouched_test'):
        for pool,key in [('supported','supported_groups'),('scoped_negative','protected_groups')]:
            actual=len(components[name].get(pool,set()))
            if actual<needed[key]:limitations.append({'partition':name,'pool':pool,'source_groups':actual,
                                                       'zero_error_design_groups':needed[key]})
    scale={'schema':'security-corpus-scale/1','sampling_unit':'leakage_component' if groups else 'source_family',
           'full_run_suitability':'pilot_only' if limitations else 'requires_learning_curve_and_held_out_measurement',
           'security_targets_measured':False,'targets':dict(TARGETS),'zero_error_design':{'confidence':.95,**needed},
           'limitations':limitations,
           'scope':'Sampling-design limits only. Group counts do not establish independence, training sufficiency, model accuracy or a deployment safety claim.'}
    return coverage,scale
