"""Build the vulnerable/fixed code dataset from the Dataset records.

Only records whose code has been mined (``code_status = 'mined'``) are used, with
their usable pairs in the record's language (detected from git or set by hand).
Every row joins the advisory (CVE, CWE, CVSS -> severity, ecosystem, package,
advisory text) to the code (vulnerable, fixed, diff, file, function, commits).

Rules that always apply:

* **Train on all data, validate on a small slice** (``pipeline/split.py``): the
  validation split is about 5% of packages (whole packages, chosen by a stable
  hash), used by the training notebooks for early stopping and the decision
  threshold. "Train on everything" folds it into train. There is no test split.
* **De-duplication** of identical (vulnerable, fixed) pairs and a **leak guard**:
  a validation row whose vulnerable code also appears in train is dropped.
* **Freeze** writes the next unused ``code_pairs_v<N>`` (see ``versions.py``).

A row's ``date`` is the earliest of the advisory publication and the fix commit
(information; the split does not use dates).

Data quality:

* **Fix links** - pairs of a commit that fails the release-date check
  (``fixcheck.py``: committed before the last affected release or after the fixed
  one) are excluded by default; "only verified" keeps commits the check confirms.
* **Granularity** - function pairs (or hunks) replace the file pair of the same
  file by default (``funcsplit.py``).
* **Severity** - from the record's CVSS (score, else v3, else v4 vector), else the
  NVD score of its CVE; ``severity_source`` says which. GitHub's qualitative
  label is kept apart in ``osv_severity``; without any score, a LOW, HIGH or
  CRITICAL label gives the category (``severity_source`` ``osv_label``, no
  ``cvss_score``) and a MODERATE one leaves the row unscored.

Outputs (``data/processed``), one curated schema (``SCHEMA``) for all of them:
``code_pairs_<tag>.parquet`` and ``code_pairs_<tag>.jsonl`` (one sample per row:
patch generation / general), ``code_pairs_<tag>_detection.jsonl`` (vulnerable = 1
/ safe = 0: each pair's two sides; functions of the fixed file that the fix did not
touch only when ``include_untouched`` asks for them, as weak unverified negatives, at
``safe_ratio`` safe per vulnerable; see ``detection.py``) and
``code_pairs_stats.json`` (split rule, filters and what each one removed).
Every row is validated (``validate``) before anything is written.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import re

import pandas as pd

from app.backend.pipeline import codestore, fsio, store, versions
from app.backend.pipeline import split as splitter
from app.backend.pipeline.build_common import PROC, SEVERITIES, CODE_SOURCES, OSV_LABEL_SOURCE, label, label_severity, severity_score
from app.backend.pipeline.ingest import attribution, depversions, detection, fixcheck
from app.backend.pipeline.ingest import context as ctx
from app.backend import paths
from app.backend.pipeline import util

GRANULARITIES = ("function", "hunk", "file")
INTERIM = paths.INTERIM

DEFAULTS = {
    "date_from": None, "date_to": None,         # advisory publication window
    "languages": [], "severities": [], "require_cvss": False,
    "cwes": [], "require_cwe": False, "ecosystems": [],
    "exclude_suspect": True, "only_verified": False,      # fix-link check (fixcheck.py)
    "granularity": "", "prefer_function": True,
    "max_lines": 300, "min_lines": 1, "max_changed": 0,      # 0 = no limit
    "require_advisory": False, "require_commit_msg": False,
    "only_enriched": False, "include_synthetic": True,
    "weak_context": "keep",                      # keep | enriched_only | drop (context.py)
    "val_share": splitter.VAL_SHARE, "train_all": False,   # split (pipeline/split.py)
    "include_tests": False, "include_tangled": False,
    "sibling_languages": True,                   # pairs in a language other than the record's (mined, tagged)
    "include_quarantined": False,                # ambiguous merge parent, revert, release bump (codemine)
    "unit": "pairs", "per_cve_cap": 5,           # unit: pairs | best (one per record)
    "collapse_backports": True,                  # the same change in several commits of one CVE (a fix cherry-picked to release branches) becomes one row
    "attribution": "",                           # "" every pair | "supporting" the advisory or the fix points at the unit or its file | "decision" only what the review gate trusts
    # detection set (ingest/detection.py): safe samples per vulnerable one, and the
    # longest code kept (longer pairs are skipped, never truncated). Functions the fix
    # did not touch are not evidence of secure code: they are left out unless asked for
    # (include_untouched), and then they are weak, unverified negatives
    "safe_ratio": detection.DEFAULTS["safe_ratio"], "max_chars": detection.DEFAULTS["max_chars"], "include_untouched": False,
    "profile": "training",                       # the Build-card profile these filters came from
}

# Build profiles: presets of the quality settings (the scope - languages,
# ecosystems, severities, CWEs, dates - is chosen separately). Each overrides
# DEFAULTS; test files, tangled commits and suspect fix commits stay excluded in all.
PROFILES = [
    {"id": "training", "name": "Training (recommended)",
     "description": "Every usable pair, at most 5 per CVE, function level where possible, at most "
                    "300 lines; 5% validation for early stopping and the threshold.",
     "filters": {}},
    {"id": "final", "name": "Final model",
     "description": "The Training data with validation folded in. Use it for the last run only, with "
                    "the run_config.json of a Training run (FINAL_FROM in the notebook).",
     "filters": {"train_all": True}},
    {"id": "max_data", "name": "Maximum data",
     "description": "No per-CVE cap, up to 600 lines and every copy of a fix kept (also the ones cherry-picked "
                    "to release branches): the most rows, more weight on CVEs with many changed functions. Still clean.",
     "filters": {"per_cve_cap": 0, "max_lines": 600, "collapse_backports": False}},
    {"id": "high_quality", "name": "Highest quality",
     "description": "Only fixes the release dates confirm, CWE and CVSS required, weak-context rows "
                    "dropped, at most 3 per CVE and 200 lines: smaller and cleanest.",
     "filters": {"only_verified": True, "require_cwe": True, "require_cvss": True,
                 "weak_context": "drop", "per_cve_cap": 3, "max_lines": 200}},
    {"id": "attributed", "name": "Attributed only",
     "description": "Only pairs whose unit the advisory names or that is the only one the fix changed (the review gate's decision tier): a smaller set whose label of 'the vulnerable function' you can trust.",
     "filters": {"attribution": "decision"}},
]


def options() -> dict:
    """Choices for the UI, from the mined records: languages, ecosystems, CWEs and
    severities with counts, the collection window and the split cutoff."""
    recs = _mined_records()
    langs, ecos, cwes, sev = {}, {}, {}, {}
    for r in recs:
        for l in (r.get("language") or "").split(","):
            if l:
                langs[l] = langs.get(l, 0) + 1
        if r.get("ecosystem"):
            ecos[r["ecosystem"]] = ecos.get(r["ecosystem"], 0) + 1
        for c in _cwes(r):
            cwes[c] = cwes.get(c, 0) + 1
        s = r["severity"] or "unscored"
        sev[s] = sev.get(s, 0) + 1
    if not langs and recs:            # records mined before detection: use their pairs' languages
        rows = _rows(recs)
        if len(rows):
            langs = rows.drop_duplicates(["record_id", "language"]).language.value_counts().to_dict()
    by = lambda d, n=None: dict(sorted(d.items(), key=lambda kv: -kv[1])[:n])
    pubs = sorted(r["published"] for r in recs if r.get("published"))
    return {
        "records_mined": len(recs),
        "window": {"since": store.get_meta("collection_since"),
                   "collected_at": store.get_meta("collection_at"),
                   "earliest": pubs[0][:10] if pubs else None,
                   "latest": pubs[-1][:10] if pubs else None},
        "split": splitter.describe(),
        "languages": by(langs), "ecosystems": by(ecos), "cwes": by(cwes, 40),
        "severities": by(sev), "defaults": DEFAULTS,
        "profiles": [{**p, "filters": {**p["filters"], "profile": p["id"]}} for p in PROFILES],
        "fix_checks": codestore.fix_commit_summary().get("by_fix_check", {}),
        "records_suspect": _count_status("suspect"),
    }


def _count_status(status: str) -> int:
    return store.count_by_code_status(status)


# ── loading ──────────────────────────────────────────────────────────────────

def _cwes(r: dict) -> list[str]:
    out = []
    for key in ("cwe", "osv_cwe"):
        for c in str(r.get(key) or "").replace(",", ";").split(";"):
            c = c.strip()
            if c.startswith("CWE-") and c not in out:
                out.append(c)
    return out


def _osv_severity(ids: set[str]) -> dict[str, str]:
    """GitHub's qualitative severity of advisories stored before records kept it,
    from the parsed OSV dumps."""
    out: dict[str, str] = {}
    if not ids:
        return out
    import pyarrow.parquet as papq
    for pq in sorted(INTERIM.glob("osv_*.parquet")):
        try:
            if "osv_severity" not in papq.read_schema(pq).names:
                continue
            for b in papq.ParquetFile(pq).iter_batches(batch_size=50000, columns=["osv_id", "osv_severity"]):
                for oid, sev in zip(b.column(0).to_pylist(), b.column(1).to_pylist()):
                    if oid in ids and sev and oid not in out:
                        out[oid] = str(sev).upper()
        except Exception:  # ignored: an unreadable parquet file is skipped; severities come from the others
            continue
    return out


def _nvd(cves: set[str]) -> dict[str, dict]:
    """NVD score / CWE of the records' CVEs (data/interim/nvd.parquet)."""
    path = INTERIM / "nvd.parquet"
    if not cves or not path.exists():
        return {}
    import pyarrow.parquet as papq
    cols = [c for c in ("cve_id", "cvss_score", "cvss_version", "cwe") if c in papq.read_schema(path).names]
    out = {}
    for b in papq.ParquetFile(path).iter_batches(batch_size=50000, columns=cols):
        d = b.to_pydict()
        for i, cve in enumerate(d["cve_id"]):
            if cve in cves:
                out[cve] = {c: d[c][i] for c in cols}
    return out


