"""Download and parse OSV advisories for any ecosystem.

OSV publishes a bulk ZIP per ecosystem, which is preferable to thousands of API
calls: one request, no rate limit, and the cached file makes the pipeline
reproducible offline. The dump is streamed to disk and refreshed only when its ETag
changed. Every ecosystem parses into the same record shape (``pipeline/ecosystems``);
package-less sources (GIT, Linux, OSS-Fuzz) yield one row per advisory from their git
ranges.

For each advisory this parser captures, in addition to the identifiers and
version ranges, the fields that make a severity label defensible without
depending on NVD enrichment:

* ``cwe_ids`` and a qualitative ``osv_severity`` from ``database_specific``
  (populated for GitHub-reviewed advisories, which is the large majority of the
  PyPI set);
* the CVSS v3.x and, separately, the CVSS v4.0 vector strings;
* ``withdrawn`` so retracted advisories can be dropped downstream;
* ``github_reviewed`` and ``nvd_published_at`` for provenance reporting;
* ``fix_commits``: the security-fix commit pointers (``GIT`` ranges and commit
  reference URLs) that the code-pair miner follows to the vulnerable/fixed code;
* ``aliases`` (every alias plus ``upstream``), ``source_urls`` (all typed references)
  and ``source_record_hash`` (sha256 of the raw OSV file) for traceability.

This matters for the methodology: the thesis argues that the NVD enrichment
backlog makes NVD unreliable as the sole label source, and OSV/GHSA already
supplies a CVSS vector and a CWE for most records. See Chapter 6.
"""
import dataclasses
import functools
import email.utils
import hashlib
import json
import os
import re
import sys
import time
import zipfile
import pathlib
import datetime

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from urllib.parse import quote
from tqdm import tqdm

from app.backend.pipeline import ecosystems, fsio, stopping, util
from app.backend.pipeline.ingest import depversions, fixrefs, osv_fetch, osv_read
from app.backend import paths

RAW = pathlib.Path("data/raw")
INTERIM = paths.INTERIM
BULK_URL = "https://osv-vulnerabilities.storage.googleapis.com/{eco}/all.zip"
ECOSYSTEMS_URL = "https://osv-vulnerabilities.storage.googleapis.com/ecosystems.txt"


def default_ecosystems() -> list[str]:
    """The ecosystems named by ``data.ecosystems`` in the config."""
    return [a.name for a in ecosystems.enabled()]


def list_ecosystems() -> list[str]:
    """Every ecosystem OSV publishes a dump for (``ecosystems.txt`` in the bucket)."""
    r = requests.get(ECOSYSTEMS_URL, timeout=60)
    r.raise_for_status()
    return [line.strip() for line in r.text.splitlines() if line.strip()]


