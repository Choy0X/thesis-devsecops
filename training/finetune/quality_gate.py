"""Fail-closed admission for new full training runs. Standard library only.

Smoke runs are software diagnostics and cannot establish model security quality.
An old frozen dataset remains readable, but freezing is not evidence of readiness.
"""
from __future__ import annotations
import hashlib
import json
import pathlib

SCHEMA = 'training-correctness/3'
ADMISSION_POLICY = 'automatic-evidence/2'
STRICT_ADMISSION_POLICIES = (ADMISSION_POLICY, 'automatic-evidence/3')
REQUIRED_CHECKS = ('serialization', 'token_shifting', 'completion_masking', 'field_ownership',
                   'adapter_updates', 'checkpoint_restoration', 'tiny_overfit', 'unseen_admitted_pairs')
SOURCE_CHECKS = REQUIRED_CHECKS[:-2] + ('finite_losses',)
LEGACY_SCHEMA = 'training-correctness/1'
LEGACY_CHECKS = REQUIRED_CHECKS[:-2] + ('reviewed_tiny_overfit', 'unseen_reviewed_pairs')
QUALIFICATION_POLICY = 'qualified-npm/1'
QUALIFICATION_SCHEMA = 'operational-data-qualification/1'
QUALIFICATION_MECHANISMS = ('prototype_pollution', 'path_traversal', 'command_injection')
QUALIFICATION_PARTITIONS = ('train', 'selection', 'calibration', 'untouched_test')
QUALIFICATION_REQUIREMENTS = {'validated_families': 1000, 'train_groups_per_mechanism': 50,
    'held_out_groups_per_mechanism_per_role': 10, 'mechanisms': list(QUALIFICATION_MECHANISMS)}


def qualification_summary(families):
    """Count deduplicated families and independent groups as separate units."""
    groups = {role: {mechanism: set() for mechanism in QUALIFICATION_MECHANISMS} for role in QUALIFICATION_PARTITIONS}
    for row in families:
        groups[row['partition']][row['mechanism']].add(row['source_group'])
    counts = {role: {mechanism: len(values) for mechanism, values in mechanisms.items()} for role, mechanisms in groups.items()}
    blockers = []
    if len(families) < 1000:
        blockers.append('insufficient_validated_vulnerability_fix_families')
    for role, mechanisms in counts.items():
        for mechanism, count in mechanisms.items():
            if count < (50 if role == 'train' else 10):
                blockers.append('insufficient_qualified_source_groups:' + role + ':' + mechanism)
    return {'schema': QUALIFICATION_SCHEMA, 'policy': QUALIFICATION_POLICY,
        'sampling_unit': 'independent_source_family',
        'requirements': {**QUALIFICATION_REQUIREMENTS, 'mechanisms': list(QUALIFICATION_MECHANISMS)},
        'qualified': not blockers, 'blockers': blockers, 'validated_families': len(families),
        'partition_mechanism_groups': counts, 'families': families}