def _severity(r: dict, nvd: dict) -> tuple[float | None, str | None]:
    """(CVSS base score, source), in the same order as the classification dataset
    (build_common.py; data/README.md "Labelling"), so every builder labels a CVE the same:
    the NVD score of its CVE, the OSV CVSS v3 vector, the OSV CVSS v4 vector, and
    last a score typed on the record."""
    n = nvd.get(r.get("cve_id")) or {}
    score, source = severity_score(nvd=n.get("cvss_score"), v3=r.get("cvss_vector"), v4=r.get("cvss_v4_vector"),
                                   record=r.get("cvss_score"))
    return (score, CODE_SOURCES[source]) if source else (None, None)


def _severity_category(r: dict, nvd: dict) -> tuple[float | None, str | None, str | None]:
    """(CVSS score, severity category, source): the category of the score, else the one GitHub's label settles (``label_severity``), else nothing. The score is never made up from a label."""
    score, source = _severity(r, nvd)
    category = label(score)
    if category is None and (category := label_severity(r.get("osv_severity"))):
        source = OSV_LABEL_SOURCE
    return score, category, source


def _mined_records(include_suspect: bool = False) -> list[dict]:
    statuses = ("mined", "suspect") if include_suspect else ("mined",)
    recs = store.mined_records(statuses, ("id", "osv_id", "cve_id", "package", "ecosystem", "summary", "details", "published", "cvss_vector",
                                          "cvss_v4_vector", "cvss_score", "osv_cwe", "cwe", "language", "fix_commits", "affected_versions",
                                          "fixed_version", "safe_version", "dependency_status", "osv_severity", "affected"))
    nvd = _nvd({r["cve_id"] for r in recs if r.get("cve_id")})
    sev = _osv_severity({r["osv_id"] for r in recs if r.get("osv_id") and not r.get("osv_severity")})
    for r in recs:
        r["osv_severity"] = (r.get("osv_severity") or sev.get(r.get("osv_id")) or None)
        r["osv_severity"] = r["osv_severity"].upper() if r["osv_severity"] else None
        r["cvss"], r["severity"], r["severity_source"] = _severity_category(r, nvd)
        if not _cwes(r) and (nvd.get(r.get("cve_id")) or {}).get("cwe"):
            r["cwe"] = nvd[r["cve_id"]]["cwe"]                    # CWE from NVD
    return recs


