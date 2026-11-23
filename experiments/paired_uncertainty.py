"""Post-hoc uncertainty estimates for final-corpus vulnerable/fixed pairs."""
from collections import defaultdict
from pathlib import Path
import json

import numpy as np
from scipy.stats import binomtest


ROOT = Path(__file__).parent


def paired_decisions(arm):
    rows = json.loads((ROOT / f"grounded_eval_{arm}" / "scored_views.json").read_text())[arm]
    packages = json.loads((ROOT / "evaluation_packages.json").read_text())
    pairs = defaultdict(dict)
    for row in rows:
        if row["kind"].startswith(("discovery_", "negative_fixed")):
            key = (packages[row["view_id"]], row["unit"], row["form"])
            label = row["expected_status"]
            if label in pairs[key]:
                raise ValueError(f"Duplicate side in {key}")
            pairs[key][label] = row["valid_json"] and row["got_status"] == label
    return {k: all(v.values()) for k, v in pairs.items() if len(v) == 2}


def cluster_interval(pairs, by_package):
    groups = defaultdict(list)
    for (package, unit, _), passed in pairs.items():
        groups[package if by_package else unit].append(passed)
    counts = np.array([(sum(v), len(v)) for _, v in sorted(groups.items())])
    rng = np.random.default_rng(17)
    replicates = []
    for _ in range(100):
        draws = rng.integers(len(counts), size=(100, len(counts)))
        totals = counts[draws].sum(axis=1)
        replicates.extend(totals[:, 0] / totals[:, 1])
    return len(groups), np.quantile(replicates, [0.025, 0.975])


if __name__ == "__main__":
    first, second = (paired_decisions(arm) for arm in ("v40a", "v40b"))
    assert first.keys() == second.keys()
    for label, pairs in (("first", first), ("second", second)):
        print(label, sum(pairs.values()), len(pairs))
        print("function and package intervals", cluster_interval(pairs, False), cluster_interval(pairs, True))
    functions = defaultdict(list)
    for key in first:
        functions[key[1]].append(key)
    outcomes = [(all(first[k] for k in keys), all(second[k] for k in keys)) for keys in functions.values()]
    a = outcomes.count((True, False))
    b = outcomes.count((False, True))
    print("functions, discordant directions, exact McNemar p", len(outcomes), a, b,
          binomtest(a, a + b).pvalue if a + b else 1.0)
