"""Fused unsigned kernels compiled with PyTorch's NVRTC, no MSVC dependency.

Only this fixed source is compiled. Inputs are numeric buffers, never source code
to compile. The application process does not import CUDA or Torch.
"""
import ctypes as ct
import os
from pathlib import Path
import threading

import numpy as np

SOURCE = r'''
extern "C" __global__ void shingles(const unsigned long long* src,
    const long long* offsets, const unsigned long long* powers, int width,
    unsigned long long* dest) {
    int row = blockIdx.x;
    long long start = offsets[row], count = offsets[row+1]-start-width+1;
    for (long long i=threadIdx.x; i<count; i+=blockDim.x) {
        unsigned long long value = 0;
        for (int j=0;j<width;j++) value += (src[start+i+j]+1ull)*powers[j];
        dest[start+i] = value;
    }
}
extern "C" __global__ void minhash(const unsigned long long* src,
    const long long* offsets, const unsigned long long* a,
    const unsigned long long* b, int permutations, int trim,
    unsigned long long* dest) {
    int row=blockIdx.x, p=blockIdx.y;
    long long start=offsets[row], count=offsets[row+1]-start-trim;
    unsigned long long value=~0ull;
    for (long long i=threadIdx.x;i<count;i+=blockDim.x) {
        unsigned long long h=src[start+i]*a[p]+b[p];
        h ^= h>>29;
        value = value<h ? value:h;
    }
    for (int d=16;d>0;d>>=1) {
        unsigned long long other=__shfl_down_sync(0xffffffffu,value,d);
        value=value<other ? value:other;
    }
    __shared__ unsigned long long partial[4];
    int lane=threadIdx.x & 31, warp=threadIdx.x>>5;
    if (!lane) partial[warp]=value;
    __syncthreads();
    if (!warp) {
        value=lane<4 ? partial[lane]:~0ull;
        for (int d=16;d>0;d>>=1) {
            unsigned long long other=__shfl_down_sync(0xffffffffu,value,d);
            value=value<other ? value:other;
        }
        if (!lane) dest[row*permutations+p]=value;
    }
}
'''

_compiled = None
_lock = threading.Lock()


def _check(code, operation):
    if code:
        raise RuntimeError(f'{operation} failed with CUDA status {code}')


def _compile():
    global _compiled
    with _lock:
        if _compiled is not None:
            return _compiled
        import torch
        torch.cuda.init()
        context_anchor = torch.empty(1, dtype=torch.int64, device='cuda')
        library = Path(torch.__file__).parent / 'lib'
        if os.name != 'nt':
            raise OSError('Fused kernel discovery currently supports the Windows CUDA package')
        candidates = [p for p in library.glob('nvrtc64_*.dll') if '.alt.' not in p.name]
        if not candidates:
            raise OSError('NVRTC is not bundled with this runtime')
        rtc = ct.CDLL(str(candidates[0]))
        driver = ct.WinDLL('nvcuda.dll')
        rtc.nvrtcCreateProgram.argtypes = [ct.POINTER(ct.c_void_p), ct.c_char_p, ct.c_char_p, ct.c_int, ct.c_void_p, ct.c_void_p]
        rtc.nvrtcCompileProgram.argtypes = [ct.c_void_p, ct.c_int, ct.POINTER(ct.c_char_p)]
        for name in ('nvrtcGetPTXSize', 'nvrtcGetProgramLogSize'):
            getattr(rtc, name).argtypes = [ct.c_void_p, ct.POINTER(ct.c_size_t)]
        for name in ('nvrtcGetPTX', 'nvrtcGetProgramLog'):
            getattr(rtc, name).argtypes = [ct.c_void_p, ct.c_void_p]
        rtc.nvrtcDestroyProgram.argtypes = [ct.POINTER(ct.c_void_p)]
        program = ct.c_void_p()
        _check(rtc.nvrtcCreateProgram(ct.byref(program), SOURCE.encode(), b'campaign.cu', 0, None, None), 'NVRTC creation')
        try:
            major, minor = torch.cuda.get_device_capability()
            options = (ct.c_char_p * 2)(f'--gpu-architecture=compute_{major}{minor}'.encode(), b'--std=c++11')
            status = rtc.nvrtcCompileProgram(program, 2, options)
            if status:
                size = ct.c_size_t()
                rtc.nvrtcGetProgramLogSize(program, ct.byref(size))
                log = ct.create_string_buffer(size.value)
                rtc.nvrtcGetProgramLog(program, log)
                raise RuntimeError('CUDA kernel compilation failed: ' + log.value.decode(errors='replace')[:1200])
            size = ct.c_size_t()
            _check(rtc.nvrtcGetPTXSize(program, ct.byref(size)), 'NVRTC size')
            ptx = ct.create_string_buffer(size.value)
            _check(rtc.nvrtcGetPTX(program, ptx), 'NVRTC PTX')
        finally:
            rtc.nvrtcDestroyProgram(ct.byref(program))
        driver.cuModuleLoadData.argtypes = [ct.POINTER(ct.c_void_p), ct.c_void_p]
        driver.cuModuleGetFunction.argtypes = [ct.POINTER(ct.c_void_p), ct.c_void_p, ct.c_char_p]
        driver.cuLaunchKernel.argtypes = [ct.c_void_p, *([ct.c_uint]*7), ct.c_void_p, ct.POINTER(ct.c_void_p), ct.c_void_p]
        module = ct.c_void_p()
        _check(driver.cuModuleLoadData(ct.byref(module), ptx), 'Module loading')
        functions = {}
        for name in ('minhash', 'shingles'):
            function = ct.c_void_p()
            _check(driver.cuModuleGetFunction(ct.byref(function), module, name.encode()), 'Kernel lookup')
            functions[name] = function
        _compiled = driver, module, functions, rtc
        return _compiled


