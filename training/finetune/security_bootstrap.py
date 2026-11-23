"""Standard-library remote supervisor, embedded before dependency setup.

The notebook and child share one absolute monotonic deadline. Failures retain
small reports without exporting model tensors or claiming security accuracy.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
import zipfile

MAX_FILES = 4096
MAX_BYTES = 256 << 20
MAX_JSON = 16 << 20


def _write(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def _check_deadline(deadline=None, clock=time.monotonic):
    if deadline is not None and clock() >= deadline:
        raise subprocess.TimeoutExpired('artifact_verification', 0)


def _hash(path, *, deadline=None, clock=time.monotonic):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            _check_deadline(deadline, clock)
            digest.update(block)
    return digest.hexdigest()


def _safe(name):
    if not isinstance(name, str) or not name or len(name) > 1024:
        return False
    rel = PurePosixPath(name)
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    return (not rel.is_absolute() and rel.as_posix() == name and '\\' not in name
            and all(part not in ('.', '..') and ':' not in part and not part.endswith(('.', ' '))
                    and part.split('.')[0].upper() not in reserved
                    and all(ord(char) >= 32 for char in part) for part in rel.parts))


def unpack(path, dest, expected_sha256, *, manifest_name='security_runtime_manifest.json',
           schema='security-comparison-runtime/1', deadline=None, clock=time.monotonic):
    """Authenticate bounded archive bytes before writing any safe member."""
    if _hash(path, deadline=deadline, clock=clock) != expected_sha256:
        raise ValueError('Uploaded artifact SHA256 differs from its pinned identity')
    dest = Path(dest).resolve()
    if dest.exists() and any(dest.iterdir()):
        raise ValueError('Artifact extraction requires a fresh directory')
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist(); names = [entry.filename for entry in infos]
        if (len(names) > MAX_FILES or sum(entry.file_size for entry in infos) > MAX_BYTES
                or len(set(name.casefold() for name in names)) != len(names)
                or any(not _safe(name) or entry.is_dir() or entry.flag_bits & 1
                       or stat.S_ISLNK(entry.external_attr >> 16) for name, entry in zip(names, infos))):
            raise ValueError('Unsafe artifact path or inventory exceeds bounds')
        if manifest_name not in names or archive.getinfo(manifest_name).file_size > MAX_JSON:
            raise ValueError('Artifact manifest is absent or exceeds bounds')
        manifest = json.loads(archive.read(manifest_name))
        files = manifest.get('files')
        if manifest.get('schema') != schema or not isinstance(files, dict) or set(names) != set(files) | {manifest_name}:
            raise ValueError('Artifact manifest inventory differs')
        for name, identity in files.items():
            if (not isinstance(identity, dict) or type(identity.get('bytes')) is not int
                    or identity['bytes'] != archive.getinfo(name).file_size):
                raise ValueError('Artifact member byte identity differs: ' + name)
            digest, count = hashlib.sha256(), 0
            with archive.open(name) as stream:
                for block in iter(lambda: stream.read(1 << 20), b''):
                    _check_deadline(deadline, clock)
                    count += len(block); digest.update(block)
                    if count > identity['bytes']:
                        raise ValueError('Artifact member exceeds declared size: ' + name)
            if count != identity['bytes'] or digest.hexdigest() != identity.get('sha256'):
                raise ValueError('Artifact member SHA256 differs: ' + name)
        dest.mkdir(parents=True, exist_ok=True)
        for name in names:
            _check_deadline(deadline, clock)
            target = dest.joinpath(*PurePosixPath(name).parts)
            if not target.resolve().is_relative_to(dest):
                raise ValueError('Artifact path leaves extraction root')
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output, 1 << 20)
    return manifest


def find_upload(expected_sha256, path=None, *, deadline=None, clock=time.monotonic):
    if path:
        target = Path(path).expanduser().resolve()
        if target.is_file() and _hash(target, deadline=deadline, clock=clock) == expected_sha256:
            return target
        raise ValueError('Uploaded file does not match its pinned SHA256: ' + str(target))
    seen = set()
    for root in (Path.cwd(), Path('/content'), Path('/kaggle/input'), Path('/kaggle/working')):
        if not root.is_dir():
            continue
        candidates = root.rglob('*.zip') if str(root) == '/kaggle/input' else root.glob('*.zip')
        for candidate in candidates:
            _check_deadline(deadline, clock)
            candidate = candidate.resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            if len(seen) > 400:
                break
            if candidate.is_file() and _hash(candidate, deadline=deadline, clock=clock) == expected_sha256:
                return candidate
    raise ValueError('Upload the matching ZIP before running, or set its full notebook path')


def _process(command, *, timeout, stdout, stderr, check=False, **kwargs):
    options = ({'start_new_session': True} if os.name != 'nt' else
               {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW})
    child = subprocess.Popen(command, stdout=stdout, stderr=stderr, **options, **kwargs)
    try:
        return subprocess.CompletedProcess(command, child.wait(timeout=timeout))
    except BaseException:
        if child.poll() is None:
            if os.name == 'nt':
                try:
                    subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], capture_output=True, timeout=5, check=False)
                except (subprocess.SubprocessError, OSError):
                    pass
                if child.poll() is None:
                    child.kill()
            else:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.wait(timeout=5)
        raise


def detect_runtime():
    tpu = any(os.environ.get(key) for key in ('COLAB_TPU_ADDR', 'TPU_NAME', 'TPU_ACCELERATOR_TYPE', 'TPU_WORKER_ID'))
    tpu = tpu or os.environ.get('PJRT_DEVICE', '').upper() == 'TPU' or bool(list(Path('/dev').glob('accel*')))
    if tpu:
        return {'kind': 'tpu', 'hardware_validation': 'not_run'}
    if Path('/dev/nvidiactl').exists() or Path('/proc/driver/nvidia/version').exists() or os.environ.get('COLAB_GPU') == '1':
        return {'kind': 'cuda', 'hardware_validation': 'not_run'}
    return {'kind': 'cpu', 'hardware_validation': 'not_run'}


def _export(output, status):
    """Always write a bounded compact recovery inventory, never model tensors."""
    included, inventory, total = [], [], 0
    forbidden = {'.zip', '.tmp', '.safetensors', '.pt', '.bin', '.onnx', '.gguf'}
    for path in sorted(output.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        name = path.relative_to(output).as_posix()
        if path.name in ('bootstrap_status.json', 'security_bootstrap_manifest.json') or path.suffix in ('.tmp', '.zip'):
            continue
        size = path.stat().st_size
        keep = (path.suffix not in forbidden and not set(PurePosixPath(name).parts) & {'adapter', 'checkpoints', 'models'}
                and size <= 16 << 20 and total + size <= 32 << 20)
        inventory.append({'path': name, 'bytes': size, 'included_in_archive': keep})
        if keep:
            included.append(name); total += size
    status['recovery'] = {'state': 'files_preserved', 'workspace': str(output), 'files': inventory,
                          'compact_archive_limit_bytes': 32 << 20, 'validation': 'not_validated'}
    _write(output / 'bootstrap_status.json', status)
    included.append('bootstrap_status.json')
    files = {name: {'sha256': _hash(output / name), 'bytes': (output / name).stat().st_size} for name in included}
    _write(output / 'security_bootstrap_manifest.json', {'schema': 'security-comparison-bootstrap-outputs/1', 'files': files})
    archive = output / 'security_comparison_outputs.zip'
    with zipfile.ZipFile(archive.with_suffix('.zip.tmp'), 'w', zipfile.ZIP_DEFLATED) as target:
        for name in sorted(files):
            target.write(output / name, name)
        target.write(output / 'security_bootstrap_manifest.json', 'security_bootstrap_manifest.json')
    os.replace(archive.with_suffix('.zip.tmp'), archive)
    return archive


def run(runtime_zip, expected_sha256, *, adapter_source, output_root, total_budget_minutes=30,
        deadline=None, started_monotonic=None, evaluation_dir=None, evaluation_manifest_sha256=None,
        tiny=False, python=None, clock=time.monotonic, run_process=_process, detect_runtime=detect_runtime):
    if not math.isfinite(total_budget_minutes) or total_budget_minutes <= 0:
        raise ValueError('Comparison budget must be positive and finite')
    entered = clock()
    started = entered if started_monotonic is None else started_monotonic
    deadline = min(started + total_budget_minutes * 60, deadline) if deadline is not None else started + total_budget_minutes * 60
    workspace = Path(output_root).expanduser().resolve() / ('security-comparison-' + uuid.uuid4().hex)
    output, runtime, original = workspace / 'outputs', workspace / 'runtime', workspace / 'original_bundle'
    output.mkdir(parents=True)
    status = {'schema': 'security-comparison-bootstrap/1', 'state': 'initializing', 'phase': 'runtime_verification',
              'mode': 'independent_benchmark' if evaluation_dir else 'development_probe', 'security_accuracy_permitted': False,
              'experimental': True, 'total_budget_minutes': total_budget_minutes,
              'started_monotonic': started, 'deadline_monotonic': deadline, 'runtime_sha256': expected_sha256}
    archive = output / 'security_comparison_outputs.zip'
    try:
        upload = find_upload(expected_sha256, runtime_zip, deadline=deadline, clock=clock)
        unpack(upload, runtime, expected_sha256, deadline=deadline, clock=clock)
        spec = json.loads((runtime / 'security_runtime_config.json').read_text(encoding='utf-8'))
        if spec.get('schema') != 'security-comparison-launch/1':
            raise ValueError('Unsupported security comparison launch schema')
        status['phase'] = 'adapter_verification'
        adapter = find_upload(spec['adapter_archive_sha256'], adapter_source, deadline=deadline, clock=clock)
        status['adapter_archive_sha256'] = spec['adapter_archive_sha256']
        status['adapter_tensor_sha256'] = spec['adapter_tensor_sha256']
        with zipfile.ZipFile(adapter) as source:
            info = source.getinfo('trial_config.json')
            if info.file_size > MAX_JSON:
                raise ValueError('Adapter trial configuration exceeds bounds')
            adapter_config = json.loads(source.read(info))
        if adapter_config.get('runtime_recipe') != spec['runtime_recipe']:
            raise ValueError('Adapter runtime recipe differs from pinned original experiment')
        status['phase'] = 'original_bundle_verification'
        training = spec['training_bundle']
        if not _safe(training['path']):
            raise ValueError('Unsafe original bundle path')
        unpack(runtime / training['path'], original, training['sha256'], manifest_name='bundle_manifest.json',
               schema='training-bundle/1', deadline=deadline, clock=clock)
        if _hash(original / 'bundle_manifest.json', deadline=deadline, clock=clock) != training['manifest_sha256']:
            raise ValueError('Original training bundle manifest identity differs')
        status['training_bundle'] = training
        probe = spec['development_probe']
        if not _safe(probe['path']) or _hash(runtime / probe['path'] / 'manifest.json', deadline=deadline, clock=clock) != probe['manifest_sha256']:
            raise ValueError('Development probe manifest identity differs')
        status['development_probe'] = probe
        status['phase'] = 'runtime_detection'
        detection = detect_runtime(); status['runtime_detection'] = detection
        if not tiny and detection.get('kind') != spec['runtime_recipe']['backend']:
            status.update(state='blocked', reason='runtime_recipe_mismatch',
                detail='This adapter was trained with CUDA NF4. Select a CUDA GPU runtime. TPU/CPU migration requires a separate declared experiment and is not performed.')
        else:
            environment = dict(os.environ); environment['PYTHONPATH'] = str(runtime)
            environment['ZTSA_SECURITY_BOOTSTRAP'] = str(output / 'bootstrap_status.json')
            executable = python or sys.executable
            status['phase'] = 'dependency_setup'
            _write(output / 'bootstrap_status.json', status)
            requirements = spec.get('requirements') or []
            if requirements:
                remaining = deadline - clock() - 20
                if remaining <= 0:
                    raise subprocess.TimeoutExpired('dependency_setup', 0)
                with (output / 'setup.log').open('w', encoding='utf-8') as log:
                    result = run_process([executable, '-m', 'pip', 'install', '-q', *requirements],
                        timeout=remaining, stdout=log, stderr=subprocess.STDOUT, env=environment, check=False)
                if result.returncode:
                    raise RuntimeError('Dependency installation failed. See setup.log.')
            status['phase'] = 'comparison_execution'
            remaining = deadline - clock() - 10
            if remaining <= 0:
                raise subprocess.TimeoutExpired('comparison_execution', 0)
            child_output = output / 'comparison'
            command = [executable, '-u', '-m', 'training.finetune.security_comparison', '--bundle-dir', str(original),
                '--adapter-source', str(adapter), '--out-dir', str(child_output), '--total-budget-minutes', str(total_budget_minutes),
                '--deadline', str(deadline), '--max-views', str(spec.get('max_views', 24))]
            if evaluation_dir:
                if not evaluation_manifest_sha256:
                    raise ValueError('Independent benchmark requires its pinned manifest SHA256')
                command += ['--evaluation-dir', str(Path(evaluation_dir).resolve()), '--evaluation-manifest-sha256', evaluation_manifest_sha256]
            else:
                command += ['--development-probe', str(runtime / probe['path']), '--probe-manifest-sha256', probe['manifest_sha256']]
            if tiny:
                command.append('--tiny')
            with (output / 'runtime.log').open('w', encoding='utf-8') as log:
                result = run_process(command, cwd=runtime, env=environment, timeout=remaining, stdout=log,
                                     stderr=subprocess.STDOUT, check=False)
            child_status = next((path for path in (child_output / 'security_comparison.json', child_output / 'comparison_status.json') if path.is_file()), None)
            if child_status:
                child = json.loads(child_status.read_text(encoding='utf-8'))
                status['child_state'] = child.get('state')
                status['child_exit_code'] = result.returncode
                status['state'] = child.get('state') if child.get('state') in ('finished', 'blocked', 'partial', 'failed') else 'partial'
                status['child_reason_code'] = child.get('reason_code')
                if result.returncode and status['state'] == 'finished':
                    status.update(state='failed', reason='nonzero_child_exit',
                                  detail='The child process failed despite leaving a finished status. Retained reports require review.')
            else:
                status.update(state='failed', reason='missing_child_status', detail=f'Child exited {result.returncode} without a comparison status.')
    except subprocess.TimeoutExpired:
        status.update(state='partial', reason=status['phase'] + '_deadline', detail='The shared comparison deadline stopped this phase.')
    except Exception as error:
        status.update(state='blocked' if status['phase'] in ('runtime_verification', 'adapter_verification', 'original_bundle_verification') else 'failed',
                      reason=status['phase'] + '_' + type(error).__name__, detail=str(error))
    finally:
        status.update(elapsed_seconds=clock() - started, remaining_seconds=max(0., deadline - clock()))
        archive = _export(output, status)
    return {'run_dir': str(output), 'state': status['state'], 'archive': str(archive)}
