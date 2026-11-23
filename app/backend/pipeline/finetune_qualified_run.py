"""Conditional, idempotent handoff of qualified data to a new immutable run.

No selected-model loading or training occurs here. Notebook setup is simulated in
an isolated standard-library child; matching accelerator diagnostics remain pending.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import zipfile
import shutil

from . import finetune_catalog as catalog, finetune_prep as fp, versions
from .oslock import FileLock
from training.finetune import bundle as bd, notebook_gen as ng, quality_gate, profiles, overrides as ov

SCHEMA = 'qualified-finetune-preparation/1'
MODEL = 'Qwen/Qwen2.5-Coder-7B-Instruct'
APPROVED = {'model.id': MODEL, 'model.serialization': 'native_chat_v1', 'context.max_total_tokens': 4096,
            'lora.r': 64, 'lora.lora_alpha': 128, 'data.train_slice_start': 0.0, 'data.train_slice_end': 1.0}
_COMMIT = re.compile(r'[0-9a-f]{40}\Z')
_TAG = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\Z')


def _sha(path):
    digest = hashlib.sha256()
    with pathlib.Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _read(path):
    try:
        value = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError('Expected an object in ' + str(path))
    return value


def _check(check):
    if check is not None and check() is False:
        raise InterruptedError('Qualified run preparation cancelled or superseded.')


def _inside(root, path):
    """All artifact paths are fixed children of the selected workspace."""
    path = pathlib.Path(path)
    relative = path.relative_to(root)
    current = root
    for component in relative.parts:
        current = current / component
        if current.exists() or current.is_symlink():
            if current.is_symlink() or getattr(current.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked qualified run storage is protected: ' + str(current))
    if not path.resolve().is_relative_to(root):
        raise ValueError('Qualified run artifacts must remain inside the selected workspace.')
    return path


def _settings(root):
    """Preserve the approved saved run settings; never adopt another model's run."""
    saved_path = _inside(root, root / 'training/runs/run2/prepared.json')
    saved = _read(saved_path) or {}
    base = fp.rs.cfgmod.load().model_dump(mode='json')
    provenance = {'basis': 'higher_quality_profile_and_current_contract'}
    revision = base['model']['revision']
    tokenizer = base['model']['tokenizer_revision']
    if saved.get('model') == MODEL:
        overrides = dict(saved.get('overrides') or {})
        resolved_path = _inside(root, saved_path.with_name(fp.rs.FILE))
        if resolved_path.is_file():
            historical = _read(resolved_path)
            cfg = historical.get('config') or {}
            if cfg.get('model', {}).get('id') != MODEL or _digest(cfg) != historical.get('config_sha256'):
                raise ValueError('The approved historical run has an invalid resolved configuration.')
            if (saved.get('model_revision') != cfg.get('model', {}).get('revision')
                    or saved.get('tokenizer_revision') != cfg.get('model', {}).get('tokenizer_revision')):
                raise ValueError('The historical model/tokenizer pins disagree with its resolved configuration.')
            for key, value in overrides.items():
                if _lookup(cfg, key) != value:
                    raise ValueError('The historical prepared overrides disagree with its resolved configuration: ' + key)
            for key, reason in ov.settable().items():
                if not reason:
                    try:
                        value = _lookup(cfg, key)
                    except KeyError:
                        continue
                    if value != _lookup(base, key):
                        overrides.setdefault(key, value)
            provenance['resolved_config_sha256'] = _sha(resolved_path)
        revision, tokenizer = saved.get('model_revision'), saved.get('tokenizer_revision')
        provenance.update(basis='approved_run2', prepared_sha256=_sha(saved_path))
        profile = saved.get('profile') or 'custom'
    else:
        overrides = dict(profiles.load()['higher_quality']['overrides'])
        profile = 'higher_quality'
    overrides.update(APPROVED)
    # Validate settings before allocating a run or resolving metadata remotely.
    ov.apply(base, overrides)
    revision = revision if isinstance(revision, str) and _COMMIT.fullmatch(revision) else None
    tokenizer = tokenizer if isinstance(tokenizer, str) and _COMMIT.fullmatch(tokenizer) else revision
    return {'overrides': overrides, 'profile': profile, 'revision': revision, 'tokenizer_revision': tokenizer}, provenance


