"""Looking up advisories in the OSV dumps that were downloaded, and saying for each whether it is already in the store.

A dump is parsed into ``data/interim/osv_<ecosystem>.parquet`` when it is installed or ingested; those tables are what is searched, so a search needs no network and no download. A match is an
advisory whose CVE id, advisory id, alias or package name holds what was typed (case does not matter). The parsed table has one row per package of an advisory, so rows of one
advisory in one ecosystem are folded into a single result that lists its packages. The state of each is read from the store with one lookup for the page, never one per row:
``in_store`` when the store holds a record with that key (the first ecosystem to hold an advisory id keeps the plain id, another gets ``<id>@<ecosystem>``, as the ingest files it),
``downloaded`` when it is only in the dump. Read-only.

The folded, newest-first table of each file is kept in memory (up to ``CACHE_BYTES``) and dropped when the file changes (its size or modification time), so listing the newest advisories of
every downloaded ecosystem reads each parquet once, not once per request. A dump that was downloaded but not read yet has no table; ``search`` names it under ``unread`` and ``detail`` can
still open one advisory straight from the dump.
"""
from __future__ import annotations

import json
import pathlib
import re
import threading
import zipfile
from collections import OrderedDict

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from app.backend.pipeline import store
from app.backend.pipeline.ingest import fixrefs, osv, osv_catalog

FIELDS = ("osv_id", "cve_id", "ecosystem", "package", "summary", "published", "osv_severity", "aliases")
MAX_LIMIT = 100
MAX_FILTERED = 20000
MAX_PACKAGES = 5                       # packages listed for one advisory; the count of the rest is given
STATES = ("", "in_store", "downloaded")
SUMMARY_CHARS = 300                    # a summary is cut here in the kept table (the whole text is in the detail)
CACHE_BYTES = 256 * 1024 * 1024        # the most the kept tables may hold together; a table larger than this is read again each time
MAX_RAW_BYTES = 400_000                # the raw JSON of one advisory is sent with the detail only up to this size
ID_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$")

_cache: "OrderedDict[tuple, tuple[pa.Table, str]]" = OrderedDict()
_cache_bytes = 0
_lock = threading.Lock()


def parsed_files(interim: pathlib.Path | None = None) -> list[pathlib.Path]:
    return sorted((pathlib.Path(interim) if interim else osv.INTERIM).glob("osv_*.parquet"))


def forget() -> None:
    """Drop every kept table (tests start each case from nothing)."""
    global _cache_bytes
    with _lock:
        _cache.clear()
        _cache_bytes = 0


def _aliases(raw) -> list[str]:
    if isinstance(raw, list):
        return [a for a in raw if isinstance(a, str)]
    try:
        data = json.loads(raw) if isinstance(raw, str) and raw else []
    except ValueError:
        return []
    return [a for a in data if isinstance(a, str)] if isinstance(data, list) else []


def _matches(table: pa.Table, q: str) -> pa.Table:
    """The rows of a folded table whose CVE id, advisory id, aliases or any package hold ``q``."""
    if not q:
        return table
    mask = None
    for name in ("cve_id", "osv_id", "packages", "aliases"):
        col = table[name]
        if pa.types.is_list(col.type) or pa.types.is_large_list(col.type):
            col = pc.binary_join(col, " ")
        part = pc.fill_null(pc.match_substring(pc.cast(col, pa.string()), q, ignore_case=True), False)
        mask = part if mask is None else pc.or_(mask, part)
    return table.filter(mask)


def _read(path: pathlib.Path) -> pa.Table:
    """The columns of one parsed table that the search shows, as strings; a column the table lacks (older tables have no aliases) is empty. A missing package is an empty one."""
    names = pq.ParquetFile(path).schema_arrow.names
    table = pq.read_table(path, columns=[c for c in FIELDS if c in names])
    cols = {c: (pc.cast(table[c], pa.string()) if c in table.column_names and not pa.types.is_list(table[c].type) else
                (pc.binary_join(table[c], "\n") if c in table.column_names else pa.nulls(table.num_rows, pa.string()))) for c in FIELDS}
    cols["package"] = pc.fill_null(cols["package"], "")
    cols["summary"] = pc.utf8_slice_codeunits(cols["summary"], 0, SUMMARY_CHARS)
    return pa.table(cols)


