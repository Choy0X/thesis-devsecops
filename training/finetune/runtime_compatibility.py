"""Colab capability admission and the explicit, separate experimental TPU recipe.

Detection and dependency planning use the standard library only. Actual kernel
validation runs in the training child after setup, before any model download.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def tpu_pci_devices(root=Path('/sys/bus/pci/devices')):
    """Mirror the pinned XLA vendor/device identification, including VFIO TPUs."""
    generations = {'0x0027': 'v2_or_v3', '0x005e': 'v4', '0x0063': 'v5e', '0x006f': 'v6e'}
    found = []
    for vendor in Path(root).glob('*/vendor'):
        try:
            device = vendor.with_name('device').read_text().strip().lower()
            if vendor.read_text().strip().lower() == '0x1ae0' and device in generations:
                found.append({'path': str(vendor.parent), 'generation': generations[device], 'device_id': device})
        except OSError:
            continue
    return found


def detect_runtime(environ=None, *, device_paths=None):
    env = os.environ if environ is None else environ
    paths = list(Path('/dev').glob('accel*')) if device_paths is None else device_paths
    markers = [key for key in ('COLAB_TPU_ADDR', 'TPU_NAME', 'TPU_ACCELERATOR_TYPE', 'TPU_WORKER_ID') if env.get(key)]
    pci = tpu_pci_devices()
    tpu = bool(markers or paths or pci or env.get('PJRT_DEVICE', '').upper() == 'TPU')
    return {'schema': 'colab-runtime-detection/1', 'kind': 'tpu' if tpu else 'cuda',
            'evidence': markers + [str(path) for path in paths], 'pci_devices': pci,
            'hardware_validation': 'not_run',
            'note': 'Selection hints only. The training child must validate executable kernels and usable memory.'}


def requirements_for_runtime(kind, gpu_requirements, tpu_requirements):
    clean = lambda rows: [row.strip() for row in rows if row.strip() and not row.lstrip().startswith('#')]
    if kind == 'tpu':
        rows = clean(tpu_requirements)
        if not rows or 'torch==2.9.0' not in rows or 'torch_xla==2.9.0' not in rows or 'libtpu==0.0.21.1' not in rows:
            raise ValueError('Experimental TPU setup requires the bundled matching torch/XLA/libtpu pins')
        if any(row.split('=')[0].lower() in ('bitsandbytes', 'torchvision', 'torchaudio') for row in rows):
            raise ValueError('CUDA and optional vision/audio extensions are excluded from isolated TPU setup')
        return {'isolated': True, 'torch_index': 'https://download.pytorch.org/whl/cpu',
                'torch_requirement': 'torch==2.9.0', 'requirements': [row for row in rows if row != 'torch==2.9.0'],
                'environment': {'PJRT_DEVICE': 'TPU'}}
    if kind != 'cuda':
        raise ValueError('Unknown runtime kind')
    return {'isolated': False, 'torch_index': None, 'torch_requirement': None,
            'requirements': clean(gpu_requirements), 'environment': {}}


def fingerprint(cfg):
    value = cfg.model_dump(mode='json') if hasattr(cfg, 'model_dump') else cfg
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def effective_config(cfg, backend, *, allow_experimental_tpu=False):
    """Return a validated copy. The original resolved configuration stays intact."""
    from training.finetune.config import parse
    value = cfg.model_dump(mode='json')
    overrides = {}
    tpu = backend.name == 'tpu'
    if tpu:
        if not allow_experimental_tpu:
            raise ValueError('TPU needs explicit authorization for the separate experimental INT4 recipe')
        if cfg.precision.mode not in ('qlora_nf4', 'tpu_int4_bf16'):
            raise ValueError('The experimental TPU recipe requires a quantized base configuration')
        from training.finetune.tpu_quantization import validate_tpu_config
        overrides = {'precision.mode': 'tpu_int4_bf16', 'training.micro_batch_tuning': 'off'}
        for key, item in overrides.items():
            section, name = key.split('.')
            value[section][name] = item
        result = parse(value)
        validate_tpu_config(result)
    else:
        result = parse(value)
        if backend.name == 'cuda' and cfg.precision.mode != 'qlora_nf4':
            raise ValueError('A TPU or offload recipe cannot silently become CUDA NF4')
    recipe = {'schema': 'portable-runtime-recipe/1', 'backend': backend.name,
              'recipe_id': 'experimental_tpu_symmetric_int4_bf16_v1' if tpu else 'cuda_nf4_v1' if backend.name == 'cuda' else 'tiny_software_v1',
              'experimental': tpu, 'hardware_validation': 'pending' if tpu else 'not_run',
              'overrides': overrides, 'base_config_sha256': fingerprint(cfg),
              'effective_config_sha256': fingerprint(result)}
    if tpu:
        recipe.update(quantization='symmetric_block_int4/1', block_size=128, compute_dtype='bf16',
                      scope='Experimental single-device TPU source trial. Not equivalent to CUDA NF4.',
                      stack={'torch': '2.9.0', 'torch_xla': '2.9.0', 'libtpu': '0.0.21.1'})
    return result, recipe


def validate_recipe(cfg, recipe):
    from types import SimpleNamespace
    if not isinstance(recipe, dict) or recipe.get('backend') not in ('cuda', 'tpu', 'cpu'):
        raise ValueError('Unknown runtime recipe declaration')
    result, expected = effective_config(cfg, SimpleNamespace(name=recipe['backend']), allow_experimental_tpu=True)
    if recipe != expected:
        raise ValueError('Runtime recipe differs from the bounded declared experiment')
    return result


def _cuda_probe(backend):
    import torch
    import bitsandbytes as bnb
    device = torch.device(backend.device)
    with torch.cuda.device(device), torch.random.fork_rng(devices=[device.index or 0]):
        layer = bnb.nn.Linear4bit(64, 64, bias=False, compute_dtype=backend.dtype,
                                 compress_statistics=True, quant_type='nf4').to(device)
        value = torch.ones((2, 64), device=device, dtype=backend.dtype, requires_grad=True)
        loss = layer(value).float().square().mean()
        loss.backward()
        backend.synchronize()
        if not bool(torch.isfinite(loss)) or value.grad is None or not bool(torch.isfinite(value.grad).all()):
            raise RuntimeError('NF4 projection/input-gradient probe produced invalid values')
        props = torch.cuda.get_device_properties(device)
        return {'device_name': props.name, 'compute_capability': [props.major, props.minor],
                'torch': torch.__version__, 'torch_cuda': torch.version.cuda,
                'bitsandbytes': bnb.__version__, 'compiled_architectures': torch.cuda.get_arch_list(),
                'nf4_double_quant_forward_backward': 'passed'}


def validate_runtime(backend):
    report = {'schema': 'runtime-compatibility/1', 'backend': backend.name, 'device': str(backend.device),
              'precision': str(backend.dtype), 'state': 'blocked', 'model_hardware_validation': 'not_run'}
    try:
        if backend.name == 'cuda':
            report.update(_cuda_probe(backend))
        elif backend.name == 'tpu':
            import torch
            from training.finetune.tpu_quantization import FrozenInt4Linear
            with backend.autocast():
                layer = FrozenInt4Linear.from_weight(torch.eye(64, dtype=torch.bfloat16)).to(backend.device)
                value = torch.ones((2, 64), dtype=torch.bfloat16, device=backend.device, requires_grad=True)
                loss = layer(value).float().square().mean()
                loss.backward()
                backend.synchronize()
            if not bool(torch.isfinite(loss).cpu()) or value.grad is None or not bool(torch.isfinite(value.grad).all().cpu()):
                raise RuntimeError('TPU INT4 projection/input-gradient probe produced invalid values')
            report.update(int4_forward_backward='passed', stack='experimental PyTorch/XLA 2.9',
                          quantization='symmetric_block_int4/1')
        else:
            raise RuntimeError('No CUDA GPU or TPU is available for the pinned model')
        report['state'] = 'ready'
    except Exception as error:
        report.update(detail=f'{type(error).__name__}: {error}',
                      action=('Select a current Colab GPU runtime with a PyTorch CUDA build supporting this device '
                              '(Blackwell needs CUDA 12.8+ builds) and a compatible bitsandbytes wheel. '
                              'Restart the runtime after changing binary packages. See setup/runtime logs.'
                              if backend.name == 'cuda' else
                              'Select a local Colab v5e/v6e TPU runtime with the isolated matching XLA stack. '
                              'Check libtpu device access and the experimental runtime log.'))
    return report
