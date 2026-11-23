"""The memory to serve a GGUF model with llama.cpp, from the file itself and from what the real program was measured to allocate.

Measurements used llama-server b11528, CUDA 12.4, started with ``--verbose``, on the GTX 1660: a 5 million parameter file that
shows the structure of every number, and two random-weight models with the head size of a real one, 128, that show flash attention and the activations. What they say, and what this module
therefore does:

* ``-ngl`` counts the output head as the first thing offloaded: with 1 only the output head is on the card, with 2 the last repeating layer is there too, with ``layers + 1`` everything is,
  except the input embedding table, which stays in main memory in every case.
* The key and value cache is ``cells x layers x (key width + value width) x bytes of the cache type``, and a layer's share is on the card only if that layer is.
* The compute buffer, on the card, once a repeating layer is there (it does not grow with the number of layers): without flash attention the attention scores, (4 x heads + 4) bytes for every
  pair of a token of the batch and a cell of the context, plus the activations of a layer; with flash attention only a mask of 2 bytes for every such pair plus the activations. The server turns
  flash attention on by itself for a head size it has a kernel for (128 and others), and off for the tiny file of the first measurement (head size 2).
* What the card's used memory grows by after a load is the model, the cache and the compute buffer plus 65 to 73 MiB for the program's own context, whatever the size.
* On Windows an allocation larger than the free video memory does not fail: it spills into shared system memory and the program runs, slowly (a 9.4 GB compute buffer was "allocated" on the
  6 GB card). So a memory error is not the signal that a load was too big; the estimate before and the card's free memory after are.

What is not measured is said where it is used: the constant part of the activations is a fit to two models (within 5% of both), the effect on a real 7B is not measured.
"""
from __future__ import annotations

import pathlib
import re

from app.backend.pipeline import gguf

MiB = 1024 ** 2
GB = 1024 ** 3
MARGIN = 0.90                          # the share of free memory a model may use and still be called to fit (as in the gateway)
DRIVER_BYTES = 96 * MiB                # the program's own context on the card: 65 to 73 MiB measured, with a margin
FRAGMENTATION = 0.02                   # rounding of the allocations, as a share of what is allocated
FLASH_HEAD_DIMS = (64, 80, 96, 112, 128, 256)          # the head sizes that the server's CUDA flash attention has a kernel for (the auto setting turned it on for 128)
FLASH_ACTIVATION_MARGIN = 1.07         # the fitted activations of a flash attention graph were up to 6.6% above the formula on one of the two models measured
DEFAULT_BATCH = 512                    # the server's physical batch (-ub) when none is given
MIN_BATCH = 32
MIN_OFFLOAD_WIDTH = 32                 # the card does not run the matrices of a model narrower than this (measured on the 8-wide file)

# the bytes an element of the key and value cache takes, for each cache type the server accepts
CACHE_BYTES = {"f32": 4.0, "f16": 2.0, "bf16": 2.0, "q8_0": 34 / 32, "q5_1": 24 / 32, "q5_0": 22 / 32, "q4_1": 20 / 32, "q4_0": 18 / 32}
FULL_PRECISION_CACHES = ("f32", "f16", "bf16")         # a cache in any other type needs flash attention (the server refuses the value cache otherwise)
GGML_TYPES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1", 8: "Q8_0", 9: "Q8_1", 10: "Q2_K", 11: "Q3_K", 12: "Q4_K", 13: "Q5_K", 14: "Q6_K", 15: "Q8_K", 30: "BF16"}
_LAYER = re.compile(r"^blk\.(\d+)\.")


class ServeFitError(ValueError):
    """The estimate cannot be made from this file; the message says why."""


# -- the tensor table ----------------------------------------------------------------------------------------------------------------------------------

