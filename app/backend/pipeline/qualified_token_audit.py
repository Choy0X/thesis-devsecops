"""Audit the exact training serialization without loading model weights."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import re


def validate_shortage(report, *, maximum=None):
    """Validate a blocked audit's bounded summary before planning more sources.

    This accepts no positive qualification claim. Source-backed encoding remains
    the authority; the summary supplies only conservative recovery priorities.
    """
    from training.finetune import quality_gate as gate
    invalid = 'invalid_token_qualification_shortage'
    retained = report.get('retained_qualification') if isinstance(report, dict) else None
    if (not isinstance(retained, dict) or report.get('schema') != 'qualified-token-budget/1'
            or report.get('state') != 'blocked' or retained.get('schema') != gate.QUALIFICATION_SCHEMA
            or retained.get('policy') != gate.QUALIFICATION_POLICY
            or retained.get('sampling_unit') != 'independent_source_family'
            or retained.get('requirements') != gate.QUALIFICATION_REQUIREMENTS
            or type(retained.get('validated_families')) is not int or retained['validated_families'] < 0):
        raise ValueError(invalid)
    number, groups = retained['validated_families'], retained.get('partition_mechanism_groups')
    if not isinstance(groups, dict) or set(groups) != set(gate.QUALIFICATION_PARTITIONS):
        raise ValueError(invalid)
    expected = ['insufficient_validated_vulnerability_fix_families'] if number < 1000 else []
    total = 0
    for role in gate.QUALIFICATION_PARTITIONS:
        counts = groups[role]
        if (not isinstance(counts, dict) or set(counts) != set(gate.QUALIFICATION_MECHANISMS)
                or any(type(value) is not int or not 0 <= value <= number for value in counts.values())):
            raise ValueError(invalid)
        total += sum(counts.values())
        for mechanism in gate.QUALIFICATION_MECHANISMS:
            if counts[mechanism] < (50 if role == 'train' else 10):
                expected.append('insufficient_qualified_source_groups:' + role + ':' + mechanism)
    if total > number or retained.get('blockers') != expected or type(retained.get('qualified')) is not bool or retained['qualified'] != (not expected):
        raise ValueError(invalid)
    if maximum is not None:
        if type(maximum.get('validated_families')) is not int or number > maximum['validated_families']:
            raise ValueError(invalid)
        for role, counts in groups.items():
            upper = (maximum.get('partition_mechanism_groups') or {}).get(role) or {}
            if any(type(upper.get(mechanism)) is not int or value > upper[mechanism] for mechanism, value in counts.items()):
                raise ValueError(invalid)
    additional = {'missing_encoded_task:' + role + ':' + task for role in gate.QUALIFICATION_PARTITIONS
                  for task in ('discovery', 'condition_assessment', 'repair')}
    additional |= {'missing_encoded_protected_pool:' + role for role in gate.QUALIFICATION_PARTITIONS}
    blockers = report.get('blockers')
    if (not isinstance(blockers, list) or not blockers or any(not isinstance(value, str) for value in blockers)
            or len(set(blockers)) != len(blockers) or not set(expected) <= set(blockers)
            or not set(blockers) <= set(expected) | additional
            or report.get('source_evidence_truncated') is not False or report.get('model_weights_loaded') is not False):
        raise ValueError(invalid)
    for key in ('dataset_manifest_sha256', 'config_sha256', 'bundle_sha256'):
        if not isinstance(report.get(key), str) or not re.fullmatch('[0-9a-f]{64}', report[key]):
            raise ValueError(invalid)
    for key in ('counts', 'exclusions'):
        if not isinstance(report.get(key), dict) or any(type(value) is not int or value < 0 for value in report[key].values()):
            raise ValueError(invalid)
    return {key: value for key, value in retained.items() if key != 'families'}


def audit(dataset, cfg, *, config_sha256, tokenizer=None, check=None):
    import pyarrow.parquet as pq
    from training.finetune import serialization, tokenization, quality_gate
    from .negatives import _sid

    if tokenizer is None:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(cfg.model.id, revision=cfg.model.tokenizer_revision,
            trust_remote_code=False, use_fast=True)
    manifest_bytes = (dataset / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    members = manifest['security_readiness']['data_qualification']['families']
    usable, counts, exclusions, coverage = defaultdict(set), Counter(), Counter(), Counter()
    for batch in pq.ParquetFile(dataset / 'views.parquet').iter_batches(batch_size=64):
        if check is not None and check() is False:
            raise InterruptedError('Token budget audit cancelled.')
        for row in batch.to_pylist():
            if not row.get('eligible') or not (row.get('weight') or 0) > 0:
                continue
            counts['considered'] += 1
            role = row.get('val_role') if row['split'] == 'val' else 'untouched_test' if row['split'] == 'test' else row['split']
            if role not in ('train', 'selection', 'calibration', 'untouched_test'):
                exclusions['unsupported_partition'] += 1
                continue
            try:
                response = json.loads(row['response_json'])
                serialized = serialization.serialize_example(row['task'], row['context_text'], response,
                    protocol=cfg.model.serialization, tokenizer=tokenizer)
            except (ValueError, KeyError):
                exclusions['invalid_response'] += 1
                continue
            if serialized.reserved_heading_in_context:
                exclusions['reserved_heading_in_context'] += 1
                continue
            encoded, reason = tokenization.try_encode(tokenizer, serialized, context=cfg.context)
            if encoded is None:
                exclusions[reason] += 1
                continue
            counts['encoded'] += 1
            counts['prompt_tokens'] += encoded.prompt_tokens
            counts['completion_tokens'] += encoded.completion_tokens
            counts['max_total_tokens'] = max(counts['max_total_tokens'], len(encoded.input_ids))
            usable[(row['record_id'], role)].add((row['task'], row['pool'], row['subject_id']))
            coverage[(role, row['task'], row['pool'])] += 1
    retained = []
    for member in members:
        rid = member['record_id']
        needed = {('discovery', 'supported', _sid(rid, 'vulnerable')),
                  ('discovery', 'scoped_negative', _sid(rid, 'fixed_twin')),
                  ('repair', 'supported', _sid(rid, 'vulnerable'))}
        if needed <= usable[(rid, member['partition'])]:
            retained.append(member)
    qualified = quality_gate.qualification_summary(retained)
    blockers = list(qualified['blockers'])
    for role in ('train', 'selection', 'calibration', 'untouched_test'):
        for task in ('discovery', 'condition_assessment', 'repair'):
            if not coverage[(role, task, 'supported')]:
                blockers.append('missing_encoded_task:' + role + ':' + task)
        if not coverage[(role, 'discovery', 'scoped_negative')]:
            blockers.append('missing_encoded_protected_pool:' + role)
    return {'schema': 'qualified-token-budget/1', 'state': 'blocked' if blockers else 'passed',
            'dataset_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(), 'config_sha256': config_sha256,
            'model': cfg.model.id, 'tokenizer_revision': cfg.model.tokenizer_revision,
            'serialization': cfg.model.serialization, 'context_tokens': cfg.context.max_total_tokens,
            'counts': dict(counts), 'exclusions': dict(exclusions), 'blockers': blockers,
            'retained_qualification': {key: value for key, value in qualified.items() if key != 'families'},
            'source_evidence_truncated': False, 'model_weights_loaded': False}
