"""The CVE records of the working store, in the local SQLite database.

This is the editable backbone the dataset is built from. Ingest upserts OSV (and NVD, when reachable) records into it; the user can also add, edit, delete, import and live-fetch records from the
Data tab. ``build_dataset`` reads this store as its backbone and applies the frozen labelling / dedup / temporal-split logic, so the resulting ``dataset_stats.json`` reflects real records the user curated.

One advisory per record (keyed by the OSV id, or the CVE id, or a generated id for purely manual entries). The embedded document functions retain the compatibility module name ``db/store_mongo.py`` and are
bound at the end of this module under the names the rest of the application uses; what stays here is the shape of a record (``FIELDS``), the rules that tell what kind of advisory it is and the
helpers that clean a record before it is stored.
"""
from __future__ import annotations
import threading
import time
import uuid
import json

import pandas as pd
from app.backend.pipeline.db import store_mongo as _store_mongo

# Columns stored per record (besides the internal id / key / timestamps).
FIELDS = [
    "osv_id", "cve_id", "package", "summary", "details", "published",
    "modified", "withdrawn", "cvss_vector", "cvss_v4_vector", "cvss_score",
    "osv_cwe", "cwe", "code_before", "code_after", "filename", "source",
    "ecosystem",
    # code mining (filled by Extract / Mine; ``language`` is detected from git
    # unless the user sets it, which makes ``language_source`` "manual")
    "language", "language_source", "fix_commits", "fix_commit_count",
    "code_status", "pair_count",
    # dependency versions (depversions.py): affected ranges per package and the
    # first fixed / latest safe release
    "affected", "affected_versions", "fixed_version", "safe_version",
    "dependency_status", "deps_checked_at",
    # the advisory database's qualitative severity (GitHub: LOW / MODERATE / HIGH /
    # CRITICAL), kept beside the CVSS-derived label, never mapped onto it
    "osv_severity",
    # what kind of advisory this is: only 'vulnerability' records are mined (see classify_kind)
    "advisory_kind",
    # provenance of the OSV record: every alias, typed reference URLs, hash of the raw file
    "aliases", "source_urls", "source_record_hash",
]
# -- advisory kinds ---------------------------------------------------------------------

ADVISORY_KINDS = ("vulnerability", "malware", "withdrawn", "informational")


def classify_kind(osv_id, withdrawn) -> str:
    """malware (an OSV MAL- report), withdrawn, else vulnerability. Malware reports and
    withdrawn advisories describe no fixable vulnerability and are never mined."""
    if isinstance(osv_id, str) and osv_id.upper().startswith("MAL-"):
        return "malware"
    if isinstance(withdrawn, str) and withdrawn.strip():
        return "withdrawn"
    return "vulnerability"


def _key_for(rec: dict) -> str:
    return (rec.get("osv_id") or rec.get("cve_id")
            or f"manual-{uuid.uuid4().hex[:12]}")


def _clean(rec: dict) -> dict:
    """Keep only known fields; coerce blanks to None; score to float/None."""
    out = {c: rec.get(c) for c in FIELDS}
    for c, v in list(out.items()):
        if isinstance(v, str) and v.strip() == "":
            out[c] = None
        elif isinstance(v, float) and v != v:       # pandas NaN for a missing value
            out[c] = None
    if not out.get("advisory_kind"):
        out["advisory_kind"] = classify_kind(out.get("osv_id"), out.get("withdrawn"))
    for c in ("fix_commit_count", "pair_count"):
        try:
            out[c] = None if out[c] in ("", None) else int(out[c])
        except (TypeError, ValueError):
            out[c] = None
    if isinstance(out.get("fix_commits"), (list, tuple)):
        out["fix_commits"] = json.dumps(list(out["fix_commits"]))
    score = out.get("cvss_score")
    if score in ("", None):
        out["cvss_score"] = None
    else:
        try:
            out["cvss_score"] = float(score)
        except (TypeError, ValueError):
            out["cvss_score"] = None
    return out


# ── writes ──────────────────────────────────────────────────────────────────

KEEP_SOURCES = ("manual", "import")     # records a person added: never pruned by date


def _eco(v) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def import_rows(rows: list[dict], source: str = "import") -> int:
    if not rows:
        return 0
    return upsert_many(pd.DataFrame(rows), source=source)