def _rows(recs: list[dict], sibling_languages: bool = True) -> pd.DataFrame:
    """One row per (record, usable pair); with ``sibling_languages`` off, only pairs in the
    record's own language(s)."""
    keys = sorted({k for r in recs for k in codestore._record_commit_keys(r)})
    cves = sorted({r["cve_id"] for r in recs if r.get("cve_id")})
    raw_checks, by_commit, manual = codestore.light_pair_inputs(keys, cves)
    checks = {k: (c["fix_check"], fixcheck.link_strength(c["via"]), c["fix_check_package"]) for k, c in raw_checks.items()}
    rows = []
    for r in recs:
        langs = [l for l in (r.get("language") or "").split(",") if l]
        pairs = [p for k in codestore._record_commit_keys(r) for p in by_commit.get(k, [])]
        pairs += manual.get(r.get("cve_id"), [])
        advisory = ctx.advisory_text(r.get("summary"), r.get("details"))
        for p in pairs:
            if langs and not sibling_languages and p["language"] not in langs:
                continue
            chk, link, fix_pkg = checks.get(p["commit_key"],
                                            (None, "manual" if p.get("source") == "manual" else None, None))
            versions = _versions_for(r, fix_pkg)
            rows.append({**p, "pair_id": p["id"], "record_id": r["id"], "osv_id": r["osv_id"],
                         "fix_check": chk or ("manual" if p.get("source") == "manual" else "unchecked"),
                         "fix_link": link, "severity_source": r["severity_source"],
                         "osv_severity": r["osv_severity"], "summary": r.get("summary"),
                         "details": r.get("details"),
                         "cve_id": r.get("cve_id") or p.get("cve_id"), "package": r["package"],
                         "ecosystem": r["ecosystem"], "published": r["published"],
                         "cvss_vector": r.get("cvss_vector") or r.get("cvss_v4_vector"),
                         "cvss_score": r["cvss"], "severity": r["severity"],
                         "cwe": ";".join(_cwes(r)) or None, "record_language": r.get("language"),
                         **versions, "advisory_len": len(advisory.strip())})
    return pd.DataFrame(rows)


def _versions_for(r: dict, fix_pkg: str | None) -> dict:
    """Dependency-version fields of the package this fix belongs to. An advisory
    can list several packages (e.g. an abandoned original and the maintained fork
    whose commit fixed it); the fix check records which package's releases
    verified the commit, and that package's versions describe the row. Without it
    the record's headline package is used."""
    head = {"fix_package": None, "affected_versions": r.get("affected_versions"),
            "fixed_version": r.get("fixed_version"), "safe_version": r.get("safe_version"),
            "dependency_status": r.get("dependency_status")}
    try:
        entries = json.loads(r.get("affected") or "[]")
    except ValueError:
        entries = []
    if not entries:
        return head
    same = [e for e in entries if (e.get("ecosystem") or "").lower() == (r.get("ecosystem") or "").lower()]
    main = (same or entries)[0]
    pick = next((e for e in entries if fix_pkg and e.get("package") == fix_pkg), None)
    if pick is None or pick is main:
        return {**head, "fix_package": main.get("package")}
    return {"fix_package": pick.get("package"), **depversions.summarise([pick], pick.get("ecosystem"))}


# The columns each per-row rule reads (context.assess_row and codestore.rank_pair).
ASSESS_COLUMNS = ("summary", "details", "msg_head", "commit_message", "cwe", "severity", "file_path", "function_name")
RANK_COLUMNS = ("language", "granularity", "changed_lines", "record_language", "attribution_score")


def _records(df: pd.DataFrame, columns) -> list[dict]:
    """Rows as dicts with only ``columns`` (the ones present), so a per-row rule does not copy every column of a large frame."""
    return df[[c for c in columns if c in df.columns]].to_dict("records")


def _date(df: pd.DataFrame) -> pd.Series:
    pub = pd.to_datetime(df["published"], errors="coerce", utc=True, format="mixed")
    com = pd.to_datetime(df["commit_date"], errors="coerce", utc=True, format="mixed")
    return pd.concat([pub, com], axis=1).min(axis=1)


def _hash(code) -> str:
    return hashlib.sha1(util.norm_code(str(code or "")).encode()).hexdigest()


# ── selection ────────────────────────────────────────────────────────────────

_LINK_RANK = {"declared": 2, "mentioned": 1}


def _with_attribution(df: pd.DataFrame) -> pd.DataFrame:
    """Add each pair's ``attribution_basis``, ``attribution_score`` and ``change_key``. They come from a second small read of the kept pairs (the review index and the quality table), not from the
    covering indexes of the light columns. A change key computed from other code than the pair has now (a stale row) is left out."""
    got = codestore.pair_attribution_for([int(i) for i in df.pair_id]) if len(df) else {}
    info = pd.DataFrame.from_dict(got, orient="index").reindex(df.pair_id.astype(int)) if got else pd.DataFrame(index=df.pair_id.astype(int))
    for col in ("attribution_basis", "attribution_score", "change_key", "quality_hash"):
        if col not in info:
            info[col] = None
    info.index = df.index
    stale = info.quality_hash.notna() & df.content_hash.notna() & (info.quality_hash != df.content_hash)
    return df.assign(attribution_basis=info.attribution_basis, attribution_score=pd.to_numeric(info.attribution_score, errors="coerce").fillna(0.0),
                     change_key=info.change_key.where(~stale & info.quality_hash.notna(), None))


def _best_first(df: pd.DataFrame) -> pd.DataFrame:
    """The pairs in the order in which a copy should win: attribution, an LLM draft already written (so a draft is not thrown away), a declared fix link, the earliest commit, the key."""
    order = df.assign(_when=_date(df), _link=df.fix_link.map(_LINK_RANK).fillna(0), _enr=df.enriched.fillna(0).astype(int))
    return order.sort_values(["attribution_score", "_enr", "_link", "_when", "pair_key"], ascending=[False, False, False, True, True], kind="mergesort", na_position="last").drop(columns=["_when", "_link", "_enr"])


def _rank_of(r: dict) -> tuple:
    """``codestore.rank_pair`` with the attribution after the language: the record's language, then how sure we are that the unit is the vulnerable one, then function level, then changed lines."""
    lang, granularity, lines = codestore.rank_pair(r, r["record_language"].split(",") if isinstance(r.get("record_language"), str) else [])
    return lang, float(r.get("attribution_score") or 0.0), granularity, lines


