"""Opt-in Windows CUDA Driver numerical adapter, without importing Torch.

Only fixed application kernels are compiled. Two transfer slots and a bounded
host coefficient cache own all retained buffers. No CUDA context is reset.
"""
from collections import Counter, OrderedDict
import ctypes as ct
import importlib.util
import os
from pathlib import Path
import threading

import numpy as np

MAX_BYTES = 32 << 20
COEFFICIENT_BYTES = 1 << 20

EXTRA_SOURCE = r'''
extern "C" __global__ void integer_minimum(const unsigned long long* a,
    const unsigned long long* b, unsigned long long* out, unsigned long long n) {
    for (unsigned long long i=blockIdx.x*blockDim.x+threadIdx.x;
         i<n;i+=(unsigned long long)gridDim.x*blockDim.x)
        out[i]=a[i]<b[i]?a[i]:b[i];
}
extern "C" __global__ void equality_count(const unsigned long long* a,
    const unsigned long long* b, unsigned long long* out, unsigned long long width) {
    unsigned long long value=0, start=(unsigned long long)blockIdx.x*width;
    for (unsigned long long i=threadIdx.x;i<width;i+=blockDim.x)
        value += a[start+i]==b[start+i];
    __shared__ unsigned long long partial[128];
    partial[threadIdx.x]=value;
    __syncthreads();
    for (int d=64;d;d>>=1) {
        if (threadIdx.x<d) partial[threadIdx.x]+=partial[threadIdx.x+d];
        __syncthreads();
    }
    if (!threadIdx.x) out[blockIdx.x]=partial[0];
}
'''


def runtime_name(device='cuda'):
    if device != 'cuda':
        return 'cpu'
    value = os.environ.get('ZTSA_EXPERIMENTAL_CUDA_RUNTIME', '')
    if value not in ('', 'driver'):
        raise ValueError('ZTSA_EXPERIMENTAL_CUDA_RUNTIME must be empty or driver')
    return value or 'torch'


class CudaOOM(MemoryError):
    """Only clean allocation failure can trigger bounded numeric splitting."""


def _check(status, operation, *, allocation=False):
    if status:
        error = CudaOOM if allocation and status == 2 else RuntimeError
        raise error(f'{operation} failed with CUDA status {status}')


def nvrtc_path():
    # Top-level discovery does not execute torch/__init__.py or load its DLLs.
    spec = importlib.util.find_spec('torch')
    if spec is None or not spec.origin:
        raise RuntimeError('The selected runtime has no installed NVRTC package')
    directory = Path(spec.origin).resolve().parent / 'lib'
    candidates = sorted(p for p in directory.glob('nvrtc64_*.dll') if '.alt.' not in p.name)
    if len(candidates) != 1:
        raise RuntimeError('The selected runtime requires one bundled NVRTC library')
    return candidates[0].resolve()


