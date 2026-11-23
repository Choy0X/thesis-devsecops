"""Will a model fit on this computer? Memory estimates for running a model and for fine-tuning it with QLoRA, and the verdict against the device profile.

Every number here is an estimate, built from the shape of the model (read from its own configuration, never typed in) and a few stated assumptions. The
callers must show it as an estimate; the only way to know is to run it. Nothing is guessed when a dimension is missing: the function says which one.

Sources of the formulas
* Key and value cache: two tensors (key and value) per layer, ``kv_heads * head_dim`` wide, one row per token of context.
* QLoRA: the linear projections of the layers are held in 4 bits (NF4) with double quantisation, about 4.127 bits per weight, as the QLoRA paper reports (Dettmers
  et al., 2023); this figure is taken from the paper and has not been re-measured here. The embedding table and the output head are not quantised: they are 16-bit, and
  32-bit once ``prepare_model_for_kbit_training`` has run (read in the installed peft, ``peft/utils/other.py``, which casts every 16-bit parameter that is not 4-bit). The
  head is the same table as the embedding when the model ties them. Adapter weights, their gradients and the two AdamW moments are held in 32 bits.
* Activations: with gradient checkpointing only the input of each layer is kept, plus the full activations of the one layer being recomputed, for which the
  per-layer size ``s * b * h * (34 + 5 * a * s / h)`` bytes (16-bit, no parallelism) of Korthikanti et al. (2022) is used; this has also not been re-measured.
* The language-model head produces ``batch * context * vocabulary`` logits in 32 bits, which dominates for models with a large vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass

GB = 1024 ** 3
OVERHEAD_GB = 0.75            # the CUDA context, the framework's workspace and fragmentation: an assumption, to be replaced by a measured value
MARGIN = 0.10                 # a model is called "fits" only when it leaves this share of the memory free
NF4_BITS_PER_WEIGHT = 4.127
ADAPTER_BYTES_PER_PARAM = 4 + 4 + 8          # weight, gradient, two AdamW moments, all in 32 bits


class MissingDimension(ValueError):
    """The model's configuration lacks a number the estimate needs; the message names it."""


