"""Preference optimisation (DPO) with explicit SFT replay (optional, off by default).

For a pair (chosen, rejected) of answers to one prompt the loss is ``-log sigmoid(beta * ((pi_c - pi_r) - (ref_c - ref_r)))`` with
**summed** response log-probabilities (not means), where ``ref`` is a frozen reference: the selected SFT adapter. Reference values are
computed once and cached by checkpoint, tokenization, completion and mask hashes, so a step never needs the reference model again and a
changed checkpoint, tokenization or mask can never reuse a stale value. A pair needs a *verified* ordering (a receipt, not an opinion);
a share of the supervised loss (``replay_weight``) is kept so the policy does not drift away from the SFT behaviour.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import torch
import torch.nn.functional as F

from training.finetune import collate as co, losses as ls, model as md, tokenization as tk


@dataclass(frozen=True)
class PreferencePair:
    pair_id: str
    chosen: tk.Encoded
    rejected: tk.Encoded
    ordering_verified: bool
    source: str | None = None


@dataclass(frozen=True)
class DPOConfig:
    beta: float
    learning_rate: float
    max_passes: int
    replay_weight: float
    verified_ordering_required: bool

    @classmethod
    def from_contract(cls, pref) -> "DPOConfig":
        return cls(beta=pref.beta, learning_rate=pref.learning_rate, max_passes=pref.max_passes, replay_weight=pref.replay_weight,
                   verified_ordering_required=pref.verified_ordering_required)


def validate_pair(pair: PreferencePair, require_verified: bool = True) -> None:
    if require_verified and not pair.ordering_verified:
        raise ValueError(f"preference pair {pair.pair_id} has no verified ordering: a receipt is required, not an opinion")


def summed_logprobs(tl: ls.TokenLosses, batch_size: int) -> torch.Tensor:
    """The log-probability of each example's completion: the sum of its token log-probabilities."""
    return torch.zeros(batch_size, device=tl.loss.device).index_add(0, tl.example, -tl.loss)


def dpo_loss(policy_chosen, policy_rejected, reference_chosen, reference_rejected, beta: float) -> torch.Tensor:
    margin = (policy_chosen - policy_rejected) - (reference_chosen - reference_rejected)
    return -F.logsigmoid(beta * margin).mean()


def total_loss(dpo: torch.Tensor, replay: torch.Tensor | None, *, replay_weight: float) -> torch.Tensor:
    return dpo if replay is None else dpo + replay_weight * replay


def _digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()


def _hashes(enc: tk.Encoded) -> list[str]:
    completion = [t for t, label in zip(enc.input_ids, enc.labels) if label != tk.IGNORE]
    return [_digest(enc.input_ids), _digest(completion), _digest([label == tk.IGNORE for label in enc.labels])]


def reference_key(checkpoint_id: str, chosen: tk.Encoded, rejected: tk.Encoded) -> str:
    """Identity of a cached reference value: the checkpoint and, for each answer, its tokenization, completion and mask hashes."""
    return _digest(checkpoint_id, _hashes(chosen), _hashes(rejected))


class ReferenceCache:
    def __init__(self):
        self._values: dict[str, tuple[torch.Tensor, torch.Tensor]] = {}
        self.hits = self.misses = 0

    def get_or_compute(self, key: str, compute):
        if key in self._values:
            self.hits += 1
        else:
            self.misses += 1
            c, r = compute()
            self._values[key] = (c.detach().clone(), r.detach().clone())
        return self._values[key]


def sequence_logprobs(model, encodings, pad_id: int, *, chunk: int = 512) -> torch.Tensor:
    """Summed completion log-probabilities of ``encodings`` under ``model`` (with gradient while the model is training)."""
    batch = co.collate(list(encodings), pad_id)
    hidden = md.hidden_states(model, batch["input_ids"], batch["attention_mask"])
    tl = ls.token_losses(hidden, md.lm_head_of(model), batch["labels"], batch["field_ids"], chunk=chunk, checkpoint=model.training)
    return summed_logprobs(tl, len(encodings))


def preference_step(model, pairs, cache: ReferenceCache, cfg: DPOConfig, *, reference_id: str, pad_id: int, compute_reference):
    """The DPO loss of a batch of pairs. ``compute_reference(pair)`` returns the frozen reference's (chosen, rejected) summed
    log-probabilities and is called only for pairs not yet in the cache."""
    for p in pairs:
        validate_pair(p, cfg.verified_ordering_required)
    policy = sequence_logprobs(model, [e for p in pairs for e in (p.chosen, p.rejected)], pad_id)
    refs = [cache.get_or_compute(reference_key(reference_id, p.chosen, p.rejected), lambda p=p: compute_reference(p)) for p in pairs]
    ref_c = torch.stack([r[0] for r in refs]).to(policy.device)
    ref_r = torch.stack([r[1] for r in refs]).to(policy.device)
    return dpo_loss(policy[0::2], policy[1::2], ref_c, ref_r, cfg.beta)
