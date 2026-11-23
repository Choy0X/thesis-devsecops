"""Comparing two runs on the same items: when is one allowed to be called better?

Two runs answer the same evaluation items. For each item each run is right or wrong. Only the items where the runs *disagree* tell them apart, so the
test looks at those: with ``only_a`` items where only run A is right and ``only_b`` where only run B is right, the exact McNemar test asks how likely a split
at least this uneven would be if the runs were equally good. A verdict of better is given only when
  * there are at least ``MIN_ITEMS`` items and at least ``MIN_DISAGREEMENTS`` of them where the runs disagree (below that the test has no power, and the answer
    is "not enough data", never "no difference"), and
  * the exact two-sided p-value is below ``ALPHA``.
The size of the difference is reported with a paired bootstrap interval (items are resampled together, so the pairing is kept). These thresholds are the
project's stated rule, not a law of statistics: they are in one place so a change is visible, and every result carries them.
"""
from __future__ import annotations

from math import comb

import numpy as np

MIN_ITEMS = 30
MIN_DISAGREEMENTS = 10
ALPHA = 0.05
BOOTSTRAP = 2000


def mcnemar_exact(only_a: int, only_b: int) -> float:
    """Two-sided exact p-value of the McNemar test: a fair-coin binomial test on the disagreements."""
    n = only_a + only_b
    if n == 0:
        return 1.0
    k = min(only_a, only_b)
    tail = sum(comb(n, i) for i in range(k + 1))
    return min(1.0, 2.0 * tail / 2 ** n)


def compare(a: list[bool], b: list[bool], *, alpha: float = ALPHA, min_items: int = MIN_ITEMS, min_disagreements: int = MIN_DISAGREEMENTS,
            bootstrap: int = BOOTSTRAP, seed: int = 0) -> dict:
    """``a`` and ``b`` say, item by item, whether each run was right. The result names the verdict, the evidence behind it and the rule that was applied."""
    if len(a) != len(b):
        raise ValueError(f"the runs answered {len(a)} and {len(b)} items; they must be the same items")
    x, y = np.asarray(a, dtype=bool), np.asarray(b, dtype=bool)
    n = len(x)
    only_a, only_b = int((x & ~y).sum()), int((~x & y).sum())
    both_right, both_wrong = int((x & y).sum()), int((~x & ~y).sum())
    diff = float(x.mean() - y.mean()) if n else 0.0
    out = {"items": n, "both_right": both_right, "both_wrong": both_wrong, "only_a": only_a, "only_b": only_b, "disagreements": only_a + only_b,
           "accuracy_a": float(x.mean()) if n else None, "accuracy_b": float(y.mean()) if n else None, "difference": diff,
           "rule": {"alpha": alpha, "min_items": min_items, "min_disagreements": min_disagreements, "test": "exact McNemar, two-sided"}}
    if n < min_items or only_a + only_b < min_disagreements:
        why = f"only {n} items" if n < min_items else f"the runs disagree on only {only_a + only_b} items"
        return {**out, "verdict": "not_enough_data", "p_value": None, "interval": None,
                "reason": f"Not enough data to say: {why} (the rule needs at least {min_items} items and {min_disagreements} where the runs disagree)."}
    p = mcnemar_exact(only_a, only_b)
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, n, size=(bootstrap, n))
    diffs = x[picks].mean(axis=1) - y[picks].mean(axis=1)
    lo, hi = (float(v) for v in np.percentile(diffs, [2.5, 97.5]))
    if p < alpha:
        verdict = "a_better" if only_a > only_b else "b_better"
        reason = f"{'A' if only_a > only_b else 'B'} is right more often on the items where the runs disagree (p = {p:.4f})."
    else:
        verdict, reason = "no_difference_shown", f"The runs do not differ enough to tell apart on these items (p = {p:.4f}); that is not proof they are equal."
    return {**out, "verdict": verdict, "p_value": p, "interval": [lo, hi], "reason": reason}
