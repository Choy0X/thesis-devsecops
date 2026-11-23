"""Evidence-bound decisions and separate model/tool/system measurements.

An empty scanner result is never a safeguard. The validator is supplied by the
trusted verification boundary, not by an imported model response.
"""
from __future__ import annotations

POS = 'vulnerability_supported'
NEG = 'no_supported_finding_in_scope'
UNKNOWN = 'insufficient_context'


def decide(model, evidence, *, validate=lambda item: False):
    valid = [item for item in evidence if isinstance(item, dict) and item.get('scope')
             and item.get('state') in ('supported', 'scoped_safeguard') and validate(item)]
    conclusions = {POS if item['state'] == 'supported' else NEG for item in valid}
    prediction = model.get('prediction')
    reason = 'evidence_not_verified'
    if model.get('status') != 'answered' or prediction not in (POS, NEG):
        reason = 'model_uncertain'
    elif conclusions and (len(conclusions) != 1 or prediction not in conclusions):
        reason = 'conflicting_evidence'
    elif conclusions:
        return {'status': 'answered', 'prediction': prediction, 'reason': 'corroborated_with_verified_evidence',
                'scope': sorted({str(item['scope']) for item in valid}), 'evidence': valid,
                'review_required': False, 'repository_safe': False}
    return {'status': 'review_required', 'prediction': UNKNOWN, 'reason': reason, 'evidence': valid,
            'scope': sorted({str(item['scope']) for item in valid}), 'review_required': True, 'repository_safe': False}


def benchmark(cases, model_results, tool_results, *, validate=lambda item: False):
    from training.finetune.security_validation import compare
    channels = {'model': [], 'tools': [], 'combined': []}
    traces = []
    for case in cases:
        if case.get('review_status') != 'human_reviewed':
            raise ValueError('Hybrid evaluation requires independently reviewed cases and declared scopes.')
        key = case['case_id']
        model = model_results.get(key, {'status': 'missing'})
        evidence = tool_results.get(key, [])
        tool = decide({'status': 'answered', 'prediction': POS}, evidence, validate=validate)
        if tool['status'] != 'answered':
            tool = decide({'status': 'answered', 'prediction': NEG}, evidence, validate=validate)
        combined = decide(model, evidence, validate=validate)
        identity = {name: case.get(name) for name in ('truth', 'issue_group_id', 'repository_identity', 'pair_id')}
        for name, result in (('model', model), ('tools', tool), ('combined', combined)):
            channels[name].append({**identity, 'status': result['status'], 'prediction': result.get('prediction')})
        traces.append({'case_id': key, 'scope': case.get('scope'), 'model': model, 'tools': evidence, 'combined': combined})
    return {'schema': 'hybrid-security-validation/1', 'channels': compare(channels), 'traces': traces,
            'deployment_ready': False, 'note': 'Review referrals count as misses for automatic positive detection.'}