def qualification_problems(readiness):
    """Recompute report claims from membership and bound them by available coverage.

    Manifest validation checks consistency; frozen file hashes and source receipt
    validation establish the provenance of these membership entries in the app.
    """
    report = readiness.get('data_qualification')
    if not isinstance(report, dict):
        return ['missing_operational_data_qualification']
    invalid = ['invalid_operational_data_qualification']
    excluded = report.get('excluded_records', {})
    if (not isinstance(excluded, dict)
            or any(not isinstance(key, str) or type(value) is not int or value < 0 for key, value in excluded.items())):
        return invalid
    if (readiness.get('qualification_policy') != QUALIFICATION_POLICY
            or report.get('schema') != QUALIFICATION_SCHEMA or report.get('policy') != QUALIFICATION_POLICY
            or report.get('sampling_unit') != 'independent_source_family'
            or report.get('requirements') != QUALIFICATION_REQUIREMENTS):
        return invalid
    families = report.get('families')
    if not isinstance(families, list):
        return invalid
    family_ids, records, group_roles = set(), set(), {}
    for row in families:
        if (not isinstance(row, dict) or set(row) != {'family_id', 'source_group', 'record_id', 'mechanism', 'partition'}
                or any(not isinstance(row.get(key), str) or not row[key].strip() for key in row)
                or row['partition'] not in QUALIFICATION_PARTITIONS or row['mechanism'] not in QUALIFICATION_MECHANISMS
                or row['family_id'] in family_ids or row['record_id'] in records
                or group_roles.get(row['source_group'], row['partition']) != row['partition']):
            return invalid
        family_ids.add(row['family_id']); records.add(row['record_id'])
        group_roles[row['source_group']] = row['partition']
    expected = qualification_summary(families)
    counts = report.get('partition_mechanism_groups')
    if (type(report.get('validated_families')) is not int or report['validated_families'] < 0
            or type(report.get('qualified')) is not bool or not isinstance(counts, dict)
            or set(counts) != set(QUALIFICATION_PARTITIONS)):
        return invalid
    for role in QUALIFICATION_PARTITIONS:
        actual = counts[role]
        if (not isinstance(actual, dict) or set(actual) != set(QUALIFICATION_MECHANISMS)
                or any(type(value) is not int or value < 0 for value in actual.values())):
            return invalid
    for key in ('validated_families', 'qualified', 'blockers', 'partition_mechanism_groups'):
        if report.get(key) != expected[key]:
            return invalid
    coverage = readiness.get('partition_coverage')
    if not isinstance(coverage, dict):
        return invalid
    for role in QUALIFICATION_PARTITIONS:
        detail = coverage.get(role)
        if not isinstance(detail, dict):
            return invalid
        pools, mechanisms = detail.get('source_groups'), detail.get('mechanism_groups')
        if not isinstance(pools, dict) or not isinstance(mechanisms, dict):
            return invalid
        if any(type(value) is not int or value < 0 for value in [*pools.values(), *mechanisms.values()]):
            return invalid
        total = sum(partition == role for partition in group_roles.values())
        if any(pools.get(pool, 0) < total for pool in ('supported', 'scoped_negative')):
            return invalid
        if any(mechanisms.get(mechanism, 0) < number for mechanism, number in counts[role].items()):
            return invalid
    return expected['blockers']


def automatic_readiness(readiness):
    if isinstance(readiness, dict) and readiness.get('schema') == 'sft-readiness/1' and readiness.get('admission_policy') == 'sft-admission/1':
        return True
    return (readiness.get('schema'), readiness.get('admission_policy')) in (
        ('security-readiness/2', 'automatic-evidence/1'), ('security-readiness/3', ADMISSION_POLICY),
        ('security-readiness/4', ADMISSION_POLICY), ('security-readiness/4', 'automatic-evidence/3'))


def diagnostic_schema(readiness):
    if readiness.get('admission_policy') == 'sft-admission/1':
        return SCHEMA
    if not automatic_readiness(readiness):
        return LEGACY_SCHEMA
    return SCHEMA if readiness.get('admission_policy') in STRICT_ADMISSION_POLICIES else 'training-correctness/2'