def select(f: dict) -> tuple[pd.DataFrame, list[dict], int]:
    """Apply the filters; returns (rows, funnel, mined records)."""
    f = {**DEFAULTS, **{k: v for k, v in (f or {}).items() if v is not None}}
    recs = _mined_records(include_suspect=not f["exclude_suspect"])
    df = _rows(recs, bool(f["sibling_languages"]))
    funnel = [{"step": "usable pairs of mined records", "rows": len(df)}]
    if not len(df):
        return df, funnel, len(recs)

    def step(name, mask):
        nonlocal df
        df = df[mask]
        funnel.append({"step": name, "rows": len(df)})

    # a fix shared by several advisories of one CVE (GHSA + PYSEC) is one sample
    step("each pair once (advisories of a CVE share their fix)", ~df.duplicated("pair_id"))
    if not f["include_quarantined"]:
        step("quarantined commits excluded (ambiguous merge parent, revert, release bump)",
             df.quarantine_reason.isna())
    if not df.has_code.all():
        step("pairs with an empty vulnerable or fixed side dropped", df.has_code == 1)
    if f["exclude_suspect"]:
        step("fix commits failing the release-date check excluded", df.fix_check != "suspect")
    if f["only_verified"]:
        step("only fix commits verified by release dates (and hand-made pairs)",
             df.fix_check.isin(["verified", "manual"]))

    def pub():
        return pd.to_datetime(df["published"], errors="coerce", utc=True, format="mixed")
    if f["date_from"]:
        step(f"published from {f['date_from']}", pub() >= pd.Timestamp(f["date_from"], tz="UTC"))
    if f["date_to"]:
        step(f"published to {f['date_to']}",
             pub() < pd.Timestamp(f["date_to"], tz="UTC") + pd.Timedelta(days=1))
    if f["languages"]:
        step("languages: " + ", ".join(f["languages"]), df.language.isin(f["languages"]))
    if f["require_cvss"]:
        step("has a CVSS score", df.cvss_score.notna())
    if f["severities"]:
        step("severity: " + ", ".join(f["severities"]), df.severity.isin(f["severities"]))
    if f["require_cwe"]:
        step("has a CWE", df.cwe.notna())
    if f["cwes"]:
        want = set(f["cwes"])
        step("CWE: " + ", ".join(f["cwes"][:6]) + ("…" if len(f["cwes"]) > 6 else ""),
             df.cwe.fillna("").map(lambda c: bool(want & set(c.split(";")))))
    if f["ecosystems"]:
        step("ecosystem: " + ", ".join(f["ecosystems"]), df.ecosystem.isin(f["ecosystems"]))
    df = _with_attribution(df)
    if f["attribution"]:
        allowed = list(attribution.TIERS["decision"]) + (list(attribution.TIERS["supporting"]) if f["attribution"] == "supporting" else [])
        step("attribution: " + ("the advisory or the fix names the unit" if f["attribution"] == "decision" else "the advisory or the fix names the unit or its file"), df.attribution_basis.isin(allowed))
    if not f["include_tests"]:
        step("test files excluded", df.is_test.fillna(0) == 0)
    if not f["include_tangled"]:
        step("tangled commits excluded", df.tangled.fillna(0) == 0)
    if f["granularity"]:
        step(f"{f['granularity']}-level only", df.granularity == f["granularity"])
    elif f["prefer_function"]:
        sub = df.granularity.isin(["function", "hunk"])
        fn_files = set(zip(df.loc[sub, "record_id"], df.loc[sub, "file_path"]))
        step("function (or hunk) level where available",
             ~((df.granularity == "file")
               & pd.Series(list(zip(df.record_id, df.file_path)), index=df.index).isin(fn_files)))
    if f["min_lines"] and int(f["min_lines"]) > 1:
        step(f"at least {f['min_lines']} lines", df.lines >= int(f["min_lines"]))
    if f["max_lines"]:
        step(f"at most {f['max_lines']} lines of vulnerable code", df.lines <= int(f["max_lines"]))
    if f["max_changed"]:
        step(f"at most {f['max_changed']} changed lines", df.changed_lines <= int(f["max_changed"]))
    if f["require_advisory"]:
        step("has advisory text", df.advisory_len > 0)
    if f["require_commit_msg"]:
        step("has a fix commit message", df.has_msg == 1)
    if f["only_enriched"]:
        step("LLM-enriched only", df.enriched == 1)
    if len(df):
        # Only the columns the rating reads become dicts: the whole 60-column frame as 200,000 dicts was most of the preview's peak memory.
        df = df.assign(context=[ctx.assess_row(r)["level"] for r in _records(df, ASSESS_COLUMNS)])
    else:
        df = df.assign(context=pd.Series(dtype=str))
    if f["weak_context"] == "drop":
        step("weak-context rows dropped (no full advisory, no informative commit message)",
             df.context != "weak")
    elif f["weak_context"] == "enriched_only":
        step("weak-context rows kept only when LLM-enriched",
             (df.context != "weak") | (df.enriched == 1))

    # Copies of one fix are collapsed here, after every filter and before the ranking and the per-CVE cap: a copy that a filter removed cannot hide the one that stays, and copies cannot use up the
    # cap and then be deleted by the split. The copy that stays is the best attributed one.
    df = _best_first(df)
    same_code = df.content_hash.fillna(df.pair_key)
    df = df.assign(dup_size=same_code.groupby(same_code).transform("size").astype(int))        # how many copies the row stands for
    step("copies of the same code collapse into the best attributed one", ~same_code.duplicated())
    if f["collapse_backports"]:
        collapsible = df.change_key.fillna("") != ""
        same_change = pd.Series(list(zip(df.cve_id.fillna(df.osv_id), df.file_path, df.granularity, df.function_name.where(df.granularity == "function", None).fillna(""), df.change_key)), index=df.index)
        df = df.assign(dup_size=df.dup_size.where(~collapsible, df.dup_size.groupby(same_change).transform("sum")))
        step("the same change in several release branches collapses into one", ~(collapsible & same_change.duplicated()))

    # rows per record / CVE: best first (record language, attribution, function level, changed lines); a tie is broken by the pair's key, so the same data gives the same rows in any order
    df = df.sort_values("pair_key", kind="mergesort")
    df = df.assign(_rank=[_rank_of(r) for r in _records(df, RANK_COLUMNS)])      # no language: NaN, not None
    df = df.sort_values("_rank", ascending=False, kind="mergesort")
    if f["unit"] == "best":
        step("best pair per record", ~df.duplicated("record_id"))
    elif f["per_cve_cap"]:
        grp = df.cve_id.fillna(df.osv_id)
        step(f"at most {f['per_cve_cap']} pairs per CVE",
             df.groupby(grp).cumcount() < int(f["per_cve_cap"]))
    return df.drop(columns="_rank"), funnel, len(recs)


