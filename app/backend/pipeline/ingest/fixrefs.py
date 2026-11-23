"""Extract security-fix commit pointers from advisories (OSV, NVD).

Neither NVD nor OSV stores the vulnerable source code itself; the code lives in the
project's repository. Advisories point to the commit that fixed the vulnerability in
two ways, and this module turns both into normalised commit pointers:

* OSV ``affected[].ranges[]`` entries of type ``GIT`` carry the repository URL and
  the ``fixed`` commit SHA(s);
* reference URLs that link to a commit (OSV ``references``, often typed ``FIX``;
  NVD ``references``, often tagged ``Patch``).

A pointer is ``{host, owner, repo, sha, repo_url, commit_key}``. Only the commits
of the Dataset records are queued (``queue_from_records``). The code miner
(``codemine.py``) later fetches the commit, its parent, and the before/after code.
"""
from __future__ import annotations
import json
import pathlib
import re
from urllib.parse import urlparse
from app.backend import paths

RAW = pathlib.Path("data/raw")
INTERIM = paths.INTERIM

# .../owner/repo/commit/<sha>, .../owner/repo/pull/12/commits/<sha>,
# .../group/sub/repo/-/commit/<sha> (GitLab), .../owner/repo/commits/<sha> (Bitbucket)
_COMMIT_URL = re.compile(
    r"^https?://(?:www\.)?(github\.com|gitlab\.com|bitbucket\.org)/(.+?)/commits?/"
    r"([0-9a-fA-F]{7,40})(?:[/?#.].*)?$")
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_KNOWN_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")


def _pointer(host: str, owner: str, repo: str, sha: str, repo_url: str | None = None) -> dict:
    repo = repo[:-4] if repo.endswith(".git") else repo
    host = host.lower()
    if host in _KNOWN_HOSTS:
        repo_url = f"https://{host}/{owner}/{repo}"
    return {
        "host": host, "owner": owner, "repo": repo, "sha": sha.lower(),
        "repo_url": repo_url, "commit_key": f"{host}/{owner}/{repo}@{sha.lower()}",
    }


def parse_commit_url(url: str) -> dict | None:
    """Commit URL -> pointer, or None if the URL is not a commit link."""
    m = _COMMIT_URL.match((url or "").strip())
    if not m:
        return None
    host, path, sha = m.group(1).lower(), m.group(2), m.group(3)
    parts = [p for p in path.split("/") if p and p != "-"]
    if host in ("github.com", "bitbucket.org"):
        if len(parts) < 2:
            return None
        owner, repo = parts[0], parts[1]          # drops pull/<n> for PR-commit links
    else:                                          # GitLab: nested groups allowed
        if len(parts) < 2:
            return None
        if "merge_requests" in parts:
            parts = parts[:parts.index("merge_requests")]
        owner, repo = "/".join(parts[:-1]), parts[-1]
    return _pointer(host, owner, repo, sha)


def parse_repo_url(url: str) -> tuple[str, str, str, str] | None:
    """Repository URL (as in OSV GIT ranges) -> (host, owner, repo, clone_url)."""
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith("git@") and ":" in url:              # git@github.com:o/r.git
        host, path = url[4:].split(":", 1)
        url = f"https://{host}/{path}"
    p = urlparse(url if "://" in url else f"https://{url}")
    host = (p.hostname or "").lower()
    parts = [x for x in p.path.split("/") if x]
    if not host or len(parts) < 2:
        return None
    if host in ("github.com", "bitbucket.org"):
        owner, repo = parts[0], parts[1]
    else:
        owner, repo = "/".join(parts[:-1]), parts[-1]
    repo = repo[:-4] if repo.endswith(".git") else repo
    clone = f"https://{host}/{owner}/{repo}" if host in _KNOWN_HOSTS else url
    return host, owner, repo, clone


def from_osv_record(rec: dict) -> list[dict]:
    """All fix-commit pointers of one OSV advisory (GIT ranges + commit references)."""
    out: dict[str, dict] = {}
    for aff in rec.get("affected", []) or []:
        for rng in aff.get("ranges", []) or []:
            if rng.get("type") != "GIT":
                continue
            repo = parse_repo_url(rng.get("repo", ""))
            if not repo:
                continue
            host, owner, name, clone = repo
            for ev in rng.get("events", []) or []:
                sha = str(ev.get("fixed", ""))
                if _SHA.match(sha):
                    p = _pointer(host, owner, name, sha, clone)
                    p["via"] = "osv_git_range"
                    out.setdefault(p["commit_key"], p)
    for ref in rec.get("references", []) or []:
        p = parse_commit_url(ref.get("url", ""))
        if p:
            p["via"] = f"osv_ref_{str(ref.get('type', '')).lower() or 'web'}"
            out.setdefault(p["commit_key"], p)
    return list(out.values())


