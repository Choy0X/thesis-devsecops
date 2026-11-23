"""The advisories counted by year and severity, for the stacked columns.

Every vulnerability with a publication date is counted in its year, under the severity word the advisory database gives it (or as unrated when it gives none), so the columns add up to
the whole and nothing is sampled. Vulnerabilities without a date are counted apart.
"""
from __future__ import annotations

from app.backend.pipeline import store

_BAND = {"critical": "critical", "high": "high", "moderate": "medium", "medium": "medium", "low": "low"}


SEVERITIES = ("low", "medium", "high", "critical")


def _year(value) -> int | None:
    """The year a ``published`` value starts with (an ISO date or time, or a date object), or None when it has none that makes sense."""
    if hasattr(value, "year") and not isinstance(value, str):
        value = getattr(value, "year", None)
    text = str(value or "").strip()[:4]
    return int(text) if text.isdigit() and 1970 <= int(text) <= 2100 else None


def by_year() -> dict:
    """How many vulnerabilities were published in each year, split by the severity word the advisory carries: ``{"total", "dated", "undated", "unrated", "years": [{"year", "low",
    "medium", "high", "critical", "unrated", "total"}]}``. Every year from the first to the last is listed, also those with none. ``unrated`` are dated vulnerabilities without a severity
    word; ``undated`` ones cannot be placed in a year at all. Malware and withdrawn advisories are not vulnerabilities and are not counted."""
    rows = store.select_mineable(("published", "osv_severity"), order="id")
    per_year: dict[int, dict[str, int]] = {}
    undated = 0
    for r in rows:
        year = _year(r.get("published"))
        if year is None:
            undated += 1
            continue
        band = _BAND.get(str(r.get("osv_severity") or "").strip().lower(), "unrated")
        per_year.setdefault(year, dict.fromkeys((*SEVERITIES, "unrated"), 0))[band] += 1
    years = []
    if per_year:
        for y in range(min(per_year), max(per_year) + 1):
            c = per_year.get(y) or dict.fromkeys((*SEVERITIES, "unrated"), 0)
            years.append({"year": y, **c, "total": sum(c.values())})
    dated = sum(y["total"] for y in years)
    return {"total": len(rows), "dated": dated, "undated": undated, "unrated": sum(y["unrated"] for y in years), "years": years}
