"""Tokenization of serialized examples: offset alignment, loss masks, field ownership and token budgets.

The whole example is tokenized as one string (``add_special_tokens=False``) with a fast tokenizer's offsets.
The prompt/completion boundary is the character offset the serializer reports; the prompt is the tokens that
end at or before it and the completion the tokens that start at or after it. A token that crosses the boundary
rejects the example: tokenizing the parts separately would change the tokens the model sees at inference.

* The terminal EOS is appended by hand and is supervised (it is how the model learns to stop).
* Prompt tokens have label -100. Padding is masked by the attention mask and by position, never by token id:
  the base model's pad and EOS tokens share an id, so masking by id would silence the real EOS.
* Each completion token belongs to the field that contains its first character (``Serialized.owner``); keys,
  punctuation and the EOS are ``structure_eos``. Every token has one owner, so nothing is counted twice.
* Budgets come from the phase contract: 4096 tokens in total, with 1024 (1536 for repair) kept for the
  completion. An example over budget is rejected, never silently truncated. ``fit_context`` brings the
  context under the prompt budget by dropping whole low-priority items from the end; the target and any item
  marked ``protected`` are never dropped.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass

from training.finetune import config as cfgmod, contracts as ct, serialization as sz

IGNORE = -100
FIELD_GROUPS = ("decision", "explanation", "evidence", "uncertainty", "patch", "structure_eos")


class Rejected(Exception):
    """An example that cannot be used as it is; ``reason`` says why (and is what the build counts)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Encoded:
    task: str
    input_ids: list
    labels: list
    attention_mask: list
    field_ids: list                  # index into FIELD_GROUPS for a completion token, -1 for the prompt
    prompt_tokens: int
    completion_tokens: int           # including the EOS
    field_tokens: dict


@functools.lru_cache(maxsize=1)
def _context():
    """The contract's own context settings, read once: what is used when a caller names no run."""
    return cfgmod.load().context


def limits(task: str, long_context: bool = False, context=None) -> tuple[int, int]:
    """(total tokens, completion reserve) for a task, repair with the larger reserve. ``context`` is the ``context`` section of the run's configuration (a run may set its own context
    length); without it the contract's defaults are used. A run's value is never cached: the next run in the same process starts from its own."""
    c = _context() if context is None else context
    total = c.long_context_ablation_tokens if long_context else c.max_total_tokens
    from training.finetune.sft_contracts import CORPUS_TASKS
    name = task if task == 'regression_test_design' or task in CORPUS_TASKS else ct.by_name(task).name
    if name in CORPUS_TASKS:
        return total, total // 2
    return total, c.repair_completion_reserve if name in ('repair', 'regression_test_design') else c.ordinary_completion_reserve


def _require_fast(tokenizer) -> None:
    if not getattr(tokenizer, "is_fast", False):
        raise TypeError("a fast tokenizer with offset mapping is required (the slow tokenizer cannot align fields)")


def token_count(tokenizer, text: str) -> int:
    """Tokens of ``text`` as part of a sequence (no special tokens added)."""
    return len(tokenizer(text, add_special_tokens=False)["input_ids"])


def encode_example(tokenizer, ser: sz.Serialized, *, max_total: int | None = None, completion_reserve: int | None = None,
                   long_context: bool = False, context=None) -> Encoded:
    """Token ids, labels, masks and field owners of one serialized example; ``Rejected`` when it cannot be used."""
    _require_fast(tokenizer)
    if ser.completion is None:
        raise ValueError("a training example needs a completion")
    enc = tokenizer(ser.text, add_special_tokens=False, return_offsets_mapping=True)
    return _encode_from(tokenizer, ser, enc["input_ids"], enc["offset_mapping"], max_total=max_total,
                        completion_reserve=completion_reserve, long_context=long_context, context=context)


def encode_examples(tokenizer, sers, *, batch: int = 256, progress=None, **kw) -> list:
    """``[try_encode(tokenizer, s) for s in sers]``, but the text is tokenized a batch at a time: the fast tokenizer then spreads the batch over its
    own threads, which is several times faster than one call per example (measured 37.9 s to under 10 s for 3,651 real views). Every
    entry is ``(Encoded, None)`` or ``(None, reason)``, in the order of ``sers``, with the same ids, offsets and checks as the single call."""
    _require_fast(tokenizer)
    sers = list(sers)
    out: list = []
    for lo in range(0, len(sers), batch):
        part = sers[lo:lo + batch]
        for ser in part:
            if ser.completion is None:
                raise ValueError("a training example needs a completion")
        enc = tokenizer([s.text for s in part], add_special_tokens=False, return_offsets_mapping=True)
        for ser, ids, offsets in zip(part, enc["input_ids"], enc["offset_mapping"]):
            try:
                out.append((_encode_from(tokenizer, ser, ids, offsets, **kw), None))
            except Rejected as r:
                out.append((None, r.reason))
        if progress is not None:
            progress(min(lo + batch, len(sers)), len(sers))
    return out


