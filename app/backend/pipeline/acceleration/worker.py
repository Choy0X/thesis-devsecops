"""Private stdio worker. Input is bounded JSON, never executable serialized objects."""
import json
import sys
import time

import numpy as np

MAX_MESSAGE = 32 << 20


def main():
    device = sys.argv[1] if len(sys.argv) > 1 else 'cuda'
    from . import numeric
    from . import wire
    from . import cuda_driver
    backend = None
    try:
        runtime = cuda_driver.runtime_name(device)
        if device == 'cuda':
            if runtime == 'driver':
                backend = cuda_driver.get_backend()
            else:
                import torch
                if not torch.cuda.is_available():
                    raise RuntimeError('The selected runtime has no CUDA device')
                torch.cuda.init()
        print(json.dumps({'ready': True, 'device': device, 'numeric_runtime': runtime,
                          'torch_imported': 'torch' in sys.modules,
                          'numeric_device': backend.identity if backend is not None else None}), flush=True)
    except Exception as error:
        print(json.dumps({'ready': False, 'error': type(error).__name__ + ': ' + str(error)[:700]}), flush=True)
        return

    try:
        for line in iter(lambda: sys.stdin.buffer.readline(MAX_MESSAGE + 1), b''):
            if len(line) > MAX_MESSAGE:
                break
            try:
                request = json.loads(line)
                started = time.perf_counter()
                operation = request['operation']
                if operation == 'close':
                    return
                if operation == 'warmup':
                    from .warmup import validate
                    result = validate(device, *(wire.unpack(request[k]) for k in ('a', 'b', 'powers')))
                elif operation == 'minhash':
                    rows = wire.unpack_rows(request['packed_rows'])
                    a, b = (wire.unpack(request[k]) for k in ('a', 'b'))
                    powers = wire.unpack(request['powers']) if request.get('powers') else None
                    if powers is not None and device == 'cuda':
                        result = numeric.signatures_cuda(rows, a, b, powers=powers)
                    else:
                        result = getattr(numeric, 'signatures_' + device)(rows, a, b, **({'powers': powers} if powers is not None else {}))
                elif operation == 'overlap':
                    result = getattr(numeric, 'overlap_' + device)(request['pairs'])
                elif operation == 'compare':
                    result = (numeric.comparisons_cuda(request['left'], request['right']) if device == 'cuda'
                              else np.sum(np.asarray(request['left']) == np.asarray(request['right']), axis=1).tolist())
                else:
                    raise ValueError('Unknown compute operation')
                metrics = {'worker_seconds': time.perf_counter()-started, 'items': 0 if operation == 'warmup' else len(result)}
                if device == 'cuda':
                    metrics['peak_vram_bytes'] = backend.peak_owned_device_bytes if backend is not None else torch.cuda.max_memory_allocated()
                    metrics['gpu_allocation_scope'] = 'adapter_owned_device_buffers' if backend is not None else 'torch_allocated_device_buffers'
                    metrics['gpu_timing_coverage'] = 'unavailable'
                    if operation in ('overlap', 'compare', 'minhash'):
                        metrics.update(numeric.last_metrics)
                print(json.dumps({**({'packed_result': wire.pack(result)} if operation == 'minhash' else {'result': result}), 'metrics': metrics}), flush=True)
            except Exception as error:
                print(json.dumps({'error': type(error).__name__ + ': ' + str(error)[:500]}), flush=True)
                if backend is not None and backend.poisoned:
                    return
    finally:
        if backend is not None:
            cuda_driver.close()


if __name__ == '__main__':
    main()
