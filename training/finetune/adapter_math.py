"""Adapter parameter arithmetic. No torch.

``expected_adapter_parameters`` is the number of LoRA parameters of an architecture (for every wrapped projection ``r * (in + out)``, over
all layers); the runner audits the adapter against it before the first step and the importer uses it to tell an adapter of the pinned base
from any other. ``QWEN2_5_CODER_7B`` holds the published architecture of the exact base checkpoint (checked against peft on the meta
device in ``tests/test_model.py``). ``safetensors_parameters`` counts the parameters of an adapter file from its header alone.
"""
from __future__ import annotations

import json
import math
import pathlib
import struct
from types import SimpleNamespace

QWEN2_5_CODER_7B = SimpleNamespace(hidden_size=3584, intermediate_size=18944, num_hidden_layers=28, num_attention_heads=28,
                                   num_key_value_heads=4)


def architecture_from(facts) -> SimpleNamespace | None:
    """The architecture a run reported about the model it loaded (``model_loaded`` of its run config), or None when it did not report every number the arithmetic needs
    (runs made before the runner recorded them). ``layers`` is the runner's name for ``num_hidden_layers``."""
    if not isinstance(facts, dict):
        return None
    got = {"hidden_size": facts.get("hidden_size"), "intermediate_size": facts.get("intermediate_size"),
           "num_hidden_layers": facts.get("num_hidden_layers", facts.get("layers")), "num_attention_heads": facts.get("num_attention_heads"),
           "num_key_value_heads": facts.get("num_key_value_heads")}
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in got.values()):
        return None
    if isinstance(facts.get("head_dim"), int) and facts["head_dim"] > 0:
        got["head_dim"] = facts["head_dim"]
    return SimpleNamespace(**got)


def expected_adapter_parameters(hf_config, r: int, modules, use_dora: bool = False) -> int:
    """LoRA parameters of the architecture: for every wrapped projection ``r * (in + out)``, over all layers. DoRA adds one magnitude value per output feature of each
    wrapped projection (``out``); ``tests/test_model.py`` checks both against peft on the meta device."""
    h, inter = hf_config.hidden_size, hf_config.intermediate_size
    heads, kv = hf_config.num_attention_heads, hf_config.num_key_value_heads
    head_dim = getattr(hf_config, "head_dim", None) or h // heads
    shapes = {"q_proj": (h, heads * head_dim), "k_proj": (h, kv * head_dim), "v_proj": (h, kv * head_dim),
              "o_proj": (heads * head_dim, h), "gate_proj": (h, inter), "up_proj": (h, inter), "down_proj": (inter, h)}
    return hf_config.num_hidden_layers * sum(r * (i + o) + (o if use_dora else 0) for name, (i, o) in shapes.items() if name in set(modules))


def safetensors_parameters(path) -> int:
    """The number of parameters in a safetensors file, from its header. The data must fit the header (a truncated file is refused)."""
    path = pathlib.Path(path)
    size = path.stat().st_size
    with path.open("rb") as fh:
        raw = fh.read(8)
        if len(raw) < 8:
            raise ValueError("not a safetensors file: the header is missing")
        (n,) = struct.unpack("<Q", raw)
        if n <= 0 or 8 + n > size:
            raise ValueError("not a safetensors file: the header length does not fit the file")
        header = json.loads(fh.read(n))
    tensors = {k: v for k, v in header.items() if k != "__metadata__"}
    end = max((v["data_offsets"][1] for v in tensors.values()), default=0)
    if 8 + n + end > size:
        raise ValueError(f"truncated safetensors file: the header needs {8 + n + end:,} bytes, the file has {size:,}")
    return sum(math.prod(v["shape"]) for v in tensors.values())
