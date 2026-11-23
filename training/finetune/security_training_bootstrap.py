"""Standard-library, setup-inclusive supervisor for a fresh security adapter trial.

This source is embedded verbatim in the remote notebook. It imports no training
dependency and never treats a child process's nonzero exit as success.
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

SCHEMA = 'security-training-trial/1'
STATUS_FILE = 'security_trial.json'
ARCHIVE_FILE = 'security_trial_outputs.zip'


def _check_deadline(clock, deadline):
    if deadline is not None and clock() >= deadline:
        raise subprocess.TimeoutExpired('bundle_verification', 0)


def _hash(path, *, clock=time.monotonic, deadline=None):
    _check_deadline(clock, deadline)
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            _check_deadline(clock, deadline)
            digest.update(block)
    return digest.hexdigest()


def _write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def _safe_name(name):
    rel = PurePosixPath(name)
    if (not name or rel.is_absolute() or '..' in rel.parts or '\\' in name or ':' in name
            or rel.as_posix() != name or not rel.parts):
        raise ValueError('Unsafe bundle path: ' + name)
    return rel


def unpack(path, dest, expected_sha256, *, clock=time.monotonic, deadline=None):
    """Verify every member before extraction; stream hashing and copying."""
    dest = Path(dest).resolve()
    actual = _hash(path, clock=clock, deadline=deadline)
    if actual != expected_sha256:
        raise ValueError(f'The uploaded bundle SHA256 differs from this notebook (found {actual[:16]}, expected {expected_sha256[:16]}). ' + BUNDLE_GUIDANCE)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate bundle members')
        for item in archive.infolist():
            rel = _safe_name(item.filename)
            if (stat.S_ISLNK(item.external_attr >> 16) or item.is_dir()
                    or not dest.joinpath(*rel.parts).resolve().is_relative_to(dest)):
                raise ValueError('Unsafe bundle path: ' + item.filename)
        manifest = json.loads(archive.read('bundle_manifest.json'))
        if manifest.get('schema') != 'training-bundle/1':
            raise ValueError('Unknown training bundle schema')
        for name in manifest['files']:
            _safe_name(name)
        if set(names) != set(manifest['files']) | {'bundle_manifest.json'}:
            raise ValueError('Bundle inventory differs from its manifest')
        for name, expected in manifest['files'].items():
            digest, size = hashlib.sha256(), 0
            with archive.open(name) as source:
                for block in iter(lambda: source.read(1 << 20), b''):
                    _check_deadline(clock, deadline)
                    size += len(block)
                    digest.update(block)
            if size != expected['bytes'] or digest.hexdigest() != expected['sha256']:
                raise ValueError('Bundle member changed: ' + name)
        for name in names:
            _check_deadline(clock, deadline)
            target = dest.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, target.open('wb') as output:
                for block in iter(lambda: source.read(1 << 20), b''):
                    _check_deadline(clock, deadline)
                    output.write(block)
    return manifest


class BundleNotFound(ValueError):
    """No uploaded zip is this notebook's bundle. ``candidates`` lists what was found, so the failure archive can show it."""

    def __init__(self, message, candidates=()):
        super().__init__(message)
        self.candidates = list(candidates)


BUNDLE_GUIDANCE = ('Upload the security_trial_bundle.zip from the SAME delivery ZIP as this notebook (the v4 and v5 deliveries each contain a file with '
                   'that name and different content, and the outer delivery ZIP is not the bundle). Wait for the upload to finish; if the Colab runtime '
                   'was reset the upload is gone, so upload it again in this session. Or set BUNDLE_PATH to its full path. Do not edit the expected hash.')


def _describe(path, expected_sha256, expected_bytes, *, clock, deadline):
    """One zip seen during the search. A zip of another size cannot be the bundle, so it is not read through."""
    size = path.stat().st_size
    entry = {'path': str(path), 'bytes': size, 'sha256_prefix': None, 'zip_valid': zipfile.is_zipfile(path), 'matches': False}
    if expected_bytes is not None and size != expected_bytes:
        return entry
    digest = _hash(path, clock=clock, deadline=deadline)
    entry.update(sha256_prefix=digest[:16], matches=digest == expected_sha256)
    return entry


