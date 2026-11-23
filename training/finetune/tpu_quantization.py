"""Experimental frozen INT4 base projections with explicit input gradients.

Storage is two signed four-bit values per byte, symmetric blocks along input
features. No inference-only quantized operator or bitsandbytes dependency.
Merging adapters into these layers is unsupported; save standard PEFT adapters.
"""
from __future__ import annotations

import math

import torch
import torch.nn.functional as F


def _unpack(packed, scales, in_features, dtype):
    lo = (packed & 15).to(torch.int16) - 8
    hi = (packed >> 4).to(torch.int16) - 8
    values = torch.stack((lo, hi), dim=-1).flatten(-2).to(dtype)
    return (values * scales.to(dtype).unsqueeze(-1)).flatten(1)[:, :in_features]


class _FrozenProjection(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, packed, scales, bias, in_features):
        ctx.save_for_backward(packed, scales)
        ctx.in_features, ctx.dtype = in_features, x.dtype
        return F.linear(x, _unpack(packed, scales, in_features, x.dtype), None if bias is None else bias.to(x.dtype))

    @staticmethod
    def backward(ctx, grad):
        packed, scales = ctx.saved_tensors
        weight = _unpack(packed, scales, ctx.in_features, ctx.dtype)
        return grad.to(ctx.dtype).matmul(weight), None, None, None, None


