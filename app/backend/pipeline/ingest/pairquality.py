"""Per-pair quality data that is kept beside the pairs, not in them.

``change_key`` is a hash of what a diff changes (the added and removed lines with whitespace squashed, the headers and the context left out), so the same fix cherry-picked to several
release branches gets the same key whatever lines surround it; the dataset build collapses such copies by it. ``signals`` are the pieces of evidence the attribution found. Both live in a side
collection (``pair_quality`` in the embedded database) keyed by the pair, because re-mining or re-splitting a pair rewrites every field of the pair itself. A row is valid while its
``content_hash`` equals the pair's, and counts as current while its ``version`` is ``QUALITY_VERSION``: bump it when the attribution rules or the key change, and every commit is done again.
"""
from __future__ import annotations

import hashlib
import re

QUALITY_VERSION = 1


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def change_key(diff: str | None) -> str:
    """The key of what a diff changes, or ``""`` when it changes nothing that is not blank."""
    body = [_squash(line[1:]) for line in (diff or "").split("\n") if line[:1] in "+-" and not line.startswith(("+++", "---")) and _squash(line[1:])]
    return hashlib.sha1("\n".join(body).encode()).hexdigest() if body else ""


def rows(pairs: list[dict]) -> list[dict]:
    """The quality rows of freshly attributed pairs (the ones with code), as ``set_pair_quality`` takes them."""
    return [{"pair_key": p["pair_key"], "version": QUALITY_VERSION, "content_hash": p.get("content_hash"), "change_key": change_key(p.get("diff")),
             "signals": list(p.get("attribution_signals") or [])}
            for p in pairs if p.get("pair_key") and p.get("code_available", 1) and p.get("attribution_basis") is not None]
