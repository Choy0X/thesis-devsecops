"""Immutable SFT export preparation. No model loading or training occurs here."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import zipfile

from . import finetune_catalog as catalog, finetune_prep as fp, versions
from . import finetune_qualified_run as generic
from .oslock import FileLock
from training.finetune import bundle as bd, overrides as ov, quality_gate, planning

SCHEMA = 'sft-finetune-preparation/1'
POLICY = 'sft-admission/1'
PIN = 'c03e6d358207e414f1eca0bb1891e29f1db0e242'
RECEIPT = 'sft_preparation.json'
_SOURCE_ROOT = Path(__file__).resolve().parents[3]
_sha, _digest, _read, _inside, _check = generic._sha, generic._digest, generic._read, generic._inside, generic._check


def _settings(root):
    settings, provenance = generic._settings(root)
    if any(settings[key] not in (None, PIN) for key in ('revision', 'tokenizer_revision')):
        raise ValueError('Approved SFT model and tokenizer revisions must retain the approved run2 pins.')
    settings.update(revision=PIN, tokenizer_revision=PIN)
    settings['overrides']['model.serialization'] = 'sft_native_chat_v1'
    base = fp.rs.cfgmod.load().model_dump(mode='json')
    effective = ov.apply(base, settings['overrides'])
    shares = effective['sampling']['task_shares']
    if set(shares) != set(fp.rs.cfgmod.TASKS):
        raise ValueError('The approved historical task shares must contain the original seven tasks.')
    context = effective['context']
    if (context['max_total_tokens'] != 4096 or context['ordinary_completion_reserve'] != 1024
            or context['repair_completion_reserve'] != 1536):
        raise ValueError('SFT preparation requires the approved 4096-token context and 1024/1536 reserves.')
    return settings, provenance, effective


def _hardware_settings(settings, provenance, effective, profile):
    if profile is None:
        return settings, provenance, effective
    if profile not in ('auto', 'a100'):
        raise ValueError('Unknown SFT hardware profile: ' + str(profile))
    updates = {'training.micro_batch_tuning': profile, 'training.length_grouping': True,
               'training.lm_loss_chunk_tokens': 512}
    return ({**settings, 'overrides': {**settings['overrides'], **updates}},
            {**provenance, 'performance_profile': {'name': profile, 'schema': 'portable-throughput/1' if profile == 'auto' else 'a100-throughput/2',
                'selection': 'bounded on-device throughput measurement with memory headroom',
                'effective_batch_unchanged': True}}, ov.apply(effective, updates))


def _training_cohort(dataset, *, check):
    """Read sampler metadata only; held-out, ineligible and zero-weight rows contribute nothing."""
    import pyarrow.parquet as pq
    repositories = {}
    with pq.ParquetFile(dataset / 'records.parquet') as records:
        for batch in records.iter_batches(batch_size=128, columns=['record_id', 'repository_identity']):
            _check(check)
            for row in batch.to_pylist():
                repositories[row['record_id']] = row['repository_identity'] or 'record:' + row['record_id']
    def slot():
        return {'views': 0, 'families': set(), 'repositories': set()}
    total, tasks = slot(), {}
    columns = ['record_id', 'task', 'pool', 'family_id', 'split', 'eligible', 'weight']
    with pq.ParquetFile(dataset / 'views.parquet') as views:
        for batch in views.iter_batches(batch_size=128, columns=columns):
            _check(check)
            for row in batch.to_pylist():
                weight = row['weight'] or 0
                if row['split'] != 'train' or row['eligible'] is not True or not math.isfinite(weight) or weight <= 0:
                    continue
                if any(not isinstance(row[key], str) or not row[key] for key in ('record_id', 'task', 'pool', 'family_id')):
                    raise ValueError('SFT training cohort contains missing sampler identities.')
                task = tasks.setdefault(row['task'], {**slot(), 'pools': {}})
                pool = task['pools'].setdefault(row['pool'], slot())
                repo = repositories.get(row['record_id']) or 'record:' + row['record_id']
                for target in (total, task, pool):
                    target['views'] += 1
                    target['families'].add(row['family_id'])
                    target['repositories'].add(repo)
    def counts(value):
        return {'views': value['views'], 'families': len(value['families']), 'repositories': len(value['repositories'])}
    return {**counts(total), 'tasks': {name: {**counts(value),
            'pools': {pool: counts(items) for pool, items in sorted(value['pools'].items())}}
            for name, value in sorted(tasks.items())}}


def _cohort_settings(settings, provenance, effective, cohort):
    """Preserve injection exposure and allocate T8 by its actual training family coverage."""
    original = dict(effective['sampling']['task_shares'])
    tasks = cohort['tasks']
    from training.finetune.sft_contracts import CORPUS_TASKS
    if set(tasks) & set(CORPUS_TASKS):
        targets = dict(upstream_patch=.55, code_completion=.30, change_analysis=.15)
        denominator = sum(value for task, value in targets.items() if task in tasks)
        shares = {task: 0.0 for task in (*original, 'regression_test_design', *CORPUS_TASKS)}
        shares.update({task: value / denominator for task, value in targets.items() if task in tasks})
        counts = {task: {pool: value['views'] for pool, value in data['pools'].items()} for task, data in tasks.items()}
        plan = {'schema': 'mined-corpus-sampling/1', 'cohort': cohort,
                'requested': shares, 'effective': planning.plan_task_shares(counts, shares),
                'record_coverage': 'every_training_record_once_per_record_pass',
                'progress_unit': 'record_pass', 'caps': 'soft; full record coverage takes precedence',
                'formula': 'patch=0.55; completion=0.30; edit_analysis=0.15; normalize available objectives'}
        passes = max(1.0, effective['training']['maximum_pilot_family_passes'])
        points = sorted({passes, *(p for p in effective['training']['evaluate_at_family_passes'] if 1 <= p <= passes)})
        updates = {'sampling.task_shares': shares, 'training.maximum_pilot_family_passes': passes,
                   'training.evaluate_at_family_passes': points, 'training.evaluate_every_steps': None}
        settings = {**settings, 'overrides': {**settings['overrides'], **updates}}
        return settings, {**provenance, 'sampling_plan': plan}, ov.apply(effective, updates)
    if not tasks.get('instruction_integrity', {}).get('views'):
        raise ValueError('SFT training injection examples are required for the requested 0.10 exposure share.')
    if set(tasks) - (set(original) | {'regression_test_design'}):
        raise ValueError('SFT training cohort contains unsupported sampler tasks.')
    counts = {task: {pool: value['views'] for pool, value in data['pools'].items()} for task, data in tasks.items()}
    try:
        planning.check_core(counts)
    except planning.EmptyCorePool as error:
        raise ValueError('SFT training cohort is missing a required core task: ' + str(error)) from error
    available = [task for task, share in original.items() if task != 'instruction_integrity' and share > 0 and task in tasks]
    denominator = sum(original[task] for task in available)
    if not denominator or not cohort['families']:
        raise ValueError('SFT training cohort has no available weighted base task families.')
    t8 = min(.10, .10 * tasks.get('regression_test_design', {}).get('families', 0) / cohort['families'])
    shares = {task: 0. for task in (*original, 'regression_test_design')}
    shares.update(instruction_integrity=.10, regression_test_design=t8)
    remainder = .90 - t8
    for task in available[:-1]:
        shares[task] = remainder * original[task] / denominator
    shares[available[-1]] = remainder - sum(shares[task] for task in available[:-1])
    planned = planning.plan_task_shares(counts, shares)
    warnings = [{'task': task, 'planned_share': share,
                 'family_capacity': .05 * tasks[task]['families'], 'repository_capacity': .10 * tasks[task]['repositories']}
                for task, share in planned['shares'].items()
                if share > .05 * tasks[task]['families'] or share > .10 * tasks[task]['repositories']]
    plan = {'schema': 'sft-cohort-shares/1', 'cohort': cohort, 'original_task_shares': original,
            'formula': 'injection=0.10; T8=min(0.10,0.10*T8_train_families/all_train_families); remainder proportional to available original base shares',
            'requested': shares, 'effective': planned,
            'caps': {'family': .05, 'repository': .10, 'warmup_draws': 200, 'unchanged': True},
            'capacity_warnings': warnings, 'capacity_check': 'necessary_counts_only; joint feasibility and actual cap relaxations are not measured'}
    settings = {**settings, 'overrides': {**settings['overrides'], 'sampling.task_shares': shares}}
    provenance = {**provenance, 'sampling_plan': plan}
    effective = ov.apply(effective, {'sampling.task_shares': shares})
    return settings, provenance, effective


def _dataset(root, tag):
    dataset = _inside(root, root / 'data/processed' / ('dataset_' + tag))
    _inside(root, dataset / 'manifest.json')
    assessment = quality_gate.data_preflight(dataset, purpose='train')
    if not assessment['allowed']:
        raise ValueError('SFT dataset is not ready: ' + '; '.join(assessment['blockers']))
    manifest = _read(dataset / 'manifest.json') or {}
    if (manifest.get('tag') != tag or manifest.get('dataset_status') != 'frozen'
            or manifest.get('admission_policy') != POLICY):
        raise ValueError('SFT preparation requires the exact frozen sft-admission/1 dataset.')
    _inside(root, dataset.parent / versions.REGISTRY_NAME)
    entry = versions.load_registry(dataset.parent)['frozen'].get(dataset.name) or {}
    files = entry.get('files') or {}
    if entry.get('base') != dataset.name or set(files) != set(manifest['files']) | {'manifest.json'}:
        raise ValueError('SFT dataset has no complete registered immutable publication identity.')
    actual = set()
    for path in dataset.rglob('*'):
        _inside(root, path)
        if path.is_file():
            actual.add(path.relative_to(dataset).as_posix())
    if actual != set(files):
        raise ValueError('SFT immutable publication inventory changed.')
    for relative, expected in files.items():
        path = _inside(root, dataset / relative)
        if not path.is_file() or _sha(path) != expected:
            raise ValueError('SFT immutable publication file hash changed: ' + relative)
    return dataset, manifest, files


def _source_binding(root, dataset):
    """Bind the publication's captured policy to trusted current source bytes."""
    capture_path = _inside(root, dataset / 'source_capture.json')
    capture = _read(capture_path) or {}
    files = (capture.get('policy') or {}).get('files') or {}
    if not isinstance(files, dict) or not files:
        raise ValueError('SFT publication is missing its captured source policy identity.')
    checked = {}
    for name, expected in files.items():
        path = Path(name)
        path = path if path.is_absolute() else root / path
        owner = next((base for base in (root, _SOURCE_ROOT) if path.is_relative_to(base)), None)
        if owner is None or _sha(_inside(owner, path)) != expected:
            raise ValueError('SFT captured policy source changed: ' + str(name))
        checked[str(path.resolve())] = expected
    for filename in ('sft_serialization.py', 'sft_contracts.py', 'serialization.py'):
        source = _SOURCE_ROOT / 'training/finetune' / filename
        if checked.get(str(source.resolve())) != _sha(source):
            raise ValueError('SFT publication did not capture its trusted serialization source: ' + filename)
    return {'capture_sha256': _sha(capture_path), 'policy_files': checked}


