"""Policy 3: complete source/binding proofs and explicit stale-gate revalidation."""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib

from . import automatic_evidence as base, evidence_binding_proof, evidence_proof_context, evidence_recursive_filter

POLICY = 'automatic-evidence/3'
_QUALITY = ('data_quality_status', 'production_relevance', 'quarantine_reasons', 'exclusion_reasons')


def repairable_tangled(record):
    from .npm_case_collector import _production
    return (record.get('production_relevance') == 'production'
        and record.get('data_quality_status') == 'excluded'
        and set(record.get('exclusion_reasons') or []) == {'tangled'}
        and not record.get('quarantine_reasons')
        and all(_production(record.get('file_path_' + side) or '') for side in ('before', 'after')))


def registry():
    from . import evidence_module_witness
    modules = (evidence_binding_proof, evidence_proof_context, evidence_module_witness, evidence_recursive_filter)
    return {**base._registry(), 'policy':POLICY, 'implementation':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
            **{module.__name__.rsplit('.', 1)[-1]:hashlib.sha256(pathlib.Path(module.__file__).read_bytes()).hexdigest() for module in modules}}


def source_fingerprint(row):
    return base.legacy._digest({'source':base.source_fingerprint(row),
                              **{key:row.get(key) for key in (*_QUALITY, 'split', 'issue_group_id')}})


def _missing_from_exact_context(row, proof):
    """Missing-source labels require actual absence in the authenticated scope.

    Legacy diagnostics inspect selected prompt snippets. A declaration omitted
    from that prompt can already exist in the complete proof module. Its
    unsupported semantics are not a missing source fact. This check grants no
    helper semantics, admission or extra student context.
    """
    from .verify.closure_diagnostics import binding_origins
    gaps = base._decisive_missing(row)
    if not gaps:
        return []
    for side in ('before', 'after'):
        side_gaps = [gap.split(':') for gap in gaps if gap.startswith(side + ':')]
        if not side_gaps:
            continue
        if any(len(parts) < 3 or parts[1] not in ('definition', 'binding', 'environment') for parts in side_gaps):
            return []
        bindings = [binding for binding in proof['bindings'] if binding['side'] == side]
        if len(bindings) != 1:
            return []
        binding = bindings[0]
        context = base._json(proof['record'].get('context_' + side), {})
        modules = [item for item in context.get('items', [])
                   if item.get('kind') == 'module_scope' and item.get('path') == binding['path']]
        if len(modules) != 1:
            return []
        names = {parts[2] for parts in side_gaps}
        origins = binding_origins(modules[0]['text'], binding['byte_span'], row.get('language'), names)
        # Ambiguous/dynamic analysis, present-but-unmodeled declarations and
        # transitive helper dependencies outside this exact target are all
        # unsupported. Finding one cannot justify a mixed missing-facts label.
        if (origins is None or set(origins) != names
                or any(fact['origin'] != 'global' for fact in origins.values())):
            return []
    return gaps


def prove(row, *, provider=None, diagnostics=None):
    provider = provider or evidence_proof_context._provider
    proof = evidence_proof_context.prepare(row, provider=provider)
    if proof['state'] != 'available':
        return None, None, [], [proof['cause']]
    facts = base._source_rule(proof['record'])
    executed = None
    if not facts:
        specialized = evidence_recursive_filter.analyze(row, provider=provider)
        if diagnostics is not None:
            diagnostics['recursive_key_domains'] = {key: specialized.get(key) for key in ('schema', 'state', 'reason', 'applies')}
        if specialized.get('state') == 'proved':
            witnesses = [binding for side in ('before', 'after') for binding in specialized['module_bindings'][side]]
            return specialized['facts'], None, witnesses, []
        if specialized.get('applies'):
            return None, None, proof['bindings'], ['recursive_key_domains:' + specialized.get('reason', 'unsupported')]
        facts, executed = base._execution(row)
    if facts:
        witnesses = []
        for side in ('before', 'after'):
            bound = evidence_binding_proof.verify(proof['record'], side, proof['helpers'][side], provider=provider, validated_rule=True)
            if not bound['valid']:
                detail = ':' + bound['binding_name'] if bound.get('binding_name') else ''
                return None, None, [], [side + ':' + bound['cause'] + detail]
            witnesses.extend(bound['module_bindings'])
        return facts, executed, witnesses, []
    if diagnostics is not None:
        diagnostics['missing_facts'] = _missing_from_exact_context(row, proof)
    return None, None, proof['bindings'], ['unsupported_security_semantics']


