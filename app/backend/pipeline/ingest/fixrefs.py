"""Extract fix-commit references from OSV and NVD advisory metadata."""
from __future__ import annotations

import re
from urllib.parse import urlparse

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