class Native:
    """Typed, documented Driver/NVRTC calls. Own one primary-context reference."""
    def __init__(self):
        self.context, self.previous, self.module = ct.c_void_p(), ct.c_void_p(), ct.c_void_p()
        self.retained = False
        self.directory = None
        self.driver = None
        if os.name != 'nt':
            raise RuntimeError('The experimental driver runtime requires Windows')
        if any(key in os.environ for key in ('CUDA_VISIBLE_DEVICES', 'CUDA_DEVICE_ORDER')):
            raise RuntimeError('The driver runtime does not support CUDA visibility or ordering overrides')
        try:
            path = nvrtc_path()
            self.directory = os.add_dll_directory(str(path.parent))
            builtins = sorted(path.parent.glob('nvrtc-builtins64_*.dll'))
            if len(builtins) != 1:
                raise RuntimeError('The selected runtime requires one bundled NVRTC builtins library')
            self.builtins = ct.CDLL(str(builtins[0].resolve()), winmode=0x900)
            self.rtc = ct.CDLL(str(path), winmode=0x900)  # DLL directory + System32 only.
            self.driver = ct.WinDLL('nvcuda.dll', winmode=0x800)  # System32 only.
            self._bind()
            self.call('cuInit', 0)
            count = ct.c_int()
            self.call('cuDeviceGetCount', ct.byref(count))
            if count.value != 1:
                raise RuntimeError('The driver runtime requires exactly one physical CUDA device')
            self.device = ct.c_int()
            self.call('cuDeviceGet', ct.byref(self.device), 0)
            name, pci = ct.create_string_buffer(256), ct.create_string_buffer(64)
            major, minor, version = ct.c_int(), ct.c_int(), ct.c_int()
            self.call('cuDeviceGetName', name, len(name), self.device)
            self.call('cuDeviceGetPCIBusId', pci, len(pci), self.device)
            self.call('cuDeviceGetAttribute', ct.byref(major), 75, self.device)
            self.call('cuDeviceGetAttribute', ct.byref(minor), 76, self.device)
            self.call('cuDriverGetVersion', ct.byref(version))
            self.identity = {'name': name.value.decode(errors='replace'), 'pci_bus_id': pci.value.decode(),
                             'physical_ordinal': 0, 'compute_capability': [major.value, minor.value],
                             'driver_version': version.value}
            self.call('cuCtxGetCurrent', ct.byref(self.previous))
            self.call('cuDevicePrimaryCtxRetain', ct.byref(self.context), self.device)
            self.retained = True
            self.call('cuCtxSetCurrent', self.context)
            self._compile(major.value, minor.value)
        except BaseException:
            self.close()
            raise

    def _bind(self):
        pointer, size, u64, uint, integer = ct.c_void_p, ct.c_size_t, ct.c_uint64, ct.c_uint, ct.c_int
        signatures = {
            'cuInit': [uint], 'cuDeviceGetCount': [ct.POINTER(integer)],
            'cuDeviceGet': [ct.POINTER(integer), integer],
            'cuDeviceGetName': [pointer, integer, integer], 'cuDeviceGetPCIBusId': [pointer, integer, integer],
            'cuDeviceGetAttribute': [ct.POINTER(integer), integer, integer], 'cuDriverGetVersion': [ct.POINTER(integer)],
            'cuCtxGetCurrent': [ct.POINTER(pointer)], 'cuCtxSetCurrent': [pointer],
            'cuDevicePrimaryCtxRetain': [ct.POINTER(pointer), integer], 'cuDevicePrimaryCtxRelease_v2': [integer],
            'cuModuleLoadData': [ct.POINTER(pointer), pointer], 'cuModuleUnload': [pointer],
            'cuModuleGetFunction': [ct.POINTER(pointer), pointer, ct.c_char_p],
            'cuMemAlloc_v2': [ct.POINTER(u64), size], 'cuMemFree_v2': [u64],
            'cuMemHostAlloc': [ct.POINTER(pointer), size, uint], 'cuMemFreeHost': [pointer],
            'cuMemcpyHtoDAsync_v2': [u64, pointer, size, pointer], 'cuMemcpyDtoHAsync_v2': [pointer, u64, size, pointer],
            'cuStreamCreate': [ct.POINTER(pointer), uint], 'cuStreamSynchronize': [pointer], 'cuStreamDestroy_v2': [pointer],
            'cuEventCreate': [ct.POINTER(pointer), uint], 'cuEventRecord': [pointer, pointer],
            'cuEventElapsedTime': [ct.POINTER(ct.c_float), pointer, pointer], 'cuEventDestroy_v2': [pointer],
            'cuLaunchKernel': [pointer, *([uint]*7), pointer, ct.POINTER(pointer), pointer],
        }
        for name, arguments in signatures.items():
            function = getattr(self.driver, name)
            function.argtypes, function.restype = arguments, integer
        rtc_signatures = {'nvrtcCreateProgram': [ct.POINTER(pointer), ct.c_char_p, ct.c_char_p, integer, pointer, pointer],
            'nvrtcCompileProgram': [pointer, integer, ct.POINTER(ct.c_char_p)],
            'nvrtcGetPTXSize': [pointer, ct.POINTER(size)], 'nvrtcGetProgramLogSize': [pointer, ct.POINTER(size)],
            'nvrtcGetPTX': [pointer, pointer], 'nvrtcGetProgramLog': [pointer, pointer],
            'nvrtcDestroyProgram': [ct.POINTER(pointer)]}
        for name, arguments in rtc_signatures.items():
            function = getattr(self.rtc, name)
            function.argtypes, function.restype = arguments, integer

    def call(self, name, *arguments, allocation=False):
        _check(getattr(self.driver, name)(*arguments), name, allocation=allocation)

    def _compile(self, major, minor):
        from .cuda_kernels import SOURCE
        program = ct.c_void_p()
        _check(self.rtc.nvrtcCreateProgram(ct.byref(program), (SOURCE+EXTRA_SOURCE).encode(), b'numeric.cu', 0, None, None), 'nvrtcCreateProgram')
        try:
            options = (ct.c_char_p*2)(f'--gpu-architecture=compute_{major}{minor}'.encode(), b'--std=c++11')
            status = self.rtc.nvrtcCompileProgram(program, 2, options)
            if status:
                size = ct.c_size_t()
                _check(self.rtc.nvrtcGetProgramLogSize(program, ct.byref(size)), 'nvrtcGetProgramLogSize')
                if size.value > 1 << 20:
                    raise RuntimeError('NVRTC compilation failed with oversized diagnostics')
                log = ct.create_string_buffer(max(1, size.value))
                _check(self.rtc.nvrtcGetProgramLog(program, log), 'nvrtcGetProgramLog')
                raise RuntimeError('NVRTC compilation failed: ' + log.value.decode(errors='replace')[:1000])
            size = ct.c_size_t()
            _check(self.rtc.nvrtcGetPTXSize(program, ct.byref(size)), 'nvrtcGetPTXSize')
            if not 0 < size.value <= MAX_BYTES:
                raise RuntimeError('Compiled numerical module exceeds its bound')
            ptx = ct.create_string_buffer(size.value)
            _check(self.rtc.nvrtcGetPTX(program, ptx), 'nvrtcGetPTX')
        finally:
            _check(self.rtc.nvrtcDestroyProgram(ct.byref(program)), 'nvrtcDestroyProgram')
        self.call('cuModuleLoadData', ct.byref(self.module), ptx)
        self.functions = {}
        for name in ('minhash', 'shingles', 'integer_minimum', 'equality_count'):
            function = ct.c_void_p()
            self.call('cuModuleGetFunction', ct.byref(function), self.module, name.encode())
            self.functions[name] = function

    def alloc(self, size):
        value = ct.c_uint64()
        self.call('cuMemAlloc_v2', ct.byref(value), size, allocation=True)
        return value.value
    def free(self, pointer): self.call('cuMemFree_v2', pointer)
    def host_alloc(self, size):
        value = ct.c_void_p()
        self.call('cuMemHostAlloc', ct.byref(value), size, 0, allocation=True)
        return value.value
    def host_free(self, pointer): self.call('cuMemFreeHost', pointer)
    def stream(self):
        value = ct.c_void_p()
        self.call('cuStreamCreate', ct.byref(value), 1)  # Nonblocking, explicitly synchronized.
        return value.value
    def event(self):
        value = ct.c_void_p()
        self.call('cuEventCreate', ct.byref(value), 1)  # Blocking completion, timing enabled.
        return value.value
    def record(self, event, stream): self.call('cuEventRecord', event, stream)
    def sync(self, stream): self.call('cuStreamSynchronize', stream)
    def elapsed(self, start, end):
        value = ct.c_float()
        self.call('cuEventElapsedTime', ct.byref(value), start, end)
        return value.value
    def destroy_event(self, event): self.call('cuEventDestroy_v2', event)
    def destroy_stream(self, stream): self.call('cuStreamDestroy_v2', stream)
    def htod(self, device, host, size, stream): self.call('cuMemcpyHtoDAsync_v2', device, host, size, stream)
    def dtoh(self, host, device, size, stream): self.call('cuMemcpyDtoHAsync_v2', host, device, size, stream)
    def launch(self, name, grid, stream, arguments):
        values = (ct.c_void_p*len(arguments))(*[ct.cast(ct.byref(a), ct.c_void_p) for a in arguments])
        self.call('cuLaunchKernel', self.functions[name], grid[0], grid[1], 1, 128, 1, 1, 0, stream, values, None)
    def close(self):
        if self.module.value:
            self.call('cuModuleUnload', self.module)
            self.module = ct.c_void_p()
        if self.retained:
            self.call('cuCtxSetCurrent', self.previous)
            self.call('cuDevicePrimaryCtxRelease_v2', self.device)
            self.retained = False
        if self.directory is not None:
            self.directory.close()
            self.directory = None