def _collapse(table: pa.Table) -> pa.Table:
    """One row per advisory of one ecosystem, with its packages in a list."""
    if not table.num_rows:
        return table.append_column("packages", pa.array([], pa.list_(pa.string()))).drop_columns(["package"])
    grouped = table.group_by(["osv_id", "ecosystem"], use_threads=False).aggregate(
        [("cve_id", "first"), ("package", "list"), ("summary", "first"), ("published", "first"), ("osv_severity", "first"), ("aliases", "first")])
    return grouped.rename_columns(["osv_id", "ecosystem", "cve_id", "packages", "summary", "published", "osv_severity", "aliases"])


def _slim(path: pathlib.Path) -> tuple[pa.Table, str]:
    """``(table, ecosystem)`` of one parsed file: one row per advisory, newest first. Kept in memory while the file is unchanged."""
    global _cache_bytes
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    table = _read(path)
    eco = next((v for v in table["ecosystem"].to_pylist()[:1] if v), None) or path.stem[4:]
    folded = _collapse(table)
    slim = (folded.sort_by([("published", "descending")]) if folded.num_rows else folded), eco
    cost = slim[0].nbytes
    if cost <= CACHE_BYTES:
        with _lock:
            for old in [k for k in _cache if k[0] == key[0]]:                   # the file changed: its earlier table goes
                _cache_bytes -= _cache.pop(old)[0].nbytes
            _cache[key] = slim
            _cache_bytes += cost
            while _cache_bytes > CACHE_BYTES and len(_cache) > 1:
                _, (dropped, _name) = _cache.popitem(last=False)
                _cache_bytes -= dropped.nbytes
    return slim


def _keys(row: dict) -> list[str]:
    key = row.get("osv_id") or row.get("cve_id")
    eco = (row.get("ecosystem") or "").strip()
    return [key, f"{key}@{eco}"] if key and eco else ([key] if key else [])


def _state(row: dict, known: dict[str, str | None]) -> str:
    key = row.get("osv_id") or row.get("cve_id")
    eco = (row.get("ecosystem") or "").strip() or None
    if not key:
        return "downloaded"
    if key in known and (known[key] is None or eco is None or known[key] == eco):
        return "in_store"
    return "in_store" if f"{key}@{eco}" in known else "downloaded"


def unread(interim: pathlib.Path | None = None) -> list[dict]:
    """Dumps that are on this computer and were not read yet, so no table of them exists: ``[{"ecosystem", "dump_bytes"}]``, with ``partial`` (see ``osv_read.peek``) when a read was cut."""
    folder = pathlib.Path(interim) if interim else osv.INTERIM
    out = []
    for d in osv_catalog.local_downloads():
        if (folder / f"osv_{osv._slug(d['name'])}.parquet").is_file():
            continue
        entry = {"ecosystem": d["name"], "dump_bytes": d["dump_bytes"]}
        if d.get("read_partial"):                     # a read that was cut: how far it got
            entry["partial"] = d["read_partial"]
        out.append(entry)
    return out


