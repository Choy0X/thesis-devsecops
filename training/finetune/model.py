"""Model plumbing: the base model, the LoRA adapter and its audit, precision, and hidden states.

* ``load_base_model`` loads the exact base checkpoint at its pinned commit, never with remote code. The 4-bit (QLoRA) path
  needs a CUDA GPU and bitsandbytes and refuses clearly without them; it is exercised on the GPU runtime (Colab or Kaggle).
* ``add_adapter`` wraps the model with the contract's LoRA (rank 64, alpha 128, the seven projections).
* ``audit_adapter`` checks that exactly the adapter trains: the trainable parameters equal the count computed from the
  architecture (161,480,704 for Qwen2.5-Coder-7B: 28 layers, hidden 3584, intermediate 18944, key/value width 512) and no base
  weight is trainable. The same count is obtained on the meta device through peft, without loading any weight.
* Precision: bf16 when the GPU supports it, else fp16 with loss scaling; a non-finite loss stops the run (no mid-run switch).
* ``hidden_states`` runs the backbone only, so the language-model head can be applied at supervised positions alone.
"""
from __future__ import annotations

import re

import torch

from training.finetune.adapter_math import expected_adapter_parameters  # noqa: F401  (torch-free; re-exported)

_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class AdapterAuditError(RuntimeError):
    """The trainable parameters are not exactly the adapter the contract describes."""


class NonFiniteLoss(FloatingPointError):
    """The loss is NaN or infinite; the run stops instead of training on it."""


# -- a tiny model for tests and the plumbing smoke test ---------------------------------------------------------------------------

def tiny_config(vocab_size: int, hidden: int = 32, layers: int = 2, heads: int = 4, kv_heads: int = 2, intermediate: int = 64):
    import transformers
    return transformers.Qwen2Config(vocab_size=vocab_size, hidden_size=hidden, intermediate_size=intermediate, num_hidden_layers=layers,
                                    num_attention_heads=heads, num_key_value_heads=kv_heads, max_position_embeddings=256)


def build_tiny_model(vocab_size: int, seed: int = 0):
    """A small randomly initialised Qwen2 on the CPU (same architecture, tiny sizes)."""
    import transformers
    torch.manual_seed(seed)
    return transformers.Qwen2ForCausalLM(tiny_config(vocab_size))


# -- the adapter ------------------------------------------------------------------------------------------------------------------------

def lora_config(settings):
    from peft import LoraConfig
    return LoraConfig(r=settings.r, lora_alpha=settings.lora_alpha, lora_dropout=settings.lora_dropout, bias=settings.bias,
                      task_type=settings.task_type, use_rslora=settings.use_rslora, use_dora=bool(getattr(settings, "use_dora", False)),
                      target_modules=list(settings.target_modules))


def add_adapter(model, settings):
    from peft import get_peft_model
    return get_peft_model(model, lora_config(settings))


def _weights_folder(model_id: str, revision: str) -> str | None:
    """The folder of the cached commit that holds the model's safetensors (or their index), or None. The cache may hold only the files that were needed, so the folder is
    found from a file in it and not by asking for the whole snapshot (which refuses a snapshot that lacks the repository's other files)."""
    import os

    from huggingface_hub import try_to_load_from_cache
    for name in ("model.safetensors", "model.safetensors.index.json"):
        found = try_to_load_from_cache(model_id, name, revision=revision)
        if isinstance(found, str):
            return os.path.dirname(found)
    return None