def _not_found_message(expected_sha256, expected_bytes, searched, found):
    size = f'{expected_bytes} bytes, ' if expected_bytes is not None else ''
    text = f"No uploaded zip is this notebook's security trial bundle (expected {size}sha256 {expected_sha256})."
    if not found:
        text += f" Found no .zip files in: {', '.join(searched) if searched else 'none of the search folders exist'}."
    else:
        text += f' Zip files found ({len(found)}):'
        for number, entry in enumerate(found[:20], 1):
            hashed = f"sha256={entry['sha256_prefix']}" if entry['sha256_prefix'] else 'size differs, not hashed'
            note = ''
            if expected_bytes is not None and entry['bytes'] < expected_bytes and not entry['zip_valid']:
                note = f" | incomplete upload: {expected_bytes - entry['bytes']} bytes missing"
            text += f" {number}. {entry['path']} | bytes={entry['bytes']} | {hashed} | ZIP={'valid' if entry['zip_valid'] else 'invalid'}{note};"
        if len(found) > 20:
            text += f' and {len(found) - 20} more.'
    return text + ' ' + BUNDLE_GUIDANCE


def find_bundle(expected_sha256, bundle_path=None, *, clock=time.monotonic, deadline=None, roots=None, expected_bytes=None):
    _check_deadline(clock, deadline)
    if bundle_path:
        path = Path(bundle_path).expanduser().resolve()
        if not path.is_file():
            raise BundleNotFound(f'Bundle path does not exist: {path}. ' + BUNDLE_GUIDANCE)
        return path
    roots = (Path.cwd(), Path('/content'), Path('/kaggle/input'), Path('/kaggle/working')) if roots is None else tuple(Path(r) for r in roots)
    checked, found, searched = set(), [], []
    for root in roots:
        if not root.is_dir():
            continue
        searched.append(str(root))
        for path in root.rglob('*.zip'):
            _check_deadline(clock, deadline)
            if path in checked:
                continue
            checked.add(path)
            if len(checked) > 1000:
                break
            if not path.is_file():
                continue
            try:
                entry = _describe(path, expected_sha256, expected_bytes, clock=clock, deadline=deadline)
            except OSError:
                continue
            if entry['matches']:
                return path.resolve()
            found.append(entry)
    raise BundleNotFound(_not_found_message(expected_sha256, expected_bytes, searched, found), found)


def _process(command, *, timeout, stdout, stderr, check=False, **kwargs):
    """Bound the entire child process group and mirror logs into the notebook."""
    options = ({'start_new_session': True} if os.name != 'nt' else
               {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW})
    process = subprocess.Popen(command, stdout=stdout, stderr=stderr, **options, **kwargs)
    deadline, offset = time.monotonic() + timeout, 0
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
                output, offset = stream.read(), stream.tell()
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