def admit_record(record, *, gate=None, root=None, provider=None):
    from . import security_quality
    from .explanation_quality import context_suggestions
    from .schemas import reviewed
    gate = gate.as_dict() if hasattr(gate, 'as_dict') else (gate or {})
    out = copy.deepcopy(record)
    quality = {key:copy.deepcopy(record.get(key)) for key in _QUALITY}
    diagnostics = {}
    with base.source_scope(root if root is not None else base._SOURCE_ROOT.get()):
        problems, foundation = base._source_state(out, gate, root=root)
        potential = {'attributed', 'link_evidence', 'parse'}
        if foundation:
            potential.update(('unverified_fix_source', 'parent_recorded'))
        if repairable_tangled(record):
            potential.update(('source_excluded', 'production_code'))
        if set(problems) - potential:
            facts, executed, modules, proof_problems = None, None, [], []
        else:
            facts, executed, modules, proof_problems = prove(out, provider=provider, diagnostics=diagnostics)
    revalidated = set()
    if facts and facts.get('repair_valid') is True:
        revalidated.update(set(problems) & {'attributed', 'link_evidence'})
        if foundation:
            revalidated.update(set(problems) & {'unverified_fix_source', 'parent_recorded'})
        if modules:
            revalidated.update(set(problems) & {'parse'})
        # Tangled describes a commit, not a causal unit. Only a full unit proof
        # can supersede it; tests, ambiguous units and graph conflicts remain out.
        if modules and repairable_tangled(record):
            revalidated.update(set(problems) & {'source_excluded', 'production_code'})
            out.update(data_quality_status='ok', exclusion_reasons=[])
    problems = sorted(set(problems) - revalidated)
    for key in base._FIELDS:
        out[key] = [] if key in ('preconditions', 'missing_facts') else None
    for field in reviewed.FIELDS:
        if field.group == 'verification' and field.name != 'provenance_status':
            out[field.name] = [] if field.type == 'list' else None
    out.update(admission_policy=POLICY, admission_outcome='excluded', label_state='unreviewed', review_status='unreviewed')
    basis, missing = 'none', []
    if not problems and facts:
        out.update(base._target(facts), review_status='auto_checked', admission_outcome='admitted')
        issues = security_quality.admission_problems(out, admission_policy=POLICY, check_receipt=False)
        if issues:
            problems.extend(issues)
            out.update(label_state='unreviewed', review_status='unreviewed', admission_outcome='excluded')
        else:
            basis = 'trusted_execution' if executed else 'source_rule'
            if executed:
                out['verification_receipt_ids'] = [executed['receipt_id']]
                binding = executed.get('binding') or {}
                out['environment_digest'] = binding.get('image_id')
                out['dependency_lock_hash'] = binding.get('dependency_sha256')
                out['test_artifact_hashes'] = [binding['harness_sha256']] if binding.get('harness_sha256') else []
    elif not problems:
        # Unsupported parser/binding semantics are never missing-source labels.
        if not proof_problems or proof_problems == ['unsupported_security_semantics']:
            missing = diagnostics.get('missing_facts', [])
        if missing:
            out.update(base._missing_target(missing), review_status='auto_checked', admission_outcome='insufficient_context')
            basis = 'decisive_missing_facts'
        else:
            problems.extend(proof_problems or ['unsupported_security_semantics'])
    if problems and proof_problems:
        problems.extend(proof_problems)
    out['admission_reasons'] = sorted(set(problems)) or missing
    out['context_suggestions'] = json.dumps(context_suggestions(out, decisive_missing=missing), sort_keys=True)
    receipt = {'policy':POLICY, 'outcome':out['admission_outcome'], 'basis':basis,
        'source_sha256':source_fingerprint(out), 'target_sha256':base.target_fingerprint(out),
        'rule_registry':registry(), 'input_gate_failed':sorted(set(gate.get('failed') or [])),
        'input_quality':quality, 'revalidated_checks':sorted(revalidated),
        'source_foundation':foundation, 'module_bindings':modules,
        **base._proof_metadata(facts, executed)}
    out['admission_provenance'] = json.dumps(receipt, sort_keys=True)
    return out


def receipt_problems(row, *, root=None, provider=None):
    receipt = base._json(row.get('admission_provenance'), {})
    if not isinstance(receipt, dict) or receipt.get('policy') != POLICY:
        return ['missing_automatic_admission_receipt']
    if (receipt.get('source_sha256') != source_fingerprint(row)
            or receipt.get('target_sha256') != base.target_fingerprint(row)):
        return ['automatic_source_or_target_fingerprint_mismatch']
    quality = receipt.get('input_quality')
    failures = receipt.get('input_gate_failed')
    if not isinstance(quality, dict) or set(quality) != set(_QUALITY) or not isinstance(failures, list):
        return ['automatic_revalidation_input_mismatch']
    original = {**row, **quality}
    expected = admit_record(original, gate={'failed':failures}, root=root, provider=provider)
    if (row.get('review_status') != expected['review_status'] or row.get('admission_outcome') != expected['admission_outcome']
            or row.get('admission_outcome') not in ('admitted', 'insufficient_context')
            or base.target_fingerprint(row) != base.target_fingerprint(expected)
            or receipt != json.loads(expected['admission_provenance'])):
        return ['automatic_proof_revalidation_mismatch']
    return []