def tensor_table(path) -> dict:
    """What the file says about its tensors: ``{"tensors": [{name, shape, type, offset, bytes}], "alignment", "data_start", "metadata"}``.

    The size of each tensor is read from the offsets, as the next tensor's offset minus its own (the last one runs to the end of the file), so that it is exact for every quantisation without a
    table of block sizes; it includes the padding that aligns the next tensor (at most the alignment, 32 bytes by default). The file is opened for reading and never written."""
    path = pathlib.Path(path)
    with path.open("rb") as f:
        r = gguf._Reader(f)
        if f.read(4) != gguf.MAGIC:
            raise gguf.GgufError(f"{path.name} is not a GGUF file")
        version = r.unpack("<I", 4)
        if version not in gguf.SUPPORTED:
            raise gguf.GgufError(f"{path.name} is GGUF version {version}; versions {', '.join(map(str, gguf.SUPPORTED))} are read")
        count, entries = r.unpack("<Q", 8), r.unpack("<Q", 8)
        if entries > 1_000_000 or count > 1_000_000:
            raise gguf.GgufError("the header counts are implausible (the file is damaged)")
        meta = {}
        for _ in range(entries):
            key = r.string()
            meta[key] = r.value(r.unpack("<I", 4))
        tensors = []
        for _ in range(count):
            name = r.string()
            dims = [r.unpack("<Q", 8) for _ in range(r.unpack("<I", 4))]
            kind, offset = r.unpack("<I", 4), r.unpack("<Q", 8)
            tensors.append({"name": name, "shape": dims, "type": kind, "type_name": GGML_TYPES.get(kind, f"type {kind}"), "offset": offset})
        header_end = f.tell()
    alignment = int(meta.get("general.alignment") or 32)
    data_start = -(-header_end // alignment) * alignment
    end = path.stat().st_size - data_start
    ordered = sorted(tensors, key=lambda t: t["offset"])
    for i, t in enumerate(ordered):
        nxt = ordered[i + 1]["offset"] if i + 1 < len(ordered) else end
        t["bytes"] = max(0, nxt - t["offset"])
    return {"tensors": tensors, "alignment": alignment, "data_start": data_start, "metadata": meta}


def groups(table: dict, n_layers: int) -> dict:
    """The weights by where llama.cpp can put them: ``layers`` (bytes of each repeating layer), ``embedding`` (the input table, always in main memory), ``head`` (the output head and what
    goes with it) and ``tied`` (the model has no output head of its own and reuses the embedding table for it, which is then copied to the card with the head)."""
    layers = [0] * int(n_layers)
    embedding = head = other = 0
    has_output = False
    for t in table["tensors"]:
        m = _LAYER.match(t["name"])
        if m and int(m.group(1)) < len(layers):
            layers[int(m.group(1))] += t["bytes"]
        elif t["name"] == "token_embd.weight":
            embedding += t["bytes"]
        elif t["name"] == "output.weight":
            head += t["bytes"]
            has_output = True
        else:
            other += t["bytes"]
    tied = not has_output
    return {"layers": layers, "embedding": embedding, "head": head + other + (embedding if tied else 0), "tied": tied}


# -- the estimate --------------------------------------------------------------------------------------------------------------------------------------

def head_size(info: dict) -> int:
    """The size of one attention head: the file's key length when it gives one, else the width over the heads."""
    return int(info.get("head_dim") or int(info["hidden"]) // int(info["heads"]))


def uses_flash_attention(info: dict, flash_attention: bool | None = None) -> bool:
    """Whether the server will run with flash attention: as asked when it is asked, else (its auto setting) when it has a kernel for the model's head size."""
    if flash_attention is not None:
        return bool(flash_attention)
    return head_size(info) in FLASH_HEAD_DIMS


def compute_bytes(heads: int, hidden: int, intermediate: int, vocab: int, batch: int, context: int, *, on_card: int, flash_attention: bool = False) -> int:
    """The compute buffer on the card. With only the output head there it is the logits of the sequences being decoded (4 of them by default, 4 bytes a word of the vocabulary: 2.39 MiB measured
    for a vocabulary of 152,064). With a repeating layer it is the larger of that and one layer's graph, because the allocator reuses the memory of the layer's graph for the logits.

    Without flash attention the graph is (4 x heads + 4) bytes for every pair of a token of the batch and a cell of the context (measured on three models, exact) plus the activations,
    batch x 4 x the larger of 2 x feed-forward and 6 x hidden (exact for one model, 1.2% above the other). With flash attention it is a mask of 2 bytes a pair plus the activations,
    batch x 4 x (2 x feed-forward + 6 x hidden), which was exact on one model and 6.6% below on the other, so it is taken 7% higher. ``on_card`` is the number of units there (the output
    head and the repeating layers), at least 1: with nothing on the card there is no buffer to estimate (the server shows 0.25 MiB)."""
    logits = int(vocab or 0) * 4 * 4 + 64 * 1024
    if on_card == 1:
        return logits
    ffn = int(intermediate or 4 * hidden)
    if flash_attention:
        graph = 2 * batch * context + FLASH_ACTIVATION_MARGIN * batch * 4 * (2 * ffn + 6 * hidden)
    else:
        graph = (4 * heads + 4) * batch * context + batch * 4 * max(2 * ffn, 6 * hidden)
    return int(max(graph, logits))


def host_compute_bytes(hidden: int, batch: int, context: int, *, flash_attention: bool) -> int:
    """The pinned host memory of the compute graph: batch x hidden x 16 bytes, a mask of 2 bytes (flash attention) or 4 bytes a pair of a token and a cell, and 2 KiB a token of the batch
    without flash attention. Exact (to 0.01 MiB) on the thirteen configurations measured on two models, and on the head-only ones."""
    return int(batch * hidden * 16 + (2 if flash_attention else 4) * batch * context + (0 if flash_attention else 2048 * batch))


def serve_estimate(info: dict, table: dict, context: int, *, gpu_layers: int | None = None, batch: int = DEFAULT_BATCH, cache_type: str = "f16", flash_attention: bool | None = None,
                   adapter_bytes: int = 0, driver_bytes: int = DRIVER_BYTES) -> dict:
    """The memory to serve the model of ``info`` (``gguf.describe``) and ``table`` (``tensor_table``) with ``context`` tokens, with ``gpu_layers`` layers on the card (``-ngl``; None is all,
    the output head counting as the first), a physical batch of ``batch`` tokens and a key and value cache of ``cache_type``. ``flash_attention`` None is the server's own choice, which is on for a
    head size it has a kernel for.

    ``{"gpu": {...}, "cpu": {...}, "gpu_total", "cpu_total", "layers_on_card", "head_on_card", "assumptions"}``. Measured: the weights, the cache, the scores, the mask, the program's context and the
    host memory of the graph. Fitted to two models: the activations. Assumed: the share for rounding."""
    n_layers = int(info.get("layers") or 0)
    if not n_layers or not info.get("hidden") or not info.get("heads"):
        raise ServeFitError("the file does not state its layers, its width or its heads, which the estimate needs")
    if cache_type not in CACHE_BYTES:
        raise ServeFitError(f"the cache type {cache_type!r} is not one of {', '.join(CACHE_BYTES)}")
    flash = uses_flash_attention(info, flash_attention)
    if cache_type not in FULL_PRECISION_CACHES and not flash:
        raise ServeFitError(f"a cache in {cache_type} needs flash attention, which this model's head size ({head_size(info)}) does not have a kernel for")
    if info.get("context_length") and context > info["context_length"]:
        context = int(info["context_length"])                      # the server cannot use more than the model was trained for
    g = groups(table, n_layers)
    ngl = n_layers + 1 if gpu_layers is None else max(0, min(int(gpu_layers), n_layers + 1))
    head_on_card = ngl >= 1
    repeating = max(0, ngl - 1)                                     # the last ones, counted from the end
    on_card = g["layers"][n_layers - repeating:] if repeating else []
    heads, kv_heads = int(info["heads"]), int(info.get("kv_heads") or info["heads"])
    per_layer_cache = int(context * 2 * kv_heads * head_size(info) * CACHE_BYTES[cache_type])
    uses_card = bool(head_on_card or repeating)
    weights_gpu = sum(on_card) + (g["head"] if head_on_card else 0)
    weights_cpu = g["embedding"] + sum(g["layers"][: n_layers - repeating]) + (0 if head_on_card else g["head"])
    # The card runs the operations of layers whose weights stay in main memory too (for a batch of 32 or more it takes the weights across one operation at a time), so its compute buffer is the
    # whole graph's wherever the layers are: measured with only the output head on the card, and with nothing on it, on two models. Matrices this small are not run by the card (measured on the
    # smallest model), and then only what is on the card counts. With no layer on the card the server is started with no device at all, and the whole graph is in main memory.
    ops_offloaded = int(info["hidden"]) >= MIN_OFFLOAD_WIDTH
    shape = (heads, int(info["hidden"]), int(info.get("intermediate") or 0), int(info.get("vocab") or 0), int(batch), int(context))
    units = n_layers + 1 if (uses_card and ops_offloaded) else (1 if head_on_card else 0) + repeating
    compute = compute_bytes(*shape, on_card=units, flash_attention=flash) if uses_card else 0
    parts_gpu = {"weights": weights_gpu, "kv_cache": repeating * per_layer_cache, "compute": compute, "adapter": adapter_bytes if uses_card else 0}
    subtotal = sum(parts_gpu.values())
    parts_gpu["driver"] = driver_bytes if uses_card else 0
    parts_gpu["fragmentation"] = int(FRAGMENTATION * subtotal) if uses_card else 0
    parts_cpu = {"weights": weights_cpu, "kv_cache": (n_layers - repeating) * per_layer_cache,
                 "compute": host_compute_bytes(int(info["hidden"]), int(batch), int(context), flash_attention=flash) if uses_card else compute_bytes(*shape, on_card=n_layers + 1, flash_attention=flash),
                 "adapter": 0 if uses_card else adapter_bytes}
    return {"context": int(context), "batch": int(batch), "cache_type": cache_type, "flash_attention": flash, "gpu_layers": ngl, "layers": n_layers, "layers_on_card": repeating,
            "head_on_card": head_on_card, "gpu": parts_gpu, "cpu": parts_cpu, "gpu_total": sum(parts_gpu.values()), "cpu_total": sum(parts_cpu.values()),
            "per_layer_bytes": g["layers"], "embedding_bytes": g["embedding"], "head_bytes": g["head"], "tied_head": g["tied"],
            "assumptions": ["flash attention is " + (f"on: the head size {head_size(info)} has a kernel and the server's auto setting turned it on for that size when it was measured" if flash
                            else f"off: the head size {head_size(info)} has no kernel, so the attention scores are in the compute buffer"),
                            "the compute buffer is the whole graph's wherever the layers are, as long as one is on the card; with none on it the server is started with no device and the graph is in main memory",
                            "the activations of the compute buffer are fitted to two models (within 5% of both) and not measured on this one",
                            "the program's own context on the card is taken as 96 MiB (65 to 73 MiB measured, with a margin)",
                            "the rounding of the allocations is taken as 2% (an assumption)"]}


def place(info: dict, table: dict, device: dict, context: int, *, batch: int = DEFAULT_BATCH, cache_type: str = "f16", flash_attention: bool | None = None, adapter_bytes: int = 0,
          gpu_layers: int | None = None, margin: float = MARGIN) -> dict:
    """Where the model runs: the most layers on the card that fit the free video memory (the output head first, then the repeating layers from the end), the rest on the processor, or the
    processor alone, or nowhere. ``device`` is ``device.snapshot()``. With ``gpu_layers`` given the choice is the person's and is only checked against the free memory.

    ``{"runs_on", "gpu_layers", "layers", "fits", "total_bytes", "gpu_bytes", "gpu_free_bytes", "ram_free_bytes", "message", "estimate"}``; ``runs_on`` is gpu, split, cpu or nowhere."""
    cards = (device.get("gpu") or {}).get("cards") or []
    gpu_free = int(cards[0]["free_mb"]) * MiB if cards else 0
    ram_free = int(float((device.get("ram") or {}).get("free_gb", 0)) * GB)
    n_layers = int(info.get("layers") or 0)

    def est(ngl):
        return serve_estimate(info, table, context, gpu_layers=ngl, batch=batch, cache_type=cache_type, flash_attention=flash_attention, adapter_bytes=adapter_bytes)

    def fits(e):
        return e["gpu_total"] <= gpu_free * margin and e["cpu_total"] <= ram_free * margin

    def result(e, runs_on, ok, message):
        return {"runs_on": runs_on, "gpu_layers": e["gpu_layers"], "layers": n_layers, "fits": ok, "total_bytes": e["gpu_total"] + e["cpu_total"], "gpu_bytes": e["gpu_total"], "gpu_free_bytes": gpu_free,
                "ram_free_bytes": ram_free, "message": message, "estimate": e}

    def nowhere(e):
        return result(e, "nowhere", False, f"The model needs about {(e['gpu_total'] + e['cpu_total']) / GB:.1f} GB with a context of {context} tokens; {gpu_free / GB:.1f} GB of video memory and "
                      f"{ram_free / GB:.1f} GB of main memory are free: it fits nowhere on this computer. Choose a smaller model or a smaller quantisation, or a shorter context.")

    if gpu_layers is not None:
        e = est(gpu_layers)
        if e["gpu_layers"] <= 0 or not gpu_free:
            e = est(0)
            return result(e, "cpu", e["cpu_total"] <= ram_free * margin, "It runs on the processor." if gpu_layers <= 0 else "No graphics card is free, so it runs on the processor.")
        ok = fits(e)
        return result(e, "gpu" if e["gpu_layers"] > n_layers else "split", ok, f"{e['gpu_layers']} of {n_layers + 1} layers (the output head counts as one) on the card as chosen."
                      + ("" if ok else " That is more than the free memory allows."))
    full = est(None)
    if gpu_free and fits(full):
        return result(full, "gpu", True, f"Every layer fits on the graphics card (about {full['gpu_total'] / GB:.1f} GB of the {gpu_free / GB:.1f} GB that is free).")
    if gpu_free:
        for ngl in range(n_layers, 0, -1):                          # the most that fit: the output head first, then the layers from the end
            e = est(ngl)
            if fits(e):
                return result(e, "split", True, f"{ngl} of {n_layers + 1} layers (the output head counts as one) fit on the card; the rest run on the processor, which is slower.")
    none = est(0)
    if none["cpu_total"] <= ram_free * margin:
        return result(none, "cpu", True, "The model does not fit on the graphics card, so it runs on the processor, which is much slower." if gpu_free
                      else "There is no graphics card with free memory, so it runs on the processor.")
    return nowhere(none)


def max_context(info: dict, table: dict, budget_bytes: int, *, gpu_layers: int | None = None, batch: int = DEFAULT_BATCH, cache_type: str = "f16", flash_attention: bool | None = None,
                adapter_bytes: int = 0, margin: float = MARGIN, step: int = 1024) -> int:
    """The largest context, in whole ``step``s and at most what the model was trained for, with which the model, placed as ``gpu_layers`` says, keeps within ``margin`` of ``budget_bytes``
    (on the card when any of it is there, else in main memory). 0 when not even one step fits. The estimate grows with the context, so the answer is found by halving."""
    top = int(info.get("context_length") or 131072) // step
    lo, hi = 0, top

    def fits(k: int) -> bool:
        e = serve_estimate(info, table, k * step, gpu_layers=gpu_layers, batch=batch, cache_type=cache_type, flash_attention=flash_attention, adapter_bytes=adapter_bytes)
        return (e["gpu_total"] or e["cpu_total"]) <= budget_bytes * margin
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo * step
