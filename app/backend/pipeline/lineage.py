"""How far the collected advisories get on the way to a dataset, and why the rest drop out.

The stages, in order: vulnerability advisories, those with a fix commit, those whose code was mined, the code pairs that came out, the pairs that can be
used, and the records of the latest dataset build. Each stage after the first lists the reasons for the drop from the one before. The counts come from the
repository functions of the store; the build numbers come from the build manifests.

The unit changes twice: the first three stages count advisories, the next two count pairs (one advisory gives several), the last counts dataset records.
The stage says which unit it counts, and a drop is shown only between stages of the same unit.
"""
from __future__ import annotations

from app.backend.pipeline import codestore, store


def _reason(id_: str, label: str, count: int) -> dict:
    return {"id": id_, "label": label, "count": max(0, int(count))}


def lineage(build: dict | None = None, funnel_max_age: float = 0.0) -> dict:
    """``build`` is the manifest summary of the latest dataset build (``counts`` and ``tier_mix``), or None when nothing was built."""
    funnel = store.funnel_cached(funnel_max_age)
    total = lambda key: sum(int(v.get(key, 0)) for v in funnel.values())  # noqa: E731
    adv, ptr, mined = total("advisories"), total("with_fix_pointer"), total("mined")
    pairs = codestore.pair_summary()
    unavailable = sum(int(n) for n in pairs["by_skip_reason"].values())

    stages = [
        {"id": "advisories", "label": "Vulnerability advisories", "unit": "advisories", "count": adv, "lost": []},
        {"id": "fix_commit", "label": "With a fix commit", "unit": "advisories", "count": ptr,
         "lost": [_reason("no_fix_commit", "No fix commit known", adv - ptr)]},
        {"id": "mined", "label": "Code mined", "unit": "advisories", "count": mined,
         "lost": [_reason("not_mined", "Not mined yet, failed or without code", ptr - mined)]},
        {"id": "pairs", "label": "Code pairs", "unit": "pairs", "count": pairs["total"], "lost": []},
        {"id": "usable", "label": "Usable pairs", "unit": "pairs", "count": pairs["usable"],
         "lost": [_reason("unavailable", "Code not available", unavailable), _reason("tangled", "Commit changes unrelated things", pairs["tangled"]),
                  _reason("tests", "Test files", pairs["tests"])],
         "reasons_overlap": True},
    ]
    if build:
        counts, mix = build.get("counts", {}), build.get("tier_mix", {})
        records = int(counts.get("records", 0))
        dup = int(counts.get("duplicates", 0))
        stages.append({"id": "dataset", "label": f"Dataset records ({build.get('tag', 'latest build')})", "unit": "records", "count": records,
                       "lost": [_reason("duplicates", "Duplicates set aside", dup),
                                _reason("unreviewed", "Not checked by any tier", mix.get("unreviewed", 0))],
                       "tier_mix": {k: int(v) for k, v in mix.items()}})
    return {"stages": stages, "set_aside": {"malware": total("malware"), "withdrawn": total("withdrawn"), "informational": total("informational")}}
