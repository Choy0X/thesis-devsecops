"""Pools and shares: what is available per task, what the build must refuse, and how shares are renormalized.

The phase contract sets a share per task and a target mix of decision pools (supported, scoped negative,
insufficient context). A pool that is empty is reported and the rest are renormalized explicitly; an empty *core*
task (discovery, condition assessment, repair) fails the build, because a model trained without it would not be the
model that was asked for. General code replay (T6) is blocked until a corpus is chosen and passes the license,
duplicate and benchmark-overlap checks.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict

from app.backend.pipeline.views.core import View

from training.finetune.planning import (CORE_TASKS, DECISION_POOL_NAMES, EmptyCorePool, _renormalize, check_core,
                                        plan_decision_pools, plan_task_shares)

__all__ = ["CORE_TASKS", "DECISION_POOL_NAMES", "EmptyCorePool", "check_core", "count_pools", "plan_decision_pools",
           "plan_task_shares", "replay_status", "replay_views", "text_key"]


def count_pools(views) -> dict[str, dict[str, int]]:
    """{task: {pool: number of eligible views}}; a view without weight is not available."""
    counts: dict[str, Counter] = defaultdict(Counter)
    for v in views:
        if v.eligible and v.weight > 0:
            counts[v.task][v.pool] += 1
    return {t: dict(c) for t, c in counts.items()}


# -- T6: general code replay -------------------------------------------------------------------------------------------------------------

def text_key(text: str) -> str:
    return hashlib.sha1(re.sub(r"\s+", " ", text or "").strip().encode("utf-8")).hexdigest()


def replay_status(items) -> dict:
    if not items:
        return {"status": "blocked", "reason": "no replay corpus selected: choose a licensed corpus and run the checks"}
    return {"status": "ready", "reason": None, "items": len(items)}


def replay_views(items, *, allowed_licenses, known_keys, benchmark_keys) -> dict:
    """Replay views from a candidate corpus: only items under an allowed license, not already known, not repeated
    in the corpus and not overlapping a benchmark answer. Counts of what was excluded and why are returned."""
    seen, views = set(known_keys), []
    excluded: Counter = Counter()
    for it in items or []:
        key = text_key(it["answer"])
        if it.get("license") not in allowed_licenses:
            excluded["license"] += 1
        elif key in benchmark_keys:
            excluded["benchmark_overlap"] += 1
        elif key in seen:
            excluded["duplicate"] += 1
        else:
            seen.add(key)
            views.append(View(view_id="view_" + hashlib.sha1(f"replay|{it['source']}".encode()).hexdigest()[:16],
                              task="general_code_replay", subject_id=f"replay:{it['source']}", record_id=None,
                              issue_group_id=None, family_id=f"replay:{it['source']}", pool="replay",
                              context_text=it["prompt"], response=it["answer"], weight=1.0, review_status="auto_checked",
                              eligible=True, flags={"license": it.get("license")}, lineage={"source": it["source"]}))
    return {"views": views, "excluded": dict(excluded),
            "status": "ready" if views else "blocked", "reason": None if views else "no item passed the checks"}
