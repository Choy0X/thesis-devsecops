"""A slice of the training families: train on part of the training split, the same part every time.

The training views belong to families (the views of one issue). A slice is a contiguous range of the families in one fixed order, given as fractions
``start`` to ``end`` of the whole (0 to 1 is everything). The order is the SHA-256 of the family id, so it does not depend on the order of the rows in the file,
on the dataset version's size, or on a random seed: the same family is at the same place whatever else is in the split. Slices that meet (0 to 0.5 and
0.5 to 1) cover every family once and share none, so two runs on the two halves are on disjoint data. Views are never cut from a family, only families are
chosen, and only the training split is sliced: the validation split is not passed through here. Nothing here is random.
"""
from __future__ import annotations

import hashlib


class SliceError(ValueError):
    """The slice selects no family, or its bounds are not a range inside 0 to 1."""


def family_order(views: list[dict]) -> list[str]:
    """The distinct family ids of ``views`` in the fixed order: by the SHA-256 of the id, then the id itself (a tie is not expected, but order must be total)."""
    return sorted({v["family_id"] for v in views}, key=lambda f: (hashlib.sha256(f.encode("utf-8")).hexdigest(), f))


def select(views: list[dict], start: float = 0.0, end: float = 1.0) -> tuple[list[dict], dict]:
    """``(views of the chosen families, report)``; the views keep their order. The report says how many families and views were kept of how many."""
    if not (0.0 <= start < end <= 1.0):
        raise SliceError(f"the slice must be a range inside 0 to 1 with start below end, not {start} to {end}")
    families = family_order(views)
    lo, hi = int(len(families) * start), int(len(families) * end)
    if end == 1.0:
        hi = len(families)                                   # the last slice takes the remainder, so slices that meet always cover every family
    chosen = set(families[lo:hi])
    if not chosen:
        raise SliceError(f"the slice {start} to {end} selects none of the {len(families)} training families; widen it")
    kept = [v for v in views if v["family_id"] in chosen]
    return kept, {"start": start, "end": end, "families": len(chosen), "families_total": len(families), "views": len(kept), "views_total": len(views),
                  "order": "sha256 of the family id"}
