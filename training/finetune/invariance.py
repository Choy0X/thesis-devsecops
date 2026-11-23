"""Invariance to harmless transforms (optional, off by default).

A harmless variant of an example (the same code reformatted, with a comment added, or with its local names changed) must get the same
verdict. This loss asks the decision head to agree on the two versions: the Jensen-Shannon divergence between its class distributions
on the parent and on the variant. Only transforms of the approved registry may enter it (the equivalence of each is checked when the
variant is made); a variant made by anything else stops the run rather than teach the model that a behaviour change is harmless.
"""
from __future__ import annotations

import torch
import torch.nn.functional as F

APPROVED_TRANSFORMS = ("comment_insert", "reformat", "rename_locals")      # the variant registry of the dataset pipeline


def jensen_shannon(logits_p: torch.Tensor, logits_q: torch.Tensor) -> torch.Tensor:
    """Per-row Jensen-Shannon divergence (in nats, at most ln 2) between the class distributions given by two sets of logits."""
    lp, lq = F.log_softmax(logits_p.float(), -1), F.log_softmax(logits_q.float(), -1)
    p, q = lp.exp(), lq.exp()
    lm = torch.log(0.5 * (p + q) + 1e-12)
    return 0.5 * (p * (lp - lm)).sum(-1) + 0.5 * (q * (lq - lm)).sum(-1)


def invariance_loss(parent_logits: torch.Tensor, variant_logits: torch.Tensor, transforms, approved=APPROVED_TRANSFORMS) -> torch.Tensor:
    """Mean divergence over the parent / variant pairs; ``transforms`` names the transform of each variant."""
    unknown = sorted(set(transforms) - set(approved))
    if unknown:
        raise ValueError(f"transform(s) {unknown} are not approved for invariance training (approved: {list(approved)})")
    return jensen_shannon(parent_logits, variant_logits).mean()
