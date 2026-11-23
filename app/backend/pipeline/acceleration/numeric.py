"""Reference and CUDA numeric kernels. Torch is imported only by CUDA entrypoints."""
from collections import Counter
import numpy as np

last_metrics = {}


def _reset_cuda_metrics():
    global last_metrics
    last_metrics = {'gpu_kernel_seconds': 0.0, 'gpu_transfer_seconds': 0.0,
                    'gpu_timing_coverage': 'complete'}


def _cuda_binary(left, right, kernel):
    """Time completed transfers and arithmetic on the operation's CUDA stream."""
    import torch
    start, ready, computed, done = (torch.cuda.Event(enable_timing=True) for _ in range(4))
    start.record()
    a, b = torch.from_numpy(left).to('cuda'), torch.from_numpy(right).to('cuda')
    ready.record()
    result = kernel(a, b)
    computed.record()
    result = result.cpu()
    done.record()
    done.synchronize()
    last_metrics['gpu_kernel_seconds'] += ready.elapsed_time(computed)/1000
    last_metrics['gpu_transfer_seconds'] += (start.elapsed_time(ready)+computed.elapsed_time(done))/1000
    return result


def shingles_cpu(row, powers):
    count = max(0, len(row)-len(powers)+1)
    out = np.zeros(count, dtype=np.uint64)
    with np.errstate(over='ignore'):
        for j, power in enumerate(powers):
            out += (row[j:j+count] + np.uint64(1)) * power
    return out


def signatures_cpu(rows, a, b, powers=None):
    if powers is not None:
        rows = [shingles_cpu(row, powers) for row in rows]
    out = np.full((len(rows), len(a)), np.iinfo(np.uint64).max, dtype=np.uint64)
    with np.errstate(over='ignore'):
        for i, row in enumerate(rows):
            if not len(row):
                continue
            for lo in range(0, len(a), 32):
                m = row[None, :] * a[lo:lo+32, None] + b[lo:lo+32, None]
                m ^= m >> np.uint64(29)
                out[i, lo:lo+32] = m.min(axis=1)
    return out


def bounded_signatures(kernel, rows, a, b, powers=None):
    """Shrink numeric batches under pressure without dropping any token shingles."""
    import torch
    try:
        return kernel(rows, a, b, powers)
    except torch.OutOfMemoryError:
        pass
    # Leave the except block first so the failed kernel's traceback releases buffers.
    torch.cuda.empty_cache()
    if len(rows) > 1:
        middle = len(rows)//2
        return np.concatenate([bounded_signatures(kernel, rows[:middle], a, b, powers),
                               bounded_signatures(kernel, rows[middle:], a, b, powers)])
    overlap = len(powers)-1 if powers is not None else 0
    if not rows or len(rows[0]) <= max(16, 2*overlap+2):
        raise torch.OutOfMemoryError('CUDA remains full at the minimum numeric batch size')
    row = rows[0]
    middle = (len(row)+overlap)//2
    left = bounded_signatures(kernel, [row[:middle]], a, b, powers)
    right = bounded_signatures(kernel, [row[middle-overlap:]], a, b, powers)
    return np.minimum(left, right)


def signatures_cuda(rows, a, b, powers=None):
    from . import cuda_driver
    if cuda_driver.runtime_name() == 'driver':
        backend = cuda_driver.get_backend()
        result = backend.minhash(rows, a, b, powers)
        last_metrics.clear()
        last_metrics.update(backend.last_metrics)
        return result
    import torch
    import os
    last_metrics.clear()
    last_metrics['gpu_timing_coverage'] = 'unavailable'
    if os.name == 'nt':
        from . import cuda_kernels
        totals = {'gpu_kernel_seconds':0.0,'gpu_transfer_seconds':0.0}
        coverage = []
        def measured(rows,a,b,powers=None):
            value = cuda_kernels.minhash(rows,a,b,powers)
            values = cuda_kernels.last_metrics
            coverage.append(all(k in values for k in totals))
            for name in totals: totals[name] += values.get(name,0)
            return value
        try:
            value = bounded_signatures(measured, rows, a, b, powers)
            if coverage and all(coverage):
                last_metrics.update(totals, gpu_timing_coverage='complete')
            return value
        except OSError:
            pass  # A CUDA-only Torch path remains available when NVRTC is not bundled.
    tile = 1024
    while True:
        try:
            return _signatures_cuda(rows, a, b, powers, tile)
        except torch.OutOfMemoryError:
            if tile <= 16:
                raise
            tile //= 2
            torch.cuda.empty_cache()


