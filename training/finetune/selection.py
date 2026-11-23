"""Checkpoint selection on validation: the best recall at the chosen false-positive rate among the candidates that pass the gates.

The gates are clean utility (the model has not lost its ordinary ability) and repair (the patch task still works). The validation loss
only breaks a tie. A candidate that fails a gate is never selected however high its recall; when none passes, nothing is selected and
the report says why.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Gates:
    min_clean_utility: float
    min_repair_pass: float


def gate_failures(candidate: dict, gates: Gates) -> list[str]:
    out = []
    if candidate.get("clean_utility") is None or candidate["clean_utility"] < gates.min_clean_utility:
        out.append("clean_utility")
    if candidate.get("repair_pass") is None or candidate["repair_pass"] < gates.min_repair_pass:
        out.append("repair_pass")
    return out


def select_checkpoint(candidates, gates: Gates) -> tuple[dict | None, dict]:
    rejected = {c["id"]: f for c in candidates if (f := gate_failures(c, gates))}
    eligible = sorted((c for c in candidates if c["id"] not in rejected),
                      key=lambda c: (-c["recall_at_fpr"], c.get("val_loss", math.inf), c["id"]))
    report = {"rejected": rejected, "eligible": [c["id"] for c in eligible]}
    return (eligible[0] if eligible else None), report


def selection_metric(candidate: dict, gates: Gates) -> float:
    """What the trainer maximizes: the recall at the chosen false-positive rate, or minus infinity when a gate fails."""
    return -math.inf if gate_failures(candidate, gates) else float(candidate["recall_at_fpr"])