@dataclass(frozen=True)
class Shape:
    """The dimensions of a transformer language model, as its configuration file states them."""
    params: int | None = None                # total parameters
    n_layers: int | None = None
    hidden: int | None = None
    n_heads: int | None = None
    n_kv_heads: int | None = None            # equal to n_heads without grouped-query attention
    head_dim: int | None = None              # hidden / n_heads when absent
    intermediate: int | None = None          # width of the feed-forward block
    vocab: int | None = None
    tie_embeddings: bool | None = None       # the output head shares the input embedding table; None (not stated) is counted as separate, which can only overestimate

    def need(self, *names: str) -> None:
        missing = [n for n in names if getattr(self, n) in (None, 0)]
        if missing:
            raise MissingDimension(f"the model's configuration does not state: {', '.join(missing)}")

    def kv_dim(self) -> int:
        self.need("n_heads", "hidden")
        heads = self.n_kv_heads or self.n_heads
        return heads * (self.head_dim or self.hidden // self.n_heads)


def kv_cache_bytes(shape: Shape, context: int, bytes_per_element: int = 2) -> int:
    """Memory of the key and value cache for ``context`` tokens (16-bit by default)."""
    shape.need("n_layers")
    return 2 * shape.n_layers * shape.kv_dim() * int(context) * bytes_per_element


def inference_estimate(shape: Shape, context: int, weights_bytes: int | None = None, overhead_gb: float = OVERHEAD_GB) -> dict:
    """Memory to run the model: the weights (the size of the file when known, else 16-bit weights), the cache and a fixed overhead, in bytes."""
    if weights_bytes is None:
        shape.need("params")
        weights_bytes = shape.params * 2
    kv = kv_cache_bytes(shape, context)
    overhead = int(overhead_gb * GB)
    return {"weights": int(weights_bytes), "kv_cache": kv, "overhead": overhead, "total": int(weights_bytes) + kv + overhead}


def lora_params(shape: Shape, rank: int, mlp: bool = True) -> int:
    """Parameters of LoRA adapters on every attention projection and, with ``mlp``, on the three feed-forward projections of every layer."""
    shape.need("n_layers", "hidden")
    kv = shape.kv_dim()
    h = shape.hidden
    per_layer = rank * ((h + h) + (h + kv) + (h + kv) + (h + h))                      # q, k, v, o
    if mlp:
        shape.need("intermediate")
        per_layer += 3 * rank * (h + shape.intermediate)                              # gate, up, down
    return per_layer * shape.n_layers


def qlora_weights(shape: Shape, upcast_16bit: bool = True) -> dict:
    """The base weights of a QLoRA fine-tune in bytes, in the two kinds they are held in: ``quantised`` (every layer's linear projections, in 4 bits) and ``embeddings_and_head``
    (the input embedding table and, unless it shares that table, the output head), which bitsandbytes does not quantise. They stay 16-bit, and 32-bit when
    ``upcast_16bit`` (``prepare_model_for_kbit_training`` casts every 16-bit parameter that is not 4-bit to 32 bits, which the training configuration turns on). For a 7-billion
    model with a 152,000-word vocabulary this is the difference between 3.2 GiB and 7.2 GiB."""
    shape.need("params", "hidden", "vocab")
    table = shape.vocab * shape.hidden
    outside = table if shape.tie_embeddings else 2 * table
    outside = min(outside, shape.params)
    return {"quantised": int((shape.params - outside) * NF4_BITS_PER_WEIGHT / 8), "embeddings_and_head": outside * (4 if upcast_16bit else 2)}


def qlora_estimate(shape: Shape, context: int, micro_batch: int, rank: int, mlp: bool = True, overhead_gb: float = OVERHEAD_GB, upcast_16bit: bool = True) -> dict:
    """Memory to fine-tune with QLoRA and gradient checkpointing, in bytes, split into its parts. ``weights`` is both kinds of base weights (see ``qlora_weights``)."""
    shape.need("params", "n_layers", "hidden", "n_heads", "vocab")
    weights = sum(qlora_weights(shape, upcast_16bit).values())
    adapters = lora_params(shape, rank, mlp) * ADAPTER_BYTES_PER_PARAM
    boundary = shape.n_layers * micro_batch * context * shape.hidden * 2
    one_layer = int(micro_batch * context * shape.hidden * (34 + 5 * shape.n_heads * context / shape.hidden))
    logits = micro_batch * context * shape.vocab * 4
    overhead = int(overhead_gb * GB)
    parts = {"weights": weights, "adapters_and_optimizer": adapters, "activations": boundary + one_layer, "logits": logits, "overhead": overhead}
    parts["total"] = sum(parts.values())
    return parts


def cpu_offload_estimate(shape: Shape, context: int, micro_batch: int, rank: int) -> dict:
    shape.need("params", "n_layers", "hidden", "n_heads", "vocab", "intermediate")
    adapter_params = lora_params(shape, rank, True)
    head = shape.vocab * shape.hidden * 2
    projection = max(shape.hidden * shape.intermediate, shape.hidden * shape.hidden) * 2
    one_layer = int(micro_batch * context * shape.hidden * (34 + 5 * shape.n_heads * context / shape.hidden))
    parts = {"weights": head + projection, "adapters_and_optimizer": adapter_params * 8,
             "activations": one_layer, "logits": 64 * shape.vocab * 4, "overhead": int(OVERHEAD_GB * GB)}
    parts["total"] = sum(parts.values())
    parts["host_required"] = shape.params * 2 + adapter_params * 12 + int(2 * GB)
    return parts


def verdict(total_bytes: int, free_bytes: int | None, margin: float = MARGIN) -> str:
    """``fits`` with the margin left free, ``tight`` when it fits but without the margin, ``too_big`` when it does not, ``unknown`` without a figure."""
    if not free_bytes:
        return "unknown"
    if total_bytes <= free_bytes * (1 - margin):
        return "fits"
    return "tight" if total_bytes <= free_bytes else "too_big"


def check(estimate_total: int, device: dict, *, download_bytes: int = 0, drive: str | None = None) -> dict:
    """The estimate against a device profile (``device.snapshot()``): the graphics card first, then the main memory, and the disk for a download.

    ``gpu`` is judged against the free video memory of the first card, ``ram`` against the free main memory (running on the processor alone), ``disk``
    against the free space of ``drive`` (else the disk with the most free space)."""
    cards = (device.get("gpu") or {}).get("cards") or []
    gpu_free = cards[0]["free_mb"] * 1024 * 1024 if cards else None
    ram_free = int(float((device.get("ram") or {}).get("free_gb", 0)) * GB) or None
    disks = device.get("disks") or []
    chosen = next((d for d in disks if d.get("drive") == drive), None) if drive else (max(disks, key=lambda d: d.get("free_gb", 0)) if disks else None)
    disk_free = int(chosen["free_gb"] * GB) if chosen else None
    out = {"gpu": verdict(estimate_total, gpu_free), "ram": verdict(estimate_total, ram_free),
           "disk": ("unknown" if disk_free is None else "fits" if download_bytes <= disk_free * (1 - MARGIN) else "tight" if download_bytes <= disk_free else "too_big"),
           "gpu_free_bytes": gpu_free, "ram_free_bytes": ram_free, "disk_free_bytes": disk_free, "disk_drive": chosen.get("drive") if chosen else None}
    out["runs_on"] = "gpu" if out["gpu"] in ("fits", "tight") else "cpu" if out["ram"] in ("fits", "tight") else "nowhere"
    return out


# -- reading the model's own metadata (no network here: the callers hand over what the Hugging Face API and the configuration file returned) ------------

def shape_from_config(config: dict, params: int | None = None) -> Shape:
    """The dimensions out of a Hugging Face ``config.json`` (the field names are the library's own). A nested ``text_config`` (multimodal models) is
    looked at when the top level has no layer count. ``params`` comes from the model information (``safetensors.total``), not from the name."""
    c = config.get("text_config") if "num_hidden_layers" not in config and isinstance(config.get("text_config"), dict) else config
    heads = c.get("num_attention_heads")
    return Shape(params=params, n_layers=c.get("num_hidden_layers"), hidden=c.get("hidden_size"), n_heads=heads,
                 n_kv_heads=c.get("num_key_value_heads") or heads, head_dim=c.get("head_dim"),
                 intermediate=c.get("intermediate_size"), vocab=c.get("vocab_size"),
                 tie_embeddings=c.get("tie_word_embeddings", config.get("tie_word_embeddings")))


def parameters_from_info(info: dict) -> int | None:
    """The total parameter count the Hub states for a model with safetensors weights (``safetensors.total``); None when it states none."""
    total = (info.get("safetensors") or {}).get("total")
    return int(total) if isinstance(total, (int, float)) and total > 0 else None


_QUANT = None


# -- which quantisation to offer first -------------------------------------------------------------------------------------------------------------------

QUANT_BITS = {"Q8_0": 8.50, "Q6_K": 6.56, "Q5_1": 6.0, "Q5_K_M": 5.69, "Q5_K_S": 5.54, "Q5_0": 5.5, "Q4_1": 5.0, "Q4_K_M": 4.89, "Q4_K_S": 4.58, "Q4_0": 4.5, "IQ4_NL": 4.5, "Q3_K_L": 4.27,
              "IQ4_XS": 4.25, "Q3_K_M": 3.91, "Q3_K_S": 3.5, "Q2_K": 3.35, "F16": 16.0, "BF16": 16.0, "F32": 32.0}        # typical bits per weight; the order of quality
RECOMMEND_CAP = QUANT_BITS["Q6_K"]            # above this the quality gain is negligible while the file and the time per token keep growing
RECOMMEND_FLOOR = QUANT_BITS["Q4_K_M"]        # below this the loss of quality is too large to be a default, even when it would squeeze onto the card
RECOMMEND_CONTEXT = 4096                      # the context the cache is counted for
CACHE_SHARE = 0.08                            # the cache as a share of the file when the model's shape is not known (an estimate)


def quant_bits(tag: str) -> float:
    """Bits per weight of a quantisation tag: the table, else the digit in the tag plus a half, else far above the cap so that an unknown tag is never preferred."""
    import re
    t = tag.upper()
    if t in QUANT_BITS:
        return QUANT_BITS[t]
    m = re.match(r"(?:IQ|Q)(\d)", t)
    return int(m.group(1)) + 0.5 if m else 99.0


def recommend_quant(quant_bytes: dict[str, int], device: dict | None, shape: Shape | None = None, context: int = RECOMMEND_CONTEXT) -> dict | None:
    """The quantisation to offer first among the ones a repository has (``{"Q4_K_M": bytes, ...}``), with the reason in a sentence: ``{"quant", "reason", "fit"}`` where ``fit`` is
    ``card`` (it fits the free video memory with the usual margin), ``split`` (it does not, so it runs split between the card and the processor), ``too_big`` (not even that) or
    ``unknown`` (no memory reading). The best quantisation up to Q6_K that fits the card, counting the cache for ``context`` tokens from the model's shape (else as a share of the file,
    which the reason says is an estimate); when none at Q4_K_M or above fits, Q4_K_M (the usual balance) rather than something smaller only to squeeze in. None for no quantisation."""
    if not quant_bytes:
        return None
    ranked = sorted(quant_bytes, key=lambda t: -quant_bits(t))
    pool = [t for t in ranked if quant_bits(t) <= RECOMMEND_CAP] or [min(ranked, key=quant_bits)]      # a repository with only large files: the smallest of them
    cards = ((device or {}).get("gpu") or {}).get("cards") or []
    gpu_free = cards[0]["free_mb"] * 1024 * 1024 if cards else None
    ram = (device or {}).get("ram") or {}
    ram_total = int(float(ram.get("total_gb") or 0) * GB)
    exact = True
    try:
        cache = kv_cache_bytes(shape, context) if shape else None
    except MissingDimension:
        cache = None
    if cache is None:
        exact = False

    def total(tag: str) -> int:
        return quant_bytes[tag] + (cache if cache is not None else int(CACHE_SHARE * quant_bytes[tag])) + int(OVERHEAD_GB * GB)

    def size(tag: str) -> str:
        n = quant_bytes[tag]
        return f"{n / GB:.1f} GB" if n >= 0.1 * GB else f"{n / 1024 ** 2:.1f} MB"           # a test model is megabytes, not 0.0 GB

    note = "" if exact else " The memory for the conversation is an estimate."
    if gpu_free:
        best = next((t for t in pool if verdict(total(t), gpu_free) == "fits"), None)
        if best and quant_bits(best) >= RECOMMEND_FLOOR:
            return {"quant": best, "fit": "card", "reason": f"{best}: {size(best)}, leaves room on the card ({gpu_free / GB:.1f} GB free).{note}"}
    target = next((t for t in pool if quant_bits(t) <= RECOMMEND_FLOOR), None) or min(pool, key=quant_bits)
    if not gpu_free and not ram_total:
        return {"quant": target, "fit": "unknown", "reason": f"{target}: {size(target)}, the usual balance of size and quality; there is no free-memory reading to choose by.{note}"}
    capacity = (gpu_free or 0) + int(ram_total * 0.85)
    if quant_bytes[target] > capacity:
        small = min(pool, key=lambda t: quant_bytes[t])
        return {"quant": small, "fit": "too_big", "reason": f"{small}: {size(small)}, the smallest this repository has. Even that may not run on this computer ({gpu_free / GB if gpu_free else 0:.1f} GB free on the card, {ram_total / GB:.0f} GB main memory).{note}"}
    where = f"the card's {gpu_free / GB:.1f} GB free" if gpu_free else "a card"
    return {"quant": target, "fit": "split", "reason": f"{target}: {size(target)}, the usual balance of size and quality. It does not fit {where} as a whole, so it runs split between the card and the processor, more slowly.{note}"}


def _quant_pattern():
    global _QUANT
    import re
    if _QUANT is None:
        _QUANT = re.compile(r"(?<![A-Za-z0-9])((?:IQ|Q)\d(?:_[A-Z0-9]+)*|F16|F32|BF16)(?![A-Za-z0-9])", re.IGNORECASE)
    return _QUANT


def gguf_names(siblings: list[dict], quant: str) -> list[str]:
    """The exact file names of one quantisation (``Q4_K_M``, whatever the case of the file name), the parts of a split file included, for a download to ask for by name: a
    pattern with the tag in one case does not match a file name in the other (the Hugging Face library matches case-sensitively)."""
    out = []
    for s in siblings:
        name = s.get("rfilename") or ""
        if name.lower().endswith(".gguf"):
            m = _quant_pattern().search(name.rsplit("/", 1)[-1])
            if m and m.group(1).upper() == quant.upper():
                out.append(name)
    return sorted(out)


def single_gguf_sizes(siblings: list[dict]) -> dict[str, int]:
    """Known sizes of whole model GGUF files, keyed by their exact repository paths."""
    import re
    return {
        name: int(size)
        for sibling in siblings
        if isinstance(name := sibling.get("rfilename"), str)
        and name.lower().endswith(".gguf")
        and not name.rsplit("/", 1)[-1].lower().startswith("mmproj")
        and not re.search(r"-\d{5}-of-\d{5}", name)
        and isinstance(size := sibling.get("size"), (int, float)) and size > 0
    }


def gguf_sizes(siblings: list[dict]) -> dict[str, int]:
    """The size in bytes of each GGUF file of a repository by its quantisation tag (``Q4_K_M``, ``Q8_0``, ``F16`` ...), read from the file list with sizes.
    Files without a recognisable tag, and the parts of a split file, are left out rather than guessed."""
    global _QUANT
    import re
    if _QUANT is None:
        _QUANT = re.compile(r"(?<![A-Za-z0-9])((?:IQ|Q)\d(?:_[A-Z0-9]+)*|F16|F32|BF16)(?![A-Za-z0-9])", re.IGNORECASE)
    out: dict[str, int] = {}
    for name, size in single_gguf_sizes(siblings).items():
        m = _QUANT.search(name.rsplit("/", 1)[-1])
        if m:
            out[m.group(1).upper()] = size
    return out
