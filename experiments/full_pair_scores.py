"""Compute status-based metrics from the saved final-corpus evaluation."""
import json
from collections import Counter, defaultdict
from pathlib import Path


def summarize(rows):
    counts = Counter()
    pairs = defaultdict(dict)
    for row in rows:
        kind = row["kind"]
        if kind.startswith("repair"):
            continue
        expected = row["expected_status"]
        answer = row["got_status"] if row["valid_json"] else None
        counts[expected, answer] += 1
        if kind.startswith(("discovery_", "negative_fixed")):
            sides = pairs[row["unit"], row["form"]]
            if expected in sides:
                raise ValueError("Duplicate side of a pair")
            sides[expected] = answer

    complete = [p for p in pairs.values() if set(p) == {"finding", "no_finding"}]
    tp = counts["finding", "finding"]
    fp = counts["no_finding", "finding"]
    positives = sum(n for (label, _), n in counts.items() if label == "finding")
    return {
        "detection_views": counts.total(),
        "true_positives": tp,
        "false_positives": fp,
        "missed_positives": positives - tp,
        "true_negatives": counts["no_finding", "no_finding"],
        "precision": tp / (tp + fp),
        "recall": tp / positives,
        "f1": 2 * tp / (tp + fp + positives),
        "pairs_passed": sum(p["finding"] == "finding" and p["no_finding"] == "no_finding" for p in complete),
        "complete_pairs": len(complete),
        "same_verdict": sum(p["finding"] == p["no_finding"] for p in complete),
    }


if __name__ == "__main__":
    for arm in ("v40a", "v40b"):
        path = Path(__file__).parent / f"grounded_eval_{arm}" / "scored_views.json"
        print(arm, json.dumps(summarize(json.loads(path.read_text("utf-8"))[arm]), indent=2))