def split(df: pd.DataFrame, val_share: float = splitter.VAL_SHARE,
          train_all: bool = False) -> tuple[pd.DataFrame, dict]:
    """Train / validation split (whole packages, pipeline/split.py), de-duplication
    and the leak guard. Every row is kept whatever its date."""
    df = df.copy()
    df["group"] = df.cve_id.fillna(df.osv_id).fillna(df.pair_key)
    df["date"] = _date(df)
    df["date"] = df.groupby("group").date.transform("min")
    df = df.sort_values(["date", "pair_id"], na_position="last")
    before = len(df)
    df = df.drop_duplicates("content_hash", keep="first")
    n_dupes = before - len(df)
    df["split"] = splitter.assign(df, val_share=val_share, train_all=train_all)
    # leak guard: a validation row whose vulnerable code is also in train is dropped
    # (train keeps all of its data)
    n_leak = 0
    if (df.split == "val").any():
        code = _code(df.pair_id.tolist(), ["code_before"])
        df["before_hash"] = df.pair_id.map(lambda i: _hash(code.get(i, {}).get("code_before")))
        leak = (df.split == "val") & df.before_hash.isin(set(df.loc[df.split == "train", "before_hash"]))
        n_leak = int(leak.sum())
        df = df[~leak]
    else:
        df["before_hash"] = None
    return df, {"duplicates_removed": int(n_dupes), "leak_guard_removed": n_leak,
                "no_date": int(df.date.isna().sum())}


def _code(ids: list[int], cols: list[str]) -> dict[int, dict]:
    return codestore.pair_code_of(ids, tuple(cols))


def _counts(s) -> dict:
    return {str(k): int(v) for k, v in s.value_counts().items()}


MIN_TRAIN = 500


def warnings(df: pd.DataFrame, f: dict, funnel: list[dict] | None = None) -> list[str]:
    """Settings that leave too little data to fine-tune on, or that put wrong labels
    in it."""
    out = []
    if f.get("include_tests"):
        out.append("test-file pairs are included: test code is not vulnerable code, so these rows "
                   "teach the wrong label (they are kept out of the detection set)")
    if f.get("include_tangled"):
        out.append("tangled commits are included: the fix is mixed with unrelated changes, so "
                   "unrelated functions are labelled vulnerable (kept out of the detection set)")
    if f.get("unit") == "best" and funnel:
        before = next((s["rows"] for s in reversed(funnel) if s["step"] != "best pair per record"), None)
        after = next((s["rows"] for s in funnel if s["step"] == "best pair per record"), None)
        if before and after is not None and after < before:
            out.append(f"one row per record keeps {after:,} of {before:,} usable pairs; "
                       "'every usable pair' keeps more training data")
    if f.get("train_all"):
        out.append("train on everything: there are no validation rows, and the training notebooks "
                   "need them for early stopping and the threshold. Use this only for the final run, "
                   "with the run_config.json of an earlier run built with a validation share "
                   "(FINAL_FROM in the notebook)")
    if len(df) and "split" in df:
        n = df.split.value_counts()
        train, val = int(n.get("train", 0)), int(n.get("val", 0))
        if train < MIN_TRAIN:
            out.append(f"only {train:,} train rows - too few to fine-tune well; loosen the filters "
                       "or mine more advisories")
        if not f.get("train_all") and val == 0 and train:
            out.append(f"no validation rows: too few packages for a {float(f.get('val_share') or 0):.0%} share; "
                       "the training notebooks need val - raise the validation share")
        if not f.get("train_all") and 0 < val < 50:
            out.append(f"only {val:,} validation rows - early stopping and the threshold will be noisy; "
                       "raise the validation share")
    return out


def _stats(df: pd.DataFrame, funnel, n_recs, extra, f) -> dict:
    cwe_top = (df.cwe.dropna().str.split(";").explode().str.strip()
               .loc[lambda s: s.str.startswith("CWE-")].value_counts().head(15)) if len(df) else pd.Series(dtype=int)
    return {
        "records_mined": n_recs, "funnel": funnel, **extra, "warnings": warnings(df, f, funnel),
        "rows": int(len(df)), "records": int(df.record_id.nunique()) if len(df) else 0,
        "cves": int(df.cve_id.fillna(df.osv_id).nunique()) if len(df) else 0,
        "commits": int(df.commit_key.nunique()) if len(df) else 0,
        "split_counts": _counts(df.split) if len(df) else {},
        "split_cves": {s: int(g.cve_id.fillna(g.osv_id).nunique()) for s, g in df.groupby("split")}
        if len(df) else {},
        "by_language": _counts(df.language) if len(df) else {},
        "by_severity": _counts(df.severity.fillna("unscored")) if len(df) else {},
        "by_severity_split": {s: _counts(g.severity.fillna("unscored")) for s, g in df.groupby("split")} if len(df) else {},
        "by_granularity": _counts(df.granularity) if len(df) else {},
        "by_fix_check": _counts(df.fix_check.fillna("unchecked")) if len(df) else {},
        "by_context": _counts(df.context) if len(df) and "context" in df else {},
        "by_severity_source": _counts(df.severity_source.fillna("none")) if len(df) else {},
        "by_source": _counts(df.source.fillna("?")) if len(df) else {},
        "top_cwe": {k: int(v) for k, v in cwe_top.items()},
        "llm_enriched": int(df.enriched.sum()) if len(df) else 0,
        "split": splitter.describe(f.get("val_share", splitter.VAL_SHARE), bool(f.get("train_all"))),
        "window": {"from": f.get("date_from"), "to": f.get("date_to"),
                   "collection_since": store.get_meta("collection_since")},
        "filters": f,
    }


