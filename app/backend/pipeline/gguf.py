"""Read what a GGUF model file says about itself, without loading any weight: its architecture, the sizes of its layers, its context length, its vocabulary and the
number of parameters, summed from the shapes of its tensors. This is what the memory estimate needs to judge a quantised model before a server is started.

The format (versions 2 and 3): the magic ``GGUF``, a version, the number of tensors and of metadata entries, the entries (a key, a type and a value; an array has an
element type and a length), then one description per tensor (name, dimensions, type, offset). Only the header is read; the file is opened for reading and never
written. A file that is not GGUF, or is cut short, raises ``GgufError`` with a sentence.
"""
from __future__ import annotations

import pathlib
import struct

from app.backend.pipeline import fit

MAGIC = b"GGUF"
SUPPORTED = (2, 3)
MAX_STRING = 1 << 24                 # a key or a value longer than this is not a header we trust
MAX_ARRAY_KEEP = 64                  # arrays longer than this (the vocabulary) are counted, not kept
_SCALARS = {0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2), 4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 7: ("<?", 1), 10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8)}
STRING, ARRAY = 8, 9
# general.file_type: the quantisation of the file, for the words a person reads
FILE_TYPES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S",
              15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 32: "BF16"}


class GgufError(ValueError):
    """The file is not a GGUF file this reader understands; the message says why."""


class _Reader:
    def __init__(self, f):
        self.f = f

    def take(self, n: int) -> bytes:
        data = self.f.read(n)
        if len(data) != n:
            raise GgufError("the file ends before its header does (it is cut short or damaged)")
        return data

    def unpack(self, fmt: str, size: int):
        return struct.unpack(fmt, self.take(size))[0]

    def string(self) -> str:
        n = self.unpack("<Q", 8)
        if n > MAX_STRING:
            raise GgufError("a name in the header is implausibly long (the file is damaged)")
        return self.take(n).decode("utf-8", errors="replace")

    def skip_string(self) -> None:
        n = self.unpack("<Q", 8)
        if n > MAX_STRING:
            raise GgufError("a name in the header is implausibly long (the file is damaged)")
        self.f.seek(n, 1)

    def value(self, kind: int, *, keep: bool = True):
        if kind in _SCALARS:
            fmt, size = _SCALARS[kind]
            return self.unpack(fmt, size)
        if kind == STRING:
            return self.string() if keep else self.skip_string()
        if kind == ARRAY:
            inner, count = self.unpack("<I", 4), self.unpack("<Q", 8)
            if inner == ARRAY:
                raise GgufError("an array of arrays in the header is not something this reader handles")
            if inner in _SCALARS:
                size = _SCALARS[inner][1]
                if count > MAX_ARRAY_KEEP:
                    self.f.seek(size * count, 1)
                    return {"count": count}
                return [self.value(inner) for _ in range(count)]
            if count > MAX_ARRAY_KEEP:
                for _ in range(count):
                    self.skip_string()
                return {"count": count}
            return [self.value(inner) for _ in range(count)]
        raise GgufError(f"a value of an unknown type ({kind}) in the header")


def read_header(path) -> dict:
    """``{"version", "tensor_count", "metadata", "parameters", "file_bytes"}`` of a GGUF file; arrays longer than a few entries appear as ``{"count": n}``."""
    path = pathlib.Path(path)
    with path.open("rb") as f:
        r = _Reader(f)
        if f.read(4) != MAGIC:
            raise GgufError(f"{path.name} is not a GGUF file")
        version = r.unpack("<I", 4)
        if version not in SUPPORTED:
            raise GgufError(f"{path.name} is GGUF version {version}; versions {', '.join(map(str, SUPPORTED))} are read")
        tensors, entries = r.unpack("<Q", 8), r.unpack("<Q", 8)
        if entries > 1_000_000 or tensors > 1_000_000:
            raise GgufError("the header counts are implausible (the file is damaged)")
        meta: dict = {}
        for _ in range(entries):
            key = r.string()
            meta[key] = r.value(r.unpack("<I", 4))
        params = 0
        for _ in range(tensors):
            r.skip_string()
            dims = [r.unpack("<Q", 8) for _ in range(r.unpack("<I", 4))]
            r.take(12)                                              # the tensor's type (4 bytes) and its offset in the data (8 bytes)
            count = 1
            for d in dims:
                count *= d
            params += count
    return {"version": version, "tensor_count": tensors, "metadata": meta, "parameters": params, "file_bytes": path.stat().st_size}


POOLING = {0: "none", 1: "mean", 2: "cls", 3: "last", 4: "rank"}            # the values of ``<architecture>.pooling_type``


def describe(path) -> dict:
    """What a person and the memory estimate need: architecture, name, quantisation, context length, layers, widths, vocabulary, parameters and file size."""
    h = read_header(path)
    m = h["metadata"]
    arch = m.get("general.architecture")
    if not isinstance(arch, str):
        raise GgufError(f"{pathlib.Path(path).name} does not say which architecture it is")
    get = lambda suffix: m.get(f"{arch}.{suffix}")
    heads = get("attention.head_count")
    tokens = m.get("tokenizer.ggml.tokens")
    vocab = tokens.get("count") if isinstance(tokens, dict) else (len(tokens) if isinstance(tokens, list) else get("vocab_size"))
    kv_heads = get("attention.head_count_kv")
    if isinstance(heads, list):                                    # some models give one value per layer
        heads = max(heads)
    if isinstance(kv_heads, list):
        kv_heads = max(kv_heads)
    file_type = m.get("general.file_type")
    return {"architecture": arch, "name": m.get("general.name"), "quantisation": FILE_TYPES.get(file_type, f"type {file_type}" if file_type is not None else None),
            "context_length": get("context_length"), "layers": get("block_count"), "hidden": get("embedding_length"), "heads": heads, "kv_heads": kv_heads or heads,
            "head_dim": get("attention.key_length"), "intermediate": get("feed_forward_length"), "vocab": vocab, "pooling": POOLING.get(get("pooling_type")),
            "parameters": m.get("general.parameter_count") or h["parameters"], "file_bytes": h["file_bytes"], "version": h["version"]}


def shape(info: dict) -> fit.Shape:
    """The dimensions for ``fit``, from ``describe``."""
    return fit.Shape(params=info["parameters"], n_layers=info["layers"], hidden=info["hidden"], n_heads=info["heads"], n_kv_heads=info["kv_heads"],
                     head_dim=info["head_dim"], intermediate=info["intermediate"], vocab=info["vocab"])


def estimate(path, context: int, *, kv_bytes_per_element: int = 2) -> dict:
    """The memory to serve the model in this file with ``context`` tokens: the file itself (the weights as stored), the key and value cache and the fixed overhead.
    ``kv_bytes_per_element`` is 2 for a 16-bit cache (the default of the server). An estimate, as everything in ``fit``."""
    info = describe(path)
    s = shape(info)
    if info["context_length"] and context > info["context_length"]:
        context = int(info["context_length"])                    # the server cannot use more than the model was trained for
    parts = fit.inference_estimate(s, context, weights_bytes=info["file_bytes"])
    if kv_bytes_per_element != 2:
        parts["kv_cache"] = fit.kv_cache_bytes(s, context, kv_bytes_per_element)
        parts["total"] = parts["weights"] + parts["kv_cache"] + parts["overhead"]
    return {"info": info, "context": context, "parts": parts}