def search(q: str = "", ecosystem: str = "", state: str = "", limit: int = 50, offset: int = 0, interim: pathlib.Path | None = None, lookup=None) -> dict:
    """The advisories of the downloaded dumps that match, newest first, a page of them with their state, and how many match in all. ``lookup(keys)`` tells which keys the store holds
    (the store's own by default). Asking for one state needs the state of every match, so it is refused beyond ``MAX_FILTERED`` matches: type something to narrow the search."""
    q = (q or "").strip()
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(repr(s) for s in STATES)}")
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    lookup = lookup or store.known_keys
    files = parsed_files(interim)
    wanted = offset + limit
    pieces: list[pa.Table] = []
    searched: list[str] = []
    total = 0
    for f in files:
        table, eco = _slim(f)
        if ecosystem and eco != ecosystem:
            continue
        searched.append(eco)
        hit = _matches(table, q)                                                # still newest first: a filter keeps the order
        total += hit.num_rows
        if state and total > MAX_FILTERED:
            raise ValueError(f"More than {MAX_FILTERED:,} advisories match, too many to sort by state. Type a CVE id, an advisory id or a package to narrow the search.")
        pieces.append(hit if state else hit.slice(0, wanted))
    rows = [r for t in pieces for r in t.to_pylist()]
    rows.sort(key=lambda r: str(r.get("published") or ""), reverse=True)
    if state:
        known = lookup([k for r in rows for k in _keys(r)])
        rows = [r for r in rows if _state(r, known) == state]
        total = len(rows)
        page = rows[offset:wanted]
        states = {id(r): state for r in page}
    else:
        page = rows[offset:wanted]
        known = lookup([k for r in page for k in _keys(r)])
        states = {id(r): _state(r, known) for r in page}
    out = []
    for r in page:
        packages = sorted({n.strip() for p in (r.get("packages") or []) if p for n in str(p).split(",") if n.strip()})      # a table of an ecosystem without an adapter joins an advisory's packages in one cell
        out.append({"osv_id": r["osv_id"], "cve_id": r["cve_id"], "ecosystem": r["ecosystem"], "packages": packages[:MAX_PACKAGES], "more_packages": max(0, len(packages) - MAX_PACKAGES),
                    "summary": r["summary"], "published": r["published"], "severity": r["osv_severity"], "aliases": _aliases(r["aliases"]) or [a for a in (r["aliases"] or "").split("\n") if a], "state": states[id(r)]})
    return {"query": q, "ecosystem": ecosystem, "state": state, "total": total, "offset": offset, "limit": limit, "ecosystems_searched": sorted(set(searched)),
            "downloaded_ecosystems": len(files), "unread": unread(interim), "results": out}


# -- one advisory in full ------------------------------------------------------------------------------------------------------

def _strings(raw) -> list:
    try:
        data = json.loads(raw) if isinstance(raw, str) and raw else []
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _affected(rec: dict) -> list[dict]:
    out = []
    for aff in rec.get("affected") or []:
        pkg = aff.get("package") or {}
        versions = [str(v) for v in (aff.get("versions") or [])]
        out.append({"package": pkg.get("name"), "ecosystem": pkg.get("ecosystem"), "purl": pkg.get("purl"),
                    "ranges": [{"type": r.get("type"), "repo": r.get("repo"), "events": r.get("events") or []} for r in aff.get("ranges") or []],
                    "versions": versions[:50], "versions_more": max(0, len(versions) - 50)})
    return out


def _from_record(rec: dict, raw: bytes) -> dict:
    """The sections of an advisory read from its OSV JSON."""
    ds = rec.get("database_specific") or {}
    v3, v4 = osv._pick_vectors(rec.get("severity"))
    prov = osv.provenance(rec, raw)
    return {"osv_id": rec.get("id"), "cve_id": prov["cve_id"], "aliases": _aliases(prov["aliases"]), "summary": rec.get("summary"), "details": rec.get("details"),
            "published": rec.get("published"), "modified": rec.get("modified"), "withdrawn": rec.get("withdrawn"),
            "severity_label": ds.get("severity"), "cvss_v3": v3, "cvss_v4": v4, "cwe": [str(c) for c in (ds.get("cwe_ids") or [])],
            "github_reviewed": ds.get("github_reviewed"), "nvd_published_at": ds.get("nvd_published_at"),
            "affected": _affected(rec), "fix_commits": fixrefs.from_osv_record(rec), "references": [r for r in (rec.get("references") or []) if r.get("url")],
            "source_record_hash": prov["source_record_hash"]}


