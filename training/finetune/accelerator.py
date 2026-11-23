"""Optional accelerator operations. Importing this module never imports XLA."""
from __future__ import annotations

import contextlib
import random
from dataclasses import dataclass

import torch


@dataclass
class Backend:
    name: str
    device: torch.device
    dtype: torch.dtype
    xm: object = None
    xla: object = None

    def autocast(self):
        if self.name == "tpu":
            # PEFT keeps FP32 trainable/master weights; XLA matmuls compute in BF16.
            return torch.autocast("xla", dtype=torch.bfloat16)
        if self.name == "cuda":
            return torch.autocast("cuda", dtype=self.dtype, enabled=self.dtype != torch.float32)
        return contextlib.nullcontext()

    def synchronize(self):
        if self.name == "tpu":
            self.xla.sync(wait=True)
        elif self.name == "cuda":
            torch.cuda.synchronize(self.device)

    def optimizer_step(self, optimizer):
        if self.name == "tpu":
            self.xm.optimizer_step(optimizer, barrier=False)
            self.synchronize()
        else:
            optimizer.step()

    def to_cpu(self, value):
        if isinstance(value, torch.Tensor):
            return value.detach().cpu()
        if isinstance(value, dict):
            return {k: self.to_cpu(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.to_cpu(v) for v in value)
        return value

    def memory(self):
        result = {"allocated_bytes": None, "reserved_bytes": None, "peak_allocated_bytes": None,
                  "capacity_bytes": None, "availability": "Accelerator memory counters unavailable on this backend"}
        if self.name == "cuda":
            result.update(allocated_bytes=torch.cuda.memory_allocated(self.device),
                          reserved_bytes=torch.cuda.memory_reserved(self.device),
                          peak_allocated_bytes=torch.cuda.max_memory_allocated(self.device),
                          capacity_bytes=torch.cuda.get_device_properties(self.device).total_memory,
                          availability="CUDA allocator counters; excludes non-PyTorch allocations")
        elif self.name == "tpu":
            try:
                info = self.xm.get_memory_info(self.device)
                # Keep the original diagnostic with its API-defined units; do not guess byte conversions.
                result.update(xla_memory_info=info, availability="XLA raw memory diagnostic; allocator peaks unavailable")
                if "bytes_limit" in info and "bytes_free" in info:
                    result.update(capacity_bytes=info["bytes_limit"], free_bytes=info['bytes_free'],
                                  allocated_bytes=info["bytes_limit"] - info["bytes_free"])
                elif 'bytes_limit' in info and 'bytes_used' in info:
                    result.update(capacity_bytes=info['bytes_limit'], allocated_bytes=info['bytes_used'],
                                  free_bytes=info['bytes_limit'] - info['bytes_used'])
            except (AttributeError, RuntimeError, TypeError) as error:
                result["availability"] = str(error)
        try:
            import psutil
            result["host_rss_bytes"] = psutil.Process().memory_info().rss
        except ImportError:
            result.update(host_rss_bytes=None, host_memory_availability="psutil not installed")
        return result

    def rng_state(self):
        state = {"python": random.getstate(), "torch": torch.get_rng_state(), "backend": self.name}
        if self.name == "cuda":
            state["cuda"] = torch.cuda.get_rng_state_all()
            state['cuda_selected_index'] = (self.device.index if self.device.index is not None
                                             else torch.cuda.current_device())
        elif self.name == "tpu":
            state["xla"] = self.xm.get_rng_state(self.device)
        return state

    def restore_rng(self, state):
        """Restore all generators on unchanged topology, or map the training GPU.

        Legacy checkpoints have no selected-index metadata. With changed visible
        topology they use logical GPU zero, the original default training device.
        GPU/kernel/precision changes can change numerics even with restored RNG;
        cross-hardware continuation does not promise bitwise reproducibility.
        """
        random.setstate(state["python"])
        torch.set_rng_state(state["torch"])
        if self.name == "cuda" and state.get("cuda") is not None:
            states = state['cuda']
            target = self.device.index if self.device.index is not None else torch.cuda.current_device()
            count = torch.cuda.device_count()
            legacy = 'cuda_selected_index' not in state
            source = state.get('cuda_selected_index', 0)
            if type(source) is not int or not 0 <= source < len(states):
                raise ValueError('Checkpoint has no valid selected CUDA generator state to restore')
            remapped = len(states) != count or source != target
            if remapped:
                torch.cuda.set_rng_state(states[source], self.device)
            else:
                torch.cuda.set_rng_state_all(states)
            self.rng_restore_report = {'selected_device_remapped': remapped,
                                       'saved_selected_index': source, 'selected_index': target,
                                       'saved_device_count': len(states), 'visible_device_count': count,
                                       'legacy_selected_index_assumed': legacy and remapped,
                                       'portability_note': ('RNG continuity restored; different GPU models, precision '
                                                            'modes or kernels can change floating-point results.'
                                                            if remapped else None)}
        if self.name == "tpu" and "xla" in state:
            self.xm.set_rng_state(state["xla"], self.device)


def select_backend(requested: str = "auto", *, physical_kind: str | None = None) -> Backend:
    if physical_kind not in (None, 'cuda', 'tpu'):
        raise ValueError('Validated accelerator kind must be cuda or tpu')
    bound_cuda = physical_kind == 'cuda'
    if requested == 'auto':
        if physical_kind is None:
            from training.finetune.runtime_compatibility import detect_runtime
            physical_kind = detect_runtime()['kind']
        if physical_kind == 'tpu':
            requested = 'tpu'
    if requested == "tpu" or requested.startswith("xla"):
        try:
            import torch_xla
            import torch_xla.core.xla_model as xm
            import torch_xla.runtime as xr
            if xr.device_type() != "TPU":
                raise RuntimeError("XLA runtime is not a TPU")
            return Backend("tpu", torch_xla.device(), torch.bfloat16, xm, torch_xla)
        except (ImportError, RuntimeError) as error:
            raise RuntimeError(f"TPU execution requires matching torch/torch_xla and a TPU runtime: {error}") from error
    if requested == "auto" and torch.cuda.is_available():
        candidates = []
        errors = []
        for index in range(torch.cuda.device_count()):
            device = torch.device(f"cuda:{index}")
            try:
                props = torch.cuda.get_device_properties(device)
                # NF4 requires a Pascal or newer visible CUDA device. Do not
                # rank occupied cards by physical capacity or marketing name.
                if props.major < 6:
                    continue
                # Match the hardware-validated setup policy for its pinned
                # repair builds; do not select an excluded sibling GPU.
                if bound_cuda and (props.major, props.minor) < (7, 5):
                    continue
                free, total = torch.cuda.mem_get_info(device)
                if free <= 0 or total <= 0:
                    continue
                candidates.append((int(free), int(total), -index, device))
            except (RuntimeError, AssertionError, AttributeError) as error:
                errors.append(str(error))
        if not candidates:
            raise RuntimeError("No compatible visible CUDA GPU has available memory counters; "
                               "check driver access and free GPU memory. " + "; ".join(errors))
        requested = str(max(candidates, key=lambda row: row[:3])[3])
    elif requested == "auto":
        requested = "cpu"
    if requested.startswith("cuda"):
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA was requested but no CUDA device is available")
        device = torch.device(requested)
        # PyTorch's BF16 query uses the current device. Scope the query to the
        # selected logical GPU, including CUDA_VISIBLE_DEVICES remapping.
        with torch.cuda.device(device):
            bf16 = torch.cuda.is_bf16_supported(including_emulation=False)
        return Backend("cuda", device, torch.bfloat16 if bf16 else torch.float16)
    if requested == "cpu":
        return Backend("cpu", torch.device("cpu"), torch.float32)
    raise ValueError(f"unknown accelerator {requested!r}")