# ── dependency dataset (records whose dependency versions were checked) ─────

def dependency_rows(f: dict) -> list[dict]:
    """One row per (record, affected package): what a dependency checker needs to
    learn - the affected ranges, the first fixed and the latest safe release and
    the status - with the advisory's CVE / CWE / severity and the same split as
    the code rows (the record's packages decide it). Only records whose versions
    were checked (Mine checks the mined ones; ``deps_checked_at``) are used: an
    unchecked record has raw OSV ranges without fixed / safe versions or a status.
    Withdrawn advisories are left out; advisories without a date are kept."""
    recs = store.dep_checked_records(("id", "osv_id", "cve_id", "ecosystem", "package", "summary", "published", "cvss_vector", "cvss_v4_vector",
                                      "cvss_score", "osv_cwe", "cwe", "affected", "osv_severity"))
    nvd = _nvd({r["cve_id"] for r in recs if r.get("cve_id")})
    if recs:
        rdf = pd.DataFrame(recs)
        rsplit = dict(zip(rdf.id, splitter.assign(rdf, val_share=f.get("val_share", splitter.VAL_SHARE),
                                                  train_all=bool(f.get("train_all")))))
        pubs = dict(zip(rdf.id, pd.to_datetime(rdf.published, errors="coerce", utc=True, format="mixed")))
    rows = []
    for r in recs:
        pub = pubs[r["id"]]
        if f.get("date_from") and (pd.isna(pub) or pub < pd.Timestamp(f["date_from"], tz="UTC")):
            continue
        if f.get("date_to") and (pd.isna(pub) or pub >= pd.Timestamp(f["date_to"], tz="UTC") + pd.Timedelta(days=1)):
            continue
        if f.get("ecosystems") and r.get("ecosystem") not in f["ecosystems"]:
            continue
        score, sev, sev_src = _severity_category(r, nvd)
        if f.get("severities") and sev not in f["severities"]:
            continue
        cwe = ";".join(_cwes(r)) or None
        if f.get("cwes") and not (set(f["cwes"]) & set((cwe or "").split(";"))):
            continue
        try:
            entries = json.loads(r["affected"] or "[]")
        except ValueError:
            continue
        for e in entries:
            rows.append({
                "id": f"{r['osv_id']}:{e.get('package')}", "record_id": r["id"],
                "osv_id": r["osv_id"], "cve": r.get("cve_id"), "cwe": cwe, "severity": sev,
                "severity_source": sev_src, "osv_severity": (r.get("osv_severity") or "").upper() or None,
                "cvss_score": score, "ecosystem": e.get("ecosystem"), "package": e.get("package"),
                "affected_ranges": e.get("ranges"), "affected_versions_listed": e.get("versions_total", 0),
                "first_fixed": e.get("first_fixed"), "latest_fixed": e.get("latest_fixed"),
                "latest_version": e.get("latest_version"), "safe_version": e.get("safe_version"),
                "safe_is_prerelease": e.get("safe_is_prerelease"), "status": e.get("status"),
                "summary": r.get("summary"), "date": None if pd.isna(pub) else str(pub)[:10],
                "split": rsplit[r["id"]]})
    return rows


def _dep_stats(rows: list[dict]) -> dict:
    unchecked = store.count_dep_unchecked()
    s = pd.DataFrame(rows)
    if not len(s):
        return {"rows": 0, "records_unchecked": unchecked}
    return {"rows": len(s), "records": int(s.record_id.nunique()), "records_unchecked": unchecked,
            "split_counts": _counts(s.split), "by_status": _counts(s.status.fillna("unknown")),
            "by_ecosystem": _counts(s.ecosystem.fillna("?"))}


def preview(filters: dict | None = None) -> dict:
    f = {**DEFAULTS, **(filters or {})}
    df, funnel, n_recs = select(f)
    deps = _dep_stats(dependency_rows(f))
    if not len(df):
        return {**_stats(df, funnel, n_recs, {}, f), "dependencies": deps}
    df, extra = split(df, f["val_share"], bool(f["train_all"]))
    return {**_stats(df, funnel, n_recs, extra, f), "dependencies": deps}


# ── writing ──────────────────────────────────────────────────────────────────

SCHEMA_VERSION = "code-pairs/8"
# The one schema of every output row: (field, type, required). Types: str, int,
# float, list (of str). Documented in data/README.md ("Code dataset schema").
SCHEMA = [
    ("id", "str", True), ("record_id", "int", True), ("osv_id", "str", False),
    ("cve", "str", False), ("cwe", "str", False),
    ("severity", "str", False), ("severity_source", "str", False),
    ("osv_severity", "str", False), ("cvss_score", "float", False), ("cvss_vector", "str", False),
    ("language", "str", True), ("ecosystem", "str", False), ("package", "str", False),
    ("fix_package", "str", False),
    ("affected_versions", "str", False), ("fixed_version", "str", False),
    ("safe_version", "str", False), ("dependency_status", "str", False),
    ("repository", "str", False), ("fix_commit", "str", False), ("parent_commit", "str", False),
    ("commit_date", "str", False), ("fix_link", "str", False), ("fix_check", "str", True),
    ("file", "str", True), ("function", "str", False), ("span", "str", False),
    ("granularity", "str", True),
    ("vulnerable_code", "str", True), ("fixed_code", "str", True), ("diff", "str", True),
    ("changed_lines", "int", True), ("vulnerable_lines", "int", True),
    ("explanation", "str", False), ("explanation_source", "str", True),
    ("needs_explanation_enrichment", "int", True), ("commit_message", "str", False),
    ("context", "str", True),
    ("attribution_basis", "str", False), ("attribution_score", "float", False), ("dup_size", "int", True),
    ("root_cause", "str", False), ("secure_pattern", "str", False),
    ("synthetic_fields", "list", True),
    ("llm_model", "str", False), ("source", "str", False),
    ("published", "str", False), ("date", "str", True), ("split", "str", True),
]
FIELDS = [c for c, _, _ in SCHEMA]


def _day(v) -> str | None:
    t = pd.to_datetime(v, errors="coerce", utc=True, format="mixed")
    return None if t is None or pd.isna(t) else t.strftime("%Y-%m-%d")