def _launch(name, grid, stream, args):
    driver, module, functions, _ = _compile()
    parameters = (ct.c_void_p * len(args))(*[ct.cast(ct.byref(a), ct.c_void_p) for a in args])
    _check(driver.cuLaunchKernel(functions[name], grid[0], grid[1], 1, 128, 1, 1, 0,
                                 ct.c_void_p(stream), parameters, None), 'Kernel launch')


_buffers = {}
last_metrics = {}


def minhash(rows, a, b, powers=None):
    global last_metrics
    last_metrics = {}
    import torch
    if not len(rows):
        return np.empty((0, len(a)), dtype=np.uint64)
    _compile()
    key = (a.tobytes(), b.tobytes(), powers.tobytes() if powers is not None else None)
    if key not in _buffers:
        if len(_buffers) >= 4:
            _buffers.clear()
        coefficients = [torch.from_numpy(v.view(np.int64).copy()).to('cuda') for v in (a, b)]
        dp = torch.from_numpy(powers.view(np.int64).copy()).to('cuda') if powers is not None else None
        _buffers[key] = (*coefficients, dp, torch.cuda.Stream(), torch.cuda.Stream())
    da, db, dp, copy_stream, compute_stream = _buffers[key]
    # Two in-flight groups bound pinned RAM/VRAM while transfers overlap compute.
    result = np.empty((len(rows), len(a)), dtype=np.uint64)
    pending, timings = [], []
    def finish():
        start, host_output, done, references = pending.pop(0)
        done.synchronize()
        result[start:start+len(host_output)] = host_output.numpy().view(np.uint64)
    try:
        for start in range(0, len(rows), 1024):
            group = rows[start:start+1024]
            offsets = np.zeros(len(group)+1, dtype=np.int64)
            offsets[1:] = np.cumsum([len(row) for row in group])
            values = np.concatenate(group) if offsets[-1] else np.zeros(1, dtype=np.uint64)
            host = [torch.from_numpy(np.array(v.view(np.int64), copy=True)).pin_memory() for v in (values, offsets)]
            copying, ready, kernel, computed, done = [torch.cuda.Event(enable_timing=True) for _ in range(5)]
            with torch.cuda.stream(copy_stream):
                copying.record(copy_stream)
                src, off = [v.to('cuda', non_blocking=True) for v in host]
                ready.record(copy_stream)
            host_output = torch.empty((len(group), len(a)), dtype=torch.int64, pin_memory=True)
            with torch.cuda.stream(compute_stream):
                compute_stream.wait_event(ready)
                # Coefficients were initialized on the caller's stream.
                compute_stream.wait_stream(torch.cuda.default_stream())
                kernel.record(compute_stream)
                output = torch.empty_like(host_output, device='cuda')
                hashed, trim = src, 0
                if powers is not None:
                    hashed = torch.empty_like(src)
                    _launch('shingles', (len(group), 1), compute_stream.cuda_stream, [ct.c_void_p(src.data_ptr()), ct.c_void_p(off.data_ptr()),
                        ct.c_void_p(dp.data_ptr()), ct.c_int(len(powers)), ct.c_void_p(hashed.data_ptr())])
                    trim = len(powers)-1
                _launch('minhash', (len(group), len(a)), compute_stream.cuda_stream, [ct.c_void_p(hashed.data_ptr()), ct.c_void_p(off.data_ptr()),
                    ct.c_void_p(da.data_ptr()), ct.c_void_p(db.data_ptr()), ct.c_int(len(a)), ct.c_int(trim), ct.c_void_p(output.data_ptr())])
                computed.record(compute_stream)
                host_output.copy_(output, non_blocking=True)
                done.record(compute_stream)
            timings.append((copying, ready, kernel, computed, done))
            pending.append((start, host_output, done, (host, src, off, hashed, output)))
            if len(pending) == 2:
                finish()
        while pending:
            finish()
    finally:
        # Buffers cannot be released while CUDA still refers to them, even on OOM.
        copy_stream.synchronize()
        compute_stream.synchronize()
    last_metrics = {'gpu_kernel_seconds': sum(c.elapsed_time(d) for a, b, c, d, e in timings)/1000,
                    'gpu_transfer_seconds': sum(a.elapsed_time(b)+d.elapsed_time(e) for a, b, c, d, e in timings)/1000}
    return result