def _lookup(config, key):
    for part in key.split('.'):
        config = config[part]
    return config


def _verify(folder, *, root, params, reservation, dataset_sha):
    """No artifact may be reused on its name or recorded hash alone."""
    for filename in (fp.PREPARED, fp.rs.FILE, 'bundle.zip', 'finetune.ipynb', 'qualified_preparation.json'):
        _inside(root, folder / filename)
    info = _read(folder / fp.PREPARED)
    if not info or info.get('schema') != 'finetune-prepared/1':
        raise ValueError('Run preparation metadata is missing or incomplete.')
    if (info.get('run') != reservation['name'] or info.get('run_id') != reservation['run_id']
            or info.get('model') != MODEL or info.get('purpose') != 'train' or info.get('stage') != 1
            or (info.get('dataset') or {}).get('tag') != params['tag']
            or info['dataset'].get('manifest_sha256') != dataset_sha
            or info.get('overrides') != params['overrides'] or info.get('profile') != params['profile']
            or any(info.get(key) is not None for key in ('steps', 'eval_max', 'eval_views', 'session_minutes', 'loss_mode'))):
        raise ValueError('Prepared run identity, dataset or settings do not match the qualified creation request.')
    for key, filename in (('bundle', 'bundle.zip'), ('notebook', 'finetune.ipynb')):
        artifact = info.get(key) or {}
        if artifact.get('path') != filename or artifact.get('sha256') != _sha(folder / filename):
            raise ValueError(key + ' hash or path does not match the prepared artifact.')
    cfg = fp.rs.read_resolved(folder)
    document = _read(folder / fp.rs.FILE)
    effective = cfg.model_dump(mode='json')
    if cfg.data.manifest_sha256 != dataset_sha or cfg.data.dataset_version != params['tag']:
        raise ValueError('Resolved dataset identity does not match this qualified dataset.')
    if document.get('overrides') != params['overrides'] or any(_lookup(effective, key) != value for key, value in params['overrides'].items()):
        raise ValueError('Resolved run settings do not match the approved configuration.')
    for key, value in (('revision', cfg.model.revision), ('tokenizer_revision', cfg.model.tokenizer_revision)):
        saved = info.get('model_revision' if key == 'revision' else key)
        if saved != value or params.get(key) is not None and params[key] != value:
            raise ValueError('Pinned model or tokenizer revision does not match the qualified request.')
    with zipfile.ZipFile(folder / 'bundle.zip') as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(names) != len({name.casefold() for name in names}):
            raise ValueError('Duplicate ZIP member paths are unsupported.')
        for entry in entries:
            name = entry.filename
            path = pathlib.PurePosixPath(name)
            if (not path.parts or path.is_absolute() or '\\' in name or ':' in name or '..' in path.parts
                    or path.as_posix() != name or any(part.endswith((' ', '.')) or pathlib.PureWindowsPath(part).is_reserved() for part in path.parts)
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('Unsafe ZIP member path: ' + name)
    problems = bd.verify_bundle(folder / 'bundle.zip')
    if problems:
        raise ValueError('Invalid qualified bundle: ' + '; '.join(problems))
    with zipfile.ZipFile(folder / 'bundle.zip') as archive:
        manifest = json.loads(archive.read(bd.MANIFEST))
        dataset = root / 'data/processed' / ('dataset_' + params['tag'])
        frozen = versions.load_registry(dataset.parent)['frozen'].get(dataset.name) or {}
        if (frozen.get('base') != dataset.name or
                not {'manifest.json', 'records.parquet', 'views.parquet'} <= set(frozen.get('files') or {})):
            raise ValueError('Qualified dataset has no complete immutable publication identity.')
        for relative, expected_sha in frozen['files'].items():
            published = _inside(root, dataset / relative)
            member = manifest.get('files', {}).get('data/' + relative)
            if _sha(published) != expected_sha or not member or member.get('sha256') != expected_sha:
                raise ValueError('Bundled dataset file differs from the immutable publication: ' + relative)
        if archive.read('training/finetune/quality_gate.py') != pathlib.Path(quality_gate.__file__).read_bytes():
            raise ValueError('Bundled preflight source does not match the trusted standard-library gate.')
        if (manifest.get('dataset', {}).get('manifest_sha256') != dataset_sha
                or manifest.get('dataset', {}).get('tag') != params['tag']
                or hashlib.sha256(archive.read('data/manifest.json')).hexdigest() != dataset_sha
                or archive.read(fp.rs.FILE) != (folder / fp.rs.FILE).read_bytes()):
            raise ValueError('Bundle dataset or resolved configuration identity mismatch.')
    tpu = cfg.precision.mode == 'tpu_int4_bf16'
    requirements_path = fp.REQUIREMENTS.with_name('requirements-finetune-tpu.txt') if tpu else fp.REQUIREMENTS
    expected = ng.build(bundle_sha256=info['bundle']['sha256'], requirements=requirements_path.read_text(encoding='utf-8').splitlines(),
        stage=1, purpose='train', device='tpu' if tpu else 'auto', parent_run_id=reservation['run_id'],
        bundle_zip_bytes=(folder / 'bundle.zip').stat().st_size)
    if _read(folder / 'finetune.ipynb') != expected:
        raise ValueError('The saved notebook does not match its exact bundle, parent identity and setup cells.')
    return info, document, expected


def _software_timeout(folder):
    """Allow time for ZIP hashing, extraction and repeated data verification.

The old 60-second deadline applied equally to fixture bundles and multi-GB
corpora. Use a conservative byte-work budget, retaining that floor for small
bundles. This does not skip any notebook or dataset validation.
"""
    path = pathlib.Path(folder) / 'bundle.zip'
    with zipfile.ZipFile(path) as archive:
        expanded = sum(member.file_size for member in archive.infolist())
    work = 3 * path.stat().st_size + 5 * expanded
    return max(60, (work + (8 << 20) - 1) // (8 << 20))


def _software_check(folder, notebook, *, run_id, dataset_sha):
    """Simulate trusted setup cells only. Never execute installs or train cells."""
    cells = {cell['id']: ''.join(cell['source']) for cell in notebook['cells']}
    workspace = cells['cell-06'].replace('if pathlib.Path("/kaggle/working").exists():', 'if False:').replace('elif "google.colab" in sys.modules:', 'elif False:')
    # Execute only the standard-library setup programs after exact template
    # verification. The local branch avoids external Drive/Kaggle paths.
    script = f'''import json, pathlib, runpy
scope = {{}}
exec({workspace!r}, scope)
first = str(scope['OUT'])
exec({workspace!r}, scope)
assert str(scope['OUT']) == first
other = {{}}
exec({workspace!r}, other)
assert str(other['OUT']) != first
assert other['workspace']['parent_run_id'] == {run_id!r}
exec({cells['cell-08']!r}, other)
sha = {notebook['metadata']['bundle_sha256']!r}
path = other['find_bundle'](sha, roots=[({str(folder)!r}, False)], expected_bytes={notebook['metadata']['bundle_zip_bytes']!r})
other['BUNDLE_ZIP'], other['BUNDLE_SHA256'] = path, sha
exec({cells['cell-10']!r}, other)
gate = runpy.run_path(str(other['WORK'] / 'training/finetune/quality_gate.py'))
preflight = gate['data_preflight'](other['WORK'] / 'data', purpose='train')
assert preflight['allowed'], preflight['blockers']
assert preflight['identity']['dataset_manifest_sha256'] == {dataset_sha!r}
resolved = json.loads((other['WORK'] / 'resolved_config.json').read_text())
matching = gate['check'](other['WORK'] / 'data', purpose='train', config_sha256=resolved['config_sha256'])
assert matching['blockers'] == ['missing_correctness_diagnostics'], matching['blockers']
print(json.dumps({{'state':'passed', 'discovery':True, 'unpack':True, 'data_preflight':True,
    'self_contained_standard_library':True, 'workspace_unique':True, 'parent_run_id':{run_id!r},
    'selected_model_loaded':False, 'training_started':False}}))
'''
    with tempfile.TemporaryDirectory(prefix='setup-check-', dir=folder.parent) as temporary:
        env = {'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1', 'TEMP': temporary, 'TMP': temporary}
        if os.name == 'nt' and os.environ.get('SYSTEMROOT'):
            env['SYSTEMROOT'] = os.environ['SYSTEMROOT']
        result = subprocess.run([sys.executable, '-I', '-S', '-c', script], cwd=temporary, env=env,
                                capture_output=True, text=True, encoding='utf-8', timeout=_software_timeout(folder))
    if result.returncode:
        raise ValueError('Self-contained notebook setup validation failed: ' + (result.stderr or result.stdout)[-4000:])
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ValueError('Self-contained notebook setup produced no valid completion report.') from exc


def _metadata(root, final, reservation, info, document, software, provenance, *, reused):
    return {'schema': SCHEMA, 'state': 'prepared', 'name': reservation['name'], 'run_id': reservation['run_id'],
        'request_id': reservation['request_id'], 'workspace': str(root), 'run_dir': str(final), 'reused': reused,
        'model': info['model'], 'model_revision': info['model_revision'], 'tokenizer_revision': info['tokenizer_revision'],
        'dataset': info['dataset'], 'config_sha256': document['config_sha256'], 'settings_provenance': provenance,
        'overrides': info['overrides'], 'profile': info['profile'], 'software_validation': software,
        'token_budget_audit': {'state': 'passed', 'path': str(final / 'token_budget_audit.json'),
                               'sha256': _sha(final / 'token_budget_audit.json') if final.exists() else None},
        'bundle': {'path': str(final / 'bundle.zip'), 'sha256': info['bundle']['sha256'],
                   'download_url': f"/api/finetune/runs/{reservation['name']}/bundle"},
        'notebook': {'path': str(final / 'finetune.ipynb'), 'sha256': info['notebook']['sha256'],
                     'download_url': f"/api/finetune/runs/{reservation['name']}/notebook"},
        'model_diagnostics': {'state': 'pending_colab_matching', 'dataset_manifest_sha256': info['dataset']['manifest_sha256'],
                              'config_sha256': document['config_sha256'], 'bundle_sha256': info['bundle']['sha256'],
                              'reason': 'Matching accelerator correctness checks must pass in Colab before full training.'},
        'training_started': False, 'security_performance': {'state': 'unmeasured'}}


def _audit_tokens(dataset, folder, document, check):
    from .qualified_token_audit import audit
    before = _sha(folder / 'bundle.zip')
    with tempfile.TemporaryDirectory(prefix='token-audit-', dir=folder.parent) as temporary:
        target = pathlib.Path(temporary)
        with zipfile.ZipFile(folder / 'bundle.zip') as archive:
            for name in ('manifest.json', 'views.parquet'):
                with archive.open('data/' + name) as source, (target / name).open('wb') as destination:
                    shutil.copyfileobj(source, destination, length=1 << 20)
        result = audit(target, fp.rs.read_resolved(folder), config_sha256=document['config_sha256'], check=check)
        result.update(bundle_sha256=before, views_sha256=_sha(target / 'views.parquet'))
    if _sha(folder / 'bundle.zip') != before:
        raise ValueError('Bundle changed during its token budget audit.')
    return result


def prepare(tag, *, root=None, work_key, check=None):
    """Prepare one reserved run only after operational dataset admission passes.

    `root` is the workspace root, not the processed-data directory. `check` is a
    cancellation/fence callback which raises, or returns False, when stale.
    """
    _check(check)
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise ValueError('Invalid qualified dataset version.')
    if not isinstance(work_key, str) or not work_key.strip():
        raise ValueError('Qualified preparation requires a stable workflow work_key.')
    root = pathlib.Path(root if root is not None else fp.ROOT).resolve(strict=True)
    dataset = _inside(root, root / 'data/processed' / ('dataset_' + tag))
    _inside(root, dataset / 'manifest.json')
    assessment = quality_gate.data_preflight(dataset, purpose='train')
    if not assessment['allowed']:
        raise ValueError('Dataset is not qualified and not ready for full training: ' + '; '.join(assessment['blockers']))
    manifest = _read(dataset / 'manifest.json')
    if manifest.get('tag') != tag or manifest.get('dataset_status') != 'frozen':
        raise ValueError('Qualified preparation requires the exact published frozen dataset.')
    readiness = manifest.get('security_readiness') or {}
    if (readiness.get('schema') != 'security-readiness/4'
            or readiness.get('qualification_policy') != quality_gate.QUALIFICATION_POLICY
            or (readiness.get('data_qualification') or {}).get('qualified') is not True):
        raise ValueError('The current operational qualification report is required before a new qualified run.')
    dataset_sha = _sha(dataset / 'manifest.json')
    settings, provenance = _settings(root)
    params = {'tag': tag, 'purpose': 'train', 'stage': 1, 'dataset_manifest_sha256': dataset_sha, **settings}
    request_id = 'qualified-' + hashlib.sha256(work_key.encode('utf-8')).hexdigest()
    runs = _inside(root, root / 'training/runs')
    _inside(root, runs / '.catalog')
    _check(check)
    reservation = catalog.reserve_run(runs, request_id=request_id, params=params)
    final = _inside(root, runs / reservation['name'])
    with FileLock(runs / '.catalog' / (request_id + '.lock'), note=False):
        _check(check)
        # This is export_data_preflight's new-run data/reservation gate with
        # explicit roots. Its global RUNS/PROC defaults must not select another
        # workspace or refresh a historical run when root is supplied.
        catalog.validate_creation(runs, request_id=request_id, name=reservation['name'], run_id=reservation['run_id'], params=params)
        assessment = quality_gate.data_preflight(dataset, purpose='train')
        if not assessment['allowed'] or _sha(dataset / 'manifest.json') != dataset_sha:
            raise ValueError('Qualified dataset changed or failed data preflight before run creation.')
        if final.exists():
            info, document, notebook = _verify(final, root=root, params=params, reservation=reservation, dataset_sha=dataset_sha)
            receipt = _read(final / 'qualified_preparation.json')
            token_path = _inside(root, final / 'token_budget_audit.json')
            token_audit = _read(token_path) or {}
            if (token_audit.get('state') != 'passed' or token_audit.get('dataset_manifest_sha256') != dataset_sha
                    or token_audit.get('config_sha256') != document['config_sha256']
                    or token_audit.get('bundle_sha256') != info['bundle']['sha256']
                    or (receipt or {}).get('token_budget_audit', {}).get('sha256') != _sha(token_path)):
                raise ValueError('Existing token-budget qualification is missing, changed or bound to other inputs.')
            if not receipt or receipt.get('run_id') != reservation['run_id'] or receipt.get('request_id') != request_id:
                raise ValueError('Existing run has no matching qualified preparation ownership receipt.')
            software = receipt.get('software_validation') or {}
            if (software.get('state') != 'passed' or software.get('parent_run_id') != reservation['run_id']
                    or any(software.get(key) is not True for key in ('discovery', 'unpack', 'data_preflight', 'self_contained_standard_library', 'workspace_unique'))
                    or software.get('selected_model_loaded') is not False or software.get('training_started') is not False
                    or receipt.get('config_sha256') != document['config_sha256']
                    or receipt.get('bundle', {}).get('sha256') != info['bundle']['sha256']
                    or receipt.get('notebook', {}).get('sha256') != info['notebook']['sha256']):
                raise ValueError('Prior preparation validation belongs to different artifact hashes.')
            _check(check)
            return _metadata(root, final, reservation, info, document, software, provenance, reused=True)
        stage_base = _inside(root, runs / '.qualified-preparations' / request_id)
        marker_path = stage_base / 'request.json'
        marker = {'request_id': request_id, 'run_id': reservation['run_id'], 'name': reservation['name'], 'params_sha256': _digest(params)}
        if stage_base.exists() and any(stage_base.iterdir()) and _read(marker_path) != marker:
            raise ValueError('Staged preparation belongs to another request.')
        _check(check)
        stage_base.mkdir(parents=True, exist_ok=True)
        if not marker_path.exists():
            fp._write_json(marker_path, marker)
        stage = _inside(root, stage_base / reservation['name'])
        stage.mkdir(exist_ok=True)
        if not (stage / fp.PREPARED).exists():
            fp._write_json(stage / fp.PREPARED, {'schema': 'qualified-run-reserved/1', 'run_id': reservation['run_id']})
        staged_info = _read(stage / fp.PREPARED)
        if staged_info.get('schema') != 'finetune-prepared/1':
            existing = _read(stage / fp.rs.FILE) or {}
            pinned = existing.get('pinned') or {}
            _check(check)
            fp.prepare(dataset_dir=dataset, run_dir=stage, source_root=root, stage=1, purpose='train',
                overrides=settings['overrides'], profile=settings['profile'], revision=settings['revision'] or pinned.get('model_revision'),
                tokenizer_revision=settings['tokenizer_revision'] or pinned.get('tokenizer_revision'))
        _check(check)
        info, document, notebook = _verify(stage, root=root, params=params, reservation=reservation, dataset_sha=dataset_sha)
        token_audit = _audit_tokens(dataset, stage, document, check)
        if token_audit.get('state') != 'passed':
            from .qualified_token_audit import validate_shortage
            retained = validate_shortage(token_audit, maximum=manifest['security_readiness']['data_qualification'])
            if (token_audit['dataset_manifest_sha256'] != dataset_sha or token_audit['config_sha256'] != document['config_sha256']
                    or token_audit['bundle_sha256'] != info['bundle']['sha256']):
                raise ValueError('Token shortage report belongs to different immutable inputs.')
            _check(check)
            _verify(stage, root=root, params=params, reservation=reservation, dataset_sha=dataset_sha)
            fp._write_json(stage / 'token_budget_audit.json', token_audit)
            return {'schema': SCHEMA, 'state': 'data_unqualified', 'code': 'token_budget_qualification_shortage',
                'name': reservation['name'], 'run_id': reservation['run_id'], 'request_id': request_id,
                'qualification': retained, 'blockers': token_audit['blockers'],
                'token_budget_audit': {key: value for key, value in token_audit.items() if key != 'retained_qualification'} | {
                    'path': str(stage / 'token_budget_audit.json'), 'sha256': _sha(stage / 'token_budget_audit.json')},
                'training_started': False, 'published': False}
        fp._write_json(stage / 'token_budget_audit.json', token_audit)
        software = _software_check(stage, notebook, run_id=reservation['run_id'], dataset_sha=dataset_sha)
        _check(check)
        result = _metadata(root, final, reservation, info, document, software, provenance, reused=False)
        result['token_budget_audit']['sha256'] = _sha(stage / 'token_budget_audit.json')
        fp._write_json(stage / 'qualified_preparation.json', result)
        # All prepared artifacts are still private at this final fence. Rename
        # publishes one complete new run and cannot overwrite a historical run.
        with versions.publication_scope(getattr(check, 'publication_guard', check)):
            if final.exists() or _sha(dataset / 'manifest.json') != dataset_sha:
                raise ValueError('Run target or frozen dataset changed before publication.')
            _verify(stage, root=root, params=params, reservation=reservation, dataset_sha=dataset_sha)
            stage.rename(final)
        catalog.invalidate(runs, reservation['name'])
        return result
