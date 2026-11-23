"""Optional, rate-limited resource telemetry for the selected training device."""
from __future__ import annotations

import copy
import importlib
import platform
import time

import torch


class ResourceMonitor:
    """Read device-wide free memory separately from PyTorch allocator counters.

    Missing optional counters are None, never synthetic zero utilization. NVML
    handles are matched by CUDA UUID or PCI bus ID, never CUDA's logical index.
    """

    def __init__(self, backend, clock=time.monotonic, poll_interval=.5, storage_paths=None):
        self.backend, self.clock = backend, clock
        self.poll_interval = max(0., float(poll_interval))
        self._last_time = None
        self._last = None
        self._nvml = None
        self._handle = None
        self._nvml_attempted = False
        self._process = None
        self._optional_time = None
        self._optional = None
        self.storage_paths = dict(storage_paths or {})

    @property
    def hardware_signature(self):
        result = {"backend": self.backend.name, "device": str(self.backend.device),
                  "machine": platform.machine(), "torch_version": str(torch.__version__),
                  "cuda_version": getattr(torch.version, "cuda", None),
                  "compute_dtype": str(getattr(self.backend, 'dtype', None))}
        if self.backend.name == "cuda":
            try:
                props = torch.cuda.get_device_properties(self.backend.device)
                result.update(device_name=str(props.name), total_bytes=int(props.total_memory))
                result['visible_device_count'] = int(torch.cuda.device_count())
                for key in ("uuid", "major", "minor"):
                    value = getattr(props, key, None)
                    if value is not None:
                        result[key] = value if isinstance(value, (str, int)) else str(value)
            except (RuntimeError, AttributeError, AssertionError):
                result["device_name"] = None
        elif self.backend.name == 'tpu':
            from training.finetune.runtime_compatibility import detect_runtime
            result['tpu_detection'] = detect_runtime()
            try:
                result['total_bytes'] = self.backend.memory().get('capacity_bytes')
            except (RuntimeError, AttributeError, TypeError):
                result['total_bytes'] = None
        return result

    def _load_nvml(self):
        if self._nvml_attempted:
            return
        self._nvml_attempted = True
        try:
            nvml = importlib.import_module("pynvml")
            props = torch.cuda.get_device_properties(self.backend.device)
            uuid = getattr(props, "uuid", None)
            pci = getattr(props, "pci_bus_id", None)
            # Without a stable identity it is safer to omit utilization than
            # attribute another physical GPU's activity to this training run.
            if not uuid and not pci:
                return
            nvml.nvmlInit()
            handle = (nvml.nvmlDeviceGetHandleByUUID(uuid) if uuid
                      else nvml.nvmlDeviceGetHandleByPciBusId(pci))
            self._nvml, self._handle = nvml, handle
        except Exception:
            # pynvml exposes version-dependent exception classes and may be
            # importable even when its native driver library is unavailable.
            self._nvml, self._handle = None, None

    def snapshot(self, force=False):
        now = self.clock()
        if not force and self._last_time is not None and now - self._last_time < self.poll_interval:
            return copy.deepcopy(self._last)
        names = ("free_bytes", "total_bytes", "allocated_bytes", "reserved_bytes", "peak_allocated_bytes",
                 "inactive_split_bytes", "gpu_utilization_percent", "gpu_memory_utilization_percent",
                 "host_rss_bytes", "host_available_bytes", "host_total_bytes", "cpu_utilization_percent")
        result = dict.fromkeys(names)
        available = {"cuda": False, "allocator": False, "nvml": False, "host": False}
        errors = {}
        if self.backend.name == "cuda":
            try:
                free, total = torch.cuda.mem_get_info(self.backend.device)
                result.update(free_bytes=int(free), total_bytes=int(total))
                available["cuda"] = True
            except (RuntimeError, AttributeError, AssertionError) as error:
                errors["cuda"] = str(error)
            try:
                result.update(allocated_bytes=int(torch.cuda.memory_allocated(self.backend.device)),
                              reserved_bytes=int(torch.cuda.memory_reserved(self.backend.device)),
                              peak_allocated_bytes=int(torch.cuda.max_memory_allocated(self.backend.device)))
                result["inactive_split_bytes"] = torch.cuda.memory_stats(self.backend.device).get("inactive_split_bytes.all.current")
                available["allocator"] = True
            except (RuntimeError, AttributeError, AssertionError) as error:
                errors["allocator"] = str(error)
        elif self.backend.name == 'tpu':
            try:
                memory = self.backend.memory()
                result.update(free_bytes=memory.get('free_bytes'), total_bytes=memory.get('capacity_bytes'),
                              allocated_bytes=memory.get('allocated_bytes'))
                available['tpu'] = result['free_bytes'] is not None and result['total_bytes'] is not None
                if not available['tpu']:
                    errors['tpu'] = 'XLA byte memory counters unavailable'
            except (RuntimeError, AttributeError, TypeError) as error:
                errors['tpu'] = str(error)
        # Admission probes force fresh device-wide free memory, while the more
        # expensive optional telemetry keeps its own polling interval.
        if self._optional_time is None or now - self._optional_time >= self.poll_interval:
            self._optional = self._sample_optional()
            self._optional_time = now
        optional, optional_available, optional_errors = self._optional
        result.update(optional)
        available.update(optional_available)
        errors.update(optional_errors)
        result.update(availability=available, availability_errors=errors, sampled_at=now,
                      optional_sampled_at=self._optional_time)
        self._last_time, self._last = now, result
        return copy.deepcopy(result)

    def _sample_optional(self):
        result = dict.fromkeys(("gpu_utilization_percent", "gpu_memory_utilization_percent",
                                "host_rss_bytes", "host_available_bytes", "host_total_bytes", "cpu_utilization_percent"))
        available = {"nvml": False, "host": False}
        errors = {}
        if self.backend.name == "cuda":
            self._load_nvml()
            if self._nvml is not None:
                try:
                    util = self._nvml.nvmlDeviceGetUtilizationRates(self._handle)
                    result.update(gpu_utilization_percent=float(util.gpu), gpu_memory_utilization_percent=float(util.memory))
                    available["nvml"] = True
                except Exception as error:
                    errors["nvml"] = str(error)
        try:
            psutil = importlib.import_module("psutil")
            if self._process is None:
                self._process = psutil.Process()
            memory = psutil.virtual_memory()
            result.update(host_rss_bytes=int(self._process.memory_info().rss),
                          host_available_bytes=int(memory.available),
                          host_total_bytes=int(memory.total) if getattr(memory, 'total', None) is not None else None)
            # First nonblocking psutil CPU sample has no interval to measure.
            cpu = self._process.cpu_percent(interval=None)
            result["cpu_utilization_percent"] = float(cpu) if self._optional_time is not None else None
            available["host"] = True
        except Exception as error:
            # psutil.AccessDenied/NoSuchProcess do not inherit OSError. Host
            # diagnostics must remain optional under restricted containers.
            errors["host"] = str(error)
        from training.finetune.runtime_resources import storage_snapshot
        result['storage'] = storage_snapshot(self.storage_paths)
        available['storage'] = bool(result['storage']) and all(row['free_bytes'] is not None for row in result['storage'].values())
        return result, available, errors

    def reset_peak(self):
        if self.backend.name == "cuda":
            torch.cuda.reset_peak_memory_stats(self.backend.device)
        self._last_time = None