def export_outputs(output, status, *, compact=False, compact_limit_bytes=64 << 20,
                   clock=time.monotonic, deadline=None, _deadline_pressure=False):
    """Stream an adapter/report archive; preserve optimizer files in the workspace.

    Checkpoint tensors are deliberately inventoried without hashing or compression
    so multi-GB recovery state cannot consume the remaining trial budget. Full
    exports include the entire adapter. Forced recovery uses a bounded compact
    archive and explicitly lists any omitted files for separate download.
    """
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    def guard():
        if deadline is not None and clock() >= deadline - (5 if compact else 0):
            raise subprocess.TimeoutExpired('archive_export', 0)

    def metadata_recovery():
        if status.get('state') == 'finished':
            status.update(state='partial', reason_code='archive_export_deadline')
        return export_outputs(output, status, compact=True, compact_limit_bytes=0,
                              clock=clock, deadline=None, _deadline_pressure=True)

    try:
        guard()
    except subprocess.TimeoutExpired:
        if not compact:
            raise
        return metadata_recovery()
    retained, included, size = [], [], 0
    for path in sorted(output.rglob('*')):
        try:
            guard()
        except subprocess.TimeoutExpired:
            if not compact:
                raise
            return metadata_recovery()
        if not path.is_file() or path.is_symlink():
            continue
        name = path.relative_to(output).as_posix()
        if path.suffix in ('.tmp', '.zip') or name in (STATUS_FILE, 'security_trial_outputs_manifest.json') or '.tmp' in name:
            continue
        n = path.stat().st_size
        checkpoint_tensor = ('checkpoints' in path.relative_to(output).parts
                             and path.suffix in ('.pt', '.pth', '.bin', '.safetensors'))
        keep = not checkpoint_tensor and (not compact or size + n <= compact_limit_bytes)
        retained.append({'path': name, 'bytes': n, 'included_in_archive': keep,
                         'reason': None if keep else 'workspace_checkpoint' if checkpoint_tensor else 'compact_limit'})
        if keep:
            included.append(name)
            size += n
    status['recovery'] = {'state': 'files_preserved', 'workspace': str(output.resolve()), 'files': retained,
                          'validation': 'not_validated', 'compact_archive': compact,
                          'deadline_pressure': _deadline_pressure,
                          'compact_archive_limit_bytes': compact_limit_bytes if compact else None}
    if compact and status.get('state') == 'finished' and any(
            row['path'].startswith('adapter/') and not row['included_in_archive'] for row in retained):
        status.update(state='partial', reason_code='compact_archive_omits_adapter')
    _write(output / STATUS_FILE, status)
    included.append(STATUS_FILE)
    files = {}
    temp = output / (ARCHIVE_FILE + '.tmp')
    try:
        with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for name in sorted(included):
                guard()
                digest, size = hashlib.sha256(), 0
                with (output / name).open('rb') as source, archive.open(name, 'w', force_zip64=True) as target:
                    for block in iter(lambda: source.read(1 << 20), b''):
                        guard()
                        target.write(block)
                        digest.update(block)
                        size += len(block)
                files[name] = {'sha256': digest.hexdigest(), 'bytes': size}
            guard()
            manifest = {'schema': 'security-training-outputs/1', 'files': files,
                        'state': status.get('state'), 'optimizer_checkpoint_storage': 'workspace'}
            archive.writestr('security_trial_outputs_manifest.json', json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    except subprocess.TimeoutExpired:
        if not compact:
            raise
        return metadata_recovery()
    _write(output / 'security_trial_outputs_manifest.json', manifest)
    os.replace(temp, output / ARCHIVE_FILE)
    return str(output / ARCHIVE_FILE)


def _child_status(output):
    try:
        child = json.loads((output / STATUS_FILE).read_text(encoding='utf-8'))
        return child if child.get('schema') == SCHEMA else {}
    except (ValueError, OSError):
        return {}


def _archive_matches_status(output, status):
    """Check the terminal archive inventory without rereading large tensors."""
    try:
        with zipfile.ZipFile(output / ARCHIVE_FILE) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                return False
            for name in names:
                _safe_name(name)
            raw = archive.read(STATUS_FILE)
            if json.loads(raw) != status:
                return False
            manifest = json.loads(archive.read('security_trial_outputs_manifest.json'))
            if manifest.get('schema') != 'security-training-outputs/1':
                return False
            files = manifest['files']
            if set(names) != set(files) | {'security_trial_outputs_manifest.json'}:
                return False
            if files[STATUS_FILE]['sha256'] != hashlib.sha256(raw).hexdigest():
                return False
            return all(archive.getinfo(name).file_size == info['bytes'] for name, info in files.items())
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return False



def _load_setup_source(source):
    namespace = {'__name__': 'security_accelerator_setup', '__file__': str(source)}
    exec(compile(source.read_text(encoding='utf-8'), str(source), 'exec'), namespace)
    return namespace


def _torch_probe(helper, kind, executable, output, name, *, environment, deadline,
                 clock, run_process, include_training_imports=False):
    """Import torch only in a fresh bounded child, preserving even incomplete logs."""
    timeout = min(90., deadline - clock() - 120.)
    if timeout <= 0:
        raise subprocess.TimeoutExpired(name, 0)
    script = helper['torch_probe_script'](kind, include_training_imports=include_training_imports)
    report = {'state': 'running', 'kind': kind, 'interpreter': executable,
              'timeout_seconds': timeout, 'include_training_imports': include_training_imports}
    report_path = output / (name + '.json')
    _write(report_path, report)
    with (output / (name + '.stdout.log')).open('w', encoding='utf-8') as stdout, \
            (output / (name + '.stderr.log')).open('w', encoding='utf-8') as stderr:
        try:
            result = run_process([executable, '-u', '-c', script], env=environment,
                                 timeout=timeout, stdout=stdout, stderr=stderr, check=False)
        except subprocess.TimeoutExpired:
            report.update(state='timeout', error='Fresh accelerator probe exceeded its bounded setup budget')
            _write(report_path, report)
            raise
    try:
        # Native extensions can write directly to stdout despite Python redirects.
        # Parse only a bounded final JSON record and keep the complete original log.
        with (output / (name + '.stdout.log')).open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - (1 << 20)))
            lines = stream.read().decode('utf-8', errors='replace').splitlines()
        value = None
        for line in reversed(lines):
            line = line.removeprefix('ZTSA_TORCH_PROBE_JSON=')
            try:
                candidate = json.loads(line)
            except ValueError:
                continue
            if isinstance(candidate, dict) and 'state' in candidate and 'kind' in candidate:
                value = candidate
                break
        if value is None:
            raise ValueError('No final accelerator JSON object in the bounded stdout tail')
        report.update(value)
    except (ValueError, OSError) as error:
        report.update(state='broken', kernel_smoke=False, error='Invalid fresh probe JSON: ' + str(error))
    if clock() >= deadline - 120:
        report.update(state='timeout', error='Fresh probe consumed the remaining setup budget')
        _write(report_path, report)
        raise subprocess.TimeoutExpired(name, timeout)
    report['returncode'] = result.returncode
    if result.returncode:
        report.update(state='broken', kernel_smoke=False,
                      error=report.get('error') or 'Fresh probe exited nonzero')
    _write(report_path, report)
    return report

