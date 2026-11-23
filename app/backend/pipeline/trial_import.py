"""Authenticated experimental trial outputs. These never become training results."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path, PurePosixPath
import stat
import tempfile
import zipfile

from . import util
from .finetune_deletion import guarded
from .finetune_import import ImportRejected, MAX_BYTES, MAX_FILES, _sha_file
from .oslock import FileLock
from training.finetune import bundle, resolve

SCHEMA = 'trial-import/1'
MANIFEST = 'trial_outputs_manifest.json'
CHECKS = ('zip_structure', 'manifest', 'trial_config', 'identity', 'reports')
REPORTS = ('baseline_report', 'tuned_report', 'comparison', 'resources')


def _reject(check, detail):
    raise ImportRejected([{'check': check, 'detail': detail}])


def _json(data, check, name):
    try:
        value = json.loads(data)
    except (ValueError, TypeError, UnicodeError) as problem:
        _reject(check, f'{name} is not valid JSON: {problem}')
    if not isinstance(value, dict):
        _reject(check, f'{name} must contain a JSON object')
    return value


def _safe(name):
    parts = PurePosixPath(name).parts
    return (bool(parts) and PurePosixPath(name).as_posix() == name
            and not name.startswith('/') and '\\' not in name
            and all(part not in ('.', '..') and ':' not in part and not part.endswith(('.', ' '))
                    and part.split('.')[0].upper() not in ('CON', 'PRN', 'AUX', 'NUL',
                        *('COM' + str(i) for i in range(1, 10)), *('LPT' + str(i) for i in range(1, 10)))
                    for part in parts))


def _read_archive(path, max_bytes):
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if (len(infos) > MAX_FILES or sum(item.file_size for item in infos) > max_bytes
                    or len({name.casefold() for name in names}) != len(names)
                    or any(not _safe(name) for name in names)):
                _reject('zip_structure', 'Unsafe paths, duplicate members or archive limits exceeded')
            if any(item.is_dir() or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1 for item in infos):
                _reject('zip_structure', 'Only unencrypted regular files may be imported')
            content = {item.filename: archive.read(item) for item in infos}
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as problem:
        _reject('zip_structure', f'The trial output archive cannot be read: {problem}')
    manifest = _json(content.get(MANIFEST), 'manifest', MANIFEST)
    files = manifest.get('files')
    if manifest.get('schema') != 'trial-outputs/1' or not isinstance(files, dict):
        _reject('manifest', 'Expected trial-outputs/1 and a files mapping')
    if set(files) != set(content) - {MANIFEST} or 'trial_config.json' not in files:
        _reject('manifest', 'Every archive member must be listed, including trial_config.json')
    for name, entry in files.items():
        raw = content[name]
        if (not isinstance(entry, dict) or type(entry.get('bytes')) is not int
                or entry['bytes'] != len(raw) or entry.get('sha256') != hashlib.sha256(raw).hexdigest()):
            _reject('manifest', f'{name} differs from its recorded SHA256 or byte count')
    return content


def _identity(run_dir, config):
    prepared = util.read_json(run_dir / 'prepared.json') or {}
    if prepared.get('mode') != 'trial':
        _reject('identity', 'Choose the prepared portable trial that created this archive')
    try:
        problems = bundle.verify_bundle(run_dir / 'bundle.zip')
        if problems or _sha_file(run_dir / 'bundle.zip') != prepared['bundle']['sha256']:
            _reject('identity', 'The original prepared bundle has changed')
        cfg = resolve.read_resolved(run_dir)
        resolved_bytes = (run_dir / resolve.FILE).read_bytes()
        with zipfile.ZipFile(run_dir / 'bundle.zip') as archive:
            manifest_sha = hashlib.sha256(archive.read('bundle_manifest.json')).hexdigest()
            if archive.read('resolved_config.json') != resolved_bytes:
                _reject('identity', 'The resolved configuration differs from the original bundle')
            data_sha = hashlib.sha256(archive.read('data/manifest.json')).hexdigest()
            derived_manifest = _json(archive.read('data/manifest.json'), 'identity', 'data/manifest.json')
        expected = {'model': cfg.model.id, 'model_revision': cfg.model.revision,
            'tokenizer_revision': cfg.model.tokenizer_revision,
            'resolved_config_sha256': hashlib.sha256(resolved_bytes).hexdigest()}
        for key, value in expected.items():
            if config.get(key) != value or (key in prepared and prepared[key] != value):
                _reject('identity', f'{key} differs from the original prepared run')
        if (config.get('dataset', {}).get('manifest_sha256') != data_sha
                or prepared['dataset']['manifest_sha256'] != data_sha or cfg.data.manifest_sha256 != data_sha):
            _reject('identity', 'The derived dataset manifest differs from the original prepared bundle')
        if config.get('dataset', {}).get('version') != derived_manifest.get('tag'):
            _reject('identity', 'The derived dataset version differs from the prepared bundle')
        if config.get('bundle', {}).get('manifest_sha256') != manifest_sha:
            _reject('identity', 'The archive belongs to another bundle')
        if config.get('total_budget_minutes') != prepared.get('total_budget_minutes', 30):
            _reject('identity', 'The trial budget differs from the prepared budget')
    except ImportRejected:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile) as problem:
        _reject('identity', f'The original prepared inputs cannot be verified: {problem}')
    return manifest_sha


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _equivalent_metrics(actual, expected):
    """Account for Python/libm roundoff; identities, counts and keys stay exact."""
    if type(expected) is float:
        return (type(actual) in (int, float) and math.isfinite(actual)
                and math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12))
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and actual.keys() == expected.keys()
                and all(_equivalent_metrics(actual[key], value) for key, value in expected.items()))
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(_equivalent_metrics(a, b) for a, b in zip(actual, expected)))
    return type(actual) is type(expected) and actual == expected


def _runtime_recipe(run_dir, config, content):
    recipe = config.get('runtime_recipe')
    with zipfile.ZipFile(run_dir / 'bundle.zip') as archive:
        declares_runtime_policy = 'training/finetune/runtime_compatibility.py' in archive.namelist()
    execution_evidence = (config.get('state') == 'finished' or bool(config.get('model_loaded'))
        or config.get('steps_done') not in (None, 0)
        or bool(config.get('baseline_report') or config.get('tuned_report'))
        or bool({'baseline_report.json', 'tuned_report.json'} & content.keys()))
    if recipe is None:
        if declares_runtime_policy and (execution_evidence or 'effective_config.json' in content):
            _reject('trial_config', 'Runtime recipe declaration is required for this new bundle execution')
        return  # Historical archives and early bootstrap failures have no executed recipe.
    from training.finetune import runtime_compatibility
    try:
        cfg = resolve.read_resolved(run_dir)
        expected = runtime_compatibility.validate_recipe(cfg, recipe)
        if (recipe['backend'] == 'cpu' and not config['software_test']
                and (execution_evidence or config.get('state') not in ('blocked', 'failed', 'partial'))):
            raise ValueError('A CPU recipe is software evidence only')
        if recipe['backend'] == 'tpu' and not declares_runtime_policy:
            raise ValueError('The original bundle did not offer the experimental TPU recipe')
        actual = _json(content.get('effective_config.json'), 'trial_config', 'effective_config.json')
        if actual != expected.model_dump(mode='json'):
            raise ValueError('Effective runtime recipe changed model, LoRA, context, training or objective settings')
    except (ValueError, KeyError, TypeError, AttributeError) as problem:
        _reject('trial_config', f'Runtime recipe rejected: {problem}')


def _cohort(run_dir):
    import io
    import pyarrow.parquet as pq
    with zipfile.ZipFile(run_dir / 'bundle.zip') as archive:
        views = pq.read_table(io.BytesIO(archive.read('data/views.parquet'))).to_pylist()
    rows = sorted((row for row in views if row.get('split') == 'val' and row.get('val_role') == 'source_trial'
                   and row.get('eligible') and (row.get('weight') or 0) > 0), key=lambda row: row['view_id'])
    if not rows or len({row['view_id'] for row in rows}) != len(rows):
        _reject('reports', 'The original bundle has no unique held-out source trial cohort')
    return rows


def _report(report, *, arm, expected, config):
    from training.finetune import trial_evaluation as evaluation
    if (report.get('schema') != 'source-trial-evaluation/1' or report.get('arm') != arm
            or report.get('state') not in ('finished', 'partial') or report.get('security_evaluation') is not False):
        _reject('reports', f'{arm} must be a source-trial-evaluation/1 report with the correct arm and state')
    rows = report.get('rows')
    if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            or [(row.get('view_id'), row.get('record_id'), row.get('task')) for row in rows]
            != [(row['view_id'], row['record_id'], row['task']) for row in expected]):
        _reject('reports', f'{arm} must account for the complete original held-out source trial cohort')
    generation = config.get('generation') or {}
    generation_ids = report.get('generation_ids')
    if (not isinstance(generation_ids, list) or any(not isinstance(value, str) for value in generation_ids)
            or generation_ids != sorted(set(generation_ids))
            or not set(generation_ids).issubset({row['view_id'] for row in rows})
            or generation.get('do_sample') is not False or type(generation.get('max_new_tokens')) is not int
            or generation['max_new_tokens'] != 128 or report.get('max_new_tokens') != generation['max_new_tokens']
            or not isinstance(generation.get('view_ids'), list)
            or any(not isinstance(value, str) for value in generation.get('view_ids', []))
            or sorted(generation['view_ids']) != generation_ids):
        _reject('reports', f'{arm} generation cohort and decoding must match the trial configuration')
    if not _finite(report.get('seconds')):
        _reject('reports', f'{arm} elapsed time must be finite and nonnegative')
    for row in rows:
        status = row.get('status')
        if status not in ('scored', 'excluded', 'deadline_excluded', 'resource_failure', 'not_attempted', 'running'):
            _reject('reports', f'{arm} contains an unknown view accounting status')
        if status == 'scored':
            count, nll, mean = row.get('supervised_tokens'), row.get('nll_sum'), row.get('mean_loss')
            if (type(count) is not int or count <= 0 or not _finite(nll) or not _finite(mean)
                    or not math.isclose(mean, nll / count, rel_tol=1e-6, abs_tol=1e-8)
                    or any(not re.fullmatch(r'[0-9a-f]{64}', str(row.get(key, ''))) for key in ('prompt_sha256', 'target_sha256'))):
                _reject('reports', f'{arm} scored views need finite coherent loss, positive token counts and prompt/target hashes')
        generated = row.get('generation')
        if generated is None:
            continue
        if not isinstance(generated, dict) or row['view_id'] not in generation_ids:
            _reject('reports', f'{arm} generated output is outside its declared generation cohort')
        state = generated.get('state')
        if state not in ('answered', 'token_limit', 'deadline_excluded', 'running', 'timeout'):
            _reject('reports', f'{arm} generated output has an unknown accounting state')
        if state in ('deadline_excluded', 'running', 'timeout'):
            if generated.get('text') is not None:
                _reject('reports', f'{arm} excluded generation cannot contain an answer')
            continue
        text, target, tokens = generated.get('text'), generated.get('target'), generated.get('new_tokens')
        if (status != 'scored' or not isinstance(text, str) or not isinstance(target, str)
                or type(tokens) is not int or not 0 <= tokens <= report['max_new_tokens']
                or type(generated.get('hit_limit')) is not bool
                or generated['hit_limit'] != (state == 'token_limit')
                or not _finite(generated.get('seconds'))
                or generated.get('exact_match') is not (text == target)
                or generated.get('whitespace_normalized_match') is not (' '.join(text.split()) == ' '.join(target.split()))
                or hashlib.sha256(target.encode()).hexdigest() != row['target_sha256']):
            _reject('reports', f'{arm} raw generation disagrees with its token, target or match accounting')
    summary = evaluation.summarize(rows)
    tasks = {task: evaluation.summarize([row for row in rows if row['task'] == task]) for task in sorted({row['task'] for row in rows})}
    if not _equivalent_metrics(report.get('summary'), summary) or not _equivalent_metrics(report.get('tasks'), tasks):
        _reject('reports', f'{arm} summary or task metrics disagree with its raw rows')
    complete = all(row['status'] == 'scored' and (row['view_id'] not in generation_ids or
                   (row.get('generation') or {}).get('state') in ('answered', 'token_limit')) for row in rows)
    if (report['state'] == 'finished') != complete:
        _reject('reports', f'{arm} completion state disagrees with its view and generation accounting')


def _adapter(content, config, required):
    adapter = config.get('adapter')
    if adapter is None and not required:
        return 'not_produced'
    name = 'adapter/adapter_model.safetensors'
    raw = content.get(name)
    steps = config.get('steps_done')
    recovery = config.get('recovery') or {}
    if not isinstance(recovery, dict) or not isinstance(recovery.get('files', []), list):
        _reject('reports', 'Recovery metadata must contain an explicit file inventory')
    omitted = [item for item in recovery.get('files', []) if isinstance(item, dict) and item.get('path') == name]
    if (not raw and config['state'] in ('partial', 'failed') and recovery.get('state') == 'files_preserved'
            and recovery.get('validation') == 'not_validated' and len(omitted) == 1
            and omitted[0].get('included_in_archive') is False and type(omitted[0].get('bytes')) is int
            and omitted[0]['bytes'] > 0 and isinstance(adapter, dict)
            and re.fullmatch(r'[0-9a-f]{64}', str(adapter.get('sha256', '')))
            and type(steps) is int and steps > 0 and adapter.get('steps_done') == steps):
        return 'unavailable_in_compact_recovery'
    if (not isinstance(adapter, dict) or not raw or type(steps) is not int or steps <= 0
            or adapter.get('steps_done') != steps or adapter.get('sha256') != hashlib.sha256(raw).hexdigest()):
        _reject('reports', 'An adapted evaluation requires actual adapter weights with their SHA256 and positive completed steps')
    # Read only the safetensors header and shape/byte offsets. Never deserialize
    # imported Python objects or allocate the tensors themselves.
    try:
        length = int.from_bytes(raw[:8], 'little')
        if not 2 <= length <= min(16 * 2**20, len(raw) - 8):
            raise ValueError('invalid safetensors header length')
        header = json.loads(raw[8:8+length])
        tensors = {key: value for key, value in header.items() if key != '__metadata__'}
        ranges = []
        for key, tensor in tensors.items():
            if 'lora_' not in key:
                raise ValueError('non-LoRA tensor in adapter weights')
            shape, offsets = tensor['shape'], tensor['data_offsets']
            sizes = {'F64': 8, 'F32': 4, 'F16': 2, 'BF16': 2}
            if (not shape or any(type(size) is not int or size <= 0 for size in shape)
                    or len(offsets) != 2 or any(type(offset) is not int for offset in offsets)
                    or offsets[0] < 0 or offsets[1] > len(raw) - 8 - length
                    or offsets[1] - offsets[0] != math.prod(shape) * sizes[tensor['dtype']]):
                raise ValueError('invalid tensor shape or byte range')
            ranges.append(tuple(offsets))
        ranges.sort()
        if (not tensors or not any('lora_A' in key for key in tensors) or not any('lora_B' in key for key in tensors)
                or ranges[0][0] != 0 or ranges[-1][1] != len(raw) - 8 - length
                or any(first[1] != second[0] for first, second in zip(ranges, ranges[1:]))):
            raise ValueError('missing or inconsistent LoRA tensor payload')
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as problem:
        _reject('reports', f'Adapter weights are not a coherent LoRA safetensors file: {problem}')
    return 'verified'


def _reports(content, reports, config, run_dir):
    from training.finetune import trial_evaluation as evaluation
    references = [config[key] for key in REPORTS if config.get(key) is not None]
    if len(references) != len(set(references)):
        _reject('reports', 'Baseline, tuned, comparison and resource reports require separate files')
    base, tuned, comparison = (reports.get(key) for key in ('baseline_report', 'tuned_report', 'comparison'))
    if base is not None or tuned is not None:
        expected = _cohort(run_dir)
        if base is not None:
            _report(base, arm='base', expected=expected, config=config)
        if tuned is not None:
            if base is None or base.get('state') != 'finished':
                _reject('reports', 'Adapted evaluation requires a completed baseline')
            _report(tuned, arm='adapter', expected=expected, config=config)
    adapter_evidence = _adapter(content, config, required=tuned is not None or config['state'] == 'finished')
    try:
        recomputed = evaluation.compare(base, tuned)
    except ValueError as problem:
        recomputed = {'schema': 'source-trial-comparison/1', 'state': 'partial', 'security_evaluation': False,
                      'loss_delta_tuned_minus_base': None, 'detail': str(problem)}
    extra = {'adapter_evidence': adapter_evidence}
    if adapter_evidence == 'unavailable_in_compact_recovery':
        if comparison is not None and (comparison.get('schema') != 'source-trial-comparison/1'
                or comparison.get('security_evaluation') is not False):
            _reject('reports', 'Recovered comparison must remain an experimental source trial report')
        extra['reported_comparison'] = comparison
        reports['comparison'] = {**recomputed, 'state': 'partial', 'loss_delta_tuned_minus_base': None,
            'adapter_evidence': adapter_evidence, 'missing_phases': ['adapter_weights_not_in_compact_recovery'],
            'interpretation': 'Recovered raw source reports. Adapter weights were omitted from the compact recovery archive; no verified improvement claim is available.'}
    elif comparison is not None and not _equivalent_metrics(comparison, recomputed):
        _reject('reports', 'The comparison differs from the recomputed paired raw evidence')
    if config['state'] == 'finished' and (base is None or tuned is None or comparison is None
            or recomputed['state'] != 'finished'):
        _reject('reports', 'A finished trial requires a completed authentic paired comparison')
    return extra


def list_imports(run_dir):
    imports = []
    for path in (Path(run_dir) / 'trial_imports').glob('*/import.json'):
        record = util.read_json(path)
        if isinstance(record, dict) and record.get('schema') == SCHEMA:
            imports.append(record)
    return sorted(imports, key=lambda row: (row.get('imported_at', ''), row.get('import_id', '')))


@guarded
def import_outputs(path, run_dir, *, dry_run=False, max_bytes=MAX_BYTES):
    """Verify everything before publishing under trial_imports, without promotion."""
    run_dir = Path(run_dir)
    # Snapshot the source file so an external writer cannot change the verified
    # bytes between hash computation, checks and publication.
    with tempfile.TemporaryDirectory(prefix='.trial-import-', dir=run_dir) as scratch:
        staged = Path(scratch) / 'trial_outputs.zip'
        with open(path, 'rb') as source, staged.open('wb') as target:
            copied = 0
            while block := source.read(1 << 20):
                copied += len(block)
                if copied > max_bytes:
                    _reject('zip_structure', 'The compressed archive exceeds the size limit')
                target.write(block)
        content = _read_archive(staged, max_bytes)
        config = _json(content['trial_config.json'], 'trial_config', 'trial_config.json')
        if (config.get('schema') != 'source-trial/1' or config.get('mode') != 'trial'
                or config.get('state') not in ('finished', 'partial', 'blocked', 'failed')
                or config.get('experimental') is not True or config.get('security_evaluation') is not False
                or config.get('counts_as_full_training') is not False or type(config.get('software_test')) is not bool):
            _reject('trial_config', 'Expected an experimental source-trial/1 with no training or security evaluation promotion')
        budget = config.get('total_budget_minutes')
        if type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0:
            _reject('trial_config', 'A trial needs a positive finite total budget')
        manifest_sha = _identity(run_dir, config)
        _runtime_recipe(run_dir, config, content)
        reports = {}
        for key in REPORTS:
            name = config.get(key)
            if name is None:
                reports[key] = None
                continue
            if not isinstance(name, str) or name not in content or name in (MANIFEST, 'trial_config.json'):
                _reject('reports', f'{key} names a missing or invalid report')
            if len(content[name]) > 16 * 2**20:
                _reject('reports', f'{key} exceeds the report size limit')
            reports[key] = _json(content[name], 'reports', name)
        if config['state'] == 'finished' and any(reports[key] is None for key in ('baseline_report', 'tuned_report', 'comparison')):
            _reject('reports', 'A finished trial must include both paired reports and their comparison')
        evidence = _reports(content, reports, config, run_dir)
        digest = _sha_file(staged)
        import_id = digest[:16]
        record = {'schema': SCHEMA, 'import_id': import_id, 'archive_sha256': digest,
            **evidence,
            'bundle_manifest_sha256': manifest_sha, 'experimental': True,
            'counts_as_full_training': False, 'security_evaluation': False,
            'state': config['state'], 'mode': 'trial', 'software_test': config['software_test'],
            'total_budget_minutes': budget, 'config': config, 'reports': reports,
            'imported_at': util.now_utc(), 'already_imported': False}
        destination = run_dir / 'trial_imports' / import_id
        if destination.parent.is_symlink() or (destination.parent.exists() and getattr(destination.parent.lstat(), 'st_file_attributes', 0) & 0x400):
            _reject('identity', 'Linked trial import storage is protected')
        if dry_run:
            return {**record, 'already_imported': (destination / 'import.json').is_file()}
        with FileLock(run_dir / '.trial-import.lock', note=False):
            existing = util.read_json(destination / 'import.json')
            if existing:
                if existing.get('archive_sha256') != digest:
                    _reject('identity', 'Trial import ID collision')
                return {**existing, 'already_imported': True}
            destination.parent.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.publishing-', dir=destination.parent) as work:
                folder = Path(work) / 'result'
                folder.mkdir()
                for name, raw in content.items():
                    target = folder / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                (folder / 'import.json').write_text(json.dumps(record, indent=1) + '\n', encoding='utf-8')
                os.replace(folder, destination)
        from . import finetune_catalog
        finetune_catalog.invalidate(run_dir.parent, run_dir.name)
        return record
