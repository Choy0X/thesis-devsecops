"""Auto is evidence-gated; explicit CUDA never falls back silently."""
import hashlib
import os
from pathlib import Path


def identity():
    from app.backend import compute, device
    python = compute.gpu_python()
    digest = hashlib.sha256()
    for name in ('numeric.py', 'cuda_kernels.py', 'wire.py', 'worker.py', 'worker_client.py', 'warmup.py', 'cuda_driver.py'):
        digest.update((Path(__file__).parent / name).read_bytes())
    cards = device.gpus().get('cards') or []
    from .cuda_driver import runtime_name
    return {'numeric_runtime': runtime_name(), 'python': python, 'python_mtime_ns': os.stat(python).st_mtime_ns if python else None,
            'gpu': [{k: card.get(k) for k in ('name', 'driver', 'total_mb')} for card in cards], 'source_sha256': digest.hexdigest()}


def choose(mode, operation, count, benchmarks):
    if mode not in ('auto', 'cpu', 'cuda'):
        raise ValueError('Compute mode must be auto, cpu or cuda')
    if mode != 'auto':
        return mode
    evidence = benchmarks.get(operation) or {}
    return 'cuda' if evidence.get('speedup', 0) > 1.1 and count >= max(1, evidence.get('minimum_items', 1)) else 'cpu'
