"""Planning of tasks and pools: which tasks and pools have examples, what a build must refuse, and how shares are
renormalized. Pure functions over counts ({task: {pool: n}}); no torch and nothing from the app, so a training run can use
them. The views package re-exports them (``app.backend.pipeline.views.pools``).

The phase contract sets a share per task and a target mix of decision pools (supported, scoped negative, insufficient
context). A pool that is empty is reported and the rest are renormalized explicitly; an empty *core* task (discovery,
condition assessment, repair) fails, because a model trained without it would not be the model that was asked for.
"""
from __future__ import annotations

CORE_TASKS = ("discovery", "condition_assessment", "repair")
DECISION_POOL_NAMES = {"supported": "supported", "reviewed_scoped_negative": "scoped_negative",
                       "insufficient_context": "insufficient_context"}


class EmptyCorePool(RuntimeError):
    """A core task has no eligible view: the build must not continue."""


def check_core(counts: dict, core=CORE_TASKS) -> None:
    missing = [t for t in core if sum(counts.get(t, {}).values()) == 0]
    if missing:
        raise EmptyCorePool(f"core task(s) with no eligible views: {missing}; the build cannot continue")


def _renormalize(available: list[str], targets: dict) -> dict:
    total = sum(targets[k] for k in available)
    return {k: targets[k] / total for k in available} if total else {}


def plan_task_shares(counts: dict, shares) -> dict:
    """The task shares over the tasks that have views. Unavailable tasks are listed in the report and the remaining
    shares are renormalized to sum to 1; nothing is renormalized silently."""
    shares = shares.model_dump() if hasattr(shares, "model_dump") else dict(shares)
    available = [t for t in shares if sum(counts.get(t, {}).values()) > 0]
    unavailable = [t for t in shares if t not in available]
    plan = _renormalize(available, shares)
    report = ["task shares: " + ", ".join(f"{t} {plan[t]:.3f}" for t in available)]
    if unavailable:
        report.append(f"renormalized: no eligible views for {', '.join(unavailable)} "
                      f"(contract shares {', '.join(f'{t} {shares[t]:.2f}' for t in unavailable)} redistributed)")
    return {"shares": plan, "unavailable": unavailable, "renormalized": bool(unavailable), "report": "\n".join(report)}


def plan_decision_pools(counts: dict, targets) -> dict:
    """The decision-pool mix of one task (the contract's 0.40 / 0.40 / 0.20 by default), renormalized over the
    pools that have views."""
    targets = targets.model_dump() if hasattr(targets, "model_dump") else dict(targets)
    by_pool = {DECISION_POOL_NAMES[k]: v for k, v in targets.items()}
    available = [p for p in by_pool if counts.get(p, 0) > 0]
    unavailable = [p for p in by_pool if p not in available]
    plan = _renormalize(available, by_pool)
    report = "decision pools: " + ", ".join(f"{p} {plan[p]:.3f}" for p in available)
    if unavailable:
        report += f"; renormalized: no eligible views for {', '.join(unavailable)}"
    return {"shares": plan, "unavailable": unavailable, "renormalized": bool(unavailable), "report": report}
