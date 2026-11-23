"""The make-up of the collected advisories or of a dataset by language, ecosystem and severity (what the radar charts show).

Severity is the advisory database's own word, grouped into the four bands people read (low, medium, high, critical); "none" counts the advisories that
carry no severity. A dataset build records language and ecosystem per group but not severity, so its severity is reported as unknown rather than guessed.
"""
from __future__ import annotations

import json
import pathlib
import re

from app.backend.pipeline import store

BANDS = ("critical", "high", "medium", "low", "none")
_WORD = {"critical": "critical", "high": "high", "moderate": "medium", "medium": "medium", "low": "low"}
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _top(counts: dict[str, int], keep: int) -> dict[str, int]:
    """The ``keep`` largest categories, the rest added up under "other"."""
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    head, tail = ordered[:keep], ordered[keep:]
    out = dict(head)
    if tail:
        out["other"] = sum(n for _, n in tail)
    return out


def bands(raw: dict[str, int]) -> dict[str, int]:
    out = {b: 0 for b in BANDS}
    for word, n in raw.items():
        out[_WORD.get(word, "none")] += n
    return out


def store_mix(keep: int = 8) -> dict:
    s = store.summary()
    ecosystems = {eco: int(v["advisories"]) for eco, v in s["by_ecosystem"].items() if v.get("advisories")}
    return {"scope": "store", "total": sum(ecosystems.values()),
            "language": _top({k: int(v) for k, v in s["by_language"].items()}, keep), "ecosystem": _top(ecosystems, keep),
            "severity": bands(store.severity_counts())}


def dataset_mix(root: pathlib.Path, tag: str, keep: int = 8) -> dict | None:
    """The mix of a dataset build from its ``stats.json`` coverage rows (records per ecosystem and language); None when the build does not exist."""
    if not _TAG.match(tag):
        return None
    try:
        stats = json.loads((pathlib.Path(root) / f"dataset_{tag}" / "stats.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    lang: dict[str, int] = {}
    eco: dict[str, int] = {}
    for row in stats.get("coverage", []):
        n = int(row.get("records", 0))
        lang[row.get("language") or "unknown"] = lang.get(row.get("language") or "unknown", 0) + n
        eco[row.get("ecosystem") or "unknown"] = eco.get(row.get("ecosystem") or "unknown", 0) + n
    return {"scope": f"dataset_{tag}", "total": sum(eco.values()), "language": _top(lang, keep), "ecosystem": _top(eco, keep), "severity": None}