def assess_manifest(manifest, *, purpose='train'):
    """Lightweight data-only projection shared by app and standard-library notebooks."""
    if purpose == 'smoke':
        return {'schema': 'training-readiness/1', 'allowed': True, 'blockers': [],
                'integrity': {'state': 'not_required', 'blockers': []},
                'full_run': {'state': 'not_required', 'blockers': [], 'limitations': []},
                'diagnostics': {'state': 'not_required'}}
    if isinstance(manifest, dict) and manifest.get('admission_policy') == 'sft-admission/1':
        from training.finetune import sft_quality
        return sft_quality.assess_manifest(manifest)
    if not isinstance(manifest, dict):
        manifest = {}
    readiness = manifest.get('security_readiness') or {}
    readiness = readiness if isinstance(readiness, dict) else {}
    blockers = [] if manifest else ['missing_dataset_manifest']
    profile = manifest.get('build_profile')
    profile = profile if isinstance(profile, dict) else {}
    declared = [value for value in (manifest.get('qualification_policy'), profile.get('qualification_policy')) if value is not None]
    if declared and (any(value != QUALIFICATION_POLICY for value in declared)
                     or readiness.get('schema') != 'security-readiness/4'
                     or readiness.get('qualification_policy') != QUALIFICATION_POLICY):
        blockers.append('operational_qualification_policy_mismatch')
    automatic = automatic_readiness(readiness)
    legacy = readiness.get('schema') == 'security-readiness/1'
    if manifest.get('quality_profile') != 'strict' or not (automatic or legacy):
        blockers.append('missing_strict_security_readiness')
    if readiness.get('admission_policy') in STRICT_ADMISSION_POLICIES and manifest.get('admission_policy') != readiness['admission_policy']:
        blockers.append('dataset_admission_policy_mismatch')
    operational = readiness.get('schema') == 'security-readiness/4'
    qualification_blockers = qualification_problems(readiness) if operational else []
    qualification_valid = operational and all(reason == 'insufficient_validated_vulnerability_fix_families'
        or reason.startswith('insufficient_qualified_source_groups:') for reason in qualification_blockers)
    source_blockers = [reason for reason in (readiness.get('blockers') or [])
                       if not qualification_valid or reason not in qualification_blockers]
    if source_blockers or (readiness.get('training_ready') is not True and not (qualification_valid and qualification_blockers)):
        blockers += ['dataset_not_ready'] + source_blockers
    if operational and not qualification_valid:
        blockers.extend(qualification_blockers)
    if automatic and (type(readiness.get('training_views')) is not int or readiness['training_views'] <= 0):
        blockers.append('missing_supervised_training_views')
    integrity = {'state': 'blocked' if blockers else 'passed', 'blockers': list(dict.fromkeys(blockers))}
    scale=readiness.get('scale_assessment') or {}
    if operational:
        blockers.extend(qualification_blockers)
    if readiness.get('admission_policy') in STRICT_ADMISSION_POLICIES and not operational:
        if not scale:
            blockers.append('missing_security_corpus_scale_assessment')
        elif (not isinstance(scale, dict) or scale.get('schema') != 'security-corpus-scale/1'
              or scale.get('full_run_suitability') not in ('pilot_only', 'requires_learning_curve_and_held_out_measurement')
              or not isinstance(scale.get('limitations'), list)):
            blockers.append('invalid_security_corpus_scale_assessment')
        elif scale.get('full_run_suitability') != 'pilot_only':
            coverage = readiness.get('partition_coverage') or {}
            design = scale.get('zero_error_design') or {}
            valid = (isinstance(coverage, dict) and isinstance(design, dict)
                     and design.get('supported_groups') == 29 and design.get('protected_groups') == 299
                     and design.get('confidence') == .95 and not scale['limitations'])
            for partition in ('calibration', 'untouched_test'):
                detail = coverage.get(partition) if isinstance(coverage, dict) else None
                groups = detail.get('source_groups', {}) if isinstance(detail, dict) else {}
                if not isinstance(groups, dict):
                    valid = False
                    continue
                for pool, required in (('supported', 29), ('scoped_negative', 299)):
                    actual = groups.get(pool)
                    if type(actual) is not int or actual < required:
                        valid = False
            if not valid:
                blockers.append('invalid_or_insufficient_security_partition_coverage')
    if (automatic and not operational and isinstance(scale,dict) and scale.get('schema')=='security-corpus-scale/1'
            and scale.get('full_run_suitability')=='pilot_only'):
        blockers.append('insufficient_independent_security_partitions_for_full_run')
    limitations = []
    if not operational and isinstance(scale, dict) and isinstance(scale.get('limitations'), list):
        for row in scale['limitations']:
            if not isinstance(row, dict):
                continue
            actual, required = row.get('source_groups'), row.get('zero_error_design_groups')
            if type(actual) is int and type(required) is int:
                limitations.append({key: row.get(key) for key in ('partition', 'pool', 'source_groups', 'zero_error_design_groups')})
                limitations[-1]['shortage'] = max(0, required - actual)
    blockers = list(dict.fromkeys(blockers))
    qualification = readiness.get('data_qualification')
    qualification = ({key: value for key, value in qualification.items() if key != 'families'}
                     if isinstance(qualification, dict) else None)
    return {'schema': 'training-readiness/1', 'allowed': not blockers, 'blockers': blockers,
            'admission_policy': readiness.get('admission_policy'), 'integrity': integrity,
            'qualification_policy': readiness.get('qualification_policy'), 'data_qualification': qualification,
            'security_performance': {'state': 'unmeasured'},
            'full_run': {'state': 'blocked' if blockers else 'passed', 'blockers': blockers, 'limitations': limitations},
            'diagnostics': {'state': 'blocked' if blockers else 'pending'}}