def init_loftq(peft_model, model_id: str, revision: str) -> dict:
    """LoftQ: set the adapter of a 4-bit model so that the quantised weight plus the adapter is closer to the full-precision weight than the quantised weight alone.

    peft reads the full-precision weights from the model's own safetensors files, which must be on this computer (it never downloads), and puts the correction in
    at scale 1. The layer applies lora_alpha / r (2 for this project's rank 64 and alpha 128), which would double the correction and make the model worse than plain
    4-bit (measured), so lora_B is divided by that scale. The adapter keeps its shape, so the audit's parameter count is unchanged."""
    import time

    from peft import replace_lora_weights_loftq
    try:
        from peft.tuners.lora import Linear4bit                    # only defined where bitsandbytes is installed
    except ImportError as e:
        raise RuntimeError("LoftQ needs bitsandbytes and a model loaded in 4 bits; bitsandbytes cannot be loaded here") from e

    if not any(isinstance(m, Linear4bit) for m in peft_model.modules()):
        raise RuntimeError("LoftQ needs a model loaded in 4 bits with the adapter on its linear layers; this model has none")
    folder = _weights_folder(model_id, revision)
    if folder is None:
        raise RuntimeError(f"LoftQ reads the full-precision weights of {model_id} at {revision[:12]} from this computer, and they are not here (no model.safetensors of that commit in the Hugging Face cache)")
    started = time.monotonic()
    replace_lora_weights_loftq(peft_model, model_path=folder)
    layers = 0
    for module in peft_model.modules():
        if isinstance(module, Linear4bit):
            for name in module.lora_B:
                module.lora_B[name].weight.data /= module.scaling[name]
            layers += 1
    return {"layers": layers, "seconds": round(time.monotonic() - started, 1), "scaled_by": "lora_alpha / r"}


def adapter_parameters_on_meta(hf_config, settings) -> int:
    """The trainable parameters peft creates for this architecture, counted on the meta device (no weights are allocated)."""
    import transformers
    from peft import get_peft_model
    with torch.device("meta"):
        model = transformers.AutoModelForCausalLM.from_config(hf_config)
        peft_model = get_peft_model(model, lora_config(settings))
    return sum(p.numel() for p in peft_model.parameters() if p.requires_grad)


def audit_adapter(peft_model, hf_config, settings) -> dict:
    """Pass when exactly the contract adapter is trainable; raise ``AdapterAuditError`` otherwise."""
    trainable = sum(p.numel() for p in peft_model.parameters() if p.requires_grad)
    base = [n for n, p in peft_model.named_parameters() if p.requires_grad and "lora_" not in n]
    if base:
        raise AdapterAuditError(f"base weights are trainable: {base[:5]}")
    dora = bool(getattr(settings, "use_dora", False))
    expected = expected_adapter_parameters(hf_config, settings.r, settings.target_modules, dora)
    if trainable != expected:
        raise AdapterAuditError(f"trainable adapter parameters {trainable:,} != expected {expected:,} "
                                f"(r={settings.r}, modules={list(settings.target_modules)}, dora={dora})")
    return {"ok": True, "trainable": trainable, "expected": expected, "r": settings.r, "modules": list(settings.target_modules), "use_dora": dora}


# -- precision ---------------------------------------------------------------------------------------------------------------------------

def bf16_native() -> bool:
    """Whether the GPU does bf16 in hardware. ``torch.cuda.is_bf16_supported()`` also answers True for a Turing card (a T4) through slow
    emulation, which is not what we want for training: such a card uses fp16 with loss scaling instead."""
    return bool(torch.cuda.is_available() and torch.cuda.is_bf16_supported(including_emulation=False))


def choose_compute_dtype(*, cuda: bool | None = None, bf16_supported: bool | None = None) -> torch.dtype:
    cuda = torch.cuda.is_available() if cuda is None else cuda
    if not cuda:
        return torch.float32                                   # the CPU path (tests); training itself needs the GPU
    if bf16_supported is None:
        bf16_supported = bf16_native()
    return torch.bfloat16 if bf16_supported else torch.float16


def needs_grad_scaler(dtype: torch.dtype) -> bool:
    return dtype == torch.float16


def assert_finite(loss: torch.Tensor, step: int) -> None:
    if not bool(torch.isfinite(loss).all()):
        raise NonFiniteLoss(f"non-finite loss at step {step}: {float(loss.detach())}")


# -- forward helpers ---------------------------------------------------------------------------------------------------------------------

def _base(model):
    return model.get_base_model() if hasattr(model, "get_base_model") else model


def hidden_states(model, input_ids: torch.Tensor, attention_mask: torch.Tensor, position_ids: torch.Tensor | None = None) -> torch.Tensor:
    """The final (normalised) hidden states (B, T, H), without applying the language-model head. ``position_ids`` is for a packed row, where each example counts its own positions."""
    kw = {} if position_ids is None else {"position_ids": position_ids}
    return _base(model).model(input_ids=input_ids, attention_mask=attention_mask, **kw).last_hidden_state


