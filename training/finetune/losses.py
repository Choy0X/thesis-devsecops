"""Losses of the fine-tune: the language-model loss at supervised positions, in chunks, and how stages combine terms.

The sequence is run through the backbone to its final hidden states and the language-model head is applied only at the
positions that are supervised, a chunk at a time (and recomputed in the backward pass instead of stored), so the
152k-wide logits of the whole sequence are never held in memory.

* ``per_example_completion_ce``: the first loss. The mean over examples of each example's mean token loss, so a long
  completion does not outweigh a short one.
* ``field_normalized_ce``: the next ablation. Inside each example, the mean loss of each field that is present, combined
  with the contract's field weights and normalized by the weights of the fields present, then averaged over examples.
  The weighted and unweighted loss of every field are reported.
* Quality weights are the examples' supervision weights (1 for an accepted row, 0 removes it); no confidence weights are made up.
* ``LossConfig.for_stage`` reads the contract: stage 1 is the language-model loss alone; stage 2 adds the decision-head loss
  (weight 0.2) and the pair rank loss (weight 0.02).
"""
from __future__ import annotations

import functools
from dataclasses import dataclass

import torch
import torch.nn.functional as F
from torch.utils.checkpoint import checkpoint as _checkpoint

# A 152k-vocabulary float32 logit buffer is about 297 MiB at 512 positions, before
# cross-entropy and backward buffers. Bound it to about 37 MiB per projection.
DEFAULT_TOKEN_CHUNK = 64

from training.finetune import config as cfgmod, tokenization as tk

FIELD_GROUPS = tk.FIELD_GROUPS
MODES = ("per_example_completion_ce", "present_field_normalized_ce")


@dataclass
class TokenLosses:
    loss: torch.Tensor          # (N,) float32 cross-entropy of each supervised token
    example: torch.Tensor       # (N,) index of its example in the batch
    field: torch.Tensor         # (N,) index into FIELD_GROUPS
    valid: torch.Tensor | None = None  # static TPU padding contributes neither loss nor counts