def source_of_via(via) -> str:
    """Where a fix link was found: 'nvd' for an NVD reference, 'recovered' for a link followed
    from a pull request, an issue or a fixed version, else 'osv'. The ``via`` recorded with the
    pointer is the truth; a stored ``source`` label is not trusted (the old requeue relabelled
    every stored pointer as 'osv')."""
    if isinstance(via, str):
        if via.startswith("nvd"):
            return "nvd"
        if via.startswith(("pr_", "issue_", "version_range")):
            return "recovered"
    return "osv"


_GH_REF = re.compile(r"^https?://(?:www\.)?github\.com/([^/\s#?]+)/([^/\s#?]+)(?:/([^#?\s]*))?(?:[#?].*)?$", re.I)


def classify_reference(url: str) -> dict:
    """What an advisory reference points at: a ``commit`` (the pointer fields follow), a ``pull``
    request or ``issue`` (with its number), a ``compare`` or ``release`` page, a commit ``listing``
    of a tag or branch, a ``blob`` at a commit, or ``other``. Only commits, pull requests and issues
    can be followed to a fix; the rest are kept as references."""
    u = (url or "").strip()
    if not u:
        return {"kind": "other"}
    p = parse_commit_url(u)
    if p:
        return {"kind": "commit", **p}
    m = _GH_REF.match(u)
    if not m:
        return {"kind": "other"}
    owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2))
    parts = [x for x in (m.group(3) or "").split("/") if x]
    base = {"host": "github.com", "owner": owner, "repo": repo}
    head = parts[0].lower() if parts else ""
    if head in ("pull", "issues") and len(parts) > 1 and parts[1].isdigit():
        return {**base, "kind": "pull" if head == "pull" else "issue", "number": int(parts[1])}
    if head == "compare" and len(parts) > 1 and ".." in parts[1]:
        a, _, b = parts[1].partition("...") if "..." in parts[1] else parts[1].partition("..")
        return {**base, "kind": "compare", "base": a, "head": b}
    if head == "releases" and len(parts) > 1 and parts[1] == "tag":
        return {**base, "kind": "release"}
    if head == "commits":
        return {**base, "kind": "listing"}
    if head in ("blob", "tree"):
        return {**base, "kind": "blob"}
    return {**base, "kind": "other"}


def from_nvd_item(item: dict) -> list[dict]:
    """Fix-commit pointers from one NVD API v2 vulnerability item's references."""
    out: dict[str, dict] = {}
    for ref in (item.get("cve", {}) or {}).get("references", []) or []:
        p = parse_commit_url(ref.get("url", ""))
        if p:
            tags = ref.get("tags") or []
            p["via"] = "nvd_ref_patch" if "Patch" in tags else "nvd_ref"
            out.setdefault(p["commit_key"], p)
    return list(out.values())


def iter_nvd_references(raw_path: pathlib.Path | None = None):
    """Yield ``(cve_id, item)`` for every NVD CVE, where ``item`` carries just the
    references. Reads the ``nvd_references`` column of ``data/interim/nvd.parquet``
    when present (small and fast); otherwise streams ``nvd_raw.json`` item by item.
    Neither path loads the whole harvest into memory."""
    pq = INTERIM / "nvd.parquet"
    if raw_path is None and pq.exists():
        import pyarrow.parquet as papq
        if "nvd_references" in papq.read_schema(pq).names:
            f = papq.ParquetFile(pq)
            for batch in f.iter_batches(batch_size=20000, columns=["cve_id", "nvd_references"]):
                for cve_id, refs in zip(batch.column(0).to_pylist(), batch.column(1).to_pylist()):
                    yield cve_id, {"cve": {"references": json.loads(refs) if refs else []}}
            return
    from app.backend.pipeline.jsonstream import iter_json_array
    path = raw_path or RAW / "nvd_raw.json"
    if not path.exists():
        return
    for item in iter_json_array(path):
        yield (item.get("cve") or {}).get("id"), item