def _signatures_cuda(rows, a, b, powers, tile):
    import torch
    out = np.full((len(rows), len(a)), np.iinfo(np.uint64).max, dtype=np.uint64)
    da = torch.from_numpy(a.view(np.int64)).to('cuda')
    db = torch.from_numpy(b.view(np.int64)).to('cuda')
    # Bounded vectorized batches avoid one transfer and many kernels per source file.
    for lo in range(0, len(rows), 32):
        group = rows[lo:lo+32]
        width = max((len(row) for row in group), default=0)
        packed = np.zeros((len(group), width), dtype=np.uint64)
        lengths = []
        for i, row in enumerate(group):
            packed[i, :len(row)] = row
            lengths.append(len(row))
        source = torch.from_numpy(packed.view(np.int64)).to('cuda')
        if powers is not None:
            dp = torch.from_numpy(powers.view(np.int64)).to('cuda')
            count = max(0, width-len(powers)+1)
            hashes = torch.zeros((len(group), count), dtype=torch.int64, device='cuda')
            for j in range(len(powers)):
                hashes += (source[:, j:j+count] + 1) * dp[j]
            source = hashes
            lengths = [max(0, n-len(powers)+1) for n in lengths]
            width = count
        lengths = torch.tensor(lengths, dtype=torch.int64, device='cuda')
        best = torch.full((len(group), len(a)), torch.iinfo(torch.int64).max, dtype=torch.int64, device='cuda')
        for start in range(0, width, tile):
            ds = source[:, start:start+tile]
            mask = torch.arange(start, start+ds.shape[1], device='cuda')[None, :] < lengths[:, None]
            m = ds[:, None, :] * da[None, :, None] + db[None, :, None]
            m = torch.bitwise_xor(m, torch.bitwise_and(m >> 29, (1 << 35)-1))
            m = torch.bitwise_xor(m, -(1 << 63))
            m.masked_fill_(~mask[:, None, :], torch.iinfo(torch.int64).max)
            best = torch.minimum(best, m.amin(dim=2))
        out[lo:lo+len(group)] = torch.bitwise_xor(best, -(1 << 63)).cpu().numpy().view(np.uint64)
    return out


def comparisons_cuda(left, right):
    from . import cuda_driver
    if cuda_driver.runtime_name() == 'driver':
        backend = cuda_driver.get_backend()
        result = backend.compare(left, right)
        last_metrics.clear()
        last_metrics.update(backend.last_metrics)
        return result
    import torch
    _reset_cuda_metrics()
    if not len(left):
        return []
    a, b = (np.asarray(rows, dtype=np.uint64).view(np.int64) for rows in (left, right))
    return _cuda_binary(a, b, lambda a, b: (a == b).sum(dim=1)).tolist()


def overlap_cpu(pairs):
    results = []
    for left, right in pairs:
        a, b = Counter(left), Counter(right)
        results.append([min(count, b[token]) for token, count in a.items()])
    return results


def overlap_cuda(pairs):
    """Exact per-token integer intersections; weighted sums stay on the reference CPU."""
    from . import cuda_driver
    if cuda_driver.runtime_name() == 'driver':
        backend = cuda_driver.get_backend()
        result = backend.overlap(pairs)
        last_metrics.clear()
        last_metrics.update(backend.last_metrics)
        return result
    import torch
    _reset_cuda_metrics()
    results = []
    for start in range(0, len(pairs), 256):
        group = pairs[start:start+256]
        counts = [(Counter(left), Counter(right)) for left, right in group]
        width = max((len(left) for left, _ in counts), default=0)
        if not width:
            results.extend([[] for _ in group])
            continue
        left = np.zeros((len(group), width), dtype=np.int64)
        right = np.zeros_like(left)
        for i, (a, b) in enumerate(counts):
            for j, (token, count) in enumerate(a.items()):
                left[i, j], right[i, j] = count, b[token]
        values = _cuda_binary(left, right, torch.minimum).tolist()
        results.extend([row[:len(counts[i][0])] for i, row in enumerate(values)])
    return results
