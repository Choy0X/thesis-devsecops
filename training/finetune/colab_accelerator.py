"""Standard-library hardware admission and isolated Colab torch repair policy.

This module never imports torch. The generated probe runs in a fresh, bounded
child owned by the bootstrap. CUDA/TPU package installation is the caller's job.
"""
from __future__ import annotations

import csv
import io
import os
from pathlib import Path
import platform as host_platform
import re
import subprocess
import sys
import textwrap
import time


def _capability(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return [int(value[0]), int(value[1])]
        except (ValueError, TypeError):
            return None
    match = re.fullmatch(r'(\d+)\.(\d+)', str(value).strip())
    return [int(item) for item in match.groups()] if match else None


def _number(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _tpu_devices(pci_root):
    generations = {'0x0027': 'v2_or_v3', '0x005e': 'v4', '0x0063': 'v5e', '0x006f': 'v6e'}
    devices = []
    for vendor in Path(pci_root).glob('*/vendor'):
        try:
            device_id = vendor.with_name('device').read_text().strip().lower()
            if vendor.read_text().strip().lower() == '0x1ae0' and device_id in generations:
                devices.append({'path': str(vendor.parent), 'generation': generations[device_id],
                    'device_id': device_id, 'source': 'pci'})
        except OSError:
            continue
    return devices


def detect_hardware(*, environ=None, run=subprocess.run, deadline=None, clock=time.monotonic,
        device_paths=None, pci_root=Path('/sys/bus/pci/devices'), nvidia_paths=None):
    """Probe physical evidence before torch, honoring inherited visibility masks.

    nvidia-smi calls share at most ten seconds and the caller's absolute deadline.
    Environment markers are recorded as hints, never promoted to physical proof.
    Host evidence paths and the process runner are injectable for offline tests.
    """
    env = os.environ if environ is None else environ
    masks = {key: env[key] for key in ('CUDA_VISIBLE_DEVICES', 'NVIDIA_VISIBLE_DEVICES') if key in env}
    hint_keys = ('COLAB_GPU', 'COLAB_TPU_ADDR', 'TPU_NAME', 'TPU_ACCELERATOR_TYPE', 'TPU_WORKER_ID', 'PJRT_DEVICE')
    hints = [key for key in hint_keys if env.get(key)]
    report = {'schema': 'colab-physical-accelerator/1', 'kind': 'unknown', 'devices': [],
        'evidence': [], 'errors': [], 'visibility': masks, 'hints': hints, 'reason': ''}
    start = clock()
    probe_deadline = min(start + 10.0, deadline) if deadline is not None else start + 10.0
    # Linux's accel namespace is shared by many vendors. Even an accelerator
    # character device does not establish Google TPU identity; PCI IDs do.
    paths = ([path for root in (Path('/dev'), Path('/dev/accel'))
        for path in root.glob('accel*') if not path.is_dir()]
        if device_paths is None else list(device_paths))
    report['evidence'].extend({'source': 'unverified_accel_path', 'path': str(path)} for path in paths)
    tpu = _tpu_devices(pci_root)
    nvidia_nodes = (list(Path('/dev').glob('nvidia[0-9]*')) + list(Path('/proc/driver/nvidia/gpus').glob('*'))
        if nvidia_paths is None else nvidia_paths)
    report['evidence'].extend({'source': 'nvidia_device_path', 'path': str(path)} for path in nvidia_nodes)
    fields = ['index', 'name', 'uuid', 'driver_version', 'memory.total', 'compute_cap']
    devices = []
    failed = False
    for query_fields in (fields, fields[:-1]):
        remaining = probe_deadline - clock()
        if remaining <= 0:
            report['errors'].append('Hardware probe deadline exhausted')
            failed = True
            break
        try:
            command = ['nvidia-smi', '--query-gpu=' + ','.join(query_fields), '--format=csv,noheader,nounits']
            result = run(command, capture_output=True, text=True, timeout=remaining, check=False,
                env=dict(env))
        except FileNotFoundError:
            report['errors'].append('nvidia-smi is unavailable')
            break
        except (OSError, subprocess.TimeoutExpired) as error:
            report['errors'].append(type(error).__name__ + ': ' + str(error)[:1000])
            failed = True
            break
        if result.returncode:
            error = str(result.stderr or result.stdout or '')[:1000]
            report['errors'].append('nvidia-smi: ' + error)
            # Older drivers may not expose compute_cap. Do not retry driver failures.
            if len(query_fields) == 6 and ('compute_cap' in error.lower() or 'invalid field' in error.lower()):
                continue
            failed = True
            break
        report['evidence'].append({'source': 'nvidia-smi', 'fields': query_fields})
        for row in csv.reader(io.StringIO(result.stdout or '')):
            row = [value.strip() for value in row]
            if not row:
                continue
            if len(row) != len(query_fields) or not row[0].isdigit() or not row[2].startswith('GPU-'):
                report['errors'].append('Malformed nvidia-smi device row')
                failed = True
                continue
            devices.append({'index': int(row[0]), 'name': row[1], 'uuid': row[2],
                'driver_version': row[3], 'memory_total_mib': _number(row[4]),
                'compute_capability': _capability(row[5]) if len(row) > 5 else None})
        break
    physical_gpu = bool(devices)
    for key, value in masks.items():
        value = value.strip()
        if value.lower() in ('', '-1', 'none', 'void'):
            devices = []
            report['reason'] = key + ' explicitly hides NVIDIA devices; inherited mask is preserved'
            break
        if value.lower() == 'all':
            continue
        tokens = [part.strip() for part in value.split(',')]
        devices = [device for device in devices if any(
            token == str(device['index']) or (token.startswith('GPU-') and device['uuid'].startswith(token))
            for token in tokens)]
        if physical_gpu and not devices:
            report['reason'] = key + ' does not identify an accessible probed NVIDIA device'
    if devices:
        report.update(kind='cuda', devices=devices, reason='Physical NVIDIA devices and driver queried independently of torch')
    elif tpu:
        report.update(kind='tpu', devices=tpu, reason='Known Google TPU PCI vendor/device identity; executable validation pending')
        report['evidence'].extend(tpu)
    elif physical_gpu or hints or paths or nvidia_nodes or failed:
        report['reason'] = report['reason'] or 'Accelerator evidence is incomplete or inaccessible; package installation cannot allocate hardware'
    else:
        report.update(kind='cpu', reason='No physical NVIDIA or TPU evidence was found')
    return report


def torch_probe_script(kind, include_training_imports=False):
    """Return model-free executable validation code for a fresh interpreter."""
    if kind not in ('cuda', 'tpu'):
        raise ValueError('Torch accelerator probe requires cuda or tpu')
    prefix = 'KIND = ' + repr(kind) + '\nINCLUDE_TRAINING_IMPORTS = ' + repr(bool(include_training_imports)) + '\n'
    return prefix + textwrap.dedent('''\
        import contextlib
        import json
        import signal
        import sys
        import traceback
        report = {'schema': 'colab-torch-probe/1', 'kind': KIND, 'state': 'broken',
            'python_executable': sys.executable, 'python_version': sys.version,
            'torch_version': None, 'cuda_version': None, 'cuda_available': False,
            'device_count': 0, 'devices': [], 'kernel_smoke': False, 'torch_ok': False,
            'training_imports_ok': None, 'failure_phase': 'torch_import', 'error': None}
        def operation_timeout(signum, frame):
            raise TimeoutError('Accelerator operations exceeded the 10-second probe limit')
        alarm_available = hasattr(signal, 'SIGALRM')
        try:
            # Keep any dependency chatter out of the single JSON stdout record.
            with contextlib.redirect_stdout(sys.stderr):
                import torch
                report['torch_version'] = str(torch.__version__)
                report['cuda_version'] = torch.version.cuda
                report['failure_phase'] = 'device_access'
                if alarm_available:
                    signal.signal(signal.SIGALRM, operation_timeout)
                    signal.alarm(10)
                if KIND == 'cuda':
                    report['cuda_available'] = bool(torch.cuda.is_available())
                    report['device_count'] = torch.cuda.device_count()
                    if not report['cuda_available'] or not report['cuda_version'] or not report['device_count']:
                        raise RuntimeError('Torch has no executable CUDA device; CPU-only torch cannot train this model')
                    for index in range(report['device_count']):
                        props = torch.cuda.get_device_properties(index)
                        device = {'index': index, 'name': props.name,
                            'compute_capability': [props.major, props.minor], 'total_memory_bytes': props.total_memory}
                        try:
                            free, total = torch.cuda.mem_get_info(index)
                            device.update(free_memory_bytes=int(free), total_memory_bytes=int(total))
                        except Exception as telemetry_error:
                            device['memory_error'] = type(telemetry_error).__name__ + ': ' + str(telemetry_error)
                        report['devices'].append(device)
                    candidates = [item for item in report['devices']
                        if tuple(item['compute_capability']) >= (7, 5)
                        and item.get('free_memory_bytes', 1) > 0]
                    if not candidates:
                        raise RuntimeError('No compatible visible CUDA GPU has usable free memory')
                    selected = max(candidates, key=lambda item: (item.get('free_memory_bytes', 0),
                        item['total_memory_bytes'], -item['index']))
                    report['selected_device_index'] = selected['index']
                    device = 'cuda:' + str(selected['index'])
                    dtype = torch.bfloat16 if selected['compute_capability'][0] >= 8 else torch.float16
                else:
                    import torch_xla
                    import torch_xla.core.xla_model as xm
                    device = xm.xla_device()
                    report['device_count'] = 1
                    report['devices'] = [{'device': str(device)}]
                    dtype = torch.bfloat16
                report['failure_phase'] = 'kernel_smoke'
                value = torch.ones((16, 16), device=device, dtype=dtype, requires_grad=True)
                loss = (value @ value).float().square().mean()
                loss.backward()
                if KIND == 'cuda':
                    torch.cuda.synchronize(report['selected_device_index'])
                else:
                    xm.mark_step()
                    xm.wait_device_ops()
                if not bool(torch.isfinite(loss)) or value.grad is None or not bool(torch.isfinite(value.grad).all()):
                    raise RuntimeError('Accelerator matmul/backward produced non-finite values or missing gradients')
                report.update(kernel_smoke=True, torch_ok=True)
                if alarm_available:
                    signal.alarm(0)
                if INCLUDE_TRAINING_IMPORTS:
                    report.update(failure_phase='training_imports', training_imports_ok=False)
                    from transformers import Qwen2ForCausalLM, AutoTokenizer
                    from peft import LoraConfig, get_peft_model
                    if KIND == 'cuda':
                        import bitsandbytes
                    report['training_imports_ok'] = True
                report.update(state='ready', failure_phase=None)
        except Exception as error:
            report['error'] = type(error).__name__ + ': ' + str(error)
            report['action'] = ('Use an isolated matching training environment; optional vision/audio binary packages can break model imports'
                if report['failure_phase'] == 'training_imports' else
                'Use a driver and torch wheel supporting the visible physical accelerator, then rerun this fresh-process probe')
        finally:
            if alarm_available:
                signal.alarm(0)
        print(json.dumps(report, sort_keys=True))
        ''')


def plan_torch(hardware, probe, *, python_version=None, platform=None, machine=None):
    """Pure policy: reuse validated CUDA, isolate repairs, and fail closed otherwise."""
    kind = hardware.get('kind', 'unknown')
    result = {'schema': 'colab-torch-plan/1', 'state': 'blocked', 'kind': kind,
        'isolated': False, 'torch_requirement': None, 'index_url': None,
        'constraints': [], 'compatibility_notes': [], 'reason': ''}
    if kind not in ('cuda', 'tpu'):
        result['reason'] = 'No accessible physical accelerator: ' + str(hardware.get('reason', kind))
        return result
    if kind == 'tpu':
        result.update(state='repair', isolated=True, torch_requirement='torch==2.9.0',
            index_url='https://download.pytorch.org/whl/cpu',
            constraints=['torch==2.9.0', 'torch_xla==2.9.0', 'libtpu==0.0.21.1'],
            reason='Use the separate isolated experimental TPU stack and validate it before model downloads')
        return result
    healthy = (probe.get('kind') == 'cuda' and probe.get('state') == 'ready'
        and probe.get('kernel_smoke') is True and probe.get('cuda_available') is True
        and probe.get('cuda_version') and probe.get('device_count', 0) > 0)
    version = str(probe.get('torch_version', ''))
    if healthy and re.fullmatch(r'\d+\.\d+\.\d+(?:\+[A-Za-z0-9.]+)?', version):
        result.update(state='ready', constraints=['torch==' + version],
            reason='Reuse the existing torch build after a successful fresh-process CUDA forward/backward probe')
        return result
    python_version = sys.version_info[:2] if python_version is None else python_version
    if isinstance(python_version, str):
        python_version = tuple(int(part) for part in python_version.split('.')[:2])
    platform = sys.platform if platform is None else platform
    machine = host_platform.machine() if machine is None else machine
    if tuple(python_version[:2]) not in ((3, 11), (3, 12), (3, 13)) or platform != 'linux' or machine.lower() not in ('x86_64', 'amd64'):
        result['reason'] = 'Automatic repair is verified only for published Linux x86_64 CPython 3.11-3.13 wheels'
        return result
    devices = hardware.get('devices', [])
    if not devices:
        result['reason'] = 'Physical NVIDIA device telemetry is missing'
        return result
    candidates = []
    for device in devices:
        match = re.fullmatch(r'(\d+)(?:\.\d+)+', str(device.get('driver_version', '')))
        capability = _capability(device.get('compute_capability'))
        if not match or not capability:
            continue
        driver = int(match.group(1))
        if not (7, 5) <= tuple(capability) <= (12, 0):
            continue
        if driver < 525 or (tuple(capability) >= (12, 0) and driver < 570):
            continue
        candidates.append((device, driver))
    if not candidates:
        result['reason'] = 'No visible GPU has a known supported compute capability and sufficiently recent NVIDIA driver for automatic repair'
        return result
    selected, driver = max(candidates, key=lambda item: (
        item[0].get('memory_free_mib') or 0, item[0].get('memory_total_mib') or 0,
        -item[0].get('index', 0)))
    variant = 'cu130' if driver >= 580 else 'cu128' if driver >= 570 else 'cu126'
    result.update(state='repair', isolated=True, torch_requirement='torch==2.11.0+' + variant,
        index_url='https://download.pytorch.org/whl/' + variant,
        constraints=['torch==2.11.0+' + variant],
        selected_physical_device_index=selected.get('index'),
        reason='Physical NVIDIA GPU exists but the current torch environment failed validation; repair in an isolated interpreter')
    if variant == 'cu126' and driver < 560:
        result['compatibility_notes'].append('CUDA 12.x minor-version compatibility has limited features on this driver; executable revalidation is mandatory')
    result['compatibility_notes'].append('Driver and architecture selection is provisional until the selected interpreter passes fresh executable validation')
    return result