def queue_from_records(progress=None, ecosystems: list[str] | None = None, should_stop=None) -> dict:
    """Queue the fix commits of the Dataset records only (not every advisory in the
    cached dumps): each record's OSV pointers (stored on the record at ingest, else
    looked up in the parsed OSV dump) plus NVD references of its CVE. Commits no
    record points to become 'unlinked'. The records get their fix-commit fields.

    Every commit is tagged with the ecosystem of the records that point to it. With ``ecosystems`` (a campaign's scope) only the records of those ecosystems are
    read, and only commits tagged with nothing else are set aside, so another ecosystem's queue is left as it is.

    ``should_stop`` (see ``stopping``) is asked between the reading steps; when it says yes the stage returns ``{"paused": True, ...}`` before it has written anything, and running it
    again does the whole thing (it is idempotent)."""
    from app.backend.pipeline import store, codestore, stopping
    report = progress or (lambda **_: None)
    if ecosystems is not None and not ecosystems:
        return {"records": 0}                                                     # an empty scope is no scope, not "everything"
    recs = store.select_mineable(("id", "osv_id", "cve_id", "fix_commits", "ecosystem"), ecosystems=ecosystems)      # malware and withdrawn records are never mined
    if not recs:
        print("No Dataset records yet: ingest or add records first." if ecosystems is None else f"No Dataset records in {', '.join(ecosystems)} yet.")
        return {"records": 0}
    print(f"{len(recs):,} Dataset records")
    report(state="extract", label="Reading fix pointers of the Dataset records",
           processed=0, total=len(recs))

    # OSV pointers missing on older records: look them up in the parsed dumps
    need = {r["osv_id"] for r in recs if not r.get("fix_commits") and r.get("osv_id")}
    osv_ptrs: dict[str, list] = {}
    if need:
        import pyarrow.parquet as papq
        for pq in sorted(INTERIM.glob("osv_*.parquet")):
            if stopping.stop(should_stop):
                return {"records": len(recs), "paused": True}
            if "fix_commits" not in papq.read_schema(pq).names:
                continue
            f = papq.ParquetFile(pq)
            for b in f.iter_batches(batch_size=50000, columns=["osv_id", "fix_commits"]):
                for oid, fc in zip(b.column(0).to_pylist(), b.column(1).to_pylist()):
                    if oid in need and fc and oid not in osv_ptrs:
                        osv_ptrs[oid] = json.loads(fc)

    # NVD references, only for the records' CVEs
    cves = {r["cve_id"] for r in recs if r.get("cve_id")}
    nvd_ptrs: dict[str, list] = {}
    for seen, (cve_id, item) in enumerate(iter_nvd_references()):
        if seen % 2000 == 0 and stopping.stop(should_stop):
            return {"records": len(recs), "paused": True}
        if cve_id in cves:
            ptrs = from_nvd_item(item)
            if ptrs:
                nvd_ptrs[cve_id] = ptrs

    items, rec_ptrs = [], {}
    for r in recs:
        ptrs = json.loads(r["fix_commits"]) if r.get("fix_commits") else osv_ptrs.get(r.get("osv_id"), [])
        ptrs = [p for p in ptrs if isinstance(p, dict) and p.get("commit_key")]
        merged = {p["commit_key"]: {**p, "source": source_of_via(p.get("via"))} for p in ptrs}
        for p in nvd_ptrs.get(r.get("cve_id"), []):
            merged.setdefault(p["commit_key"], {**p, "source": "nvd"})
        rec_ptrs[r["id"]] = list(merged.values())
        for p in merged.values():
            items.append({**p, "advisory_id": r.get("osv_id") or r.get("cve_id"),
                          "cve_id": r.get("cve_id"), "ecosystem": r.get("ecosystem")})
    if stopping.stop(should_stop):                 # the writes below are not interrupted
        return {"records": len(recs), "paused": True}
    new, total = codestore.queue_fix_commits(items)
    linked = {i["commit_key"] for i in items}
    off, on = codestore.sync_links(linked, ecosystems=ecosystems)
    store.set_fix_commits([(k, json.dumps(v) if v else None) for k, v in rec_ptrs.items()])
    codestore.refresh_records([r["id"] for r in recs])
    with_fix = sum(1 for v in rec_ptrs.values() if v)
    print(f"{with_fix:,} of {len(recs):,} records point to a fix commit "
          f"({len(linked):,} distinct commits; NVD added links for {len(nvd_ptrs):,} CVEs)")
    print(f"queued +{new:,} new commits (queue total {total:,}); "
          f"{off:,} commits of no record set aside as 'unlinked', {on:,} re-linked")
    report(state="done", label="Extraction complete", processed=len(recs), total=len(recs))
    return {"records": len(recs), "with_fix": with_fix, "commits": len(linked), "new": new}
