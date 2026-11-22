"""Count pairs with a finding on vulnerable code and no finding on its fixed twin."""
import json
from pathlib import Path


def matched_pairs(rows, model):
    decisions = {}
    for row in rows:
        if row["kind"].startswith("discovery_"):
            side = "vulnerable"
        elif row["kind"].startswith("negative_fixed"):
            side = "fixed"
        else:
            continue
        pair = decisions.setdefault(row["unit"], {})
        if side in pair:
            raise ValueError(f"Duplicate {side} view for {row['unit']}")
        answer = row[model]
        pair[side] = answer["got_status"] if answer["valid_json"] else None

    if any(set(pair) != {"vulnerable", "fixed"} for pair in decisions.values()):
        raise ValueError("Incomplete matched pair")
    passed = sum(pair["vulnerable"] == "finding" and pair["fixed"] == "no_finding"
                 for pair in decisions.values())
    return passed, len(decisions)


if __name__ == "__main__":
    rows = json.loads(Path(__file__).with_name("scored_views.json").read_text("utf-8"))
    for model in ("base", "v40"):
        passed, total = matched_pairs(rows.values(), model)
        print(f"{model}: {passed}/{total}")