def _token_binding(root, dataset, manifest, effective, check):
    """Validate the already exact-token-audited accepted publication, without a proof gate."""
    import pyarrow.parquet as pq
    from .sft_token_audit import implementation_identity
    tokenizer = manifest['tokenizer']
    if tokenizer.get('model_id') != effective['model']['id'] or tokenizer.get('revision') != PIN:
        raise ValueError('Published tokenizer does not match the approved model/tokenizer pin.')
    token = _read(dataset / 'token_audit.json') or {}
    if (token.get('tokenizer') != tokenizer or token.get('source_sha256') != _sha(dataset / 'records.parquet')
            or not re.fullmatch('[a-f0-9]{64}', str(token.get('views_sha256') or ''))
            or not re.fullmatch('[a-f0-9]{64}', str(token.get('serialization_sha256') or ''))):
        raise ValueError('SFT exact token audit source or serialization identity mismatch.')
    native = {}
    with (dataset / 'instruction_views.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            _check(check)
            row = json.loads(line)
            # Native JSON adds parsed messages/response beside the Parquet columns.
            row.pop('messages', None)
            row.pop('response', None)
            key = row.get('view_id')
            if not isinstance(key, str) or key in native:
                raise ValueError('Duplicate or missing published SFT view identity.')
            native[key] = _digest(row)
    seen = set()
    with pq.ParquetFile(dataset / 'views.parquet') as parquet:
        for batch in parquet.iter_batches(batch_size=16):
            _check(check)
            for row in batch.to_pylist():
                key = row.get('view_id')
                if key in seen or native.get(key) != _digest(row):
                    raise ValueError('Published SFT Parquet/native view payload or membership mismatch.')
                seen.add(key)
    if seen != set(token['accepted_view_ids']) or seen != set(native):
        raise ValueError('Published SFT accepted token audit view membership mismatch.')
    source = _source_binding(root, dataset)
    implementation = implementation_identity()
    if (token.get('implementation') != implementation or any(
            source['policy_files'].get(name) != sha for name, sha in implementation['files'].items())):
        raise ValueError('SFT token audit implementation does not match trusted captured source.')
    policy = manifest.get('injection_policy')
    corpus = manifest.get('coverage_policy', {}).get('schema') == 'mined-corpus/1'
    if not corpus and (not isinstance(policy, dict) or policy.get('schema') != 'sft-context-injection/1'
            or policy.get('enabled') is not True or policy.get('rate') != .10):
        raise ValueError('SFT dataset injection policy must enable sft-context-injection/1 at the approved 0.10 build rate.')
    return {'schema': 'sft-run-token-binding/1', 'state': 'passed',
        'dataset_token_audit_sha256': _sha(dataset / 'token_audit.json'),
        'dataset_manifest_sha256': _sha(dataset / 'manifest.json'),
        'records_sha256': _sha(dataset / 'records.parquet'), 'views_sha256': _sha(dataset / 'views.parquet'),
        'audited_source_views_sha256': token['views_sha256'],
        'accepted_view_ids_sha256': _digest(sorted(seen)), 'accepted_views': len(seen),
        'tokenizer': tokenizer, 'serialization_sha256': token['serialization_sha256'],
        'max_total_tokens': token['max_total_tokens'], 'max_tokens': 4096,
        'completion_reserves': {'ordinary': 1024, 'repair': 1536}, 'source_identity': source,
        'implementation': implementation,
        'training_cohort': _training_cohort(dataset, check=check),
        'exact_encoding_repeated': False, 'model_weights_loaded': False,
        'dataset_policy': {'injection_rate': policy['rate'],
                           'injection_policy': policy, 'scope': 'dataset_build'}}


def _trusted_sources():
    """The exporter inventory comes from trusted local paths, never the archive."""
    _inside(_SOURCE_ROOT, _SOURCE_ROOT / 'training')
    return {name: _inside(_SOURCE_ROOT, path).read_bytes()
            for name, path in bd._source_files(_SOURCE_ROOT).items()}


def _verify(folder, *, root, params, reservation, dataset_sha):
    try:
        verified = generic._verify(folder, root=root, params=params, reservation=reservation, dataset_sha=dataset_sha)
    except (KeyError, zipfile.BadZipFile) as error:
        raise ValueError('Invalid SFT bundle trusted source or metadata inventory.') from error
    sources = _trusted_sources()
    source_identity = _digest({name: hashlib.sha256(body).hexdigest() for name, body in sources.items()})
    if source_identity != params['training_source_sha256']:
        raise ValueError('Bundled SFT trusted source inventory changed during preparation.')
    with zipfile.ZipFile(folder / 'bundle.zip') as archive:
        expected = set(sources) | {'data/' + name for name in params['dataset_files']} | {
            bd.MANIFEST, fp.rs.FILE, 'SOURCE_REVISION'}
        if set(archive.namelist()) != expected:
            raise ValueError('Bundled SFT trusted source inventory has missing or extra members.')
        for member, body in sources.items():
            if archive.getinfo(member).file_size != len(body) or archive.read(member) != body:
                raise ValueError('Bundled SFT source differs from trusted source: ' + member)
        manifest = json.loads(archive.read(bd.MANIFEST))
        for member, identity in manifest['files'].items():
            if identity.get('bytes') != archive.getinfo(member).file_size:
                raise ValueError('Bundle member size differs from its inventory: ' + member)
    return verified


def _artifact_files(root, folder):
    files = {}
    for name in (fp.PREPARED, fp.rs.FILE, 'bundle.zip', 'finetune.ipynb', 'token_budget_audit.json'):
        path = _inside(root, folder / name)
        files[name] = {'sha256': _sha(path), 'bytes': path.stat().st_size}
    return files


def _metadata(root, final, reservation, info, document, software, provenance, binding, files, *, reused):
    result = generic._metadata(root, final, reservation, info, document, software, provenance, reused=reused)
    result.update(schema=SCHEMA, dataset_policy=binding['dataset_policy'], artifact_files=files)
    result['evaluation_availability'] = info.get('evaluation_availability')
    for key in ('bundle', 'notebook'):
        result[key]['bytes'] = files['bundle.zip' if key == 'bundle' else 'finetune.ipynb']['bytes']
    result['token_budget_audit'].update(schema=binding['schema'], accepted_views=binding['accepted_views'],
        dataset_token_audit_sha256=binding['dataset_token_audit_sha256'])
    return result


def _valid_software(software, run_id):
    return (isinstance(software, dict) and software.get('state') == 'passed' and software.get('parent_run_id') == run_id
        and all(software.get(key) is True for key in ('discovery', 'unpack', 'data_preflight', 'self_contained_standard_library', 'workspace_unique'))
        and software.get('selected_model_loaded') is False and software.get('training_started') is False)


def prepare(tag, *, root=None, work_key, check=None, hardware_profile='auto'):
    """Export one new UUID run for immutable SFT data; retain matching Colab diagnostics as pending."""
    _check(check)
    if not isinstance(tag, str) or not generic._TAG.fullmatch(tag):
        raise ValueError('Invalid SFT dataset version.')
    if not isinstance(work_key, str) or not work_key.strip():
        raise ValueError('SFT preparation requires a stable work_key.')
    root = Path(root if root is not None else fp.ROOT).resolve(strict=True)
    dataset, manifest, files = _dataset(root, tag)
    dataset_sha = files['manifest.json']
    settings, provenance, effective = _settings(root)
    binding = _token_binding(root, dataset, manifest, effective, check)
    settings, provenance, effective = _cohort_settings(settings, provenance, effective, binding['training_cohort'])
    settings, provenance, effective = _hardware_settings(settings, provenance, effective, hardware_profile)
    source_identity = _digest({name: hashlib.sha256(body).hexdigest() for name, body in _trusted_sources().items()})
    params = {'tag': tag, 'purpose': 'train', 'stage': 1, 'dataset_manifest_sha256': dataset_sha,
        'dataset_files': files, 'token_binding_sha256': _digest(binding), 'settings_provenance': provenance,
        'training_source_sha256': source_identity,
        'contract_sha256': fp.rs.cfgmod.file_sha256(fp.rs.cfgmod.DEFAULT_PATH), **settings}
    request_id = 'sft-' + hashlib.sha256(work_key.encode('utf-8')).hexdigest()
    runs = _inside(root, root / 'training/runs')
    _inside(root, runs / '.catalog')
    _check(check)
    reservation = catalog.reserve_run(runs, request_id=request_id, params=params)
    final = _inside(root, runs / reservation['name'])
    with FileLock(runs / '.catalog' / (request_id + '.lock'), note=False):
        _check(check)
        catalog.validate_creation(runs, request_id=request_id, name=reservation['name'], run_id=reservation['run_id'], params=params)
        if _dataset(root, tag)[2] != files:
            raise ValueError('SFT immutable publication changed before creation.')
        if final.exists():
            info, document, notebook = _verify(final, root=root, params=params, reservation=reservation, dataset_sha=dataset_sha)
            receipt = _read(_inside(root, final / RECEIPT)) or {}
            bound = {**binding, 'config_sha256': document['config_sha256'], 'bundle_sha256': info['bundle']['sha256']}
            token_path = _inside(root, final / 'token_budget_audit.json')
            if (_read(token_path) != bound or receipt.get('token_budget_audit', {}).get('sha256') != _sha(token_path)):
                raise ValueError('SFT token-budget binding is missing or changed.')
            software = receipt.get('software_validation')
            artifacts = _artifact_files(root, final)
            result = _metadata(root, final, reservation, info, document, software, provenance, binding, artifacts, reused=False)
            if not _valid_software(software, reservation['run_id']) or receipt != result:
                raise ValueError('SFT preparation ownership receipt or software validation changed.')
            _check(check)
            result['reused'] = True
            return result
        stage_base = _inside(root, runs / '.sft-preparations' / request_id)
        marker_path = _inside(root, stage_base / 'request.json')
        marker = {'request_id': request_id, 'run_id': reservation['run_id'], 'name': reservation['name'], 'params_sha256': _digest(params)}
        if stage_base.exists() and any(stage_base.iterdir()) and _read(marker_path) != marker:
            raise ValueError('Staged SFT preparation belongs to another request.')
        stage_base.mkdir(parents=True, exist_ok=True)
        if not marker_path.exists():
            fp._write_json(marker_path, marker)
        stage = _inside(root, stage_base / reservation['name'])
        stage.mkdir(exist_ok=True)
        prepared_path = _inside(root, stage / fp.PREPARED)
        if not prepared_path.exists():
            if any(stage.iterdir()):
                raise ValueError('Unowned artifacts exist in staged SFT run.')
            fp._write_json(prepared_path, {'schema': 'sft-run-reserved/1', 'run_id': reservation['run_id']})
        previous = _read(prepared_path)
        if previous.get('schema') != 'finetune-prepared/1':
            if previous != {'schema': 'sft-run-reserved/1', 'run_id': reservation['run_id']}:
                raise ValueError('Staged SFT run has no matching UUID ownership marker.')
            for path in stage.rglob('*'):
                _inside(root, path)
            _check(check)
            fp.prepare(dataset_dir=dataset, run_dir=stage, source_root=root, stage=1, purpose='train', **settings)
        _check(check)
        info, document, notebook = _verify(stage, root=root, params=params, reservation=reservation, dataset_sha=dataset_sha)
        bound = {**binding, 'config_sha256': document['config_sha256'], 'bundle_sha256': info['bundle']['sha256']}
        token_path = _inside(root, stage / 'token_budget_audit.json')
        if token_path.exists() and _read(token_path) != bound:
            raise ValueError('Staged SFT token-budget binding changed.')
        if not token_path.exists():
            fp._write_json(token_path, bound)
        software = generic._software_check(stage, notebook, run_id=reservation['run_id'], dataset_sha=dataset_sha)
        if not _valid_software(software, reservation['run_id']):
            raise ValueError('SFT notebook software validation is incomplete.')
        _check(check)
        artifacts = _artifact_files(root, stage)
        result = _metadata(root, final, reservation, info, document, software, provenance, binding, artifacts, reused=False)
        result['token_budget_audit']['sha256'] = _sha(token_path)
        receipt_path = _inside(root, stage / RECEIPT)
        if receipt_path.exists() and _read(receipt_path) != result:
            raise ValueError('Staged SFT preparation receipt changed.')
        if not receipt_path.exists():
            fp._write_json(receipt_path, result)
        with versions.publication_scope(getattr(check, 'publication_guard', check)):
            if final.exists() or _dataset(root, tag)[2] != files:
                raise ValueError('Run target or SFT immutable publication changed before publication.')
            if _token_binding(root, dataset, manifest, effective, check) != binding:
                raise ValueError('SFT source/token identity changed before publication.')
            _verify(stage, root=root, params=params, reservation=reservation, dataset_sha=dataset_sha)
            if (_artifact_files(root, stage) != artifacts or _read(receipt_path) != result
                    or _read(marker_path) != marker or _read(token_path) != bound):
                raise ValueError('SFT preparation artifact hash or ownership receipt changed before publication.')
            stage.rename(final)
        catalog.invalidate(runs, reservation['name'])
        return result
