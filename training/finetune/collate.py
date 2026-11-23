"""The collator: encodings -> padded tensors for one micro-batch.

Padding is masked by the attention mask and by position, never by token id (the pad and EOS tokens of the base model are the
same id, so the real EOS stays a supervised target). A decision label is the class index of the example's label state (-1 for
unreviewed or unknown, which the head ignores) and the weight is its supervision weight.
"""
from __future__ import annotations

import torch

from training.finetune import head as hd, tokenization as tk


def bucket_batch(batch, pad_id, max_tokens=4096):
    """Pad CPU metadata to bounded powers of two before transferring to XLA."""
    length = batch["input_ids"].shape[1]
    target = min(max_tokens, max(64, 1 << (length - 1).bit_length()))
    if length > max_tokens:
        raise ValueError("encoded sequence exceeds the configured context; truncation is forbidden")
    for key, value in (("input_ids", pad_id), ("labels", tk.IGNORE), ("attention_mask", 0), ("field_ids", -1)):
        batch[key] = torch.nn.functional.pad(batch[key], (0, target - length), value=value)
    return batch


def collate_packed(encodings, *, label_states=None, weights=None) -> dict[str, torch.Tensor]:
    """The examples of one pack as a single row (see ``tokenization.pack_batch``). ``weights`` and ``decision_labels`` are per example, not per row."""
    n = len(encodings)
    row = tk.pack_batch(encodings)
    out = {k: torch.tensor([row[k]], dtype=torch.long) for k in ("input_ids", "labels", "field_ids", "position_ids", "segment_ids")}
    out["attention_mask"] = torch.ones_like(out["input_ids"])
    out["prompt_lengths"] = torch.tensor(row["prompt_lengths"], dtype=torch.long)
    out["decision_labels"] = torch.tensor([hd.label_index(s) for s in label_states] if label_states is not None else [-1] * n, dtype=torch.long)
    out["weights"] = torch.tensor(list(weights) if weights is not None else [1.0] * n, dtype=torch.float32)
    return out


def collate(encodings, pad_id: int, *, label_states=None, weights=None) -> dict[str, torch.Tensor]:
    n = len(encodings)
    batch = tk.pad_batch(encodings, pad_id)
    out = {k: torch.tensor(batch[k], dtype=torch.long)
           for k in ("input_ids", "labels", "attention_mask", "field_ids", "prompt_lengths")}
    out["decision_labels"] = torch.tensor([hd.label_index(s) for s in label_states] if label_states is not None else [-1] * n,
                                          dtype=torch.long)
    out["weights"] = torch.tensor(list(weights) if weights is not None else [1.0] * n, dtype=torch.float32)
    return out
