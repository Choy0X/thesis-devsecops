"""The decision head and the pair loss.

``Linear(hidden, 3)`` in float32 reads the final hidden state at the **last prompt position** of each example. Under causal
attention that state cannot depend on the completion that follows, so one forward over prompt and completion serves the
language-model loss and the head, and the head sees exactly what it will see at inference (the prompt alone). Its classes are
the contract's three label states, in this order. The head is saved apart from the adapter, with the label order, the pooling
rule, the serialization and tokenizer identity and (later) its calibration, so it is never applied to the wrong model.

The pair loss encodes the vulnerable and the protected version of a record independently and asks the vulnerable score to
exceed the protected one by a margin: ``softplus(margin - z_v + z_p)``, averaged over the pairs that count (a pair whose
protected side is unknown is left out). ``binary_bce_margin_loss`` is the plain binary form, kept only as an isolated control.
"""
from __future__ import annotations

import pathlib

import torch
import torch.nn.functional as F

LABELS = ("vulnerability_supported", "no_supported_finding_in_scope", "insufficient_context")
POOLING = "last_prompt_position"


class DecisionHead(torch.nn.Module):
    def __init__(self, hidden_size: int):
        super().__init__()
        self.linear = torch.nn.Linear(hidden_size, len(LABELS)).float()

    def forward(self, h: torch.Tensor) -> torch.Tensor:
        with torch.autocast(device_type=h.device.type, enabled=False):          # always float32, whatever the backbone runs in
            return self.linear(h.float())


def pooled_prompt_state(hidden: torch.Tensor, prompt_lengths: torch.Tensor) -> torch.Tensor:
    """The hidden state at the last prompt position of each example: (B, T, H) -> (B, H)."""
    rows = torch.arange(hidden.shape[0], device=hidden.device)
    return hidden[rows, prompt_lengths.to(hidden.device) - 1]


def label_index(label_state: str) -> int:
    """Class index of a label state; -1 for anything else (``unreviewed`` has no class)."""
    return LABELS.index(label_state) if label_state in LABELS else -1


def decision_loss(logits: torch.Tensor, labels: torch.Tensor, weights=None) -> torch.Tensor:
    """Weighted cross-entropy over the examples with a known class (label >= 0)."""
    valid = labels >= 0
    w = torch.ones(labels.shape[0], device=logits.device) if weights is None else weights.to(logits.device, logits.dtype)
    w = w * valid
    if float(w.sum()) <= 0:
        return logits.sum() * 0.0
    per = F.cross_entropy(logits, labels.clamp_min(0), reduction="none")
    return (per * w).sum() / w.sum()


def vulnerability_score(logits: torch.Tensor) -> torch.Tensor:
    """Log-odds of ``vulnerability_supported`` against the other two classes."""
    return logits[..., 0] - torch.logsumexp(logits[..., 1:], dim=-1)


def pair_loss(z_vulnerable: torch.Tensor, z_protected: torch.Tensor, margin: float, valid=None) -> torch.Tensor:
    """``softplus(margin - z_v + z_p)`` averaged over the valid pairs (0 when none is valid)."""
    per = F.softplus(margin - z_vulnerable + z_protected)
    if valid is None:
        return per.mean()
    mask = valid.to(per.dtype)
    return (per * mask).sum() / mask.sum().clamp_min(1)


def binary_bce_margin_loss(z_vulnerable: torch.Tensor, z_protected: torch.Tensor, margin: float) -> torch.Tensor:
    """The binary control: both sides classified on their own plus a hinge on the gap. Never used by the default loss."""
    bce = (F.binary_cross_entropy_with_logits(z_vulnerable, torch.ones_like(z_vulnerable))
           + F.binary_cross_entropy_with_logits(z_protected, torch.zeros_like(z_protected)))
    return bce + F.relu(margin - (z_vulnerable - z_protected)).mean()


def save_head(head: DecisionHead, path, meta: dict, *, backend=None) -> pathlib.Path:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    full = {**meta, "label_order": list(LABELS), "pooling": POOLING, "hidden_size": head.linear.in_features,
            "classes": len(LABELS)}
    state = backend.to_cpu(head.state_dict()) if backend else head.state_dict()
    torch.save({"state": state, "meta": full}, path)
    return path


def load_head(path) -> tuple[DecisionHead, dict]:
    blob = torch.load(path, map_location="cpu", weights_only=True)
    meta = blob["meta"]
    if meta.get("label_order") != list(LABELS):
        raise ValueError(f"the head was saved with another label order {meta.get('label_order')}; this code uses {list(LABELS)}")
    head = DecisionHead(int(meta["hidden_size"]))
    head.load_state_dict(blob["state"])
    return head, meta