def token_losses(hidden: torch.Tensor, head, labels: torch.Tensor, field_ids: torch.Tensor, *, chunk: int = DEFAULT_TOKEN_CHUNK,
                 checkpoint: bool = True, example_ids: torch.Tensor | None = None, static=False) -> TokenLosses:
    """Cross-entropy of every supervised token. ``hidden`` is (B, T, H); the head (a module or a function of (N, H)) is applied
    to at most ``chunk`` positions at a time. A position predicts the next token, so labels and fields are shifted by one. A token belongs to the example of its row, or, for a row that
    holds several packed examples, to the example ``example_ids`` (B, T) gives it."""
    B = hidden.shape[0]
    valid = None
    if static is not False:
        # Select on CPU metadata; XLA sees bounded gathers, never nonzero/boolean selection.
        cpu_labels, cpu_fields = static if isinstance(static, tuple) else (labels.cpu(), field_ids.cpu())
        target = cpu_labels[:, 1:]
        indices = (target.flatten() != tk.IGNORE).nonzero().flatten()
        count = indices.numel()
        size = max(chunk, ((count + chunk - 1) // chunk) * chunk) if hidden.device.type=='xla' else count
        indices = F.pad(indices, (0, size - count))
        mask = torch.arange(size) < count
        idx = indices.to(hidden.device,non_blocking=hidden.device.type=='cuda')
        h = hidden[:, :-1].reshape(-1, hidden.shape[-1]).index_select(0, idx)
        y = target.flatten()[indices].clamp_min(0).to(hidden.device,non_blocking=hidden.device.type=='cuda')
        ex = (indices // target.shape[1]).to(hidden.device,non_blocking=hidden.device.type=='cuda')
        f = cpu_fields[:, 1:].flatten()[indices].clamp_min(0).to(hidden.device,non_blocking=hidden.device.type=='cuda')
        valid = mask.to(hidden.device,non_blocking=hidden.device.type=='cuda')
    else:
        tgt, fld = labels[:, 1:], field_ids[:, 1:]
        sel = tgt != tk.IGNORE
        h = hidden[:, :-1][sel]
        y = tgt[sel]
        ex = (example_ids[:, 1:] if example_ids is not None else torch.arange(B, device=hidden.device)[:, None].expand_as(tgt))[sel]
        f = fld[sel]

    def ce(hc, yc):
        return F.cross_entropy(head(hc).float(), yc, reduction="none")
    parts = []
    for i in range(0, h.shape[0], chunk):
        hc, yc = h[i:i + chunk], y[i:i + chunk]
        use = checkpoint and torch.is_grad_enabled() and hc.requires_grad
        checkpoint_fn, reentrant = _checkpoint, False
        if hidden.device.type == "xla" and use:
            from torch_xla.utils.checkpoint import checkpoint as checkpoint_fn
            reentrant = True
        parts.append(checkpoint_fn(ce, hc, yc, use_reentrant=reentrant) if use else ce(hc, yc))
    loss = torch.cat(parts) if parts else hidden.new_zeros(0, dtype=torch.float32)
    if valid is not None:
        loss = loss * valid
    return TokenLosses(loss, ex, f, valid)


def _weights(weights, counts: torch.Tensor, B: int, like: torch.Tensor) -> torch.Tensor:
    w = torch.ones(B, device=like.device) if weights is None else weights.to(like.device, like.dtype)
    return w * (counts > 0)


def per_example_completion_ce(tl: TokenLosses, batch_size: int, weights=None):
    """(loss, per-example losses): the weighted mean over examples of each example's mean token loss."""
    B = batch_size
    sums = torch.zeros(B, device=tl.loss.device).index_add(0, tl.example, tl.loss)
    counts = torch.zeros(B, device=tl.loss.device).index_add(0, tl.example, torch.ones_like(tl.loss) if tl.valid is None else tl.valid.float())
    per = sums / counts.clamp_min(1)
    w = _weights(weights, counts, B, per)
    return (per * w).sum() / w.sum().clamp_min(1e-12), per


def field_normalized_ce(tl: TokenLosses, batch_size: int, field_weights: dict, weights=None, field_names=FIELD_GROUPS, *, defer_report=False):
    """(loss, report): the present-field normalized loss and the weighted / unweighted loss of every field."""
    B, n = batch_size, len(field_names)
    idx = tl.example * n + tl.field
    sums = torch.zeros(B * n, device=tl.loss.device).index_add(0, idx, tl.loss).view(B, n)
    cnt = torch.zeros(B * n, device=tl.loss.device).index_add(0, idx, torch.ones_like(tl.loss) if tl.valid is None else tl.valid.float()).view(B, n)
    present = cnt > 0
    mean = sums / cnt.clamp_min(1)
    fw = torch.tensor([float(field_weights.get(name, 1.0)) for name in field_names], device=tl.loss.device)
    den = (fw[None, :] * present).sum(1)
    per_example = (mean * fw[None, :] * present).sum(1) / den.clamp_min(1e-12)
    w = _weights(weights, den, B, per_example)
    loss = (per_example * w).sum() / w.sum().clamp_min(1e-12)
    examples = present.sum(0)
    unweighted = ((mean * present).sum(0) / examples.clamp_min(1)).detach()
    if defer_report:
        return loss, {'unweighted': {name:unweighted[i] for i,name in enumerate(field_names)}}
    report = {"unweighted": {name: float(unweighted[i]) for i, name in enumerate(field_names)},
              "weighted": {name: float(unweighted[i] * fw[i]) for i, name in enumerate(field_names)},
              "tokens": {name: int(cnt[:, i].sum()) for i, name in enumerate(field_names)},
              "examples": {name: int(examples[i]) for i, name in enumerate(field_names)}}
    return loss, report


@dataclass(frozen=True)
class LossConfig:
    mode: str
    field_weights: dict
    decision_weight: float
    rank_weight: float
    pair_enabled: bool
    margin: float
    invariance_enabled: bool = False
    invariance_weight: float = 0.0
    lm_pair_weight: float = 0.0          # twin margin on the status logit gap (see twin_margin_loss); 0 leaves the stage-1 loss unchanged
    lm_pair_margin: float = 2.2

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def _contract():
        return cfgmod.load().loss

    @classmethod
    def for_stage(cls, stage: int, mode: str | None = None, invariance: bool | None = None) -> "LossConfig":
        c = cls._contract()
        mode = mode or c.initial
        if mode not in MODES:
            raise ValueError(f"loss mode must be one of {MODES}, got {mode!r}")
        if stage not in (1, 2):
            raise ValueError("stage must be 1 or 2")
        pair = c.pair.enabled_stage_1 if stage == 1 else c.pair.enabled_stage_2
        inv = c.invariance.enabled if invariance is None else invariance
        return cls(mode=mode, field_weights=c.field_weights.model_dump(),
                   decision_weight=c.decision_head.weight_stage_1 if stage == 1 else c.decision_head.weight_stage_2,
                   rank_weight=c.pair.rank_weight if pair else 0.0, pair_enabled=bool(pair), margin=c.pair.margin,
                   invariance_enabled=bool(inv), invariance_weight=c.invariance.weight_if_enabled if inv else 0.0)


def language_model_loss(tl: TokenLosses, batch_size: int, cfg: LossConfig, weights=None, *, defer_report=False):
    """(loss, report) of the configured language-model loss."""
    if cfg.mode == "per_example_completion_ce":
        loss, per = per_example_completion_ce(tl, batch_size, weights)
        return loss, {"mode": cfg.mode, "per_example": per.detach() if defer_report else [float(x) for x in per.detach()]}
    loss, report = field_normalized_ce(tl, batch_size, cfg.field_weights, weights, defer_report=defer_report)
    return loss, {"mode": cfg.mode, **report}


def status_gap(hidden: torch.Tensor, head, labels: torch.Tensor, field_ids: torch.Tensor, ids_a: torch.Tensor, ids_b: torch.Tensor):
    """(gap, has_decision): for every row of the batch the log-odds z[a] - z[b] of two tokens at the position that predicts the row's first decision token, with the language-model
    head applied to that one position. ids_a/ids_b: (B,) token ids per row. The first decision token is the status value ("finding", or "no" of "no_finding"); the later status tokens
    are deterministic given it, so this gap is the model's whole status decision and needs no normaliser. hidden is (B, T, H); a position predicts the next token."""
    tgt, fld = labels[:, 1:], field_ids[:, 1:]
    is_decision = (fld == FIELD_GROUPS.index('decision')) & (tgt != tk.IGNORE)
    first = is_decision.to(torch.int8).argmax(1)
    rows = torch.arange(hidden.shape[0], device=hidden.device)
    z = head(hidden[rows, first]).float()
    gap = z.gather(1, ids_a[:, None]).squeeze(1) - z.gather(1, ids_b[:, None]).squeeze(1)
    return gap, is_decision.any(1)


def first_decision_tokens(labels: torch.Tensor, field_ids: torch.Tensor) -> torch.Tensor:
    """(B,) the label token at each row's first decision position (the status value token)."""
    tgt, fld = labels[:, 1:], field_ids[:, 1:]
    is_decision = (fld == FIELD_GROUPS.index('decision')) & (tgt != tk.IGNORE)
    first = is_decision.to(torch.int8).argmax(1)
    return tgt[torch.arange(labels.shape[0], device=labels.device), first]


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


def total_loss(field_loss: torch.Tensor, cfg: LossConfig, *, decision=None, rank=None, invariance=None, twin=None) -> torch.Tensor:
    """``L = L_field + w_decision * L_decision + w_rank * L_rank`` (+ w_inv * L_invariance when that option is on); a term that
    is absent adds nothing."""
    out = field_loss
    if decision is not None:
        out = out + cfg.decision_weight * decision
    if rank is not None and cfg.pair_enabled:
        out = out + cfg.rank_weight * rank
    if invariance is not None and cfg.invariance_enabled:
        out = out + cfg.invariance_weight * invariance
    if twin is not None and cfg.lm_pair_weight > 0:
        out = out + cfg.lm_pair_weight * twin
    return out
