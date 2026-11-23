"""The advisories that apply to a list of packages of one ecosystem: from the working store (the advisories the app has ingested) and from the parsed OSV dump of that ecosystem on
disk, with the affected ranges of every advisory, ready for ``depversions.is_affected``. Only the packages asked for are read out of the dump, so a project of a hundred packages does
not hold a whole ecosystem in memory.
"""
from __future__ import annotations

import json
import re

from app.backend.pipeline.scan import capabilities

FIELDS = ("osv_id", "cve_id", "summary", "cvss_vector", "osv_cwe", "osv_severity", "affected")


def norm(ecosystem: str, name: str) -> str:
    """The name under which a package is looked up: PyPI treats ``-``, ``_`` and ``.`` alike; the others are compared in lower case."""
    n = str(name or "").lower()
    return re.sub(r"[-_.]+", "-", n) if ecosystem == "PyPI" else n


def by_package(ecosystem: str, names: list[str]) -> tuple[dict[str, list[dict]], dict]:
    """``({normalised name: [advisory with its affected entry]}, source)`` where ``source`` says what was read: ``store`` (records), ``dump`` (the parsed OSV file), and whether
    the dump exists at all, so that a missing one is reported and not mistaken for "no advisories"."""
    from app.backend.pipeline.ingest import depversions
    wanted = {norm(ecosystem, n) for n in names}
    out: dict[str, list[dict]] = {}
    seen: set[tuple[str, str]] = set()
    source = {"store": 0, "dump": 0, "dump_present": capabilities.advisory_file(ecosystem).exists()}
    try:
        from app.backend.pipeline import store
        rows = store.records_with_affected(FIELDS)
    except Exception:  # ignored: no store or no advisory table means no advisories from there; the dump is still read
        rows = []
    for r in rows:
        try:
            entries = json.loads(r["affected"] or "[]")
        except ValueError:
            continue
        for e in entries:
            if (e.get("ecosystem") or "").lower() != ecosystem.lower():
                continue
            key = norm(ecosystem, e.get("package"))
            if key in wanted:
                out.setdefault(key, []).append({**r, "entry": e})
                seen.add((r["osv_id"], key))
                source["store"] += 1
    if source["dump_present"]:
        import pandas as pd
        df = pd.read_parquet(capabilities.advisory_file(ecosystem))
        if "package" in df.columns and "affected" in df.columns:
            df = df[df["package"].map(lambda p: norm(ecosystem, p) in wanted)]
            df = df.astype(object).where(df.notna(), None)
            for r in df.drop_duplicates(["osv_id", "package"]).to_dict("records"):
                key = norm(ecosystem, r["package"])
                if (r["osv_id"], key) in seen or not r.get("affected"):
                    continue
                try:
                    entries = [e for e in json.loads(r["affected"]) if norm(ecosystem, e.get("package")) == key]
                except ValueError:
                    continue
                for e in depversions.merge_packages(entries):
                    out.setdefault(key, []).append({**{k: r.get(k) for k in FIELDS}, "entry": e})
                    source["dump"] += 1
                seen.add((r["osv_id"], key))
    return out, source