class Slot:
    def __init__(self, backend):
        self.backend, self.native = backend, backend.native
        self.stream, self.events, self.host, self.device, self.capacity = None, [], 0, 0, 0
        self.output = None
        self.stream = self.native.stream()
        for _ in range(4): self.events.append(self.native.event())

    def ensure(self, size):
        if size > MAX_BYTES:
            raise CudaOOM('Numeric transfer slot exceeds 32 MiB')
        if size <= self.capacity:
            return
        self.release_buffers()
        size = max(256, 1 << (size-1).bit_length())
        self.host = self.native.host_alloc(size)
        try:
            self.device = self.native.alloc(size)
        except BaseException:
            self.native.host_free(self.host)
            self.host = 0
            raise
        self.capacity = size
        self.backend.owned_device_bytes += size
        self.backend.peak_owned_device_bytes = max(self.backend.peak_owned_device_bytes, self.backend.owned_device_bytes)

    def finish(self, *, copy=True):
        if self.output is None:
            return None
        self.native.sync(self.stream)
        offset, shape = self.output
        count = int(np.prod(shape))
        result = np.ctypeslib.as_array((ct.c_uint64*count).from_address(self.host+offset)).copy().reshape(shape) if copy else None
        start, ready, computed, done = self.events
        metrics = self.backend.last_metrics
        metrics['gpu_kernel_seconds'] += self.native.elapsed(ready, computed)/1000
        metrics['gpu_transfer_seconds'] += (self.native.elapsed(start, ready)+self.native.elapsed(computed, done))/1000
        self.output = None
        return result

    def release_buffers(self):
        if self.host or self.device:
            self.native.sync(self.stream)
        if self.device:
            self.native.free(self.device)
            self.device = 0
            self.backend.owned_device_bytes -= self.capacity
        if self.host:
            self.native.host_free(self.host)
            self.host = 0
        self.capacity = 0
        self.output = None

    def close(self):
        self.release_buffers()
        while self.events:
            self.native.destroy_event(self.events[-1])
            self.events.pop()
        if self.stream is not None:
            self.native.destroy_stream(self.stream)
            self.stream = None