def data_preflight(data_dir, *, purpose='train'):
    """Require data evidence before expensive pinning/export/model operations, without model diagnostics."""
    try:
        raw = (pathlib.Path(data_dir) / 'manifest.json').read_bytes()
        manifest = json.loads(raw)
    except (OSError, ValueError):
        raw, manifest = None, None
    if isinstance(manifest, dict) and manifest.get('admission_policy') == 'sft-admission/1' and purpose != 'smoke':
        from training.finetune import sft_quality
        report = sft_quality.data_preflight(data_dir, manifest)
    else:
        report = assess_manifest(manifest, purpose=purpose)
    return {**report, 'identity': {'dataset_manifest_sha256': hashlib.sha256(raw).hexdigest() if raw else None}}


def check(data_dir, *, purpose, diagnostics=None, config_sha256=None):
    if purpose == 'smoke':
        return {'allowed': True, 'blockers': [], 'scope': 'software diagnostic only'}
    path = pathlib.Path(data_dir) / 'manifest.json'
    try:
        raw = path.read_bytes(); manifest = json.loads(raw)
    except (OSError, ValueError):
        return {'allowed': False, 'blockers': ['missing_dataset_manifest']}
    if not isinstance(manifest, dict):
        return {'allowed': False, 'blockers': ['missing_dataset_manifest']}
    readiness = manifest.get('security_readiness') or {}
    readiness = readiness if isinstance(readiness, dict) else {}
    assessment = data_preflight(data_dir, purpose=purpose) if manifest.get('admission_policy') == 'sft-admission/1' else assess_manifest(manifest, purpose=purpose)
    blockers = list(assessment['blockers'])
    automatic = automatic_readiness(readiness)
    identity = {'dataset_manifest_sha256': hashlib.sha256(raw).hexdigest(), 'config_sha256': config_sha256}
    schema, required = (diagnostic_schema(readiness), REQUIRED_CHECKS) if automatic else (LEGACY_SCHEMA, LEGACY_CHECKS)
    corpus = manifest.get('coverage_policy', {}).get('schema') == 'mined-corpus/1'
    if corpus:
        required = SOURCE_CHECKS
    if not isinstance(diagnostics, dict) or diagnostics.get('schema') != schema:
        blockers.append('missing_correctness_diagnostics')
    else:
        if corpus and diagnostics.get('diagnostic_policy') != 'source-optimizer-integrity/1':
            blockers.append('missing_source_optimizer_integrity_policy')
        if not config_sha256 or diagnostics.get('identity') != identity:
            blockers.append('diagnostic_identity_mismatch')
        if automatic and (diagnostics.get('state') != 'passed' or diagnostics.get('admission_policy') != readiness.get('admission_policy')):
            blockers.append('diagnostic_not_passed_for_admission_policy')
        checks = diagnostics.get('checks') or {}
        checks = checks if isinstance(checks, dict) else {}
        blockers.extend('diagnostic_failed_or_missing:' + name for name in required if checks.get(name) is not True)
    return {'allowed': not blockers, 'blockers': blockers, 'identity': identity,
            'assessment': assessment,
            'admission_policy': readiness.get('admission_policy'),
            'scope': 'Readiness for training is separate from held-out security validation.'}