def _explanation(summary, details, stored, llm) -> ctx.Explanation:
    """The row's one explanation under the agreed policy (``context.resolve_explanation``):
    the real advisory at 200 characters or more, else the accepted LLM draft (``llm`` is
    None when synthetic fields are off) marked synthetic, else the real short text or
    nothing, marked as needing enrichment."""
    return ctx.resolve_explanation(summary, details, stored, llm)


def _sample(r: dict, synthetic_ok: bool) -> dict:
    def val(k):
        v = r.get(k)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        if isinstance(v, str) and not v.strip():
            return None                                   # blank text is missing
        return v
    expl = _explanation(val("summary"), val("details"), val("explanation"),
                        val("explanation_llm") if synthetic_ok else None)
    synth = [k.replace("_llm", "") for k in ("root_cause_llm", "secure_pattern_llm")
             if synthetic_ok and val(k)] + (["explanation"] if expl.synthetic else [])
    score = val("cvss_score")
    before = val("code_before") or ""
    return {
        "id": r["pair_key"], "record_id": int(r["record_id"]), "osv_id": val("osv_id"),
        "cve": val("cve_id"), "cwe": val("cwe"),
        "severity": val("severity"), "severity_source": val("severity_source"),
        "osv_severity": val("osv_severity"),
        "cvss_score": round(float(score), 1) if score is not None else None,
        "cvss_vector": val("cvss_vector"),
        "language": val("language"), "ecosystem": val("ecosystem"), "package": val("package"),
        "fix_package": val("fix_package"),
        "affected_versions": val("affected_versions"), "fixed_version": val("fixed_version"),
        "safe_version": val("safe_version"), "dependency_status": val("dependency_status"),
        "repository": val("repo_url"), "fix_commit": val("fix_commit"),
        "parent_commit": val("parent_commit"), "commit_date": _day(val("commit_date")),
        "fix_link": val("fix_link"), "fix_check": val("fix_check"),
        "file": val("file_path"), "function": val("function_name"), "span": val("span"),
        "granularity": val("granularity"),
        "vulnerable_code": val("code_before"), "fixed_code": val("code_after"),
        "diff": val("diff"), "changed_lines": int(val("changed_lines") or 0)
        or codestore.changed_lines(val("diff")),           # pairs stored before the column
        "vulnerable_lines": len(before.splitlines()),
        "explanation": expl.text, "explanation_source": expl.source,
        "needs_explanation_enrichment": int(expl.needs_enrichment),
        "commit_message": val("commit_message"), "context": val("context"),
        "attribution_basis": val("attribution_basis"), "attribution_score": float(val("attribution_score")) if val("attribution_score") is not None else None,
        "dup_size": int(val("dup_size") or 1),
        "root_cause": val("root_cause_llm") if synthetic_ok else None,
        "secure_pattern": val("secure_pattern_llm") if synthetic_ok else None,
        "synthetic_fields": synth, "llm_model": val("llm_model") if synth else None,
        "source": val("source"), "published": _day(val("published")),
        "date": _day(r["date"]), "split": r["split"],
    }


_TYPE = {"str": str, "int": int, "float": float, "list": list}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate(samples: list[dict]) -> dict:
    """Check every row against SCHEMA before anything is written. Raises
    ValueError naming the offending ids; returns per-field null counts."""
    problems: dict[str, list[str]] = {}

    def bad(rule, sid):
        problems.setdefault(rule, []).append(str(sid))
    seen = set()
    for x in samples:
        sid = x.get("id")
        if set(x) != set(FIELDS):
            bad("fields differ from SCHEMA", sid)
        for name, typ, required in SCHEMA:
            v = x.get(name)
            if v is None:
                if required:
                    bad(f"{name} missing", sid)
            elif not isinstance(v, _TYPE[typ]) or (typ == "int" and isinstance(v, bool)):
                bad(f"{name} is {type(v).__name__}, expected {typ}", sid)
        if sid in seen:
            bad("duplicate id", sid)
        seen.add(sid)
        if x.get("vulnerable_code") is not None and \
                _hash(x["vulnerable_code"]) == _hash(x.get("fixed_code")):
            bad("vulnerable_code equals fixed_code", sid)
        if not isinstance(x.get("changed_lines"), int) or x["changed_lines"] < 1:
            bad("no changed lines", sid)
        for d in ("date", "published", "commit_date"):
            if x.get(d) is not None and not _DATE.match(str(x[d])):
                bad(f"{d} is not YYYY-MM-DD", sid)
        if x.get("split") not in ("train", "val"):
            bad("split not train/val", sid)
        if x.get("granularity") not in GRANULARITIES:
            bad("unknown granularity", sid)
        if x.get("context") not in ctx.LEVELS:
            bad("context not weak/partial/strong", sid)
        if x.get("severity") is not None and x["severity"] not in SEVERITIES:
            bad("unknown severity", sid)
        if isinstance(x.get("cvss_score"), float) and not 0 <= x["cvss_score"] <= 10:
            bad("cvss_score outside 0-10", sid)
    if problems:
        msg = "; ".join(f"{rule}: {len(ids)} rows (e.g. {', '.join(ids[:3])})"
                        for rule, ids in problems.items())
        raise ValueError(f"dataset rows fail the schema check - nothing written. {msg}")
    return {name: sum(x.get(name) is None for x in samples) for name in FIELDS}


def _arrow_schema():
    import pyarrow as pa
    # int32: int64 columns come out as strings in JavaScript-based parquet viewers
    t = {"str": pa.string(), "int": pa.int32(), "float": pa.float64(), "list": pa.list_(pa.string())}
    return pa.schema([(name, t[typ]) for name, typ, _ in SCHEMA])


def _write_lines(part, rows, **dumps) -> None:
    """One JSON object per line into the part file of an atomic write."""
    with part.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, **dumps) + "\n")


BUILD_LOCK = "code_dataset_build.lock"
BUILD_WAIT = 2.0                      # seconds to wait for another build to let go of the shared files