class Backend:
    def __init__(self, *, native=None):
        self.native = native if native is not None else Native()
        self.identity = self.native.identity
        self.thread = threading.get_ident()
        self.slots, self.coefficients = [], OrderedDict()
        self.owned_device_bytes = self.peak_owned_device_bytes = 0
        self.last_metrics = {}
        self.closed = self.poisoned = False
        self._reset_metrics()

    def _reset_metrics(self):
        self.last_metrics = {'gpu_kernel_seconds': 0.0, 'gpu_transfer_seconds': 0.0,
                             'gpu_timing_coverage': 'complete', 'gpu_allocation_scope': 'adapter_owned_device_buffers'}

    def _check(self):
        if self.closed or self.poisoned:
            raise RuntimeError('CUDA driver backend is closed or unusable')
        if threading.get_ident() != self.thread:
            raise RuntimeError('CUDA driver backend belongs to its initializing thread')

    def _slot(self, number):
        while len(self.slots) <= number:
            # Register before initialization so partial stream/event ownership survives errors.
            slot = Slot.__new__(Slot)
            self.slots.append(slot)
            Slot.__init__(slot, self)
        return self.slots[number]

    def _submit(self, number, inputs, shape, launch, *, scratch=0):
        self._check()
        slot = self._slot(number)
        slot.finish()
        inputs = [np.ascontiguousarray(value, dtype=np.uint64) for value in inputs]
        offsets, offset = [], 0
        for value in inputs:
            offsets.append(offset)
            offset += value.nbytes
        output_offset = offset
        output_size = int(np.prod(shape))*8
        slot.ensure(offset + output_size + scratch)
        start, ready, computed, done = slot.events
        self.native.record(start, slot.stream)
        for value, position in zip(inputs, offsets):
            if value.nbytes:
                ct.memmove(slot.host+position, value.ctypes.data, value.nbytes)
        if offset:
            self.native.htod(slot.device, slot.host, offset, slot.stream)
        self.native.record(ready, slot.stream)
        pointers = [ct.c_uint64(slot.device+position) for position in offsets]
        output = ct.c_uint64(slot.device+output_offset)
        launch(slot.stream, pointers, output, ct.c_uint64(slot.device+output_offset+output_size))
        self.native.record(computed, slot.stream)
        if output_size:
            self.native.dtoh(slot.host+output_offset, output.value, output_size, slot.stream)
        self.native.record(done, slot.stream)
        slot.output = output_offset, shape
        return slot

    def _clean_slots(self):
        # Synchronize every stream before freeing any allocation or permitting OOM retry.
        for slot in self.slots:
            if slot.stream is not None: self.native.sync(slot.stream)
        for slot in self.slots:
            slot.finish(copy=False)
            slot.release_buffers()

    @staticmethod
    def _vector(value):
        result = np.asarray(value, dtype=np.uint64)
        if result.ndim != 1:
            raise ValueError('Numeric vectors must have one dimension')
        return result

    def _seeds(self, a, b, powers):
        a, b = self._vector(a), self._vector(b)
        p = None if powers is None else self._vector(powers)
        if len(a) != len(b) or (p is not None and not len(p)):
            raise ValueError('MinHash coefficients must match and shingles must be nonempty')
        if a.nbytes+b.nbytes+(p.nbytes if p is not None else 0) > MAX_BYTES:
            raise ValueError('Numeric coefficients exceed 32 MiB')
        key = a.tobytes(), b.tobytes(), p.tobytes() if p is not None else None
        if sum(len(v) for v in key if v is not None) <= COEFFICIENT_BYTES:
            if key not in self.coefficients:
                # Four entries and one MiB total, including keys and array payloads.
                size = lambda: sum(sum(len(v) for v in k if v is not None)*2 for k in self.coefficients)
                while self.coefficients and (len(self.coefficients) >= 4 or size()+sum(len(v) for v in key if v is not None)*2 > COEFFICIENT_BYTES):
                    self.coefficients.popitem(last=False)
                if sum(len(v) for v in key if v is not None)*2 <= COEFFICIENT_BYTES:
                    self.coefficients[key] = a.copy(), b.copy(), None if p is None else p.copy()
            if key in self.coefficients:
                self.coefficients.move_to_end(key)
                return self.coefficients[key]
        return a, b, p

    def minhash(self, rows, a, b, powers=None):
        self._check()
        self._reset_metrics()
        rows = [self._vector(row) for row in rows]
        a, b, powers = self._seeds(a, b, powers)
        if sum(row.nbytes for row in rows) > MAX_BYTES or len(rows)*len(a)*8 > MAX_BYTES:
            raise ValueError('Numeric MinHash request exceeds 32 MiB')
        try:
            return self._bounded(rows, a, b, powers)
        except BaseException as error:
            if not isinstance(error, (ValueError, CudaOOM)):
                self.poisoned = True
            raise

    def _bounded(self, rows, a, b, powers):
        try:
            return self._minhash(rows, a, b, powers)
        except CudaOOM:
            pass
        # Drop failed launch tracebacks before reclaiming buffers. Cleanup failures are fatal.
        self._clean_slots()
        if len(rows) > 1:
            middle = len(rows)//2
            return np.concatenate((self._bounded(rows[:middle], a, b, powers), self._bounded(rows[middle:], a, b, powers)))
        overlap = len(powers)-1 if powers is not None else 0
        if not rows or len(rows[0]) <= max(16, 2*overlap+2):
            raise CudaOOM('CUDA remains full at the minimum numeric batch size')
        middle = (len(rows[0])+overlap)//2
        return np.minimum(self._bounded([rows[0][:middle]], a, b, powers),
                          self._bounded([rows[0][middle-overlap:]], a, b, powers))

    def _minhash(self, rows, a, b, powers):
        output = np.empty((len(rows), len(a)), dtype=np.uint64)
        if not len(rows) or not len(a): return output
        pending, sequence = [], 0
        try:
            for lo in range(0, len(rows), 1024):
                group = rows[lo:lo+1024]
                offsets = np.zeros(len(group)+1, dtype=np.uint64)
                offsets[1:] = np.cumsum([len(row) for row in group], dtype=np.uint64)
                values = np.concatenate(group) if offsets[-1] else np.zeros(1, dtype=np.uint64)
                for first in range(0, len(a), 1024):
                    if len(pending) == 2:
                        target, start, begin, end = pending.pop(0)
                        count = target.output[1][0]
                        output[start:start+count, begin:end] = target.finish()
                    number = sequence % 2
                    sequence += 1
                    aa, bb = a[first:first+1024], b[first:first+1024]
                    pp = np.empty(0, dtype=np.uint64) if powers is None else powers
                    def launch(stream, ptr, out, scratch):
                        src, off, da, db, dp = ptr
                        if powers is not None:
                            self.native.launch('shingles', (len(group), 1), stream, [src, off, dp, ct.c_int(len(pp)), scratch])
                            src = scratch
                        self.native.launch('minhash', (len(group), len(aa)), stream,
                            [src, off, da, db, ct.c_int(len(aa)), ct.c_int(0 if powers is None else len(pp)-1), out])
                    slot = self._submit(number, [values, offsets, aa, bb, pp], (len(group), len(aa)), launch,
                                        scratch=values.nbytes if powers is not None else 0)
                    pending.append((slot, lo, first, first+len(aa)))
            for target, start, begin, end in pending:
                count = target.output[1][0]
                output[start:start+count, begin:end] = target.finish()
            return output
        except BaseException:
            # Also finish work on the other slot before retry or process-level failure.
            self._clean_slots()
            raise

    def compare(self, left, right):
        self._check()
        self._reset_metrics()
        left, right = np.asarray(left, dtype=np.uint64), np.asarray(right, dtype=np.uint64)
        if not len(left) and not len(right): return []
        if left.ndim != 2 or left.shape != right.shape:
            raise ValueError('Comparison arrays must have equal two-dimensional shapes')
        if left.nbytes+right.nbytes > MAX_BYTES:
            raise ValueError('Numeric comparison request exceeds 32 MiB')
        result = []
        try:
            for start in range(0, len(left), 1024):
                aa, bb = left[start:start+1024], right[start:start+1024]
                slot = self._submit(0, [aa, bb], (len(aa),), lambda stream, ptr, out, scratch:
                    self.native.launch('equality_count', (len(aa), 1), stream, [*ptr, out, ct.c_uint64(left.shape[1])]))
                result.extend(slot.finish().tolist())
            return result
        except BaseException:
            self.poisoned = True
            self._clean_slots()
            raise

    def overlap(self, pairs):
        self._check()
        self._reset_metrics()
        offsets, left, right = [0], [], []
        for aa, bb in pairs:
            a, b = Counter(aa), Counter(bb)
            for token, count in a.items():
                left.append(count)
                right.append(b[token])
            offsets.append(len(left))
            if len(left)*24 > MAX_BYTES:
                raise ValueError('Numeric overlap request exceeds 32 MiB')
        values = []
        try:
            for start in range(0, len(left), 1 << 18):
                aa, bb = np.asarray(left[start:start+(1<<18)], dtype=np.uint64), np.asarray(right[start:start+(1<<18)], dtype=np.uint64)
                slot = self._submit(0, [aa, bb], (len(aa),), lambda stream, ptr, out, scratch:
                    self.native.launch('integer_minimum', (min(65535, (len(aa)+127)//128), 1), stream,
                                       [*ptr, out, ct.c_uint64(len(aa))]))
                values.extend(slot.finish().tolist())
            return [values[offsets[i]:offsets[i+1]] for i in range(len(pairs))]
        except BaseException:
            self.poisoned = True
            self._clean_slots()
            raise

    def synchronize(self):
        self._check()
        for slot in self.slots:
            if slot.stream is not None: self.native.sync(slot.stream)

    def close(self):
        if self.closed: return
        # A failed synchronization keeps ownership for retry or confirmed OS process cleanup.
        for slot in self.slots:
            if slot.stream is not None: self.native.sync(slot.stream)
        for slot in self.slots: slot.close()
        self.native.close()
        self.coefficients.clear()
        self.closed = True


_backend = None


def get_backend():
    global _backend
    if _backend is None:
        _backend = Backend()
    return _backend


def close():
    global _backend
    if _backend is not None:
        _backend.close()
        _backend = None