def require(data_dir, **kwargs):
    report = check(data_dir, **kwargs)
    if not report['allowed']:
        raise ValueError('Full training is blocked before model loading: ' + '; '.join(report['blockers'])
                         + '. Dataset readiness and automatic correctness checks must pass. Smoke runs remain available for software diagnostics.')
    return report


def ensure_diagnostics(bundle, run_dir, *, purpose='train', device='auto', launch=None, resource_check=None):
    """Check dataset admission before loading; run a bounded correctness child only when needed."""
    import os
    import subprocess
    import sys
    import uuid

    bundle, run_dir = pathlib.Path(bundle).resolve(), pathlib.Path(run_dir).resolve()
    if purpose == 'smoke':
        if resource_check is not None:
            resource_check()
        return check(bundle / 'data', purpose=purpose)
    run_dir.mkdir(parents=True, exist_ok=True)
    preflight = run_dir / 'training_preflight.json'
    state = {'schema': 'training-preflight/1', 'state': 'checking', 'admission_policy': None}
    def save():
        temporary = preflight.with_name(preflight.name + '.tmp')
        temporary.write_text(json.dumps(state, indent=2), encoding='utf-8')
        os.replace(temporary, preflight)
    def read(path):
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None
    try:
        resolved = read(bundle / 'resolved_config.json') or {}
        config_sha = resolved.get('config_sha256')
        data_report = check(bundle / 'data', purpose=purpose, config_sha256=config_sha)
        blockers = [reason for reason in data_report['blockers'] if reason != 'missing_correctness_diagnostics']
        if not isinstance(config_sha, str) or not config_sha:
            blockers.append('missing_resolved_training_configuration')
        state.update(identity=data_report.get('identity'), admission_policy=data_report.get('admission_policy'),
                     assessment=data_report.get('assessment') or data_preflight(bundle / 'data', purpose=purpose))
        if blockers:
            state.update(state='blocked', blockers=blockers); save()
            raise ValueError('Full training is blocked before model loading: ' + '; '.join(blockers))
        if resource_check is not None:
            resource_check()
        diagnostic_path = run_dir / 'correctness_diagnostics.json'
        current = read(diagnostic_path)
        report = check(bundle / 'data', purpose=purpose, diagnostics=current, config_sha256=config_sha)
        requested_tpu = device == 'tpu' or (device == 'auto' and
            ((resolved.get('config') or {}).get('precision') or {}).get('mode') == 'tpu_int4_bf16')
        if report['allowed'] and (not requested_tpu or current.get('backend') == 'tpu'):
            state.update(state='passed', reused=True); save()
            return report
        if requested_tpu:
            state.update(state='blocked', backend='tpu', blockers=['unsupported_tpu_correctness_diagnostics']); save()
            raise ValueError('Automatic full-training correctness diagnostics for TPU are not supported yet. '
                             'Use a CUDA GPU for full training; the experimental TPU hardware smoke probe remains available.')
        pilot = run_dir.parent / 'diagnostics' / ('pilot-' + uuid.uuid4().hex)
        state.update(state='running_diagnostics', pilot=str(pilot), reused=False); save()
        command = [sys.executable, '-u', '-m', 'training.finetune.correctness', '--bundle', str(bundle),
                   '--output', str(pilot), '--device', device]
        print('Running automatic training correctness checks. Evidence:', pilot, flush=True)
        (launch or subprocess.run)(command, cwd=str(bundle), check=True)
        generated = read(pilot / 'correctness_diagnostics.json')
        report = check(bundle / 'data', purpose=purpose, diagnostics=generated, config_sha256=config_sha)
        if not report['allowed']:
            state.update(blockers=report['blockers'])
            raise ValueError('Automatic correctness checks failed: ' + '; '.join(report['blockers']))
        temporary = diagnostic_path.with_name(diagnostic_path.name + '.tmp')
        temporary.write_bytes((pilot / 'correctness_diagnostics.json').read_bytes())
        os.replace(temporary, diagnostic_path)
        state.update(state='passed'); save()
        return report
    except BaseException as error:
        if state['state'] != 'blocked':
            state.update(state='failed', error={'type': type(error).__name__, 'message': str(error)})
            save()
        raise
