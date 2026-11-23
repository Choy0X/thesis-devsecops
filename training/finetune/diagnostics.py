"""Gradient diagnostics on probe batches: the gradient norm of each task and the cosine between the gradients of two tasks.

A strongly negative cosine means two tasks pull the adapter in opposite directions. The probes are computed in evaluation mode (no
dropout, so identical probes give identical gradients), take no optimizer step, leave no gradients behind and do not disturb the
random state of the run.
"""
from __future__ import annotations

import itertools

import torch


def _flat_gradient(params) -> torch.Tensor:
    parts = [p.grad.detach().flatten().float() for p in params if p.grad is not None]
    return torch.cat(parts) if parts else torch.zeros(1)


def gradient_diagnostics(trainer, probes: dict) -> dict:
    """{"norms": {task: norm}, "cosine": {"a|b": cosine}} for ``probes`` = {task: [Example, ...]}."""
    rng = torch.get_rng_state()
    was_training = trainer.model.training
    trainer.model.eval()
    trainer.head.eval()
    vectors: dict[str, torch.Tensor] = {}
    try:
        for task, examples in probes.items():
            trainer.optimizer.zero_grad(set_to_none=True)
            loss, _ = trainer.compute_loss(list(examples))
            loss.backward()
            vectors[task] = _flat_gradient(trainer.params).clone()
    finally:
        trainer.optimizer.zero_grad(set_to_none=True)
        trainer.model.train(was_training)
        trainer.head.train(was_training)
        torch.set_rng_state(rng)
    norms = {t: float(v.norm()) for t, v in vectors.items()}
    cosine = {}
    for a, b in itertools.combinations(vectors, 2):
        denom = float(vectors[a].norm() * vectors[b].norm())
        cosine[f"{a}|{b}"] = float(torch.dot(vectors[a], vectors[b])) / denom if denom > 0 else 0.0
    return {"norms": norms, "cosine": cosine}