# ── reads ───────────────────────────────────────────────────────────────────

# The columns a list needs. The long ones (the advisory text, the affected ranges, the fix pointers, the references, the aliases) are read when a
# record is opened, so a page of 25 rows is a few kilobytes instead of hundreds.
HEAVY_COLUMNS = ("details", "affected", "fix_commits", "source_urls", "aliases", "code_before", "code_after", "filename")


COUNT_CAP = 10_000                    # Legacy caller constant; list queries now return exact filtered counts.
LIST_SUMMARY = 240                    # characters of the summary a list row carries
# What a row of the records table shows (and the drawer opens with before the whole record arrives): a few short fields instead of the 25 columns of a record.
LIST_COLUMNS = ("id", "cve_id", "osv_id", "package", "ecosystem", "language", "osv_severity", "cvss_score", "fix_commit_count", "code_status", "pair_count", "published",
                "dependency_status", "summary")
SORT_COLUMNS = {"id": "id", "published": "published", "cve_id": "cve_id", "ecosystem": "ecosystem"}       # the only columns a list can be sorted by (never text from a request in SQL)
SEVERITY_WORDS = {"low": ("LOW",), "medium": ("MEDIUM", "MODERATE"), "high": ("HIGH",), "critical": ("CRITICAL",)}
BANDS = ("low", "medium", "high", "critical", "unrated")
BAND_OF_WORD = {w: band for band, words in SEVERITY_WORDS.items() for w in words}


def check_choices(sort: str = "id", direction: str = "desc", severity: str = "") -> None:
    """Refuse a sort, a direction or a severity that is not one of the allowed words (the routes answer 400)."""
    if sort not in SORT_COLUMNS:
        raise ValueError(f"sort must be one of {', '.join(SORT_COLUMNS)}")
    if direction not in ("asc", "desc"):
        raise ValueError("direction must be asc or desc")
    if severity and severity not in BANDS:
        raise ValueError(f"severity must be one of {', '.join(BANDS)}")


# The same facts, counted by the database: one row per distinct combination (a few hundred) instead of one per record (hundreds of thousands).
# The facts are selected in a MATERIALIZED subquery and grouped outside it: SQLite does not answer a grouped query that has an indexed
# expression in its select list from the covering index (it reads the table), but it does answer the plain select, which is what the subquery is.
_FUNNEL: dict = {"token": None, "at": 0.0, "value": None}
_FUNNEL_LOCK = threading.Lock()


def funnel_cached(max_age: float = 0.0) -> dict:
    """``ecosystem_funnel()`` computed once at a time and shared by the routes that show it (the dataset summary and the campaign status asked
    for it separately, each time). It is reused while nothing has been written (``change_token``), and for ``max_age`` seconds even if
    something has: a collection campaign writes all the time, and a funnel a few seconds old is the right answer for a progress display."""
    with _FUNNEL_LOCK:
        token = change_token()
        age = time.monotonic() - _FUNNEL["at"]
        if _FUNNEL["value"] is not None and (_FUNNEL["token"] == token or age < max_age):
            return _FUNNEL["value"]
        value = ecosystem_funnel()
        _FUNNEL.update(token=token, at=time.monotonic(), value=value)
        return value


# ── pipeline selections (the reads and writes the mining stages used to do with their own SQL) ───────────────────────

# ── registry cache (package_versions): versions and release dates fetched from the package registries ──────────────

# ── record reads of the dataset builders, the leakage graph and the reviewed build ─────────────────────────────────────

# ── live OSV fetch ────────────────────────────────────────────────────────────

OSV_API = "https://api.osv.dev/v1"