def _encode_from(tokenizer, ser: sz.Serialized, ids, offsets, *, max_total: int | None = None, completion_reserve: int | None = None,
                 long_context: bool = False, context=None) -> Encoded:
    """The part of ``encode_example`` that follows the tokenizer call: the boundary, the budget checks, the labels and the field owners."""
    default_total, default_reserve = limits(ser.task, long_context, context)
    max_total = default_total if max_total is None else max_total
    reserve = default_reserve if completion_reserve is None else completion_reserve
    ids = list(ids)
    boundary = ser.boundary
    n = len(ids)
    for i, (start, end) in enumerate(offsets):
        if end == start:
            continue
        if start < boundary < end:
            raise Rejected("boundary_straddle")
        if start >= boundary:
            n = i                                                          # the first completion token
            break
    if n >= len(ids):
        raise Rejected("empty_completion")
    eos = tokenizer.eos_token_id
    if eos is None:
        raise ValueError("the tokenizer has no EOS token")
    completion_tokens = len(ids) - n + 1                                  # plus the EOS
    from training.finetune.sft_contracts import CORPUS_TASKS
    if ser.task in CORPUS_TASKS:
        # Source chunks need only the actual total window; a fixed completion
        # reserve would discard usable patches despite space in that window.
        reserve = max_total - n
    if n > max_total - reserve:
        raise Rejected("prompt_over_budget")
    if completion_tokens > reserve:
        raise Rejected("completion_over_reserve")
    names = FIELD_GROUPS
    structure = names.index("structure_eos")
    field_ids = [-1] * n
    for start, _ in offsets[n:]:
        owner = ser.owner(start)
        field_ids.append(names.index(owner) if owner else structure)
    field_ids.append(structure)                                           # the EOS
    counts = {g: 0 for g in names}
    for f in field_ids[n:]:
        counts[names[f]] += 1
    input_ids = ids + [eos]
    labels = [IGNORE] * n + ids[n:] + [eos]
    return Encoded(ser.task, input_ids, labels, [1] * len(input_ids), field_ids, n, completion_tokens, counts)


def try_encode(tokenizer, ser: sz.Serialized, **kw) -> tuple[Encoded | None, str | None]:
    """(encoding, None) or (None, reason): for a build that counts why examples were excluded."""
    try:
        return encode_example(tokenizer, ser, **kw), None
    except Rejected as r:
        return None, r.reason


def pad_batch(encodings, pad_id: int) -> dict:
    """Right-pad a batch. Padding positions get attention 0, label -100 and no field; the pad id is only a
    filler, so a pad id equal to the EOS id cannot mask a real EOS."""
    width = max(len(e.input_ids) for e in encodings)
    out = {"input_ids": [], "labels": [], "attention_mask": [], "field_ids": [], "prompt_lengths": []}
    for e in encodings:
        gap = width - len(e.input_ids)
        out["input_ids"].append(e.input_ids + [pad_id] * gap)
        out["labels"].append(e.labels + [IGNORE] * gap)
        out["attention_mask"].append(e.attention_mask + [0] * gap)
        out["field_ids"].append(e.field_ids + [-1] * gap)
        out["prompt_lengths"].append(e.prompt_tokens)
    return out


def pack_batch(encodings) -> dict:
    """Several examples in one row, one after another. Each keeps its own labels (the prompt stays masked, the completion and its terminal EOS stay supervised); the position restarts at
    0 in each; ``segment_ids`` says which example a token belongs to, so that nothing attends across examples (``model.packed_attention_bias``) and every supervised token is counted
    for its own example. The first token of an example is never a target (in a row of its own it is never predicted), so its label is masked: otherwise it would be predicted from
    the last token of the example before it."""
    out = {"input_ids": [], "labels": [], "field_ids": [], "position_ids": [], "segment_ids": [], "prompt_lengths": []}
    for k, e in enumerate(encodings):
        n = len(e.input_ids)
        if n == 0 or any(m != 1 for m in e.attention_mask):
            raise ValueError("a packed example must be non-empty and have no padding of its own")
        out["input_ids"] += list(e.input_ids)
        out["labels"] += [IGNORE] + list(e.labels[1:])
        out["field_ids"] += list(e.field_ids)
        out["position_ids"] += list(range(n))
        out["segment_ids"] += [k] * n
        out["prompt_lengths"].append(e.prompt_tokens)
    return out


# -- fitting the context into the prompt budget ---------------------------------------------------------------------------------------

@dataclass
class Fit:
    status: str                      # fits | trimmed | target_too_long | protected_over_budget
    context_text: str
    kept: list
    dropped: list
    prompt_tokens: int
    limit: int                       # the prompt budget: total minus the completion reserve


def fit_context(tokenizer, task: str, target: dict, items, *, max_total: int | None = None, reserve: int | None = None,
                long_context: bool = False, context=None) -> Fit:
    """Keep as much context as the prompt budget allows. ``items`` are in priority order (highest first); whole
    items are dropped from the end, never cut, and ``protected`` items and the target are never dropped."""
    default_total, default_reserve = limits(task, long_context, context)
    total = default_total if max_total is None else max_total
    limit = total - (default_reserve if reserve is None else reserve)
    items = list(items)

    def prompt_tokens(chosen):
        return token_count(tokenizer, sz.render_prompt(task, sz.render_context(target, chosen)))

    kept = list(items)
    dropped: list = []
    while True:
        n = prompt_tokens(kept)
        if n <= limit:
            status = "fits" if not dropped else "trimmed"
            return Fit(status, sz.render_context(target, kept), kept, dropped[::-1], n, limit)
        droppable = [i for i, it in enumerate(kept) if not it.get("protected")]
        if not droppable:
            status = "protected_over_budget" if kept else "target_too_long"
            return Fit(status, sz.render_context(target, kept), kept, dropped[::-1], n, limit)
        dropped.append(kept.pop(droppable[-1]))


def tokenizer_info(tokenizer) -> dict:
    """What a manifest records about the tokenizer."""
    return {"class": type(tokenizer).__name__, "is_fast": bool(getattr(tokenizer, "is_fast", False)),
            "eos_id": tokenizer.eos_token_id, "pad_id": tokenizer.pad_token_id,
            "pad_equals_eos": tokenizer.pad_token_id is not None and tokenizer.pad_token_id == tokenizer.eos_token_id,
            "vocab_size": len(tokenizer), "name_or_path": getattr(tokenizer, "name_or_path", None)}