class FrozenInt4Linear(torch.nn.Linear):
    """Linear-compatible PEFT target; its empty weight only supplies device/dtype.

    Frozen values live in buffers and are reconstructed per projection. They are
    not retained as BF16 tensors by autograd across the model's forward pass.
    """
    def __init__(self, in_features, out_features, block_size=128, dtype=torch.bfloat16):
        torch.nn.Module.__init__(self)
        if block_size < 2 or block_size % 2:
            raise ValueError("INT4 block size must be a positive even number")
        self.in_features, self.out_features, self.block_size = in_features, out_features, block_size
        self.weight = torch.nn.Parameter(torch.empty(0, dtype=dtype), requires_grad=False)
        blocks = math.ceil(in_features / block_size)
        self.register_buffer("packed", torch.empty(out_features, blocks, block_size // 2, dtype=torch.uint8))
        self.register_buffer("scales", torch.empty(out_features, blocks, dtype=torch.float32))
        self.register_buffer("bias", None)

    @classmethod
    def from_linear(cls, linear, block_size=128):
        return cls.from_weight(linear.weight.detach().cpu(), linear.bias.detach().cpu() if linear.bias is not None else None,
                               block_size=block_size)

    @classmethod
    def from_weight(cls, weight, bias=None, block_size=128):
        out_features, in_features = weight.shape
        layer = cls(in_features, out_features, block_size, weight.dtype)
        padded = F.pad(weight.float(), (0, math.ceil(in_features / block_size) * block_size - in_features))
        blocks = padded.view(out_features, -1, block_size)
        scales = blocks.abs().amax(-1).div(7)
        scales = torch.where(scales > 0, scales, torch.ones_like(scales))
        values = (torch.round(blocks / scales.unsqueeze(-1)).clamp(-7, 7).to(torch.int16) + 8).to(torch.uint8)
        layer.packed = values[..., 0::2] | (values[..., 1::2] << 4)
        layer.scales = scales
        layer.bias = bias
        return layer

    def dequantize(self, dtype):
        return _unpack(self.packed, self.scales, self.in_features, dtype)

    def forward(self, x):
        return _FrozenProjection.apply(x, self.packed, self.scales, self.bias, self.in_features)

    def quantization_metadata(self):
        return {"algorithm": "symmetric_block_int4/1", "block_size": self.block_size,
                "rounding": "nearest_even", "range": [-7, 7], "packing": "low_nibble_first_offset_8",
                "scale_dtype": str(self.scales.dtype), "storage_bytes": self.packed.numel() + 4 * self.scales.numel()}


def quantize_model(model, block_size=128):
    for name, module in list(model.named_modules()):
        if isinstance(module, torch.nn.Linear) and not isinstance(module, FrozenInt4Linear):
            parent, _, leaf = name.rpartition(".")
            setattr(model.get_submodule(parent) if parent else model, leaf, FrozenInt4Linear.from_linear(module, block_size))
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    model.tpu_quantization = {"algorithm": "symmetric_block_int4/1", "block_size": block_size,
                              "hardware_validation": "pending", "excluded": ["embeddings", "normalization"]}
    return model


def load_tpu_model(cfg, *, revision, backend):
    """Read one safetensors tensor at a time; never load the BF16 base on TPU."""
    import json
    import pathlib
    import re
    from accelerate.utils import set_module_tensor_to_device
    from huggingface_hub import snapshot_download
    from safetensors import safe_open
    from transformers import AutoConfig, AutoModelForCausalLM
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("TPU loading requires a pinned model commit")
    validate_tpu_config(cfg)
    architecture = AutoConfig.from_pretrained(cfg.model.id, revision=revision, trust_remote_code=False)
    if architecture.model_type != "qwen2":
        raise ValueError("experimental TPU INT4 loader currently supports the Qwen2 architecture only")
    with torch.device("meta"):
        model = AutoModelForCausalLM.from_config(architecture, dtype=torch.bfloat16, attn_implementation="sdpa")
    from training.finetune.adapter_math import expected_adapter_parameters
    linear_names = {name + ".weight" for name, module in model.named_modules() if isinstance(module, torch.nn.Linear)}
    quantized = sum(math.ceil(p.shape[1] / 128) * p.shape[0] * (64 + 4) for name, p in model.named_parameters() if name in linear_names)
    unquantized = sum(2 * p.numel() for name, p in model.named_parameters() if name not in linear_names)
    adapter = expected_adapter_parameters(architecture, cfg.lora.r, cfg.lora.target_modules, False) * 16
    estimate = {"frozen_storage_bytes": quantized + unquantized, "adapter_master_gradient_adam_bytes": adapter,
                "static_lower_bound_bytes": quantized + unquantized + adapter,
                "scope": "Lower bound only; excludes activations, compiler buffers and dequantization temporaries. Not a fit verdict.",
                "adapter_compute": "BF16 autocast with FP32 master parameters/gradients/AdamW state"}
    print("TPU memory preflight:", json.dumps(estimate), flush=True)
    memory = backend.memory()
    if memory.get("capacity_bytes") and estimate["static_lower_bound_bytes"] >= memory["capacity_bytes"]:
        raise RuntimeError("Requested TPU configuration exceeds memory even before activations; settings were not changed")
    folder = pathlib.Path(snapshot_download(cfg.model.id, revision=revision,
                                          allow_patterns=["*.safetensors", "*.safetensors.index.json"], max_workers=2))
    index = folder / "model.safetensors.index.json"
    if index.exists():
        locations = json.loads(index.read_text())["weight_map"]
    else:
        with safe_open(folder / "model.safetensors", framework="pt", device="cpu") as stream:
            locations = {key: "model.safetensors" for key in stream.keys()}
    def tensor(name):
        with safe_open(folder / locations[name], framework="pt", device="cpu") as stream:
            return stream.get_tensor(name).to(torch.bfloat16)
    original_parameters = list(model.named_parameters())
    original_count = sum(p.numel() for _, p in original_parameters)
    replaced = set()
    for name, module in list(model.named_modules()):
        if isinstance(module, torch.nn.Linear):
            layer = FrozenInt4Linear.from_weight(tensor(name + ".weight"), tensor(name + ".bias") if module.bias is not None else None)
            parent, _, leaf = name.rpartition(".")
            setattr(model.get_submodule(parent) if parent else model, leaf, layer.to(backend.device))
            replaced.update({name + ".weight", name + ".bias"})
            backend.synchronize()
    for name, _ in original_parameters:
        if name not in replaced:
            set_module_tensor_to_device(model, name, backend.device, value=tensor(name))
    for name, module in list(model.named_modules()):
        if "RotaryEmbedding" in type(module).__name__:
            parent, _, leaf = name.rpartition(".")
            setattr(model.get_submodule(parent), leaf, type(module)(config=architecture).to(backend.device))
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    model.tpu_quantization = {"algorithm": "symmetric_block_int4/1", "block_size": 128,
                              "hardware_validation": "pending", "original_parameters": original_count,
                              "excluded": ["embeddings", "normalization"], "memory_estimate": estimate}
    backend.synchronize()
    return model


def validate_tpu_config(cfg):
    if cfg.precision.mode != "tpu_int4_bf16":
        raise ValueError("TPU requires the explicit tpu_int4_bf16 profile; CUDA NF4 cannot be reused")
    if cfg.training.optimizer != "adamw_torch" or cfg.training.pack_sequences or cfg.lora.use_dora or cfg.lora.init_loftq:
        raise ValueError("TPU currently requires AdamW, unpacked sequences and ordinary LoRA without DoRA or LoftQ")