def _parse_vuln(rec: dict) -> dict:
    """Parse one OSV vuln object into a store row (reusing osv._pick_vectors)."""
    from app.backend.pipeline.ingest.osv import _pick_vectors, provenance
    prov = provenance(rec)
    cve = prov["cve_id"]
    ds = rec.get("database_specific", {}) or {}
    cwe_ids = ds.get("cwe_ids") or []
    v3, v4 = _pick_vectors(rec.get("severity"))
    pkgs = []
    for aff in rec.get("affected", []):
        pkg = (aff.get("package") or {})
        if pkg.get("name"):                       # any ecosystem (e.g. Maven for Log4j)
            pkgs.append(pkg["name"])
    from app.backend.pipeline.ingest.fixrefs import from_osv_record
    from app.backend.pipeline.ingest.depversions import affected_from_osv
    ptrs = from_osv_record(rec)
    affected = affected_from_osv(rec)
    ecos = [(a.get("package") or {}).get("ecosystem") for a in rec.get("affected", [])]
    return {
        "osv_id": rec.get("id"),
        "cve_id": cve,
        "ecosystem": next((e for e in ecos if e), None),
        "fix_commits": json.dumps(ptrs) if ptrs else None,
        "affected": json.dumps(affected) if affected else None,
        "package": ", ".join(sorted(set(pkgs))) or None,
        "summary": rec.get("summary"),
        "details": rec.get("details"),
        "published": rec.get("published"),
        "modified": rec.get("modified"),
        "withdrawn": rec.get("withdrawn"),
        "cvss_vector": v3,
        "cvss_v4_vector": v4,
        "osv_cwe": ";".join(cwe_ids) if cwe_ids else None,
        "osv_severity": ds.get("severity"),
        "aliases": prov["aliases"], "source_urls": prov["source_urls"],
        "source_record_hash": prov["source_record_hash"],
    }


def fetch_osv(cve_id: str | None = None, package: str | None = None) -> dict:
    """Fetch advisories live from the OSV API and upsert them into the store.

    Supply either a vulnerability/CVE id (fetched directly) or a PyPI package
    name (queried for all its advisories). Returns {added, records}.
    """
    import requests
    added, parsed = 0, []
    if cve_id:
        vid = cve_id.strip()
        r = requests.get(f"{OSV_API}/vulns/{vid}", timeout=30)
        if r.status_code == 404:
            # A CVE id may resolve via query if not a primary OSV id.
            q = requests.post(f"{OSV_API}/query", json={"vulns": [{"id": vid}]}, timeout=30)
            q.raise_for_status()
            vulns = q.json().get("vulns", [])
        else:
            r.raise_for_status()
            vulns = [r.json()]
        parsed = [_parse_vuln(v) for v in vulns]
    elif package:
        r = requests.post(f"{OSV_API}/query",
                          json={"package": {"name": package.strip(), "ecosystem": "PyPI"}},
                          timeout=30)
        r.raise_for_status()
        parsed = [_parse_vuln(v) for v in r.json().get("vulns", [])]
    else:
        raise ValueError("provide cve_id or package")

    if parsed:
        added = upsert_many(pd.DataFrame(parsed), source="osv-api")
    return {"added": added, "records": parsed}


# -- embedded data layer (compatibility modules pipeline/db/*_mongo.py), bound under the established public names ---------------------
change_token = _store_mongo.change_token
add = _store_mongo.add
update = _store_mongo.update
delete = _store_mongo.delete
prune_before = _store_mongo.prune_before
clear = _store_mongo.clear
upsert_many = _store_mongo.upsert_many
get_meta = _store_mongo.get_meta
set_meta = _store_mongo.set_meta
meta_keys = _store_mongo.meta_keys
count = _store_mongo.count
get = _store_mongo.get
known_keys = _store_mongo.known_keys
query = _store_mongo.query
facets = _store_mongo.facets
packages_of = _store_mongo.packages_of
ecosystem_funnel = _store_mongo.ecosystem_funnel
summary = _store_mongo.summary
to_dataframe = _store_mongo.to_dataframe
select_mineable = _store_mongo.select_mineable
count_mineable_records = _store_mongo.count_mineable_records
records_by_ids = _store_mongo.records_by_ids
set_fix_commits = _store_mongo.set_fix_commits
update_records = _store_mongo.update_records
registry_rows = _store_mongo.registry_rows
registry_row = _store_mongo.registry_row
registry_save = _store_mongo.registry_save
registry_set_dates = _store_mongo.registry_set_dates
severity_counts = _store_mongo.severity_counts
count_by_code_status = _store_mongo.count_by_code_status
mined_records = _store_mongo.mined_records
dep_checked_records = _store_mongo.dep_checked_records
count_dep_unchecked = _store_mongo.count_dep_unchecked
vulnerability_records = _store_mongo.vulnerability_records
records_with_affected = _store_mongo.records_with_affected
reset_deps_checked = _store_mongo.reset_deps_checked
