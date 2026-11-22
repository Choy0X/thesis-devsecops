"""Training objectives for vulnerable and fixed code pairs."""
from __future__ import annotations

import torch
import torch.nn.functional as F


def twin_margin_loss(gap_vulnerable: torch.Tensor, gap_fixed: torch.Tensor, margin: float) -> torch.Tensor:
    """mean softplus(-(s_V - s_F - margin)) over twin pairs, s being the status gap (finding minus no_finding). It is the log-likelihood that the vulnerable view is the one of the two that
    is vulnerable, with a margin. The tokens the twins share cancel in s_V - s_F, so its gradient acts on what differs, and a per-function offset (what one exposure fits under plain
    cross-entropy) cancels too. It saturates once the gap exceeds the margin, so easy pairs stop taking the update."""
    return F.softplus(-(gap_vulnerable - gap_fixed - margin)).mean()


def pair_verdict_loss(gap_vulnerable: torch.Tensor, gap_fixed: torch.Tensor, margin: float, threshold: float = 0.0) -> torch.Tensor:
    """CANDIDATE, not wired into any recipe. Per pair ``logsumexp(0, m - z_v, m - z_f)`` with ``z_v = s_V - t`` and ``z_f = t - s_F``: each twin must sit on its own side of the decision
    threshold t by the margin m, and the side farther from its margin takes the gradient (the absolute gradient ratio is exp(z_f - z_v)). Unlike ``twin_margin_loss`` it depends on
    the pair midpoint as well as the gap: with d = (s_V - s_F) / 2 and c = (s_V + s_F) / 2 - t it equals log(1 + 2 exp(m - d) cosh(c)). Returns one value per pair, in float32;
    the caller averages over the eligible pairs of the whole optimizer update."""
    zv = gap_vulnerable.float() - threshold
    zf = threshold - gap_fixed.float()
    return torch.logsumexp(torch.stack([torch.zeros_like(zv), margin - zv, margin - zf], dim=-1), dim=-1)