def _slug(ecosystem: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", ecosystem.lower()).strip("_")


def has_local_data(ecosystem: str) -> bool:
    """True when the advisories of this ecosystem were downloaded and parsed (data/interim/osv_<slug>.parquet)."""
    return (INTERIM / f"osv_{_slug(ecosystem)}.parquet").is_file()


def _meta_path(ecosystem: str) -> pathlib.Path:
    return RAW / f"osv_{_slug(ecosystem)}.meta.json"


def read_meta(ecosystem: str) -> dict:
    """What the last download of this dump learned: ``last_modified`` (OSV's own date for its copy, ISO with a Z), ``etag``, ``downloaded_at``, ``bytes``."""
    return util.read_json(_meta_path(ecosystem)) or {}


def http_date_to_iso(value: str | None) -> str | None:
    """An HTTP date such as ``Wed, 01 Oct 2026 12:00:00 GMT`` as ``2026-10-01T12:00:00Z``; None when it cannot be read."""
    if not value:
        return None
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def download(ecosystem: str = "PyPI", refresh: bool = False) -> pathlib.Path:
    """Fetch (or reuse) the OSV bulk dump for one ecosystem.

    The dump is streamed to disk (never held in memory) and its ETag is remembered. A
    cached dump is reused as is; with ``refresh`` a conditional request keeps it when
    OSV's copy did not change and replaces it otherwise."""
    return download_result(ecosystem, refresh)["path"]


DownloadBusy = osv_fetch.DownloadBusy


def dump_path(ecosystem: str) -> pathlib.Path:
    return RAW / f"osv_{_slug(ecosystem)}.zip"


def _recorder(ecosystem: str):
    """The writer of the files that go with a finished dump (its ETag, the day it was taken, what OSV says about its copy); also used to complete a download that a cut left unfinished."""
    out = dump_path(ecosystem)

    def record(side: dict) -> None:
        etag, last_modified = side.get("etag"), http_date_to_iso(side.get("last_modified"))
        etag_file = out.with_suffix(".etag")
        if etag:
            fsio.write_text(etag_file, etag + "\n")
        else:
            etag_file.unlink(missing_ok=True)       # a copy with no ETag must not keep the ETag of the one it replaced
        stamp = RAW / ("osv_snapshot_date.txt" if ecosystem == "PyPI" else f"osv_{_slug(ecosystem)}_snapshot_date.txt")
        now_iso = datetime.datetime.utcnow().isoformat() + "Z"
        fsio.write_text(stamp, now_iso + "\n")
        fsio.write_text(_meta_path(ecosystem), json.dumps({"last_modified": last_modified, "etag": etag, "downloaded_at": now_iso, "bytes": out.stat().st_size,
                                                          "md5": side.get("md5")}))
        print(f"saved {out} ({out.stat().st_size / 1e6:.1f} MB)")
        print("snapshot date recorded in", stamp)
    return record


def recover_download(ecosystem: str) -> bool:
    """Complete a download that a cut interrupted after its last byte (see ``osv_fetch.recover``); answers whether it did."""
    return osv_fetch.recover(dump_path(ecosystem), _recorder(ecosystem))


def download_partial(ecosystem: str) -> dict | None:
    """What a stopped download of this dump has fetched (see ``osv_fetch.peek``), or None."""
    return osv_fetch.peek(dump_path(ecosystem))


def discard_download(ecosystem: str) -> None:
    """Throw away a stopped download (Start over)."""
    osv_fetch.discard(dump_path(ecosystem))


def _stoppable(progress, should_stop):
    """``progress(done, total)`` that also asks ``should_stop`` and raises ``stopping.Paused`` (not at the very end: a finished transfer is finished)."""
    if should_stop is None:
        return progress

    def call(done, total=None, *rest):
        if total is None or done < total:
            stopping.check(should_stop)
        if progress:
            progress(done, total, *rest)
    return call


def download_result(ecosystem: str = "PyPI", refresh: bool = False, progress=None, on_resume=None, fresh: bool = False, should_stop=None) -> dict:
    """``download`` with the outcome: ``{"path", "outcome", "bytes", "etag", "last_modified", "resumed_from"}``. ``outcome`` is "cached" (a dump was here and no refresh was asked), "unchanged" (OSV
    answered 304: its copy is the one here) or "downloaded". A download that was cut continues from the bytes already on the disk (see ``osv_fetch``); ``fresh`` throws them away first.
    ``progress(done_bytes, total_bytes_or_None)`` is called about twice a second while the dump streams in, and once at the end; ``on_resume(offset, total)`` once when an earlier attempt is continued.
    Raises ``DownloadBusy`` when another process is downloading the same dump. ``should_stop`` (see ``stopping``) is asked while the dump streams in: the bytes fetched are made durable and
    ``stopping.Paused`` is raised; the next call continues from them."""
    RAW.mkdir(parents=True, exist_ok=True)
    out = dump_path(ecosystem)
    etag_file = out.with_suffix(".etag")
    if fresh:
        discard_download(ecosystem)
    else:
        recover_download(ecosystem)
    stored = read_meta(ecosystem)
    etag = etag_file.read_text().strip() if etag_file.exists() else None
    if out.exists() and not refresh:
        print("cached:", out)
        return {"path": out, "outcome": "cached", "bytes": out.stat().st_size, "etag": etag, "last_modified": stored.get("last_modified"), "resumed_from": 0}
    resumed = {"from": 0}

    def on_resumed(offset, total):
        resumed["from"] = offset
        print(f"continuing the download from {offset / 1e6:.1f} MB" + (f" of {total / 1e6:.1f} MB" if total else ""))
        if on_resume:
            on_resume(offset, total)
    print(f"{'refreshing' if out.exists() else 'downloading'} OSV {ecosystem} bulk dump ...")
    result = osv_fetch.fetch(BULK_URL.format(eco=quote(ecosystem)), out, if_none_match=etag if out.exists() else None, progress=_stoppable(progress, should_stop), on_resume=on_resumed, record=_recorder(ecosystem))
    if result["outcome"] == "unchanged":
        print(f"{ecosystem}: unchanged since the cached dump")
        return {"path": out, "outcome": "unchanged", "bytes": out.stat().st_size, "etag": etag, "last_modified": stored.get("last_modified"), "resumed_from": 0}
    return {"path": out, "outcome": "downloaded", "bytes": result["bytes"], "etag": result.get("etag"), "last_modified": http_date_to_iso(result.get("last_modified")), "resumed_from": resumed["from"]}


def _pick_vectors(severity_list):
    """Return (cvss_v3_vector, cvss_v4_vector) from an OSV severity array.

    OSV uses type 'CVSS_V3' for v3.x vectors and 'CVSS_V4' for v4.0. The thesis
    labels from the CVSS v3.1 base score, so the v3 vector is preferred; the v4
    vector is retained for records that carry only v4.
    """
    v3 = v4 = None
    for s in severity_list or []:
        t = str(s.get("type", ""))
        score = s.get("score")
        if t == "CVSS_V3" and v3 is None:
            v3 = score
        elif t == "CVSS_V4" and v4 is None:
            v4 = score
        elif t.startswith("CVSS") and v3 is None and v4 is None:
            # Unknown CVSS flavour: keep it in whichever slot matches the prefix.
            if isinstance(score, str) and score.startswith("CVSS:4"):
                v4 = score
            else:
                v3 = score
    return v3, v4


def _eco_matches(name: str | None, ecosystem: str) -> bool:
    # OSV ecosystems may carry a release suffix, e.g. "Debian:12": an advisory of "Ubuntu:22.04:LTS" belongs to the ecosystem "Ubuntu", one of "Debian:12" to "Debian:12" (and to "Debian").
    return bool(name) and (name == ecosystem or name.split(":", 1)[0] == ecosystem)


def provenance(rec: dict, raw: bytes | None = None) -> dict:
    """Aliases, typed reference URLs and the hash of the raw OSV file of one advisory.

    ``aliases`` holds every alias plus ``upstream`` ids (distribution feeds carry the CVE
    only there) plus the record id when it is itself a CVE; ``cve_id`` is the first CVE."""
    aliases: list[str] = []
    ids = ([rec["id"]] if str(rec.get("id", "")).startswith("CVE-") else []) + list(rec.get("aliases") or []) \
        + list(rec.get("upstream") or [])
    for a in ids:
        if isinstance(a, str) and a and a not in aliases:
            aliases.append(a)
    refs, seen = [], set()
    for ref in rec.get("references") or []:
        url = ref.get("url")
        if url and (ref.get("type"), url) not in seen:
            seen.add((ref.get("type"), url))
            refs.append({"type": ref.get("type"), "url": url})
    blob = raw if raw is not None else json.dumps(rec, sort_keys=True).encode("utf-8")
    return {"cve_id": next((a for a in aliases if a.startswith("CVE-")), None),
            "aliases": json.dumps(aliases), "source_urls": json.dumps(refs),
            "source_record_hash": hashlib.sha256(blob).hexdigest()}


def fetch_parse(ecosystem: str, refresh: bool = False):
    """(ecosystem, rows, error): the advisories of one ecosystem downloaded and parsed; with ``refresh`` OSV is asked whether its copy changed and the cached dump
    is replaced when it did. Top level, so a worker process can run it; an error is returned, not raised, because one ecosystem failing must not stop the others."""
    try:
        return ecosystem, parse(download(ecosystem, refresh=True) if refresh else download(ecosystem), ecosystem), None
    except Exception as e:
        return ecosystem, None, str(e)


# The columns of the parsed table, in the order every reader has always seen them: all text except ``github_reviewed``.
COLUMNS = ("osv_id", "cve_id", "ecosystem", "package", "summary", "details", "published", "modified", "withdrawn", "cvss_vector", "cvss_v4_vector", "osv_cwe", "osv_severity",
           "github_reviewed", "nvd_published_at", "introduced", "fixed", "versions", "fix_commits", "affected", "aliases", "source_urls", "source_record_hash")
SCHEMA = pa.schema([pa.field(c, pa.bool_() if c == "github_reviewed" else pa.large_string()) for c in COLUMNS])
MAX_VERSIONS = 50


def _text(value) -> str | None:
    """A value of the dump as the text of a column: None stays None, text stays, a number or a flag becomes its text, a list or an object its JSON. One odd record can then never fail a whole batch."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def layout_of(ecosystem: str) -> str:
    """How an ecosystem's advisories become rows. ``package``: one row per affected package (the ecosystems with an adapter). ``package_less``: one row, no package (GIT, Linux, OSS-Fuzz).
    ``advisory``: one row per advisory with its package names joined (an ecosystem the app has no adapter for, such as Ubuntu or Debian, where one advisory lists dozens of packages and releases and
    each row would repeat the advisory's long text)."""
    adapter = ecosystems.get(ecosystem)
    return "advisory" if adapter is None else "package_less" if adapter.package_less else "package"


def _events(aff: dict) -> tuple[list[str], list[str]]:
    introduced, fixed = [], []
    for rng in aff.get("ranges") or []:
        if rng.get("type") == "GIT":               # GIT ranges hold commit SHAs, not versions; they go to fix_commits, never into the version strings
            continue
        for ev in rng.get("events") or []:
            if "introduced" in ev:
                introduced.append(str(ev["introduced"]))
            if "fixed" in ev:
                fixed.append(str(ev["fixed"]))
    return introduced, fixed


def _unique(items) -> list[str]:
    """First appearance order, no duplicates, no empty ones."""
    return list(dict.fromkeys(x for x in items if x))


def advisory_rows(rec: dict, raw: bytes, ecosystem: str, layout: str | None = None) -> list[dict]:
    """The rows of one OSV advisory for ``ecosystem``, as dicts with the keys of ``COLUMNS``. An advisory with nothing for this ecosystem gives none."""
    layout = layout or layout_of(ecosystem)
    prov = provenance(rec, raw)                    # The OSV id is usually GHSA-...; the CVE id lives in aliases (or upstream).
    ds = rec.get("database_specific") or {}
    cwe_ids = ds.get("cwe_ids") or []
    v3, v4 = _pick_vectors(rec.get("severity"))
    reviewed = ds.get("github_reviewed")
    base = {
        "osv_id": _text(rec.get("id")), "cve_id": _text(prov["cve_id"]), "ecosystem": ecosystem, "summary": _text(rec.get("summary")), "details": _text(rec.get("details")),
        "published": _text(rec.get("published")), "modified": _text(rec.get("modified")), "withdrawn": _text(rec.get("withdrawn")),
        "cvss_vector": _text(v3),                  # preferred for the v3.1 mapping
        "cvss_v4_vector": _text(v4),
        "osv_cwe": ";".join(str(c) for c in cwe_ids) if isinstance(cwe_ids, list) and cwe_ids else None, "osv_severity": _text(ds.get("severity")),
        "github_reviewed": reviewed if isinstance(reviewed, bool) else None, "nvd_published_at": _text(ds.get("nvd_published_at")),
        # Security-fix commits (GIT ranges + commit references) for code mining, and the structured affected ranges per package for the dependency-version check.
        "fix_commits": json.dumps(fixrefs.from_osv_record(rec)), "affected": json.dumps(depversions.affected_from_osv(rec, ecosystem)),
        "aliases": prov["aliases"], "source_urls": prov["source_urls"], "source_record_hash": prov["source_record_hash"],
    }

    def row(package, introduced, fixed, versions) -> dict:
        r = {**base, "package": _text(package), "introduced": ",".join(introduced), "fixed": ",".join(fixed), "versions": ",".join(versions)}
        return {c: r[c] for c in COLUMNS}

    if layout == "package_less":                   # GIT / Linux / OSS-Fuzz: no package, the fix is named by git ranges
        return [row(None, [], [], [])]
    matching = [aff for aff in rec.get("affected") or [] if _eco_matches((aff.get("package") or {}).get("ecosystem"), ecosystem)]
    if layout == "package":
        out = []
        for aff in matching:
            introduced, fixed = _events(aff)
            out.append(row((aff.get("package") or {}).get("name"), introduced, fixed, [str(v) for v in (aff.get("versions") or [])[:MAX_VERSIONS]]))
        return out
    if not matching:
        return []
    names, introduced, fixed, versions = [], [], [], []
    for aff in matching:
        names.append(str((aff.get("package") or {}).get("name") or ""))
        i, f = _events(aff)
        introduced += i
        fixed += f
        versions += [str(v) for v in aff.get("versions") or []]
    return [row(", ".join(sorted({n for n in names if n})) or None, _unique(introduced), _unique(fixed), _unique(versions)[:MAX_VERSIONS])]


@dataclasses.dataclass(frozen=True)
class TableRef:
    """A finished parsed table on disk: where it is, how many rows and advisories it holds, how many row groups (each a set of whole advisories) and from which advisory this run continued."""
    path: str
    rows: int
    advisories: int
    groups: int = 1
    resumed_from: int = 0


ReadBusy = osv_read.ReadBusy
DumpUnreadable = osv_read.DumpUnreadable
FRAME_LIMIT = 1 << 30                          # a table whose rows would take more than this as columns is not built as one DataFrame (a huge dump is read in chunks)


@functools.lru_cache(maxsize=1)
def _parser_identity() -> str:
    """The reader's identity: the format of its work files and the source of the three modules that decide what a row holds. A saved partial read is continued only by the same reader."""
    h = hashlib.sha256(str(osv_read.FORMAT).encode("utf-8"))
    for module in (sys.modules[__name__], fixrefs, depversions):
        h.update(pathlib.Path(module.__file__).read_bytes())
    return h.hexdigest()


def table_path(ecosystem: str) -> pathlib.Path:
    return INTERIM / f"osv_{_slug(ecosystem)}.parquet"


def read_partial(ecosystem: str) -> dict | None:
    """What a stopped read of this ecosystem's dump has done (see ``osv_read.peek``), or None."""
    return osv_read.peek(table_path(ecosystem), RAW / f"osv_{_slug(ecosystem)}.zip")


def discard_partial(ecosystem: str) -> None:
    """Throw away the saved work of a stopped read (Start over)."""
    osv_read.discard(table_path(ecosystem))


def _stats(path: pathlib.Path) -> None:
    """A few facts about the finished table, from the columns that are small."""
    try:
        t = pq.read_table(path, columns=["cvss_vector", "cvss_v4_vector", "osv_cwe", "withdrawn"])
    except Exception:  # ignored: the statistics are information only
        return
    n = t.num_rows
    if not n:
        return
    share = lambda col: 1 - t[col].null_count / n   # noqa: E731
    print(f"{share('cvss_vector'):.1%} carry a CVSS v3 vector; {share('cvss_v4_vector'):.1%} carry a CVSS v4 vector")
    print(f"{share('osv_cwe'):.1%} carry at least one CWE")
    print(f"{share('withdrawn'):.1%} are withdrawn")


def parse(zip_path: pathlib.Path, ecosystem: str = "PyPI", progress=None, *, frame: bool = True, fresh: bool = False, on_resume=None, should_stop=None):
    """Read the advisories of one dump into ``data/interim/osv_<slug>.parquet``, in checkpoints (see ``osv_read``): a read that was cut continues where it stopped, and the memory used is bounded
    however large the dump is. ``progress(files_done, files_total, rows, phase=...)`` is called about once a second while reading (``phase="read"``), once a part while the table is put
    together afterwards (``phase="assembling"``, the count keeps rising past the file total rather than going quiet) and once at the end (``phase="done"``); ``on_resume(next, total)`` once when an earlier run is continued;
    ``fresh`` throws the saved work away first. Returns the table as a DataFrame, or, with ``frame=False``, a ``TableRef`` (a table of more than ``FRAME_LIMIT`` bytes of text cannot be a frame: read
    it with ``frames``). Raises ``ReadBusy`` when another process is reading the same table and ``DumpUnreadable`` (naming the member) for a record that cannot be read.
    ``should_stop`` (see ``stopping``) is asked as the members are read: what was read is made durable and ``stopping.Paused`` is raised; the next call continues from it."""
    INTERIM.mkdir(parents=True, exist_ok=True)
    layout = layout_of(ecosystem)
    final = table_path(ecosystem)
    res = osv_read.run(zip_path, final, schema=SCHEMA, rows_for=lambda rec, raw: advisory_rows(rec, raw, ecosystem, layout), layout=layout, ecosystem=ecosystem,
                       parser=_parser_identity(), progress=progress, on_resume=on_resume, fresh=fresh, should_stop=should_stop)
    ref = TableRef(path=res.path, rows=res.rows, advisories=res.advisories, groups=res.groups, resumed_from=res.resumed_from)
    print(f"{ecosystem}: {res.rows} {'advisory' if layout == 'advisory' else 'affected-package'} rows ({res.advisories} distinct advisories) -> {final}"
          + (f" (continued from advisory {res.resumed_from})" if res.resumed_from else ""))
    _stats(final)
    if not frame:
        return ref
    if res.est_bytes > FRAME_LIMIT:
        raise ValueError(f"The {ecosystem} table is about {res.est_bytes / 1e9:.1f} GB of text, too much to hold as one DataFrame. It is written ({final}); read it in chunks with frames().")
    return pq.read_table(final).to_pandas()


def fetch_table(ecosystem: str, refresh: bool = False):
    """``(ecosystem, TableRef | None, error)``: one ecosystem's dump downloaded (with ``refresh`` OSV is asked whether its copy changed) and read into its table; the table's place is answered, never
    the rows, so nothing large is sent between processes. Top level, so a worker process can run it; an error is returned, not raised, because one ecosystem failing must not stop the others."""
    try:
        path = download(ecosystem, refresh=True) if refresh else download(ecosystem)
        return ecosystem, parse(path, ecosystem, frame=False), None
    except Exception as e:
        return ecosystem, None, str(e)


def row_count(source) -> int:
    """The rows of a table given as a ``TableRef`` or a DataFrame."""
    return int(source.rows) if isinstance(source, TableRef) else int(len(source))


def frames(source):
    """The table as DataFrames of whole advisories, one per row group (a ``TableRef``, a path, or a DataFrame, which is yielded once if it has rows). A huge table is thus never one frame, and an
    advisory is never split between two frames."""
    if isinstance(source, pd.DataFrame):
        if len(source):
            yield source
        return
    path = source.path if isinstance(source, TableRef) else source
    with pq.ParquetFile(path) as pf:
        for i in range(pf.num_row_groups):
            t = pf.read_row_group(i)
            if t.num_rows:
                yield t.to_pandas()


if __name__ == "__main__":
    for _eco in default_ecosystems():
        parse(download(_eco), _eco, frame=False)
