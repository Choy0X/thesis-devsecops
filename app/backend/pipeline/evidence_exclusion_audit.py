"""Metadata-only diagnostics, never a source authenticator or admission oracle."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import re

SCHEMA = 'evidence-exclusion-audit/2'
METADATA_FIELDS = ('record_id', 'raw_pair_key', 'repository_identity', 'fix_commit', 'fix_family_id',
    'issue_group_id', 'admission_outcome', 'admission_reasons', 'admission_provenance', 'label_state',
    'production_relevance', 'data_quality_status', 'exclusion_reasons', 'quarantine_reasons',
    'provenance_status', 'collector_version', 'granularity', 'split', 'cwe_ids')


def _object(value, default):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return default
    return value if isinstance(value, type(default)) else default


def _reasons(value):
    return {item for item in _object(value, []) if isinstance(item, str) and item}


def _candidate(record):
    repo, revision = record.get('repository_identity'), record.get('fix_commit')
    if not isinstance(repo, str) or not repo.strip() or not re.fullmatch(r'[0-9a-fA-F]{40,64}', str(revision or '')):
        return None
    raw = json.dumps([repo.strip().lower().removeprefix('https://').removeprefix('http://').rstrip('/'),
                      revision.lower()], separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()


def diagnose(record):
    reasons = _reasons(record.get('admission_reasons'))
    proof = _object(record.get('admission_provenance'), {})
    recorded_source = isinstance(proof.get('source_foundation'), dict) and proof['source_foundation'].get('status') == 'verified'
    excluded = _reasons(record.get('exclusion_reasons'))
    quarantine = _reasons(record.get('quarantine_reasons'))
    outcome = record.get('admission_outcome') or ('insufficient_context' if record.get('label_state') == 'insufficient_context' else 'excluded')
    stage, bucket, cause, action, repairability = 'admission', outcome, outcome, 'none', 'not_applicable'
    if outcome == 'excluded':
        repairability = 'conditional'
        if 'production_code' in reasons:
            stage, bucket = 'production_relevance', 'nonproduction'
            if record.get('production_relevance') == 'test' or 'test' in excluded:
                cause, action, repairability = 'nonproduction_test', 'retain_as_supporting_evidence', 'not_training_source'
            elif 'tangled' in excluded:
                cause, action = 'commit_scope_requires_unit_proof', 'revalidate_unit_causality'
            elif record.get('production_relevance') == 'docs':
                cause, action, repairability = 'nonproduction_documentation', 'retain_as_supporting_evidence', 'not_training_source'
            else:
                cause, action = 'production_gate_requires_revalidation', 'revalidate_production_source'
        elif reasons & {'quarantined', 'source_excluded'}:
            stage, bucket = 'source_quality', 'quarantine_or_source_exclusion'
            if 'append_source_cross_partition_conflict' in quarantine or 'graph_quarantine' in quarantine:
                stage, cause, action = 'partition_eligibility', 'partition_relationship_requires_audit', 'audit_partition_edges'
            elif 'tangled' in excluded:
                cause, action = 'commit_scope_requires_unit_proof', 'revalidate_unit_causality'
            elif quarantine & {'residual_region', 'low_match_confidence', 'cross_file_match', 'ambiguous_match', 'parse_errors_in_unit'}:
                stage, cause, action = 'unit_matching', 'exact_unit_requires_recovery', 'recover_exact_unit'
            else:
                cause, action = 'source_quality_requires_revalidation', 'revalidate_source_quality'
        elif recorded_source and 'unsupported_security_semantics' in reasons:
            stage, bucket = 'security_proof', 'unsupported_mechanism'
            cause, action = 'security_semantics_unsupported', 'extend_validated_mechanism_rules'
        elif any(reason.split(':')[1:2] and reason.split(':')[1] in (
                'consumed_binding_mutated', 'consumed_binding_escaped', 'module_initialization_unsupported',
                'ambient_global_access_unsupported', 'unresolved_consumed_helper', 'binding_scope_unsupported',
                'intrinsic_binding_not_global', 'commonjs_loader_binding_unsupported') for reason in reasons):
            stage, bucket = 'security_proof', 'unsupported_mechanism'
            cause, action = 'binding_proof_not_established', 'revalidate_complete_module_bindings'
        elif reasons & {'unverified_fix_source', 'fix_link_verified', 'commit_kind'}:
            stage, bucket = 'source_and_fix_proof', 'source_or_fix_proof'
            cause, action = 'source_or_fix_proof_not_established', 'authenticate_source_and_prove_mechanism'
        elif reasons & {'attributed', 'link_evidence'}:
            stage, bucket = 'unit_causality', 'attribution_without_accepted_proof'
            cause, action = 'unit_security_relationship_not_established', 'establish_unit_security_proof'
        elif reasons & {'unsupported_security_mechanism', 'unsupported_security_semantics'}:
            stage, bucket = 'security_proof', 'unsupported_mechanism'
            cause, action = 'security_semantics_unsupported', 'extend_validated_mechanism_rules'
        else:
            stage, bucket = 'admission', 'other'
            cause, action = 'other_admission_failure', 'inspect_authoritative_failure'
    elif outcome == 'insufficient_context':
        stage, cause, action, repairability = 'security_proof', 'recorded_decisive_missing_facts', 'recover_decisive_source_facts', 'conditional'
    status = ('recorded_' + proof['basis'] if outcome == 'admitted' and proof.get('basis') in ('source_rule', 'trusted_execution')
              else 'recorded_missing_facts' if outcome == 'insufficient_context' else 'not_established')
    return {'schema': SCHEMA, 'stage': stage, 'bucket': bucket, 'cause': cause,
        'suggested_action': action, 'repairability': repairability, 'outcome': outcome,
        'source_authenticity': 'recorded_backend_source_foundation' if recorded_source else 'not_revalidated', 'security_proof_status': status,
        'requires_authoritative_validation': True, 'candidate_repository_fix_id': _candidate(record),
        'declared_family_id': record.get('fix_family_id'), 'source_group': record.get('issue_group_id')}


class Accumulator:
    """Consume bounded metadata batches; keep only counts and bounded traces."""
    def __init__(self):
        self.outcomes, self.funnel, self.reasons, self.causes = Counter(), Counter(), Counter(), Counter()
        self.candidates, self.by_bucket = set(), defaultdict(set)
        self.samples = defaultdict(list)

    def add(self, record):
        diagnosis = diagnose(record)
        outcome = diagnosis['outcome']
        self.outcomes[outcome] += 1
        if outcome != 'excluded':
            return diagnosis
        bucket = diagnosis['bucket']
        self.funnel[bucket] += 1
        self.reasons.update(_reasons(record.get('admission_reasons')))
        self.causes[diagnosis['cause']] += 1
        if diagnosis['candidate_repository_fix_id']:
            self.candidates.add(diagnosis['candidate_repository_fix_id'])
            self.by_bucket[bucket].add(diagnosis['candidate_repository_fix_id'])
        if len(self.samples[bucket]) < 3:
            self.samples[bucket].append({'record_id': record.get('record_id'), **diagnosis})
        return diagnosis

    def report(self, *, qualification=None):
        qualification = qualification if isinstance(qualification, dict) else {}
        return {'schema': SCHEMA, 'outcomes': dict(self.outcomes), 'excluded_funnel': dict(self.funnel),
            'overlapping_reasons': dict(self.reasons), 'cause_counts': dict(self.causes),
            'candidate_repository_fixes': len(self.candidates),
            'candidate_repository_fixes_by_bucket': {key: len(value) for key, value in self.by_bucket.items()},
            'candidate_count_basis': 'repository_and_fix_upper_bound_not_independent_qualified_families',
            'reported_validated_families': qualification.get('validated_families'),
            'qualification_report_revalidated': False, 'samples': dict(self.samples)}
