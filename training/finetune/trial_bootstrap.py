"""Standard-library supervisor embedded into trial notebooks before setup."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import signal
import sys
import time
import uuid
import zipfile


def _process(command, *, timeout, stdout, stderr, check=False, **kwargs):
    """One owned process group, bounded wait, and live notebook log output."""
    options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    process = subprocess.Popen(command, stdout=stdout, stderr=stderr, **options, **kwargs)
    deadline = time.monotonic() + timeout
    offset = 0
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                code = process.wait(timeout=min(1., remaining))
                finished = True
            except subprocess.TimeoutExpired:
                finished = False
            with Path(stdout.name).open('r', encoding='utf-8', errors='replace') as stream:
                stream.seek(offset)
                output = stream.read()
                offset = stream.tell()
            if output:
                print(output, end='', flush=True)
            if finished:
                return subprocess.CompletedProcess(command, code)
    except BaseException:
        if process.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=5, check=False)
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        raise


def find_bundle(expected_sha256, bundle_path=None):
    if bundle_path:
        return Path(bundle_path).expanduser().resolve()
    candidates = []
    for root in (Path.cwd(), Path('/content'), Path('/kaggle/input'), Path('/kaggle/working')):
        if root.is_dir():
            for path in root.rglob('*.zip'):
                candidates.append(path)
                if len(candidates) >= 1000:
                    break
        if len(candidates) >= 1000:
            break
    for path in dict.fromkeys(candidates):
        if path.is_file() and _hash(path) == expected_sha256:
            return path.resolve()
    raise ValueError('Upload the matching trial bundle or set BUNDLE_PATH to its full path')


def _write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def unpack(path, dest, expected_sha256):
    if _hash(path) != expected_sha256:
        raise ValueError('The uploaded bundle SHA256 differs from this notebook')
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate bundle members')
        manifest = json.loads(archive.read('bundle_manifest.json'))
        if set(names) != set(manifest['files']) | {'bundle_manifest.json'}:
            raise ValueError('Bundle inventory differs from its manifest')
        for name in names:
            rel = PurePosixPath(name)
            if (not name or rel.is_absolute() or '..' in rel.parts or '\\' in name or ':' in name
                    or not dest.joinpath(*rel.parts).resolve().is_relative_to(dest.resolve())):
                raise ValueError('Unsafe bundle path')
        for name, info in manifest['files'].items():
            raw = archive.read(name)
            if len(raw) != info['bytes'] or hashlib.sha256(raw).hexdigest() != info['sha256']:
                raise ValueError('Bundle member changed: ' + name)
        for name in names:
            target = dest.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output, 1 << 20)
    return manifest


def _failure_archive(output, config):
    """Export truthful bootstrap failures even when dependencies never installed."""
    previous = output / 'trial_config.json'
    if previous.is_file():
        try:
            child = json.loads(previous.read_text(encoding='utf-8'))
            if child.get('schema') == 'source-trial/1' and child.get('mode') == 'trial':
                config = {**config, **child, 'state': config['state'], 'reason': config.get('reason'),
                          'detail': config.get('detail'), 'elapsed_seconds': config.get('elapsed_seconds'),
                          'remaining_seconds': config.get('remaining_seconds')}
        except (ValueError, OSError):
            pass
    for key in ('baseline_report', 'tuned_report', 'comparison', 'resources'):
        name = config.get(key)
        if name and (not isinstance(name, str) or not (output / name).is_file()):
            config[key] = None
    retained = []
    included = []
    size = 0
    # Do not spend the deadline's last seconds compressing multi-GB optimizer
    # tensors. Preserve every file in the remote workspace and include compact
    # recovery files with explicit inventory of large, downloadable remainders.
    for path in sorted(output.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        name = path.relative_to(output).as_posix()
        if path.suffix in ('.tmp', '.zip') or path.name in ('trial_outputs_manifest.json', 'trial_config.json') or '.tmp' in name:
            continue
        n = path.stat().st_size
        keep = size + n <= 64 << 20
        retained.append({'path': name, 'bytes': n, 'included_in_archive': keep})
        if keep:
            included.append(name)
            size += n
    config['recovery'] = {'state': 'files_preserved', 'workspace': str(output), 'files': retained,
                          'validation': 'not_validated', 'compact_archive_limit_bytes': 64 << 20}
    _write(output / 'trial_config.json', config)
    included.append('trial_config.json')
    files = {name: {'sha256': _hash(output / name), 'bytes': (output / name).stat().st_size} for name in included}
    _write(output / 'trial_outputs_manifest.json', {'schema': 'trial-outputs/1', 'files': files})
    with zipfile.ZipFile(output / 'trial_outputs.zip.tmp', 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            archive.write(output / name, name)
        archive.write(output / 'trial_outputs_manifest.json', 'trial_outputs_manifest.json')
    os.replace(output / 'trial_outputs.zip.tmp', output / 'trial_outputs.zip')


def run(bundle_zip, expected_sha256, *, requirements, output_root, total_budget_minutes=30,
        parent_run_id=None, python=None, clock=time.monotonic, run_process=_process,
        tpu_requirements=None, allow_experimental_tpu=False):
    import math
    if not math.isfinite(total_budget_minutes) or total_budget_minutes <= 0:
        raise ValueError('Trial budget must be positive and finite')
    started = clock()
    deadline = started + total_budget_minutes * 60
    workspace = Path(output_root).expanduser().resolve() / ('trial-' + uuid.uuid4().hex)
    output, bundle = workspace / 'outputs', workspace / 'bundle'
    output.mkdir(parents=True)
    bundle.mkdir()
    config = {'schema': 'source-trial/1', 'mode': 'trial', 'state': 'initializing', 'experimental': True,
              'security_evaluation': False, 'counts_as_full_training': False, 'counts_as_evaluation': False,
              'software_test': False, 'parent_run_id': parent_run_id, 'total_budget_minutes': total_budget_minutes,
              'baseline_report': None, 'tuned_report': None, 'comparison': None, 'resources': None}
    setup_phase = 'bundle_verification'
    try:
        raw_zip = find_bundle(expected_sha256, bundle_zip)
        with zipfile.ZipFile(raw_zip) as archive:
            payload_bytes = sum(info.file_size for info in archive.infolist())
        free = shutil.disk_usage(workspace).free
        if free < payload_bytes * 2 + (256 << 20):
            config.update(state='blocked', reason='insufficient_extraction_storage')
            return {'run_dir': str(output), 'state': config['state']}
        manifest = unpack(raw_zip, bundle, expected_sha256)
        resolved = json.loads((bundle / 'resolved_config.json').read_text(encoding='utf-8'))
        model = resolved['config']['model']
        config.update(model=model['id'], model_revision=model['revision'], tokenizer_revision=model['tokenizer_revision'],
                      dataset={'version': resolved['dataset']['version'], 'manifest_sha256': resolved['dataset']['manifest_sha256']},
                      bundle={'manifest_sha256': _hash(bundle / 'bundle_manifest.json')},
                      resolved_config_sha256=_hash(bundle / 'resolved_config.json'))
        shutil.copyfile(bundle / 'resolved_config.json', output / 'resolved_config.json')
        compatibility = None
        compatibility_source = bundle / 'training/finetune/runtime_compatibility.py'
        if compatibility_source.is_file():
            compatibility = {'__name__': 'trial_compatibility_bootstrap'}
            exec(compile(compatibility_source.read_text(encoding='utf-8'), str(compatibility_source), 'exec'), compatibility)
        detection = compatibility['detect_runtime']() if compatibility else {'kind': 'cuda'}
        config['runtime_detection'] = detection
        if detection['kind'] == 'tpu' and not allow_experimental_tpu:
            config.update(state='blocked', reason='experimental_tpu_recipe_not_authorized',
                          detail='Use a notebook explicitly enabling the separate experimental TPU INT4 recipe.')
            return {'run_dir': str(output), 'state': config['state'], 'archive': str(output / 'trial_outputs.zip')}
        plan = (compatibility['requirements_for_runtime'](detection['kind'], requirements, tpu_requirements or []) if compatibility
                else {'isolated': False, 'requirements': requirements, 'environment': {}, 'torch_requirement': None})
        if plan['isolated'] and (not sys.platform.startswith('linux') or not (3, 11) <= sys.version_info[:2] <= (3, 13)):
            config.update(state='blocked', reason='tpu_python_platform_incompatible',
                          detail='Pinned experimental TPU wheels require Linux x86_64 CPython 3.11, 3.12 or 3.13.')
            return {'run_dir': str(output), 'state': config['state'], 'archive': str(output / 'trial_outputs.zip')}
        _write(output / 'runtime_setup.json', {'detection': detection, 'dependency_plan': plan,
                                             'allow_experimental_tpu': allow_experimental_tpu})
        resource_source = bundle / 'training/finetune/runtime_resources.py'
        if resource_source.is_file():
            namespace = {'__name__': 'trial_storage_bootstrap', '__file__': str(resource_source)}
            exec(compile(resource_source.read_text(encoding='utf-8'), str(resource_source), 'exec'), namespace)
            storage_cfg = json.loads(json.dumps(resolved['config']))
            if detection['kind'] == 'tpu':
                storage_cfg.setdefault('precision', {})['mode'] = 'tpu_int4_bf16'
            storage = namespace['storage_preflight'](bundle, output, storage_cfg)
            _write(output / 'resource_report.json', storage)
            config['resources'] = 'resource_report.json'
            if storage['state'] != 'ready':
                config.update(state='blocked', reason='insufficient_storage_before_setup', detail=storage.get('reasons'))
                return {'run_dir': str(output), 'state': config['state'], 'archive': str(output / 'trial_outputs.zip')}
        _write(output / 'bootstrap.json', {'schema': 'source-trial-bootstrap/1', 'started_monotonic': started,
               'deadline_monotonic': deadline, 'parent_run_id': parent_run_id, 'total_budget_minutes': total_budget_minutes,
               'bundle_zip_sha256': expected_sha256, 'workspace': str(workspace), 'extracted_bytes': payload_bytes})
        executable = python or sys.executable
        setup_phase = 'dependency_setup'
        environment = dict(os.environ)
        environment.update(plan['environment'])
        environment['PYTHONPATH'] = str(bundle)
        environment['ZTSA_TRIAL_BOOTSTRAP'] = str(output / 'bootstrap.json')
        requirements = [line.strip() for line in plan['requirements'] if line.strip() and not line.lstrip().startswith('#')]
        with (output / 'setup.log').open('w', encoding='utf-8') as log:
            commands = []
            if plan['isolated']:
                venv = workspace / 'tpu-env'
                commands.append([executable, '-m', 'venv', str(venv)])
                executable = str(venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))
                commands.append([executable, '-m', 'pip', 'install', '-q', plan['torch_requirement'], '--index-url', plan['torch_index']])
            if requirements:
                commands.append([executable, '-m', 'pip', 'install', '-q', *requirements])
            for command in commands:
                remaining = deadline - clock() - 20
                if remaining <= 0:
                    raise subprocess.TimeoutExpired('dependency_setup', 0)
                result = run_process(command, timeout=remaining, stdout=log, stderr=subprocess.STDOUT,
                                     env=environment, check=False)
                if result.returncode:
                    raise RuntimeError('Dependency installation failed; see setup.log')
        setup_phase = 'trial_execution'
        remaining = deadline - clock() - 10
        if remaining <= 0:
            raise subprocess.TimeoutExpired('trial_execution', 0)
        with (output / 'runtime.log').open('w', encoding='utf-8') as log:
            command = [executable, '-u', '-m', 'training.finetune.trial', '--bundle', str(bundle), '--run-dir', str(output),
                       '--total-budget-minutes', str(total_budget_minutes), '--deadline-monotonic', str(deadline)]
            if allow_experimental_tpu:
                command.append('--allow-experimental-tpu')
            result = run_process(command,
                                 cwd=bundle, env=environment, timeout=remaining, stdout=log, stderr=subprocess.STDOUT, check=False)
        if (output / 'trial_config.json').exists() and (output / 'trial_outputs.zip').exists():
            config = json.loads((output / 'trial_config.json').read_text(encoding='utf-8'))
            config['bootstrap_elapsed_seconds'] = clock() - started
            return {'run_dir': str(output), 'state': config['state'], 'archive': str(output / 'trial_outputs.zip')}
        raise RuntimeError(f'Trial process exited with {result.returncode} without authenticated output')
    except subprocess.TimeoutExpired:
        config.update(state='partial', reason=setup_phase + '_deadline', detail='The shared trial deadline stopped this phase.')
    except Exception as error:
        config.update(state='failed', reason=type(error).__name__, detail=str(error))
    finally:
        if not (output / 'trial_outputs.zip').exists():
            config.update(elapsed_seconds=clock() - started, remaining_seconds=max(0., deadline - clock()), phase=setup_phase)
            _failure_archive(output, config)
    return {'run_dir': str(output), 'state': config['state'], 'archive': str(output / 'trial_outputs.zip')}
