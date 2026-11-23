"""Bounded fixtures for real worker readiness, separate from campaign evidence."""
import os
import numpy as np


def validate(device, a, b, powers):
    from . import numeric
    # Cross the transfer pipeline boundary, exercise empty/unsigned values, and
    # initialize both raw and token-shingle kernels with the production seeds.
    samples = [np.empty(0, dtype=np.uint64), np.array([0, 2**63, 2**64-1], dtype=np.uint64), np.arange(31, dtype=np.uint64)]
    rows = [samples[i % 3] for i in range(1025)]
    for p in (None, powers):
        expected = numeric.signatures_cpu(rows, a, b, p)
        actual = getattr(numeric, 'signatures_' + device)(rows, a, b, p)
        np.testing.assert_array_equal(actual, expected)
    pairs = [(['x', 'x', 'y'], ['x', 'z']), ([], [])]
    assert getattr(numeric, 'overlap_' + device)(pairs) == numeric.overlap_cpu(pairs)
    if device == 'cuda':
        assert numeric.comparisons_cuda(actual[:2], expected[:2]) == [len(a), len(a)]
        from . import cuda_driver
        if cuda_driver.runtime_name(device) == 'driver':
            cuda_driver.get_backend().synchronize()
        else:
            import torch
            torch.cuda.synchronize()
    from .cuda_driver import runtime_name
    return {'numeric_runtime': runtime_name(device), 'validated': True, 'device': device, 'pid': os.getpid(),
            'kernels': ['shingles', 'minhash', 'compare', 'overlap']}
