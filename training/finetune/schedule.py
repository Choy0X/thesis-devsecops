"""How long a run trains: the one place that turns "epochs" or "steps" into the number of optimizer steps. No torch.

The unit of progress here is the *family pass*: the number of sequences drawn divided by the number of training families (``sampler.py``). A step draws
``effective_sequence_batch_size`` sequences, so

    steps = ceil(family_passes x families / effective_batch)      and      family_passes = steps x effective_batch / families

An "epoch" in the interface is a family pass. It is not a pass over every view exactly once: the sampler draws a task by its share, a decision pool by its target and a
family uniformly, so within one pass a view of a rare task is seen more than once and a view of a common one may not be seen. The runner, the interface and the notes all use
these two functions, so a number shown to a person is the number the run uses.
"""
from __future__ import annotations

import math


def total_steps(*, family_passes: float | None = None, steps: int | None = None, families: int, effective_batch: int) -> int:
    """Optimizer steps for a run asked for in ``steps`` (used as given, at least 1) or in ``family_passes``."""
    if steps is not None:
        return max(1, int(steps))
    if family_passes is None:
        raise ValueError("give family_passes or steps")
    if families < 1 or effective_batch < 1:
        raise ValueError("families and effective_batch must be at least 1")
    return max(1, math.ceil(family_passes * families / effective_batch))


def passes_for_steps(steps: int, *, families: int, effective_batch: int) -> float:
    """The family passes ``steps`` optimizer steps amount to."""
    if families < 1:
        raise ValueError("families must be at least 1")
    return steps * effective_batch / families


def describe(*, family_passes: float | None = None, steps: int | None = None, families: int, effective_batch: int, evaluate_at_family_passes=(), evaluate_every_steps: int | None = None) -> dict:
    """What a request for ``family_passes`` or ``steps`` comes to, with the evaluation points in steps: what the interface shows before a run starts."""
    n = total_steps(family_passes=family_passes, steps=steps, families=families, effective_batch=effective_batch)
    if evaluate_every_steps:
        points = list(range(evaluate_every_steps, n + 1, evaluate_every_steps))
    else:
        points = [min(n, math.ceil(p * families / effective_batch)) for p in evaluate_at_family_passes if p * families / effective_batch <= n]
    return {"steps": n, "family_passes": round(passes_for_steps(n, families=families, effective_batch=effective_batch), 4), "sequences": n * effective_batch,
            "families": families, "effective_batch": effective_batch, "evaluation_steps": points,
            "last_evaluation_step": points[-1] if points else None,
            "steps_after_last_evaluation": (n - points[-1]) if points else n}
