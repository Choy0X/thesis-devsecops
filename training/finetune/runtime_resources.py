"""Local resource admission and execution policy, independent of GPU names.

Storage admission imports only the standard library, so it can run before pip,
model downloads or accelerator initialization. Estimates are conservative byte
bounds, not claims that a recipe has been benchmarked on a particular machine.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import zipfile

GIB = 1024**3


def _config(cfg):
    return cfg.model_dump(mode='json') if hasattr(cfg, 'model_dump') else cfg or {}


def resource_paths(bundle_dir, run_dir):
    """Paths actually used by Hub caching, bundle work and run artifacts."""
    hf_home = Path(os.environ.get('HF_HOME', Path.home() / '.cache' / 'huggingface'))
    cache = Path(os.environ.get('HF_HUB_CACHE', os.environ.get('HUGGINGFACE_HUB_CACHE', hf_home / 'hub')))
    return {'cache': cache, 'work': Path(bundle_dir), 'checkpoint': Path(run_dir) / 'checkpoints',
            'output': Path(run_dir)}


def storage_snapshot(paths):
    """Probe nearest existing ancestors without creating or modifying paths."""
    result = {}
    for role, raw in paths.items():
        path = Path(raw).expanduser().absolute()
        row = {'path': str(path), 'filesystem': None, 'free_bytes': None, 'total_bytes': None,
               'used_bytes': None, 'error': None}
        try:
            ancestor = path
            while not ancestor.exists() and ancestor != ancestor.parent:
                ancestor = ancestor.parent
            usage = shutil.disk_usage(ancestor)
            row.update(filesystem=str(ancestor.stat().st_dev),
                       free_bytes=int(usage.free), total_bytes=int(usage.total), used_bytes=int(usage.used))
        except (OSError, ValueError) as error:
            row['error'] = str(error)
        result[role] = row
    return result


def _storage_admission(storage, required):
    volumes, reasons = {}, []
    for role, need in required.items():
        row = storage.get(role, {})
        free, filesystem = row.get('free_bytes'), row.get('filesystem')
        if free is None or filesystem is None:
            reasons.append(f'{role} storage availability is unavailable; check mount permissions and retry.')
            continue
        volume = volumes.setdefault(str(filesystem), {'required_bytes': 0, 'free_bytes': int(free), 'roles': []})
        volume['required_bytes'] += max(0, int(need))
        volume['free_bytes'] = min(volume['free_bytes'], int(free))
        volume['roles'].append(role)
    for volume in volumes.values():
        if volume['required_bytes'] > volume['free_bytes']:
            roles = ', '.join(volume['roles'])
            reasons.append(f'Insufficient storage for {roles}: need {volume["required_bytes"] / GIB:.2f} GiB '
                           f'additional free space, available {volume["free_bytes"] / GIB:.2f} GiB. '
                           'Free space or choose another cache/work/output volume.')
    return volumes, reasons


def _bundle_size(bundle_dir):
    root = Path(bundle_dir)
    if root.is_file():
        return _expanded_size(root)
    if not root.exists():
        return 0
    # Do not include a previous run or repository checkout in extraction size.
    size = 0
    for directory, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in ('.git', '.venv', 'runs', 'checkpoints', '__pycache__')]
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                size += _expanded_size(path)
    return size


def _expanded_size(path):
    size = path.stat().st_size
    if path.suffix.lower() == '.zip':
        # Read only central-directory metadata. Account for expanded contents
        # before extraction even when a highly compressed archive is tiny.
        with zipfile.ZipFile(path) as archive:
            size += sum(member.file_size for member in archive.infolist())
    return size


def _model_cache(cfg, paths):
    model = cfg.get('model', {})
    snapshot = paths['cache'] / ('models--' + model.get('id', '').replace('/', '--')) / 'snapshots' / model.get('revision', '')
    result = {'snapshot': str(snapshot), 'complete': False, 'actual_bytes': 0, 'missing_files': []}
    try:
        index = snapshot / 'model.safetensors.index.json'
        if index.is_file():
            data = json.loads(index.read_text(encoding='utf-8'))
            names = sorted(set(data['weight_map'].values()))
        else:
            names = ['model.safetensors']
        if not names:
            return result
        for name in names:
            # Only immediate snapshot members can establish completeness.
            if Path(name).name != name:
                result['missing_files'].append(name)
                continue
            file = snapshot / name
            if not file.is_file() or file.stat().st_size == 0:
                result['missing_files'].append(name)
            else:
                result['actual_bytes'] += file.stat().st_size
        # A weights index alone is not a usable model snapshot.
        result['complete'] = not result['missing_files'] and (snapshot / 'config.json').is_file()
    except (OSError, ValueError, KeyError, TypeError) as error:
        result['error'] = str(error)
    return result


def _estimates(bundle_dir, cfg, paths):
    """Bound the pinned 7B recipe; refuse unrecognized geometry rather than guess."""
    model = cfg.get('model', {})
    cache = _model_cache(cfg, paths)
    geometry = {'hidden_size': 3584, 'intermediate_size': 18944, 'num_hidden_layers': 28,
                'num_attention_heads': 28, 'num_key_value_heads': 4, 'vocab_size': 152064}
    known = model.get('id', '').lower() in ('qwen/qwen2.5-coder-7b', 'qwen/qwen2.5-coder-7b-instruct')
    source = 'conservative Qwen2.5-Coder-7B architectural bound'
    # Geometry from a local pinned snapshot is allowed, without a Hub request.
    local_config = Path(cache['snapshot']) / 'config.json'
    if local_config.is_file():
        try:
            local = json.loads(local_config.read_text(encoding='utf-8'))
            if all(isinstance(local.get(key), int) and local[key] > 0 for key in geometry):
                geometry = {key: local[key] for key in geometry}
                known = True
                source = 'local pinned model geometry, conservative parameter bound'
        except (OSError, ValueError, TypeError):
            pass
    h, intermediate, layers = (geometry[k] for k in ('hidden_size', 'intermediate_size', 'num_hidden_layers'))
    kv = h * geometry['num_key_value_heads'] // geometry['num_attention_heads']
    vocab = geometry['vocab_size']
    parameters = math.ceil((layers * (2*h*h + 2*h*kv + 3*h*intermediate) + 2*vocab*h) * 1.05)
    dimensions = {'q_proj': 2*h, 'k_proj': h+kv, 'v_proj': h+kv, 'o_proj': 2*h,
                  'gate_proj': h+intermediate, 'up_proj': h+intermediate, 'down_proj': h+intermediate}
    lora, training = cfg.get('lora', {}), cfg.get('training', {})
    adapter_params = layers * int(lora.get('r', 64)) * sum(dimensions.get(name, 0) for name in lora.get('target_modules', list(dimensions)))
    if lora.get('use_dora'):
        adapter_params += layers * (4*h + 2*kv + 2*intermediate)
    adapter = 4 * adapter_params + 4 * (3*h + 3)
    # FP32 adapter/master weights, gradients and at most two FP32 moments.
    optimizer = adapter * 2
    frozen = math.ceil(parameters * (.75 if cfg.get('precision', {}).get('load_in_4bit', True) else 2))
    download = max(16 * GIB, parameters * 2)
    if cache['complete']:
        download = 0
    # A separate partial/shard copy can coexist with the final cache object.
    temporary_download = min(5 * GIB, download)
    setup_storage = 5 * GIB if cfg.get('precision', {}).get('mode') == 'tpu_int4_bf16' else 0
    checkpoint = adapter + optimizer + 32 * 1024**2
    bundle = _bundle_size(bundle_dir)
    length = int(cfg.get('context', {}).get('max_total_tokens', 4096))
    chunk = min(64, int(training.get('lm_loss_chunk_tokens', 64)))
    activation = length*h*2*layers*3 + length*length*2 + chunk*vocab*8
    estimates = {'basis': source, 'model_geometry_available': known, 'geometry': geometry,
                 'quantization_algorithm': ('symmetric_block_int4/1' if cfg.get('precision', {}).get('mode') == 'tpu_int4_bf16' else 'nf4_double_quant'),
                 'model_parameters_upper_bound': parameters, 'frozen_weight_bytes': frozen,
                 'adapter_bytes': adapter, 'optimizer_bytes': optimizer,
                 'activation_bytes': activation, 'vram_required_bytes': frozen+2*adapter+optimizer+activation+GIB,
                 'host_required_bytes': 5*GIB + checkpoint + 2*GIB + bundle*2,
                 'download_bytes': download, 'download_transient_bytes': temporary_download + setup_storage,
                 'extraction_bytes': bundle*2, 'checkpoint_bytes': checkpoint,
                 'checkpoint_transient_bytes': checkpoint, 'adapter_export_bytes': adapter,
                 'output_zip_bytes': adapter + checkpoint + bundle,
                 'storage_safety_bytes': GIB, 'bundle_bytes': bundle}
    return estimates, cache


def storage_preflight(bundle_dir, run_dir, cfg=None, *, required_bytes=None):
    """Local, stdlib-only early storage guard. No downloads, imports or cleanup."""
    cfg, paths = _config(cfg), resource_paths(bundle_dir, run_dir)
    estimates, cache = _estimates(bundle_dir, cfg, paths)
    keep = max(1, int(cfg.get('training', {}).get('keep_recent_checkpoints', 2)))
    # recent + selected + atomic new checkpoint. Existing files already consume
    # free space; retaining this whole bound also covers output zip transients.
    required = required_bytes if required_bytes is not None else {
        'cache': estimates['download_bytes'] + estimates['download_transient_bytes'],
        'work': estimates['extraction_bytes'],
        'checkpoint': (keep+2) * estimates['checkpoint_bytes'],
        'output': estimates['adapter_export_bytes'] + estimates['output_zip_bytes'] + GIB}
    storage = storage_snapshot(paths)
    while True:
        volumes, reasons = _storage_admission(storage, required)
        if required_bytes is not None or not reasons or keep <= 1:
            break
        keep -= 1
        required['checkpoint'] = (keep+2) * estimates['checkpoint_bytes']
    estimates.update(storage_required_bytes=dict(required), filesystems=volumes, keep_recent_checkpoints=keep)
    return {'schema': 'runtime-resources/1', 'state': 'blocked' if reasons else 'ready', 'reasons': reasons,
            'resources': {'storage': storage}, 'estimates': estimates, 'model_cache': cache}


def ResourceMonitor(*args, **kwargs):
    # Kept lazy so storage_preflight does not require torch or psutil.
    from training.finetune.resource_metrics import ResourceMonitor as Monitor
    return Monitor(*args, **kwargs)


def cpu_capacity():
    """Respect process CPU affinity rather than the host's advertised core count."""
    try:
        if hasattr(os, 'sched_getaffinity'):
            return len(os.sched_getaffinity(0)) or None
    except OSError:
        pass
    return os.cpu_count()