def run(bundle_zip, expected_sha256, *, requirements, output_root, total_budget_minutes=30,
        max_steps=32, tiny=False, python=None, started_monotonic=None, deadline=None,
        clock=time.monotonic, run_process=_process, tpu_requirements=None, allow_experimental_tpu=False, expected_bytes=None):
    if not math.isfinite(total_budget_minutes) or total_budget_minutes <= 0:
        raise ValueError('Trial budget must be positive and finite')
    if type(max_steps) is not int or max_steps < 1:
        raise ValueError('max_steps must be a positive integer')
    started = clock() if started_monotonic is None else started_monotonic
    deadline = started + total_budget_minutes * 60 if deadline is None else deadline
    if not math.isfinite(started) or not math.isfinite(deadline) or deadline > started + total_budget_minutes * 60:
        raise ValueError('The absolute deadline must fit the shared trial budget')
    workspace = Path(output_root).expanduser().resolve() / ('security-training-' + uuid.uuid4().hex)
    output, bundle = workspace / 'outputs', workspace / 'bundle'
    output.mkdir(parents=True)
    bundle.mkdir()
    status = {'schema': SCHEMA, 'state': 'initializing', 'phase': 'bundle_verification', 'reason_code': None,
              'steps_done': 0, 'optimizer_updates': 0, 'nonzero_lr_updates': 0, 'fresh_adapter': True,
              'counts_as_evaluation': False, 'counts_as_full_training': False, 'software_test': tiny,
              'total_budget_minutes': total_budget_minutes, 'max_steps': max_steps,
              'expected_bundle': {'sha256': expected_sha256, 'bytes': expected_bytes}}
    phase, child_valid = 'bundle_verification', False
    setup = {'schema': 'security-runtime-setup/2', 'state': 'initializing', 'commands': [],
             'probes': [], 'allow_experimental_tpu': allow_experimental_tpu,
             'software_test': tiny, 'interpreter_before': python or sys.executable}

    def save_setup():
        _write(output / 'runtime_setup.json', setup)
        _write(output / 'environment_report.json', {'schema': 'security-setup-environment/1',
            'state': setup['state'], 'hardware': setup.get('detection'),
            'torch_before': setup.get('torch_before'), 'torch_after': setup.get('torch_after'),
            'interpreter': setup.get('interpreter', setup['interpreter_before']),
            'runtime_setup_report': 'runtime_setup.json', 'software_test': tiny})

    save_setup()
    try:
        if clock() >= deadline - 120:
            raise subprocess.TimeoutExpired(phase, 0)
        raw_zip = find_bundle(expected_sha256, bundle_zip, clock=clock, deadline=deadline-120, expected_bytes=expected_bytes)
        with zipfile.ZipFile(raw_zip) as archive:
            payload_bytes = sum(info.file_size for info in archive.infolist())
        if shutil.disk_usage(workspace).free < payload_bytes * 2 + (256 << 20):
            status.update(state='blocked', reason_code='insufficient_extraction_storage')
            return {'run_dir': str(output), 'state': status['state'], 'archive': str(output / ARCHIVE_FILE)}
        unpack(raw_zip, bundle, expected_sha256, clock=clock, deadline=deadline-120)
        resolved = json.loads((bundle / 'resolved_config.json').read_text(encoding='utf-8'))
        status.update(model=resolved['config']['model'], dataset=resolved['dataset'],
                      bundle_manifest_sha256=_hash(bundle / 'bundle_manifest.json', clock=clock, deadline=deadline-120))
        shutil.copyfile(bundle / 'resolved_config.json', output / 'resolved_config.json')
        environment = dict(os.environ)
        environment.update(PYTHONPATH=str(bundle), PYTHONNOUSERSITE='1',
                           ZTSA_SECURITY_TRIAL_BOOTSTRAP=str(output / 'bootstrap.json'))
        executable = python or sys.executable
        detection, plan = {'kind': 'cpu', 'reason': 'tiny software mode'}, {'state': 'ready', 'isolated': False}
        helper, compatibility = {}, {}
        if not tiny:
            phase = 'hardware_detection'
            source = bundle / 'training/finetune/colab_accelerator.py'
            if not source.is_file():
                status['reason_code'] = 'accelerator_helper_missing'
                raise ValueError('The authenticated real-training bundle must include colab_accelerator.py')
            helper = _load_setup_source(source)
            detection = helper['detect_hardware'](deadline=deadline - 120, clock=clock)
            setup['detection'] = detection
            _write(output / 'physical_hardware.json', detection)
            save_setup()
            if detection['kind'] not in ('cuda', 'tpu'):
                status.update(state='blocked', reason_code='accelerator_hardware_unavailable',
                    detail='No usable physical GPU or TPU was detected. Select an accelerator runtime and rerun. '
                           'Installing torch cannot allocate missing hardware. ' + str(detection.get('reason', '')))
                return {'run_dir': str(output), 'state': status['state'], 'archive': str(output / ARCHIVE_FILE)}
            setup['accelerator_kind'] = detection['kind']
            setup['removed_environment_keys'] = []
            if detection['kind'] == 'cuda':
                # Bind child processes to observed hardware, independently of stale TPU hints.
                for key in ('TPU_NAME', 'COLAB_TPU_ADDR', 'TPU_WORKER_ID', 'TPU_ACCELERATOR_TYPE', 'PJRT_DEVICE'):
                    if key in environment:
                        setup['removed_environment_keys'].append(key)
                        environment.pop(key)
            save_setup()
            if detection['kind'] == 'tpu':
                if not allow_experimental_tpu:
                    status.update(state='blocked', reason_code='experimental_tpu_recipe_not_authorized')
                    return {'run_dir': str(output), 'state': status['state'], 'archive': str(output / ARCHIVE_FILE)}
                if not sys.platform.startswith('linux') or not (3, 11) <= sys.version_info[:2] <= (3, 13):
                    status.update(state='blocked', reason_code='tpu_python_platform_incompatible')
                    return {'run_dir': str(output), 'state': status['state'], 'archive': str(output / ARCHIVE_FILE)}
                source = bundle / 'training/finetune/runtime_compatibility.py'
                if not source.is_file():
                    raise ValueError('The TPU bundle must include the declared matching runtime recipe')
                compatibility = _load_setup_source(source)
                tpu_plan = compatibility['requirements_for_runtime']('tpu', requirements, tpu_requirements or [])
                environment.update(tpu_plan['environment'])
            phase = 'torch_probe_before'
            before = _torch_probe(helper, detection['kind'], executable, output, phase,
                environment=environment, deadline=deadline, clock=clock, run_process=run_process)
            setup['torch_before'] = before
            setup['probes'].append({'name': phase, **before})
            plan = helper['plan_torch'](detection, before)
            setup['dependency_plan'] = plan
            save_setup()
            if plan['state'] == 'blocked':
                status.update(state='blocked', reason_code='accelerator_repair_unavailable', detail=plan['reason'])
                return {'run_dir': str(output), 'state': status['state'], 'archive': str(output / ARCHIVE_FILE)}
        else:
            setup.update(detection=detection, dependency_plan=plan, state='tiny_software_bypass')
            _write(output / 'physical_hardware.json', detection)
            save_setup()
        source = bundle / 'training/finetune/runtime_resources.py'
        if source.is_file() and not tiny:
            resource = _load_setup_source(source)
            cfg = json.loads(json.dumps(resolved['config']))
            if detection['kind'] == 'tpu':
                cfg['precision']['mode'] = 'tpu_int4_bf16'
            storage = resource['storage_preflight'](bundle, output, cfg)
            _write(output / 'resource_report.json', storage)
            if storage['state'] != 'ready':
                status.update(state='blocked', reason_code='insufficient_storage_before_setup', detail=storage.get('reasons'))
                return {'run_dir': str(output), 'state': status['state'], 'archive': str(output / ARCHIVE_FILE)}
        _write(output / 'bootstrap.json', {'schema': 'security-training-bootstrap/1', 'started_monotonic': started,
               'deadline_monotonic': deadline, 'total_budget_minutes': total_budget_minutes,
               'bundle_zip_sha256': expected_sha256, 'workspace': str(workspace), 'export_reserve_seconds': 120,
               'parent_compact_export_reserve_seconds': 30})

        def setup_command(command, log):
            remaining = deadline - clock() - 120
            if remaining <= 0:
                raise subprocess.TimeoutExpired('dependency_setup', 0)
            row = {'command': command, 'timeout_seconds': remaining, 'state': 'running'}
            setup['commands'].append(row)
            save_setup()
            try:
                result = run_process(command, timeout=remaining, stdout=log, stderr=subprocess.STDOUT,
                                     env=environment, check=False)
            except subprocess.TimeoutExpired:
                row['state'] = 'timeout'
                save_setup()
                raise
            row.update(state='finished' if result.returncode == 0 else 'failed', returncode=result.returncode)
            save_setup()
            if result.returncode:
                status.update(reason_code='dependency_setup_nonzero', child_returncode=result.returncode)
                raise RuntimeError('Dependency installation failed; see setup.log')

        if not tiny:
            repaired = False
            while True:
                phase = 'dependency_setup'
                with (output / 'setup.log').open('a', encoding='utf-8') as log:
                    if plan['isolated']:
                        if repaired:
                            raise RuntimeError('Only one isolated accelerator repair is allowed')
                        repaired = True
                        venv = workspace / (detection['kind'] + '-env')
                        setup_command([python or sys.executable, '-m', 'venv', str(venv)], log)
                        executable = str(venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))
                        # Prevent external pip/user-site configuration from defeating isolation.
                        for key in ('PYTHONHOME', 'PYTHONUSERBASE', 'PIP_TARGET', 'PIP_PREFIX', 'PIP_USER'):
                            environment.pop(key, None)
                        setup['interpreter'] = executable
                        setup_command([executable, '-m', 'pip', 'install', '-q', plan['torch_requirement'],
                                       '--index-url', plan['index_url']], log)
                    constraint = plan.get('torch_requirement') or ('torch==' + before['torch_version'])
                    if '\n' in constraint or '\r' in constraint or not constraint.startswith('torch=='):
                        raise ValueError('A safe exact torch version constraint is required')
                    constraints = output / 'torch_constraints.txt'
                    _write(output / 'selected_torch.json', {'requirement': constraint, 'plan': plan})
                    constraints.write_text(constraint + '\n', encoding='utf-8')
                    rows = (tpu_plan['requirements'] if detection['kind'] == 'tpu' else requirements)
                    rows = [line.strip() for line in rows if line.strip() and not line.lstrip().startswith('#')]
                    if rows:
                        setup_command([executable, '-m', 'pip', 'install', '-q', '--constraint', str(constraints), *rows], log)
                phase = 'torch_probe_after'
                after = _torch_probe(helper, detection['kind'], executable, output, phase,
                    environment=environment, deadline=deadline, clock=clock, run_process=run_process,
                    include_training_imports=True)
                setup.update(torch_after=after, interpreter=executable, isolated_repair=repaired)
                setup['probes'].append({'name': phase, **after})
                save_setup()
                expected_version = constraint.split('==', 1)[1]
                version_matches = (after.get('torch_version') == expected_version or
                    (detection['kind'] == 'tpu' and str(after.get('torch_version') or '').split('+')[0] == expected_version))
                if (after.get('state') == 'ready' and after.get('kernel_smoke') is True
                        and after.get('kind') == detection['kind'] and version_matches):
                    break
                # A healthy host may have ABI-broken optional extensions. Try one clean stack.
                retry = helper['plan_torch'](detection, {**after, 'state': 'broken', 'kernel_smoke': False})
                if not repaired and retry.get('state') == 'repair' and retry.get('isolated'):
                    setup['host_validation_failure'] = after
                    setup['dependency_plan'] = plan = retry
                    save_setup()
                    continue
                status.update(state='blocked', reason_code='accelerator_validation_failed',
                    detail='Fresh post-dependency accelerator validation failed; training was not launched. ' +
                           str(after.get('error') or 'Selected torch version or executable kernel changed.'))
                return {'run_dir': str(output), 'state': status['state'], 'archive': str(output / ARCHIVE_FILE)}
        setup.update(state='ready' if not tiny else 'tiny_software_bypass', interpreter=executable)
        save_setup()
        _write(output / 'environment_report.json', {'schema': 'security-setup-environment/1',
            'state': setup['state'], 'hardware': detection, 'torch_before': setup.get('torch_before'),
            'torch_after': setup.get('torch_after'), 'interpreter': executable,
            'runtime_setup_report': 'runtime_setup.json', 'software_test': tiny})
        phase = 'trial_execution'
        remaining = deadline - clock() - 30
        if remaining <= 0:
            raise subprocess.TimeoutExpired(phase, 0)
        command = [executable, '-u', '-m', 'training.finetune.security_trial', '--bundle-dir', str(bundle),
                   '--run-dir', str(output), '--total-budget-minutes', str(total_budget_minutes),
                   '--deadline', str(deadline), '--max-steps', str(max_steps)]
        if not tiny:
            command.extend(['--accelerator-kind', detection['kind']])
        if allow_experimental_tpu:
            command.append('--allow-experimental-tpu')
        if tiny:
            command.append('--tiny')
        setup['child_command'] = command
        save_setup()
        with (output / 'runtime.log').open('w', encoding='utf-8') as log:
            result = run_process(command, cwd=bundle, env=environment, timeout=remaining,
                                 stdout=log, stderr=subprocess.STDOUT, check=False)
        child = _child_status(output)
        status.update(child)
        if result.returncode:
            status.update(state='failed', reason_code='child_exit_nonzero', child_returncode=result.returncode)
        elif clock() >= deadline:
            status.update(state='partial', reason_code='trial_execution_deadline')
        elif child.get('state') not in ('finished', 'partial', 'blocked', 'failed'):
            status.update(state='failed', reason_code='child_status_missing_or_nonterminal')
        elif not (output / ARCHIVE_FILE).is_file():
            status.update(state='failed', reason_code='child_archive_missing')
        elif not _archive_matches_status(output, child):
            status.update(state='failed', reason_code='child_archive_invalid')
        else:
            child_valid = True
    except subprocess.TimeoutExpired:
        status = {**status, **_child_status(output), 'state': 'partial', 'reason_code': phase + '_deadline'}
    except Exception as error:
        status.update(state='failed', reason_code=status.get('reason_code') or type(error).__name__, detail=str(error))
        if isinstance(error, BundleNotFound):
            status['bundle_candidates'] = error.candidates          # what the search saw, so the failure archive explains itself
    finally:
        if not child_valid:
            if phase.startswith('torch_probe_') and (output / (phase + '.json')).is_file():
                report = json.loads((output / (phase + '.json')).read_text(encoding='utf-8'))
                setup['torch_before' if phase == 'torch_probe_before' else 'torch_after'] = report
            if setup['state'] not in ('ready', 'tiny_software_bypass'):
                setup.update(state=status['state'], phase=phase, reason_code=status.get('reason_code'))
            save_setup()
            status.update(schema=SCHEMA, phase=phase, elapsed_seconds=clock() - started,
                          remaining_seconds=max(0., deadline - clock()))
            export_outputs(output, status, compact=True, clock=clock, deadline=deadline)
    return {'run_dir': str(output), 'state': status['state'], 'archive': str(output / ARCHIVE_FILE)}