def packed_attention_bias(segment_ids: torch.Tensor, dtype: torch.dtype) -> torch.Tensor:
    """The attention mask of packed rows, (B, 1, T, T) and additive: a token sees the tokens before it (and itself) of its own example only. Zero where it may look, the most negative
    number of ``dtype`` where it may not; both the eager and the fused attention accept it. Checked against separate rows in tests/test_packing.py."""
    T = segment_ids.shape[1]
    allowed = (segment_ids[:, :, None] == segment_ids[:, None, :]) & torch.ones(T, T, dtype=torch.bool, device=segment_ids.device).tril()[None]
    return torch.zeros(allowed.shape, dtype=dtype, device=segment_ids.device).masked_fill(~allowed, torch.finfo(dtype).min)[:, None]


def lm_head_of(model):
    return _base(model).lm_head


def prepare_for_training(model, *, gradient_checkpointing: bool = True, checkpoint_fn=None) -> None:
    """No key/value cache while training, and optionally gradient checkpointing (recompute activations to save memory)."""
    model.config.use_cache = False
    if gradient_checkpointing:
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": checkpoint_fn is not None})
        if checkpoint_fn is not None:
            import functools
            for module in model.modules():
                if hasattr(module, "_gradient_checkpointing_func"):
                    module._gradient_checkpointing_func = functools.partial(checkpoint_fn, use_reentrant=True)
        if hasattr(model, "enable_input_require_grads"):
            model.enable_input_require_grads()


# -- loading the real model ---------------------------------------------------------------------------------------------------------------

def _auto_model():
    from transformers import AutoModelForCausalLM
    return AutoModelForCausalLM


def _dtype_kw(dtype: torch.dtype) -> dict:
    import transformers
    from packaging.version import Version
    return {"dtype": dtype} if Version(transformers.__version__) >= Version("4.56") else {"torch_dtype": dtype}


def load_base_model(cfg, *, revision: str, quantize: bool | None = None, device_map="auto", model_id: str | None = None, compute_dtype: torch.dtype | None = None):
    """The base checkpoint at ``revision`` (a pinned 40-character commit), 4-bit NF4 by default, never with remote code.

    ``model_id`` is only for a software smoke test of the loader on a small public model (``runner --smoke-model``); it defaults to the
    contract's exact base. ``quantize=False`` with ``compute_dtype=torch.float32`` loads it whole in 32 bits (the processor path of a small public model)."""
    if not isinstance(revision, str) or not _COMMIT.match(revision):
        raise ValueError(f"a pinned 40-character commit revision is required, got {revision!r}")
    quantize = cfg.precision.load_in_4bit if quantize is None else quantize
    kwargs: dict = {"revision": revision, "trust_remote_code": False}
    if quantize:
        if not torch.cuda.is_available():
            raise RuntimeError("4-bit (QLoRA) loading needs a CUDA GPU and bitsandbytes; this runtime has no CUDA. Run the fine-tune "
                               "on Colab or Kaggle, or load without quantization for a software test.")
        try:
            import bitsandbytes  # noqa: F401
        except ImportError as e:
            raise RuntimeError("bitsandbytes is not installed; install it on the GPU runtime for 4-bit loading") from e
        from transformers import BitsAndBytesConfig
        kwargs.update(device_map=device_map, quantization_config=BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type=cfg.precision.bnb_4bit_quant_type,
            bnb_4bit_use_double_quant=cfg.precision.bnb_4bit_use_double_quant,
            bnb_4bit_compute_dtype=compute_dtype or choose_compute_dtype()))
    else:
        kwargs.update(_dtype_kw(compute_dtype or choose_compute_dtype()))
    model = _auto_model().from_pretrained(model_id or cfg.model.id, **kwargs)
    if quantize and cfg.precision.prepare_model_for_kbit_training:
        from peft import prepare_model_for_kbit_training
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=cfg.training.gradient_checkpointing)
    return model