def apply_process_policy(report):
    if report.get('state') != 'ready':
        raise RuntimeError('Resource admission must pass before worker configuration')
    workers = max(1, int(report['execution'].get('worker_threads', 1)))
    explicit_omp = 'OMP_NUM_THREADS' in os.environ
    os.environ.setdefault('RAYON_NUM_THREADS', str(workers))
    os.environ.setdefault('TOKENIZERS_PARALLELISM', 'true' if workers > 1 else 'false')
    os.environ.setdefault('OMP_NUM_THREADS', str(workers))
    if not explicit_omp:
        # This helper is called after dependency setup, never by stdlib storage admission.
        import torch
        torch.set_num_threads(workers)
    report['execution']['worker_environment'] = {key: os.environ[key] for key in
        ('RAYON_NUM_THREADS', 'TOKENIZERS_PARALLELISM', 'OMP_NUM_THREADS')}
    return report


def preflight(bundle_dir, run_dir, cfg, *, backend=None):
    """Admit the unchanged model recipe using local CUDA, RAM and disk counters."""
    cfg = _config(cfg)
    paths = resource_paths(bundle_dir, run_dir)
    report = storage_preflight(bundle_dir, run_dir, cfg)
    reasons = []
    if backend is None:
        from training.finetune.accelerator import select_backend
        try:
            backend = select_backend('auto')
        except RuntimeError as error:
            report['state'] = 'blocked'
            report['reasons'].append(str(error))
            return report
    monitor = ResourceMonitor(backend, storage_paths=paths)
    resources = monitor.snapshot(force=True)
    report['resources'] = resources
    resources['host_cpu_count'] = cpu_capacity()
    report['hardware'] = monitor.hardware_signature
    report['backend'] = {'name': backend.name, 'device': str(backend.device)}
    report['precision'] = 'bf16' if 'bfloat16' in str(backend.dtype) else 'fp16' if 'float16' in str(backend.dtype) else 'fp32'
    estimates = report['estimates']
    is_tpu = backend.name == 'tpu' and cfg.get('precision', {}).get('mode') == 'tpu_int4_bf16'
    memory_label = 'TPU HBM' if backend.name == 'tpu' else 'CUDA VRAM'
    if backend.name != 'cuda' and not is_tpu:
        reasons.append('The pinned model training recipe requires a compatible CUDA GPU; select a GPU runtime and retry.')
    if not estimates['model_geometry_available']:
        reasons.append('Model resource estimate unavailable; cache the pinned model config.json before admission.')
    free = resources.get('free_bytes')
    if free is None:
        reasons.append(f'{memory_label} availability is unavailable; check driver/runtime byte counters and retry.')
    elif free < estimates['vram_required_bytes']:
        reasons.append(f'Insufficient {memory_label}: need an estimated {estimates["vram_required_bytes"]/GIB:.2f} GiB, '
                       f'available {free/GIB:.2f} GiB. Free competing GPU allocations or use a larger GPU.')
    host = resources.get('host_available_bytes')
    if host is None:
        reasons.append('Host RAM availability is unavailable; install/enable psutil host counters and retry.')
    elif host < estimates['host_required_bytes']:
        reasons.append(f'Insufficient host RAM: need an estimated {estimates["host_required_bytes"]/GIB:.2f} GiB, '
                       f'available {host/GIB:.2f} GiB. Free RAM or select a runtime with more memory.')
    keep = max(1, int(cfg.get('training', {}).get('keep_recent_checkpoints', 2)))
    storage = resources.get('storage', {})
    # Reduce retention only when needed, always preserving latest + selected +
    # the new atomic save. Never delete caches or checkpoints during admission.
    required = dict(estimates['storage_required_bytes'])
    while True:
        required['checkpoint'] = (keep+2)*estimates['checkpoint_bytes']
        volumes, storage_reasons = _storage_admission(storage, required)
        if not storage_reasons or keep <= 1:
            break
        keep -= 1
    reasons.extend(storage_reasons)
    estimates.update(storage_required_bytes=required, filesystems=volumes)
    host_budget = 0 if is_tpu else min(64*1024**2, max(0, int((host or 0)-estimates['host_required_bytes'])//16))
    report['execution'] = {'micro_batch_size': 1, 'lm_loss_chunk_tokens': min(64, int(cfg.get('training', {}).get('lm_loss_chunk_tokens', 64))),
                           'gradient_checkpointing': True,
                           'worker_threads': min(4, max(1, resources['host_cpu_count'] or 1)),
                           'staging_worker_count': 1 if host_budget >= 8*1024**2 and (resources['host_cpu_count'] or 1) > 1 else 0,
                           'staging_depth': 2 if host_budget >= 8*1024**2 and (resources['host_cpu_count'] or 1) > 1 else 0,
                           'staging_max_host_bytes': host_budget, 'keep_recent_checkpoints': keep}
    report.update(state='blocked' if reasons else 'ready', reasons=reasons)
    return report


def apply_execution_policy(trainer, report):
    """Apply admitted execution knobs without changing config or checkpoint identity."""
    if report.get('state') != 'ready':
        raise RuntimeError('Resource admission blocked: ' + '; '.join(report.get('reasons', [])))
    from training.finetune import checkpoint as ck
    from training.finetune.throughput import _checkpointing
    execution = report['execution']
    trainer.micro_batch_size = execution['micro_batch_size']
    trainer.chunk = execution['lm_loss_chunk_tokens']
    if trainer.backend.name == 'tpu':
        from torch_xla.utils.checkpoint import checkpoint as checkpoint_fn
        from training.finetune.model import prepare_for_training
        prepare_for_training(trainer.model, gradient_checkpointing=execution['gradient_checkpointing'],
                             checkpoint_fn=checkpoint_fn)
    else:
        _checkpointing(trainer.model, execution['gradient_checkpointing'])
    trainer.policy = ck.Policy(trainer.policy.every_steps, trainer.policy.every_minutes, execution['keep_recent_checkpoints'])
    controller = getattr(trainer, 'controller', None)
    if controller is not None:
        controller.safe_batch, controller.chunk = trainer.micro_batch_size, trainer.chunk
    stager = getattr(trainer, 'stager', None)
    if stager is not None:
        stager.max_host_bytes = execution['staging_max_host_bytes']
        stager.host_budget_cap = execution['staging_max_host_bytes']
        stager.allow_prefetch = execution['staging_depth'] > 0
        stager.max_queue_depth = max(1, execution['staging_depth'])
        stager.enabled = execution['staging_depth'] > 0
    monitor = getattr(trainer, 'resource_monitor', None)
    if monitor is not None:
        monitor.storage_paths = {role: row['path'] for role, row in report['resources'].get('storage', {}).items()}
        monitor._optional_time = None
    trainer.meta['runtime_resources'] = report
    path = trainer.run_dir / 'runtime_resources.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, indent=2), encoding='utf-8')
    temporary.replace(path)