def _from_rows(rows: list[dict]) -> dict:
    """The sections of an advisory rebuilt from the rows of the parsed table (one per package), when the dump itself is not at hand."""
    first = rows[0]
    affected = []
    structured = [e for e in _strings(first.get("affected")) if isinstance(e, dict) and e.get("package")]
    if structured:                                  # the advisory's own affected entries (also the only truthful list when a row holds several packages joined)
        for e in structured:
            versions = [str(v) for v in e.get("versions") or []]
            affected.append({"package": e.get("package"), "ecosystem": e.get("ecosystem"), "purl": None,
                             "ranges": [{"type": None, "repo": None, "events": [{k: str(iv[k])} for k in ("introduced", "fixed", "last_affected") if k in iv]} for iv in e.get("ranges") or [] if isinstance(iv, dict)],
                             "versions": versions[:50], "versions_more": max(0, int(e.get("versions_total") or len(versions)) - 50)})
    for r in ([] if structured else rows):
        events = [{"introduced": x} for x in str(r.get("introduced") or "").split(",") if x] + [{"fixed": x} for x in str(r.get("fixed") or "").split(",") if x]
        versions = [v for v in str(r.get("versions") or "").split(",") if v]
        affected.append({"package": r.get("package"), "ecosystem": r.get("ecosystem"), "purl": None, "ranges": [{"type": "ECOSYSTEM", "repo": None, "events": events}] if events else [],
                         "versions": versions[:50], "versions_more": 0})
    cwe = [c for c in str(first.get("osv_cwe") or "").split(";") if c]
    return {"osv_id": first.get("osv_id"), "cve_id": first.get("cve_id"), "aliases": [a for a in _strings(first.get("aliases")) if isinstance(a, str)], "summary": first.get("summary"),
            "details": first.get("details"), "published": first.get("published"), "modified": first.get("modified"), "withdrawn": first.get("withdrawn"),
            "severity_label": first.get("osv_severity"), "cvss_v3": first.get("cvss_vector"), "cvss_v4": first.get("cvss_v4_vector"), "cwe": cwe,
            "github_reviewed": first.get("github_reviewed"), "nvd_published_at": first.get("nvd_published_at"),
            "affected": affected, "fix_commits": [p for p in _strings(first.get("fix_commits")) if isinstance(p, dict)],
            "references": [r for r in _strings(first.get("source_urls")) if isinstance(r, dict) and r.get("url")], "source_record_hash": first.get("source_record_hash")}


def detail(ecosystem: str, osv_id: str, interim: pathlib.Path | None = None, lookup=None) -> dict | None:
    """One advisory in full: its text, severity and vectors, weaknesses, aliases, affected packages with their ranges, fix commits, references and dates, plus where the store stands.
    Read from the downloaded dump itself (the raw record, also returned as ``raw``), or from the parsed table when the dump is gone. None when this ecosystem's dump does not hold it.
    Raises ValueError for an id that cannot be one."""
    if not ID_OK.match(osv_id or ""):
        raise ValueError("That is not a valid advisory id.")
    slug = osv._slug(ecosystem)
    if not slug:
        raise ValueError("That is not a valid ecosystem.")
    dump = osv.RAW / f"osv_{slug}.zip"
    out: dict | None = None
    if dump.is_file():
        raw = None
        try:
            with zipfile.ZipFile(dump) as z:
                raw = z.read(f"{osv_id}.json")
        except (KeyError, zipfile.BadZipFile, OSError):
            raw = None
        if raw is not None:
            rec = json.loads(raw)
            out = {**_from_record(rec, raw), "source": "dump"}
            if len(raw) <= MAX_RAW_BYTES:
                out["raw"] = rec
            else:
                out["raw"], out["raw_omitted"] = None, True
    if out is None:
        table = (pathlib.Path(interim) if interim else osv.INTERIM) / f"osv_{slug}.parquet"
        if table.is_file():
            rows = pq.read_table(table, filters=[("osv_id", "=", osv_id)]).to_pylist()
            if rows:
                out = {**_from_rows(rows), "source": "table", "raw": None}
    if out is None:
        return None
    out["ecosystem"] = ecosystem
    lookup = lookup or store.known_keys
    row = {"osv_id": out["osv_id"], "cve_id": out["cve_id"], "ecosystem": ecosystem}
    out["state"] = _state(row, lookup(_keys(row)))
    return out