def main(freeze: bool = False, filters: dict | None = None, **legacy) -> dict:
    """Build the code-pair dataset (a preview, or a frozen version). One build writes the shared files at a time: a second one, by hand or in a campaign, is refused with ``oslock.Busy``
    (which names the other process) instead of mixing its files with the first one's."""
    from app.backend.pipeline import oslock
    PROC.mkdir(parents=True, exist_ok=True)
    lock = oslock.FileLock(PROC / BUILD_LOCK).acquire(wait=BUILD_WAIT)
    try:
        return _build(freeze, filters)
    finally:
        lock.release()


def _build(freeze: bool, filters: dict | None) -> dict:
    f = {**DEFAULTS, **(filters or {})}
    # a freeze takes the next unused version number
    tag = versions.output_tag(freeze, "code_pairs", "dependencies", root=PROC)
    out_parquet = PROC / f"code_pairs_{tag}.parquet"
    out_jsonl = PROC / f"code_pairs_{tag}.jsonl"
    out_det = PROC / f"code_pairs_{tag}_detection.jsonl"
    out_dep = PROC / f"dependencies_{tag}.jsonl"
    deps = dependency_rows(f)
    fsio.write_with(out_dep, lambda part: _write_lines(part, deps, ensure_ascii=False, default=str))
    dstats = _dep_stats(deps)
    print(f"dependency rows: {dstats['rows']:,} (one per vulnerable package of the records whose "
          f"versions were checked; {dstats['records_unchecked']:,} unchecked records left out) -> {out_dep}")
    if dstats["rows"]:
        print("  status:", dstats["by_status"], "| split:", dstats["split_counts"])
    df, funnel, n_recs = select(f)
    for s in funnel:
        print(f"  {s['rows']:>7,}  {s['step']}")
    if not len(df):
        print("No rows: mine code for the Dataset records first, or loosen the filters.")
        return {}
    df, extra = split(df, f["val_share"], bool(f["train_all"]))
    print(f"  {len(df):>7,}  after de-duplication ({extra['duplicates_removed']}) and the "
          f"leak guard ({extra['leak_guard_removed']})")
    heavy = ["code_before", "code_after", "diff", "explanation", "commit_message",
             "root_cause_llm", "secure_pattern_llm", "explanation_llm", "llm_model"]
    code = _code(df.pair_id.tolist(), heavy)
    samples = [_sample({**r, **code.get(r["pair_id"], {})}, f["include_synthetic"])
               for r in df.to_dict("records")]
    nulls = validate(samples)                 # raises before anything is written
    import pyarrow as pa
    import pyarrow.parquet as papq
    table = pa.Table.from_pylist(samples, schema=_arrow_schema())
    fsio.write_with(out_parquet, lambda part: papq.write_table(table, part))
    fsio.write_with(out_jsonl, lambda part: _write_lines(part, samples, ensure_ascii=False))
    where = {r["pair_key"]: (r["commit_key"], r["file_path"]) for r in
             df[["pair_key", "commit_key", "file_path"]].to_dict("records")
             if isinstance(r["commit_key"], str) and isinstance(r["file_path"], str)}  # manual pairs: no file
    # test-file and tangled pairs carry wrong labels for a detector: never in that set
    wrong = set(df.loc[(df.is_test.fillna(0) != 0) | (df.tangled.fillna(0) != 0), "pair_key"])
    det_rows, det_stats = detection.build(samples, where, {"safe_ratio": f["safe_ratio"], "max_chars": f["max_chars"],
                                                           "untouched_policy": "weak_negative" if f.get("include_untouched") else "exclude"}, exclude=wrong)
    detection.write(det_rows, out_det)
    n_det = len(det_rows)
    print(detection.describe(det_stats))
    stats = {"mode": "freeze" if freeze else "preview", "built_at": datetime.datetime.utcnow().isoformat() + "Z",
             "outputs": {"parquet": str(out_parquet), "jsonl": str(out_jsonl),
                         "detection_jsonl": str(out_det), "detection_rows": n_det,
                         "dependencies_jsonl": str(out_dep)},
             "detection": det_stats,
             "dependencies": dstats, "schema_version": SCHEMA_VERSION,
             "schema": [{"field": n, "type": t, "required": req} for n, t, req in SCHEMA],
             "null_counts": nulls,
             # where each row's one explanation came from (the agreed policy's four sources)
             "by_explanation": {s: sum(1 for x in samples if x["explanation_source"] == s)
                                for s in ctx.EXPLANATION_SOURCES},
             "needs_explanation_enrichment": sum(x["needs_explanation_enrichment"] for x in samples),
             **_stats(df, funnel, n_recs, extra, f)}
    fsio.write_json(PROC / "code_pairs_stats.json", stats, indent=2, default=str)
    print(f"profile: {next((p['name'] for p in PROFILES if p['id'] == f.get('profile')), 'Custom')}")
    print(f"split: {splitter.RULE}" + (" - train on everything" if f["train_all"] else
                                        f" (val_share {f['val_share']})"))
    for w in stats["warnings"]:
        print(f"WARNING: {w}")
    print("rows per split:", stats["split_counts"], "| CVEs per split:", stats["split_cves"])
    print("severity:", stats["by_severity"], "| source:", stats["by_severity_source"])
    print("languages:", stats["by_language"], "| granularity:", stats["by_granularity"])
    print("context:", stats["by_context"], "| LLM-enriched:", stats["llm_enriched"],
          "| explanation:", " · ".join(f"{k} {v:,}" for k, v in stats["by_explanation"].items()))
    print("fix check:", stats["by_fix_check"], f"| schema {SCHEMA_VERSION}: {len(FIELDS)} fields, "
          "parquet and JSONL identical, every row validated")
    print(f"written: {out_parquet}\nwritten: {out_jsonl}\nwritten: {out_det} ({n_det} rows)")
    if freeze:
        versions.freeze([out_parquet, out_jsonl, out_det], PROC)
        versions.freeze([out_dep], PROC)
        print(f"frozen as code_pairs_{tag} and dependencies_{tag} (hashes in VERSIONS.json)")
    else:
        print("\nThis is a PREVIEW. Freeze (the next unused code_pairs version) once mining is complete.")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true",
                    help="write the next unused code_pairs version and freeze it")
    ap.add_argument("--filters", default="{}", help="JSON object of filters (see DEFAULTS)")
    a = ap.parse_args()
    main(a.freeze, json.loads(a.filters))
