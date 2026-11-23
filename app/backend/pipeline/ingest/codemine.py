"""Mine vulnerable/fixed code pairs from security-fix commits.

For every queued fix commit (``codestore.fix_commits``) the miner fetches the
commit and its parent, and for each changed source file stores

* ``code_before`` - the file at the parent commit (the vulnerable version),
* ``code_after``  - the file at the fix commit (the fixed version),
* ``diff``        - the unified diff between them,

plus one pair per changed function/method (``funcsplit``: ``ast`` for Python,
tree-sitter for the other languages), or per changed region (hunk) when no
function changed or the language does not parse. Advisory metadata
(CVE, CWE, CVSS, package, publication date, summary) is joined from the working
store, the parsed OSV dumps and the NVD harvest.

Two fetchers, chosen per commit:

* **GitHub REST API** when ``GITHUB_TOKEN`` is configured (one call lists the files
  and per-file patches; contents come from raw.githubusercontent.com);
* **git** for every host and as the fallback: a cached bare repository per project
  with a shallow, blob-less fetch of only the fix commit and its parent; file
  contents are fetched lazily, so a commit costs kilobytes, not a full clone.

Rows whose before or after side does not exist (file added/removed by the fix) are
kept with ``code_available = 0`` and a ``skip_reason`` rather than dropped, so the
dataset reports how often code is genuinely unavailable.
"""
from __future__ import annotations
import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import nullcontext
from collections import Counter, deque
from dataclasses import dataclass, field
from urllib.parse import quote

import pandas as pd
import requests

from app.backend.pipeline import codestore, oslock, stopping, store
from app.backend.pipeline.ingest import attribution, context as _ctx, pairquality, splitquality
from app.backend.pipeline import languages as langdet
from app.backend.pipeline import ecosystems, funcsplit
from app.backend import compute, paths
from app.backend.pipeline import util

REPOS = pathlib.Path("data/raw/repos")
INTERIM = paths.INTERIM
GITHUB_API = "https://api.github.com"
RAW_GH = "https://raw.githubusercontent.com"
MAX_FILE_BYTES = 200_000

LANG_BY_EXT = {
    ".py": "python", ".pyx": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".vue": "vue", ".svelte": "svelte",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".groovy": "groovy",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp", ".swift": "swift", ".m": "objective-c", ".mm": "objective-c",
    ".sh": "shell", ".bash": "shell", ".pl": "perl", ".pm": "perl", ".lua": "lua",
    ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".dart": "dart", ".r": "r",
    ".sql": "sql",
    ".mts": "typescript", ".cts": "typescript", ".sol": "solidity", ".vy": "vyper",
    ".html": "html", ".htm": "html", ".ejs": "ejs", ".hbs": "handlebars", ".pug": "pug",
}
EXTRACTION_VERSION = "extract-3"
MAX_FUNCTIONS = 20                          # more changed non-test functions than this: a tangled commit
MAX_MERGE_FILES = 20                        # a merge whose tree is its second parent's counts as one fix only when it changes this many files or fewer
_LOCKFILE = re.compile(r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|"
                       r"poetry\.lock|Pipfile\.lock|Gemfile\.lock|composer\.lock|go\.sum|mix\.lock)$")
_REVERT = re.compile(r"^\s*revert\b", re.I)
_MERGE_PR = re.compile(r"^\s*merge (pull request|commit from fork)\b", re.I)
_MERGE_BRANCH = re.compile(r"^\s*merge (branch|remote[- ]tracking branch|tag)\b", re.I)
_RELEASE_SUBJECT = re.compile(r"^\s*(bump|release|prepare (for )?release|version|v?\d+\.\d+(\.\d+)?\b|"
                              r"chore\(release\)|publish)", re.I)


def git_blob_sha(data: bytes) -> str:
    """The git blob id of some content (what ``git hash-object`` prints)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def classify_commit(message: str | None, parent_count: int, paths: list[str],
                    ecosystem: str | None = None, tree_equal: bool | None = None) -> dict:
    """What kind of commit this is and which parent its diff was taken against.

    The diff is always against the first parent. For a pull-request merge that is the net change of
    the PR (accepted). A branch merge, any other multi-parent commit, a revert and a pure
    release/version bump give pairs that are not a clean fix: they are kept but quarantined.

    A merge of two parents whose tree equals its second parent's tree (``tree_equal``) added nothing to what the second parent already had, so its diff against the first parent is the net change of
    the branch that was merged: a fix branch merged into an untouched mainline, whatever the subject says. It is accepted when it changes at most ``MAX_MERGE_FILES`` files. A merge that brings in other
    work (the tree differs), one nothing is known about (``None``) and an octopus merge stay quarantined."""
    msg = message or ""
    first = msg.strip().splitlines()[0] if msg.strip() else ""
    base = {"parent_count": parent_count}
    if parent_count > 1:
        if _MERGE_PR.match(first):
            return {**base, "kind": "merge_pr", "reason": "merge_first_parent_net_diff", "quarantine": None}
        if parent_count == 2 and tree_equal is True and len(paths) <= MAX_MERGE_FILES:
            return {**base, "kind": "merge_pr", "reason": "merge_first_parent_tree_equal", "quarantine": None}
        kind = "merge_branch" if _MERGE_BRANCH.match(first) else "merge_other"
        return {**base, "kind": kind, "reason": "merge_ambiguous_parent", "quarantine": "ambiguous_merge_parent"}
    if _REVERT.match(first) or "this reverts commit" in msg.lower():
        return {**base, "kind": "revert", "reason": "single_parent", "quarantine": "revert_commit"}
    if (_RELEASE_SUBJECT.match(first) and paths
            and all(ecosystems.categorize_path(p, ecosystem, None) in ("manifest", "docs") for p in paths)):
        return {**base, "kind": "release", "reason": "single_parent", "quarantine": "release_commit"}
    return {**base, "kind": "normal", "reason": "single_parent", "quarantine": None}
# Path rules live with the ecosystem adapters; these are the global base every ecosystem inherits.
_SKIP_DIR = ecosystems.COMMON_VENDOR
_MINIFIED = ecosystems.COMMON_GENERATED
_TEST = ecosystems.COMMON_TEST


class FetchError(Exception):
    def __init__(self, msg: str, fatal: bool = False):
        super().__init__(msg)
        self.fatal = fatal          # True: don't retry with another fetcher


class RateLimited(Exception):
    """GitHub refused for rate reasons; ``reset`` is the epoch second to resume at."""
    def __init__(self, reset: float, reason: str = "GitHub API rate limit"):
        super().__init__(f"{reason} (resumes at {time.strftime('%H:%M:%S', time.localtime(reset))})")
        self.reset = float(reset)
        self.reason = reason


def _rate_limit(r) -> RateLimited | None:
    """Primary (quota exhausted) and secondary (abuse) limits on api.github.com, and
    429s on raw.githubusercontent.com."""
    if r.status_code not in (403, 429):
        return None
    if r.headers.get("X-RateLimit-Remaining") == "0":
        return RateLimited(int(r.headers.get("X-RateLimit-Reset") or time.time() + 60),
                           "GitHub API rate limit (hourly quota used up)")
    retry = r.headers.get("Retry-After")
    if retry or r.status_code == 429 or "secondary rate limit" in r.text[:500].lower():
        return RateLimited(time.time() + (int(retry) if retry and retry.isdigit() else 60),
                           "GitHub secondary rate limit")
    return None


@dataclass
class FileChange:
    path: str
    old_path: str
    status: str                     # added | removed | modified | renamed
    patch: str | None = None
    additions: int | None = None
    deletions: int | None = None
    blob_sha: str | None = None     # git blob id of the file after the change (GitHub supplies it)


@dataclass
class Commit:
    sha: str
    parent: str | None
    message: str
    date: str | None
    files: list[FileChange] = field(default_factory=list)
    loader: object = None           # fetcher-specific content loader
    parents: list[str] = field(default_factory=list)    # every parent; ``parent`` is the first
    tree_equal: bool | None = None  # for a two-parent merge: is its tree its second parent's tree (it added nothing to it)? None: not a two-parent merge, or not known
    acquisition: dict | None = None  # recorded metadata transport and exact snapshot/fallback provenance


def language_of(path: str) -> str | None:
    return LANG_BY_EXT.get(pathlib.PurePosixPath(path).suffix.lower())


# â”€â”€ fetchers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class GitHubFetcher:
    def __init__(self, token: str):
        token = (token or '').strip()                       # a trailing CR or newline makes requests refuse the header, and its message repeats the value
        self.s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_maxsize=32)      # the default pool of 10 connections made more workers wait for one
        self.s.mount("https://", adapter)
        self.s.mount("http://", adapter)
        self.s.headers.update({"Accept": "application/vnd.github+json",
                               "Authorization": f"Bearer {token}",
                               "X-GitHub-Api-Version": "2022-11-28"})
        from app.backend.pipeline.acceleration.http_cache import wrap
        self.s = wrap(self.s)
        self.last_quota: dict | None = None

    def quota(self) -> dict | None:
        """Core API quota; /rate_limit itself does not count against it."""
        try:
            r = self.s.get(f"{GITHUB_API}/rate_limit", timeout=(15, 20))
            core = r.json()["resources"]["core"]
            self.last_quota = {"remaining": core["remaining"], "limit": core["limit"],
                               "reset": core["reset"]}
        except Exception:  # ignored: the quota is informational
            pass
        return self.last_quota

    def _track(self, r) -> None:
        if r.headers.get('X-Acceleration-Cache'):
            return
        h = r.headers
        if h.get("X-RateLimit-Remaining", "").isdigit():
            self.last_quota = {"remaining": int(h["X-RateLimit-Remaining"]),
                               "limit": int(h.get("X-RateLimit-Limit") or 0),
                               "reset": int(h.get("X-RateLimit-Reset") or 0)}

    def commit(self, ptr: dict) -> Commit:
        r = self.s.get(f"{GITHUB_API}/repos/{ptr['owner']}/{ptr['repo']}/commits/{ptr['sha']}",
                       timeout=(15, 60))
        self._track(r)
        limited = _rate_limit(r)
        if limited:
            raise limited
        if r.status_code in (403, 429):
            raise FetchError(f"github api {r.status_code}: {_msg(r)}")
        if r.status_code in (404, 422):
            raise FetchError(f"github api {r.status_code}: {_msg(r)}")
        r.raise_for_status()
        c = r.json()
        raw_files = list(c.get("files", []) or [])
        page = 1
        while len(c.get("files", []) or []) >= 300 and page < 10:      # the commit endpoint pages at 300 files
            page += 1
            nxt = self.s.get(f"{GITHUB_API}/repos/{ptr['owner']}/{ptr['repo']}/commits/{ptr['sha']}",
                             params={"page": page, "per_page": 300}, timeout=(15, 60))
            self._track(nxt)
            limited = _rate_limit(nxt)
            if limited:
                raise limited
            nxt.raise_for_status()
            c = {**c, "files": nxt.json().get("files") or []}
            raw_files += c["files"]
        files = []
        for f in raw_files:
            st = {"added": "added", "removed": "removed", "renamed": "renamed"}.get(
                f.get("status"), "modified")
            files.append(FileChange(f["filename"], f.get("previous_filename") or f["filename"],
                                    st, f.get("patch"), f.get("additions"), f.get("deletions"), f.get("sha")))
        parents = [p["sha"] for p in c.get("parents") or []]
        parent = parents[0] if parents else None
        meta = c.get("commit", {})
        loader = _RawLoader(self.s, ptr["owner"], ptr["repo"])
        return Commit(c["sha"], parent, meta.get("message", ""),
                      (meta.get("committer") or {}).get("date"), files, loader, parents, self._tree_equal(ptr, meta, parents))

    def _tree_equal(self, ptr: dict, meta: dict, parents: list[str]) -> bool | None:
        """For a two-parent merge, whether its tree is its second parent's (one more request); None for any other commit or when the lookup fails."""
        tree = (meta.get("tree") or {}).get("sha")
        if len(parents) != 2 or not tree:
            return None
        try:
            r = self.s.get(f"{GITHUB_API}/repos/{ptr['owner']}/{ptr['repo']}/git/commits/{parents[1]}", timeout=(15, 60))
            self._track(r)
            if r.status_code != 200:
                return None
            other = (r.json().get("tree") or {}).get("sha")
        except requests.RequestException:       # ignored: not decided, the merge stays quarantined
            return None
        return None if not other else other == tree


_PROFILES: dict[str, dict] = {}            # repository -> language shares (per process)
_PROFILES_LOCK = threading.Lock()


class _RawLoader:
    def __init__(self, session, owner, repo):
        self.s, self.owner, self.repo = session, owner, repo
        self.repository_facts = getattr(session, 'repository_facts', None)

    def repo_profile(self, sha: str) -> dict[str, float]:
        """GitHub's language statistics of the repository (one API call per repo)."""
        if self.repository_facts is not None:
            return self.repository_facts.profile(self.owner, self.repo)
        key = f"github.com/{self.owner}/{self.repo}"
        with _PROFILES_LOCK:
            if key in _PROFILES:
                return _PROFILES[key]
        r = self.s.get(f"{GITHUB_API}/repos/{self.owner}/{self.repo}/languages", timeout=(15, 60))
        limited = _rate_limit(r)
        if limited:
            raise limited
        prof = (langdet.profile_from_github(r.json(), set(LANG_BY_EXT.values()))
                if r.status_code == 200 else {})
        with _PROFILES_LOCK:
            _PROFILES[key] = prof
        return prof

    def content(self, ref: str, path: str) -> bytes | None:
        r = self.s.get(f"{RAW_GH}/{self.owner}/{self.repo}/{ref}/{quote(path, safe='/')}",
                       timeout=(15, 60))
        if r.status_code == 404:
            return None
        limited = _rate_limit(r) if r.status_code == 429 else None
        if limited:
            raise limited
        r.raise_for_status()
        return r.content

    def diff(self, parent, sha, old_path, path) -> str | None:
        return None                 # GitHub supplies the per-file patch


class _RepoLock:
    """The lock of one repository: a thread lock for the threads of this process and an operating-system file lock for the other processes (two campaigns that mine
    commits of one repository at the same time must not fetch into its shallow clone together)."""
    WAIT = 1800.0                   # seconds to wait for another process's git operation (a history fetch is allowed 900)

    def __init__(self, repo_dir):
        self._thread = threading.Lock()
        self._file = oslock.FileLock(pathlib.Path(str(repo_dir) + ".lock"), note=False)

    def __enter__(self):
        from app.backend.pipeline.acceleration import operations
        if operations.current():
            while not self._thread.acquire(timeout=operations.timeout(.2)):
                operations.check()
        else:
            self._thread.acquire()
        try:
            if operations.current():
                deadline = time.monotonic()+operations.timeout(self.WAIT)
                while True:
                    operations.check()
                    try:
                        self._file.acquire(wait=min(.2, operations.timeout(self.WAIT)))
                        break
                    except oslock.Busy:
                        if time.monotonic() >= deadline: raise
            else:
                self._file.acquire(wait=self.WAIT)
        except oslock.Busy as e:
            self._thread.release()
            raise FetchError(f"the repository is in use by another process ({e})")
        except BaseException:
            self._thread.release()
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            self._file.release()
        finally:
            self._thread.release()


_REPO_LOCKS: dict[str, _RepoLock] = {}
_LOCKS_GUARD = threading.Lock()


def _repo_lock(repo_dir) -> _RepoLock:
    """Git operations on one repository are serialised, across threads and across processes (concurrent fetches into the same shallow repo corrupt its shallow
    file); different repositories still run in parallel."""
    with _LOCKS_GUARD:
        return _REPO_LOCKS.setdefault(str(repo_dir), _RepoLock(repo_dir))


class GitFetcher:
    """Shallow, blob-less fetch of one commit + parent into a cached bare repo."""

    def __init__(self, *, batched=False):
        self.batched = batched

    def _repo(self, ptr: dict) -> pathlib.Path:
        d = REPOS / ptr["host"] / ptr["owner"] / f"{ptr['repo']}.git"
        if (d / "HEAD").exists():
            config = _local_git_config(d)
            if config is not None and config.has_option('remote "origin"', 'url'):
                return d
        # Setup can also be requested outside commit acquisition. Serialize both
        # initial creation and repair, then recheck after the other owner exits.
        with _repo_lock(d):
            return self._setup_repo(ptr, d)

    def _setup_repo(self, ptr: dict, d: pathlib.Path) -> pathlib.Path:
        if not (d / "HEAD").exists():
            d.mkdir(parents=True, exist_ok=True)
            _git(["init", "-q", "--bare", str(d)])
            _git(["-C", str(d), "remote", "add", "origin", ptr["repo_url"]])
        else:
            config=_local_git_config(d)
            # Normal ready caches need no extra Git process. A HEAD left by an
            # interrupted init is not proof that remote setup also completed.
            if config is not None and config.has_option('remote "origin"','url'):
                return d
            try:
                _git(["-C",str(d),"remote","get-url","origin"])
            except FetchError as error:
                if str(error).strip()!="error: No such remote 'origin'":raise
                try:_git(["-C",str(d),"remote","add","origin",ptr["repo_url"]])
                except FetchError:
                    # Another protected setup caller may have finished first.
                    # Do not replace an existing origin or reinitialize objects.
                    current=_local_git_config(d)
                    if current is None or not current.has_option('remote "origin"','url'):raise
        return d

    def commit(self, ptr: dict) -> Commit:
        d = self._repo(ptr)
        with _repo_lock(d):
            return self._commit(ptr, repo_dir=d)

    def _commit(self, ptr: dict, *, repo_dir=None) -> Commit:
        d = repo_dir if repo_dir is not None else self._repo(ptr)
        sha = ptr["sha"]
        from app.backend.pipeline.acceleration import mining_policy
        if mining_policy.current().get('version') in (3, 4):
            from app.backend.pipeline.acceleration.repository_service import reference_commit
            shared = reference_commit(self, ptr, d)
            if shared is not None:
                return shared
        cached = False
        if self.batched and re.fullmatch(r'[0-9a-f]{40}(?:[0-9a-f]{24})?', sha):
            try:
                for revision in (sha, sha + '^1'):
                    _git(['-C', str(d), 'ls-tree', '-r', '--name-only', revision], no_lazy=True)
                cached = True
                from app.backend.pipeline.acceleration.metrics import current
                current.count('git_commit_cache_hits')
            except FetchError:
                pass  # A missing tree or shallow boundary still requires depth-two hydration.
        try:
            if not cached:
                _git(["-C", str(d), "-c", "protocol.version=2", "fetch", "-q", "--depth=2",
                      "--filter=blob:none", "origin", sha], timeout=240)
        except FetchError as e:
            if len(sha) == 40:
                raise FetchError(f"git fetch failed: {e}", fatal=True)
            # Short SHAs can't be fetched by id; fetch history (no blobs) and resolve.
            _git(["-C", str(d), "fetch", "-q", "--filter=blob:none", "origin"], timeout=900)
        full = _git(["-C", str(d), "rev-parse", f"{sha}^{{commit}}"]).strip()
        parents = _git(["-C", str(d), "rev-list", "--parents", "-n", "1", full]).split()[1:]
        parent = parents[0] if parents else None
        message = _git(["-C", str(d), "log", "-1", "--format=%B", full])
        date = _git(["-C", str(d), "log", "-1", "--format=%cI", full]).strip() or None
        files: list[FileChange] = []
        if parent:
            out = _git(["-C", str(d), "diff-tree", "-r", "-M", "--no-commit-id",
                        "--name-status", parent, full])
            for line in out.splitlines():
                parts = line.split("\t")
                code = parts[0]
                if code.startswith("R") and len(parts) == 3:
                    files.append(FileChange(parts[2], parts[1], "renamed"))
                elif len(parts) >= 2:
                    st = {"A": "added", "D": "removed"}.get(code[:1], "modified")
                    files.append(FileChange(parts[1], parts[1], st))
        return Commit(full, parent, message, date, files, _GitLoader(d), parents, tree_equal_of(d, full) if len(parents) == 2 else None)


def tree_equal_of(bare: pathlib.Path, merge_sha: str) -> bool | None:
    """Is the tree of a two-parent merge the tree of its second parent? Both commit objects are in a repository fetched at depth 2, so nothing is downloaded. None when git cannot say."""
    try:
        parents = _git(["-C", str(bare), "rev-list", "--parents", "-n", "1", merge_sha]).split()[1:]
        if len(parents) != 2:
            return None
        trees = _git(["-C", str(bare), "rev-parse", f"{merge_sha}^{{tree}}", f"{parents[1]}^{{tree}}"]).split()
    except FetchError:                          # ignored: not decided, the merge stays quarantined
        return None
    return len(trees) == 2 and trees[0] == trees[1]


class _GitLoader:
    def __init__(self, repo_dir: pathlib.Path):
        self.d = str(repo_dir)
        self.lock = _repo_lock(repo_dir)

    def repo_profile(self, sha: str) -> dict[str, float]:
        """Source files per language in the repository tree at the fix commit (the
        blob-less fetch already holds every tree, so nothing is downloaded)."""
        try:
            with self.lock:
                out = _git(["-C", self.d, "ls-tree", "-r", "--name-only", sha], timeout=180)
        except FetchError:
            return {}
        paths = [p for p in out.splitlines()
                 if not (_SKIP_DIR.search(p) or _MINIFIED.search(p) or _TEST.search(p))]
        return langdet.profile_from_paths(paths, language_of)

    def content(self, ref: str, path: str) -> bytes | None:
        from app.backend.pipeline.acceleration import operations
        try:
            with self.lock:                  # may lazily fetch the blob into the repo
                from app.backend.pipeline.acceleration.git_objects import read
                try:
                    content = read(self.d, ref, path, limit=2**63-1)
                    if content is not None:
                        return content
                except operations.DeadlineExpired:
                    raise
                except OSError:
                    pass
                # Missing promisor objects still use the original explicit lazy fetch.
                from app.backend.pipeline.acceleration import mining_policy, operations
                if mining_policy.optimized():
                    from app.backend.pipeline.acceleration.bulk_git import _network
                    with operations.acquire(_network):
                        return _git(['-C', self.d, 'cat-file', '-p', f'{ref}:{path}'], text=False, timeout=180)
                return _git(["-C", self.d, "cat-file", "-p", f"{ref}:{path}"], text=False,
                            timeout=180)
        except FetchError as error:
            # A failed lazy fetch is not evidence that a pinned path is absent.
            # Let the bounded stage retry rebuild its loader and admission state.
            operations.check()
            from app.backend.pipeline.acceleration.failures import retryable
            if retryable(error):
                raise
            return None

    def diff(self, parent, sha, old_path, path) -> str | None:
        try:
            with self.lock:
                return _git(["-C", self.d, "diff", "-M", parent, sha, "--", old_path, path],
                            timeout=180)
        except FetchError:
            return None


class _BatchedGitLoader(_GitLoader):
    def __init__(self, repo_dir, profile_loader):
        super().__init__(repo_dir)
        from app.backend.pipeline.acceleration.bulk_git import Repository
        self.repository, self.profile_loader = Repository(repo_dir), profile_loader
        self.fallback = False

    def prefetch(self, refs):
        from app.backend.pipeline.acceleration import operations
        if self.fallback:
            return
        try:
            return self.repository.prefetch(refs)
        except operations.DeadlineExpired:
            raise
        except (FetchError, OSError) as error:
            from app.backend.pipeline.acceleration import operations
            operations.check()
            from app.backend.pipeline.acceleration.metrics import current
            self.fallback = True
            current.count('git_transport_fallbacks')
            print(f'Batch Git transport unavailable; using the original pinned-object transport: {str(error)[:180]}')

    def content(self, revision, path):
        self.prefetch([(revision, path)])
        return super().content(revision, path) if self.fallback else self.repository.read(revision, path)

    def repo_profile(self, sha):
        return self.profile_loader.repo_profile(sha)


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Never wait for a password: no credential helper, no prompt, no askpass GUI.
_GIT_CFG = ["-c", "credential.helper=", "-c", "core.askPass=", "-c", "credential.interactive=false"]
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_ASKPASS": "",
            "SSH_ASKPASS": "", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes"}


def _kill_tree(p: subprocess.Popen) -> None:
    """Kill git and its helpers (git fetch spawns git-remote-https, which inherits
    the pipes; killing only git leaves it running and the pipes open)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                           timeout=30, creationflags=_NO_WINDOW)
        else:
            os.killpg(p.pid, 9)
    except Exception:  # ignored: if the process-tree kill fails, kill the process itself (next line)
        p.kill()


def _local_git_config(path):
    import configparser
    try:
        with (pathlib.Path(path)/'config').open('rb') as stream:raw=stream.read((1<<20)+1)
    except FileNotFoundError:return None
    if len(raw)>1<<20:return None
    config=configparser.RawConfigParser(strict=False)
    try:config.read_string(raw.decode('utf-8-sig'))
    except (UnicodeError,configparser.Error):return None
    return config


def _owned_bare_directory(path):
    """Positive local setup proof for a narrow Windows retry, never acquisition."""
    import stat
    path=pathlib.Path(path)
    try:
        root=REPOS.resolve();relative=path.resolve().relative_to(root)
        if len(relative.parts)!=3 or path.suffix!='.git':return False
        for part in (path,*list(path.parents)[:3],path/'HEAD',path/'config',path/'objects'):
            info=part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0):return False
        if not (path/'HEAD').is_file() or not (path/'objects').is_dir():return False
        config=_local_git_config(path)
        return config is not None and config.getboolean('core','bare',fallback=False)
    except (OSError,ValueError):return False


def _git(args: list[str], text: bool = True, timeout: int = 120, *, input_data: bytes | None = None, no_lazy: bool = False, _network_bounded=False):
    """Run git with a hard timeout. subprocess.run(timeout=...) is not enough: on
    Windows it waits for the output pipes after killing git, and a lingering
    git-remote-https keeps them open, so a stuck fetch froze mining forever."""
    from app.backend.pipeline.acceleration import operations
    from app.backend.pipeline.acceleration import mining_policy
    if 'fetch' in args and mining_policy.optimized() and not _network_bounded:
        from app.backend.pipeline.acceleration.bulk_git import _network
        from app.backend.pipeline.acceleration.metrics import current
        from contextlib import ExitStack
        with ExitStack() as stack:
            with current.timer('git_fetch_queue_wait'):
                stack.enter_context(operations.acquire(_network))
            for attempt in range(2):
                try:
                    current.count('git_fetch_commands')
                    with current.timer('git_fetch_service'):
                        return _git(args,text=text,timeout=timeout,input_data=input_data,no_lazy=no_lazy,_network_bounded=True)
                except FetchError as error:
                    transient = any(text in str(error).lower() for text in ('failed to connect','could not resolve host','connection reset','connection timed out','rpc failed','early eof','unexpected disconnect'))
                    if attempt or not transient: raise
                    from app.backend.pipeline.acceleration.metrics import current
                    current.count('git_network_retries')
                    print('Transient Git connection failed; retrying the same pinned fetch')
                    time.sleep(operations.timeout(.25))
    if mining_policy.optimized():
        from app.backend.pipeline.acceleration.disk_budget import check as disk_check
        disk_check(fresh=True)
    timeout = operations.timeout(timeout)
    env = {**os.environ, **_GIT_ENV}
    if no_lazy:
        env['GIT_NO_LAZY_FETCH'] = '1'
    from app.backend.pipeline.acceleration.processes import spawn
    launch = spawn if operations.current() else subprocess.Popen
    limits = ['-c','pack.threads=1'] if mining_policy.optimized() else []
    from app.backend.pipeline.acceleration.memory_profile import current as memory_profile
    limits = [*limits, *memory_profile().git_flags]      # a smaller memory tier also caps git's own pack windows and caches
    p = launch(["git", *_GIT_CFG, *limits, *args], stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                         creationflags=_NO_WINDOW if os.name == "nt" else 0,
                         start_new_session=os.name != "nt")
    try:
        with operations.resource(lambda: _stop_git(p)):
            if operations.current():
                deadline, first = time.monotonic()+timeout, True
                while True:
                    operations.check()
                    left = deadline-time.monotonic()
                    if left <= 0: raise subprocess.TimeoutExpired('git', timeout)
                    try:
                        kwargs = {'input':input_data} if first and input_data is not None else {}
                        first = False
                        out, err = p.communicate(timeout=min(.25, left), **kwargs)
                        operations.check()
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() >= deadline: raise
            else:
                out, err = p.communicate(input=input_data, timeout=timeout) if input_data is not None else p.communicate(timeout=timeout)
            # Keep the closer registered until the entire owned job is gone,
            # including descendants left after the Git parent exits normally.
            if getattr(p,'_owned_job',None): _stop_git(p)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        try:
            p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise FetchError(f"git {args[2] if len(args) > 2 else args[0]} timed out after {timeout}s")
    if p.returncode != 0:
        message=('\n'.join(err.decode("utf-8", "replace").strip().splitlines()[-5:])
                 if err.strip() else f"git exited {p.returncode}")
        if (mining_policy.current().get('version')==4 and operations.current()
            and (re.fullmatch(rb'fatal: Out of memory, \(tried to allocate [0-9]+ bytes\)\r?\n?',err)
                or os.name=='nt' and re.fullmatch(
                    rb"fatal: unable to access 'https?://[^'\r\n]+': schannel: (?:next )?InitializeSecurityContext failed: SEC_E_INSUFFICIENT_MEMORY \(0x80090300\) - [^\r\n]*\r?\n?",err))):
            from app.backend.pipeline.acceleration.failures import LocalGitMemoryError
            raise LocalGitMemoryError(message)
        if (os.name=='nt' and mining_policy.current().get('version')==4 and len(args)>1 and args[0]=='-C'
            and message.lower().startswith('fatal: cannot change to ')
            and message.lower().endswith(': invalid argument') and _owned_bare_directory(args[1])):
            from app.backend.pipeline.acceleration.failures import LocalGitUnavailable
            raise LocalGitUnavailable(message)
        raise FetchError(message)
    return out.decode("utf-8", "replace") if text else out


def _stop_git(process):
    from app.backend.pipeline.acceleration.processes import terminate
    terminate(process)


def _msg(r) -> str:
    try:
        return str(r.json().get("message", ""))[:160]
    except Exception:  # ignored: a non-JSON error body is shown as plain text (next line)
        return r.text[:160]


# â”€â”€ advisory metadata â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_ADV_COLS = ["osv_id", "cve_id", "published", "osv_cwe", "cwe", "summary", "details",
             "ecosystem", "package", "cvss_vector"]
_NVD_COLS = ["cve_id", "nvd_published", "cwe", "nvd_description"]


def _read_columns(path: pathlib.Path, wanted: list[str]) -> pd.DataFrame:
    """Read only the metadata columns that exist (325k NVD rows stay small)."""
    import pyarrow.parquet as papq
    have = set(papq.read_schema(path).names)
    return pd.read_parquet(path, columns=[c for c in wanted if c in have])


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _adapters():
    return ecosystems.ADAPTERS


class _RowIndex:
    """A dict-of-dicts view over an indexed DataFrame: a lookup builds its small dict only for the one row actually asked for, instead of exploding every row into a
    Python object up front - the resident cost MetaIndex used to pay was dominated by that explosion, not by the DataFrame underneath it."""

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def __contains__(self, key) -> bool:
        return key in self._df.index

    def __getitem__(self, key) -> dict:
        row = self._df.loc[key]
        if isinstance(row, pd.DataFrame):            # a duplicate index slipped through: the first row is what a dict assignment would have kept
            row = row.iloc[0]
        return row.to_dict()

    def get(self, key, default=None):
        return self[key] if key in self else default

    def keys(self):
        return iter(self._df.index)

    def __iter__(self):
        return iter(self._df.index)

    def __len__(self) -> int:
        return len(self._df.index)

    def __eq__(self, other) -> bool:
        return dict(self) == (dict(other) if isinstance(other, _RowIndex) else other)

    def __repr__(self) -> str:
        return f"_RowIndex({dict(self)!r})"


def _concat_keep_first(frames: list[pd.DataFrame], subset: str) -> pd.DataFrame:
    """Each frame deduped on its own first, then concatenated in the given order and deduped again, keeping the first occurrence: the same winner a
    ``dict.setdefault`` loop over these same frames, in this same order, would keep."""
    cleaned = [df.drop_duplicates(subset) for df in frames if len(df) and subset in df.columns]
    if not cleaned:
        return pd.DataFrame(columns=[subset])
    return pd.concat(cleaned, ignore_index=True).drop_duplicates(subset, keep="first")


class MetaIndex:
    """Advisory/CVE metadata joined onto pairs (store + parsed OSV dumps + NVD). With ``ecosystems`` only those of the ecosystems are read (a campaign's miner
    needs a fraction of the memory the whole index does)."""

    def __init__(self, ecosystems: list[str] | None = None):
        frames = []
        adapter_slugs = {_slug(a.name) for a in _adapters()}
        if ecosystems is not None:
            adapter_slugs &= {_slug(e) for e in ecosystems}
        for p in sorted(INTERIM.glob("osv_*.parquet")):
            if p.stem[4:] not in adapter_slugs:      # a table of an ecosystem without an adapter (Ubuntu, Debian, ...) is for searching; mining never meets its advisories
                continue
            try:
                frames.append(_read_columns(p, _ADV_COLS))
            except Exception:  # ignored: an unreadable interim parquet file is skipped
                pass
        try:
            frames.append(store.to_dataframe(ecosystems=ecosystems))
        except Exception:  # ignored: the working store may not exist yet
            pass
        adv_df = _concat_keep_first(frames, "osv_id")
        self.adv = _RowIndex(adv_df.set_index("osv_id", drop=False))
        if "cve_id" in adv_df.columns:
            by_cve_df = adv_df[adv_df["cve_id"].map(_present)].drop_duplicates("cve_id", keep="first")
        else:
            by_cve_df = adv_df.assign(cve_id=[])
        self.by_cve = _RowIndex(by_cve_df.set_index("cve_id", drop=False))
        self.nvd = _RowIndex(pd.DataFrame(columns=["cve_id"]).set_index("cve_id"))
        nvd_path = INTERIM / "nvd.parquet"
        if nvd_path.exists():
            nvd_df = _read_columns(nvd_path, _NVD_COLS)
            if len(nvd_df) and "cve_id" in nvd_df.columns:
                self.nvd = _RowIndex(nvd_df.drop_duplicates("cve_id", keep="last").set_index("cve_id", drop=False))

    def for_commit(self, row: dict) -> dict:
        adv_ids = [a for a in json.loads(row.get("advisory_ids") or "[]") if a]
        cve_ids = [c for c in json.loads(row.get("cve_ids") or "[]") if c]
        recs = [self.adv[a] for a in adv_ids if a in self.adv]
        recs += [self.by_cve[c] for c in cve_ids if c in self.by_cve and self.by_cve[c] not in recs]
        nvds = [self.nvd[c] for c in cve_ids if c in self.nvd]

        def first(key, rows):
            return next((r.get(key) for r in rows if _present(r.get(key))), None)

        dates = [d for d in [*(r.get("published") for r in recs),
                             *(r.get("nvd_published") for r in nvds)] if _present(d)]
        cwes = []
        for r in [*recs, *nvds]:
            for key in ("osv_cwe", "cwe"):
                if _present(r.get(key)):
                    for c in str(r[key]).split(";"):
                        c = c.strip()
                        if c.startswith("CWE-") and c not in cwes:
                            cwes.append(c)
        summary = first("summary", recs)
        details = first("details", recs) or first("nvd_description", nvds)
        explanation = _ctx.advisory_text(summary, details) or None      # the one normaliser
        return {
            "cve_id": cve_ids[0] if cve_ids else None,
            "cve_ids": json.dumps(cve_ids),
            "advisory_ids": json.dumps(adv_ids),
            "ghsa_id": next((a for a in adv_ids if str(a).startswith("GHSA-")), None),
            "ecosystem": first("ecosystem", recs),
            "package": first("package", recs),
            "cwe": ";".join(cwes) or None,
            "cvss_vector": first("cvss_vector", recs),
            "published": min(map(str, dates)) if dates else None,
            "explanation": explanation[:4000] if explanation else None,
        }


def _present(v) -> bool:
    return v is not None and not (isinstance(v, float) and pd.isna(v)) and str(v) != ""


# â”€â”€ pair construction â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _decode(data: bytes | None) -> tuple[str | None, str | None]:
    """-> (text, problem)."""
    if data is None:
        return None, None
    from app.backend.pipeline.acceleration.staged_mining import ExcludedSource
    if isinstance(data, ExcludedSource):
        return None, data.reason
    if len(data) > MAX_FILE_BYTES:
        return None, "too_large"
    if b"\0" in data[:8000]:
        return None, "binary"
    return data.decode("utf-8", "replace"), None


def _hash(before: str | None, after: str | None) -> str:
    return hashlib.sha1(f"{util.norm_code(before)}\0{util.norm_code(after)}".encode()).hexdigest()


def _udiff(before: str, after: str, a: str, b: str) -> str:
    return util.udiff(before, after, a, b)


@dataclass
class MineResult:
    status: str                     # mined | no_code | skipped
    reason: str | None
    commit: Commit | None
    pairs: list[dict]
    langs: dict = field(default_factory=dict)   # source languages changed: {lang: files}
    cve_langs: list = field(default_factory=list)   # the CVE's language(s) mined
    note: str = ""                  # how the language was decided (for the log)
    fix_check: tuple | None = None  # (verdict, reason, link, package) from fixcheck.check_commit
    files: list = field(default_factory=list)   # every changed file with its category (commit_files)
    kind: dict | None = None        # the commit's kind and parent decision (classify_commit)


def _file_rows(row: dict, commit: Commit, eco: str | None) -> list[dict]:
    """Every changed file of the commit with its category; the patch is kept only for
    configuration and manifest files (never for lock files) as evidence."""
    out = []
    for f in commit.files:
        lang = language_of(f.path)
        cat = ecosystems.categorize_path(f.path, eco, lang)
        keep = cat in ("config", "manifest") and not _LOCKFILE.search(f.path) and f.patch
        out.append({"commit_key": row["commit_key"], "path": f.path, "old_path": f.old_path, "status": f.status,
                    "language": lang, "category": cat, "additions": f.additions, "deletions": f.deletions,
                    "blob_sha": f.blob_sha, "patch": f.patch[:8000] if keep else None})
    return out


def build_pairs(row: dict, commit: Commit, meta: dict, max_files: int,
                max_functions: int = MAX_FUNCTIONS, cpu_pool=None) -> MineResult:
    """Pairs of one fix commit, plus its file list and its kind. See ``_build_pairs``."""
    eco = meta.get("ecosystem")
    res = _build_pairs(row, commit, meta, max_files, max_functions, cpu_pool=cpu_pool)
    res.files = _file_rows(row, commit, eco)
    n = len(commit.parents) or (1 if commit.parent else 0)
    res.kind = classify_commit(commit.message, n, [f.path for f in commit.files], eco, commit.tree_equal)
    return res


class AnalyzedFiles(list):
    pass


def _analyze_files(items: list[tuple[dict, str | None]]) -> list[tuple[dict, list[dict], "funcsplit.Analysis | None"]]:
    """The CPU-bound half of mining a commit's changed files - tree-sitter parsing and AST-diffing (``sub_pairs_with_analysis``) - for every file whose
    ``code_before``/``code_after`` is already fetched (no I/O happens here). Picklable and top-level so a ``compute.Pool`` can run it in a worker process,
    off the thread that is fetching other commits, instead of sharing one core's GIL with all of their parsing too."""
    from app.backend.pipeline.acceleration.metrics import current
    before, cpu_start = current.snapshot(), time.process_time()
    with current.timer('parser_work'):
        result = AnalyzedFiles((rec, *sub_pairs_with_analysis(rec, old_path)) for rec, old_path in items
                               if not funcsplit.is_noop(rec.get("language"), rec["code_before"], rec["code_after"]))
    after = current.snapshot()
    result.performance = {'counts':{k:v-before['counts'].get(k,0) for k,v in after['counts'].items()},
        'timings':{k:{'calls':v['calls']-before['timings'].get(k,{}).get('calls',0),
                      'seconds':v['seconds']-before['timings'].get(k,{}).get('seconds',0)} for k,v in after['timings'].items()}}
    result.performance['counts']['parser_cpu_seconds'] = time.process_time()-cpu_start
    return result


def _build_pairs(row: dict, commit: Commit, meta: dict, max_files: int,
                 max_functions: int = MAX_FUNCTIONS, cpu_pool=None) -> MineResult:
    eco = meta.get("ecosystem")                  # path rules (tests, vendored, generated) follow the ecosystem
    n_parents = len(commit.parents) or (1 if commit.parent else 0)
    kind = classify_commit(commit.message, n_parents, [f.path for f in commit.files], eco, commit.tree_equal)
    src = [f for f in commit.files if language_of(f.path)]
    langs = dict(Counter(language_of(f.path) for f in src).most_common())
    if not commit.parent:
        return MineResult("no_code", "root_commit", commit, [], langs)
    if not src:
        return MineResult("no_code", "no_source_files", commit, [], langs)

    # The CVE's language: set by the user on the record, else detected from git.
    expected = [l for l in json.loads(row.get("expected_langs") or "[]") if l]
    if expected:
        cve_langs, note = expected, "set on the record"
    else:
        core = [f for f in src if not (ecosystems.is_test_path(f.path, eco)
                                       or ecosystems.is_vendored_path(f.path, eco)
                                       or ecosystems.is_generated_path(f.path, eco))]
        changed = dict(Counter(language_of(f.path) for f in (core or src)))
        profile = commit.loader.repo_profile(commit.sha) if hasattr(commit.loader, "repo_profile") else {}
        cve_langs = langdet.cve_languages(changed, profile)
        note = f"repo: {langdet.describe(profile)}"
    if expected:        # a language set on the record narrows the files; otherwise every language is mined
        src = [f for f in src if language_of(f.path) in cve_langs]
    if not src:
        changes = ", ".join(f"{l} {n}" for l, n in langs.items())
        return MineResult("no_code", f"no {'/'.join(cve_langs)} files changed (changes: {changes})",
                          commit, [], langs, cve_langs, note)
    if len(src) > max_files:
        return MineResult("skipped", f"too_many_files ({len(src)})", commit, [], langs,
                          cve_langs, note)

    base = {**meta, "commit_key": row["commit_key"], "repo_url": row["repo_url"],
            "fix_commit": commit.sha, "parent_commit": commit.parent,
            "commit_date": commit.date, "commit_message": (commit.message or "").strip()[:4000],
            "source": ",".join(json.loads(row.get("sources") or '["osv"]')),
            "parent_count": kind["parent_count"], "parent_selection_reason": kind["reason"],
            "quarantine_reason": kind["quarantine"], "extraction_version": EXTRACTION_VERSION}
    prefetch = getattr(commit.loader, 'prefetch', None)
    if prefetch is not None:
        from app.backend.pipeline.acceleration import operations
        operations.phase('fetching_code', total=len(src), unit='changed files')
        refs = []
        for f in src:
            if ecosystems.is_vendored_path(f.path, eco) or ecosystems.is_generated_path(f.path, eco):
                continue
            if f.status != 'added':
                refs.append((commit.parent, f.old_path))
            if f.status != 'removed':
                refs.append((commit.sha, f.path))
        prefetch(refs)
    pairs: list[dict] = []
    linkable: list[dict] = []                     # the file-level pairs that were split, and what the split found, for the cross-file pass
    analyses: list = []
    to_analyze: list[tuple[dict, str | None]] = []     # files whose content is fetched and that still need the CPU-bound parsing/AST-diff step
    for f in src:
        lang = language_of(f.path)
        rec = {**base, "file_path": f.path, "language": lang, "granularity": "file",
               "function_name": None, "is_test": int(ecosystems.is_test_path(f.path, eco)), "tangled": 0,
               "old_path": f.old_path, "pair_key": f"{row['commit_key']}:{f.path}:*"}
        if ecosystems.is_vendored_path(f.path, eco) or ecosystems.is_generated_path(f.path, eco):
            pairs.append({**rec, "code_available": 0,
                          "skip_reason": "vendored_or_generated"})
            continue
        loader = commit.loader
        before_raw = None if f.status == "added" else loader.content(commit.parent, f.old_path)
        after_raw = None if f.status == "removed" else loader.content(commit.sha, f.path)
        before, p1 = _decode(before_raw)
        after, p2 = _decode(after_raw)
        problem = p1 or p2
        if problem:
            pairs.append({**rec, "code_available": 0, "skip_reason": problem})
            continue
        diff = (f"--- a/{f.old_path}\n+++ b/{f.path}\n{f.patch}" if f.patch
                else _udiff(before or "", after or "", f"a/{f.old_path}", f"b/{f.path}"))     # before/after are already fetched (two lines up): a git
                                                                                                # subprocess diff would only repeat that, locked behind the same per-repo _RepoLock
        rec.update(code_before=before, code_after=after, diff=diff,
                   before_blob_sha=git_blob_sha(before_raw) if before_raw is not None else None,
                   after_blob_sha=git_blob_sha(after_raw) if after_raw is not None else None)
        if before is None or after is None:
            pairs.append({**rec, "code_available": 0,
                          "skip_reason": "file_added" if before is None else "file_removed"})
            continue
        if before == after:
            continue                                  # identical bytes need no parser worker
        to_analyze.append((rec, f.old_path))
    # Every file above is fetched (I/O done, on this thread); the parsing/AST-diff of all of them now runs as one unit, in a worker process when one is given
    # (so it does not share this commit's own thread, or any other commit's, with the GIL while it parses) - or right here when it is not (tests, resplit-style
    # direct calls, a single-core machine).
    from app.backend.pipeline.acceleration import operations
    operations.phase('parsing', total=len(to_analyze), unit='files')
    analyzed = (cpu_pool.submit(_analyze_files, to_analyze) if cpu_pool is not None else _analyze_files(to_analyze)) if to_analyze else []
    if cpu_pool is not None and getattr(cpu_pool, '_pool', None) is not None and hasattr(analyzed, 'performance'):
        from app.backend.pipeline.acceleration.metrics import current
        current.merge(analyzed.performance)
    for rec, subs, analysis in analyzed:
        file_row = {**rec, "code_available": 1, "skip_reason": None, "content_hash": _hash(rec["code_before"], rec["code_after"])}
        pairs.append(file_row)
        pairs += subs
        linkable.append(file_row)
        analyses.append(analysis)
    pairs += cross_file_pairs(linkable, analyses)       # a function that left one file of the commit and arrived in another
    attribution.assign(pairs, meta.get("explanation"), commit.message)
    usable = [p for p in pairs if p.get("code_available")]
    if not usable:
        return MineResult("no_code", "no_usable_pair", commit, pairs, langs, cve_langs, note)
    # A "fix" that changes very many functions is usually tangled with a refactor or
    # reformat; its pairs are kept but flagged and excluded from training by default. Test
    # functions do not count and are never flagged: they change with the code they test.
    if sum(p["granularity"] == "function" and not p["is_test"] for p in usable) > max_functions:
        for p in usable:
            if not p["is_test"]:
                p["tangled"] = 1
        return MineResult("mined", "tangled_commit", commit, pairs, langs, cve_langs, note)
    return MineResult("mined", None, commit, pairs, langs, cve_langs, note)


def sub_pairs(file_pair: dict, old_path: str | None = None, policy: splitquality.Policy | None = None) -> list[dict]:
    """Function / hunk pairs derived from a usable file-level pair (funcsplit); see ``sub_pairs_with_analysis``."""
    return sub_pairs_with_analysis(file_pair, old_path, policy)[0]


def sub_pairs_with_analysis(file_pair: dict, old_path: str | None = None, policy: splitquality.Policy | None = None):
    """Shared pure implementation; the legacy public function remains patchable."""
    from app.backend.pipeline.acceleration.parser_tasks import sub_pairs_with_analysis as analyze
    return analyze(file_pair, old_path, policy)


def cross_file_pairs(file_pairs: list[dict], analyses: list, policy: splitquality.Policy | None = None) -> list[dict]:
    """Pairs for the functions of one commit that left one file and arrived in another (``funcsplit.link_cross_file``), and a note of each link in the meta of the two file-level pairs
    (``extraction_meta.cross_file_links``, updated in place). A move that left the code as it was gives no pair. The pair belongs to the file the function arrived in (``old_path`` is
    the file it left), is stored under the key ``<file>:<name>~from~<old file>``, and is quarantined for review by default (the policy's ``cross_file``): pairing across files is a
    stronger claim than pairing within one."""
    policy = policy or splitquality.default_policy()
    kept = [(fp, an) for fp, an in zip(file_pairs, analyses) if an is not None]
    if len(kept) < 2:
        return []
    entries = [{"path": fp["file_path"], "old_path": fp.get("old_path") or fp["file_path"], "lang": fp.get("language") or "", "analysis": an} for fp, an in kept]
    links = funcsplit.link_cross_file(entries)
    out: list[dict] = []
    notes: dict[int, list[dict]] = {}
    for link in links:
        if link["kind"] != "cross_file_move":
            note = {k: link[k] for k in ("kind", "before", "after", "before_paths", "after_paths", "confidence")}
            for path in set(link["before_paths"]) | set(link["after_paths"]):
                for i, e in enumerate(entries):
                    if path in (e["path"], e["old_path"]):
                        notes.setdefault(i, []).append(note)
            continue
        rem, add = link["removed"], link["added"]
        note = {"kind": "cross_file_move", "removed_path": link["removed_path"], "added_path": link["added_path"], "removed_name": rem["name"], "added_name": add["name"],
                "confidence": link["confidence"], "changed": link["changed"]}
        notes.setdefault(link["removed_index"], []).append(note)
        notes.setdefault(link["added_index"], []).append(note)
        if not link["changed"]:
            continue
        rem_fp, add_fp = kept[link["removed_index"]][0], kept[link["added_index"]][0]
        analysis = kept[link["added_index"]][1]
        b, a = rem["text"], add["text"]
        prov = {"before": {"path": link["removed_path"], "lines": rem["lines"], "bytes": rem["bytes"], "signature": rem["signature"], "kind": rem["kind"], "scope": rem["scope"]},
                "after": {"path": link["added_path"], "lines": add["lines"], "bytes": add["bytes"], "signature": add["signature"], "kind": add["kind"], "scope": add["scope"]}}
        pair = {"granularity": "function", "function_name": add["name"], "function_before": rem["name"], "span": None, "code_before": b, "code_after": a,
                "match": {"kind": "cross_file_move", "confidence": link["confidence"], "signals": link["signals"], "alternatives": []}, "prov": prov}
        assessment = splitquality.assess(pair, analysis, policy, splitquality.check_provenance(pair, rem_fp["code_before"], add_fp["code_after"]))
        base = add_fp["pair_key"][:-2] if add_fp["pair_key"].endswith(":*") else add_fp["pair_key"]
        loc = f"{add['name']}~from~{link['removed_path']}"
        out.append({**add_fp, "granularity": "function", "function_name": add["name"], "function_name_before": rem["name"], "span": None, "code_before": b, "code_after": a,
                    "pair_key": f"{base}:{loc}", "diff": _udiff(b + "\n", a + "\n", f"a/{link['removed_path']}::{rem['name']}", f"b/{link['added_path']}::{add['name']}"),
                    "code_available": 1, "skip_reason": None, "content_hash": _hash(b, a), "changed_lines": None, "old_path": link["removed_path"],
                    "quarantine_reason": add_fp.get("quarantine_reason") or splitquality.quarantine_reason(assessment), "attribution_basis": None, "attribution_score": None,
                    "match_kind": "cross_file_move", "match_confidence": link["confidence"], "extraction_meta": splitquality.dumps(splitquality.pair_meta(pair, analysis, assessment))})
    for i, items in notes.items():
        fp = kept[i][0]
        meta = json.loads(fp["extraction_meta"]) if fp.get("extraction_meta") else {"v": splitquality.META_VERSION}
        meta["cross_file_links"] = items[:splitquality.MAX_LISTED]
        fp["extraction_meta"] = splitquality.dumps(meta)
    return out


def _split_task(fp: dict):
    """One stored file pair split into function / hunk pairs, and how its parse went. Top level, so a worker process can run it."""
    subs = sub_pairs(fp)                              # sets the file pair's parse status and meta on ``fp``
    return subs, fp["parse_status"], fp["parse_failure_reason"], fp.get("extraction_meta")


def resplit(max_functions: int = 20, progress=None, chunk: int = 200, workers=None, ecosystems: list[str] | None = None, should_stop=None, only_unsplit: bool = False, force: bool = False) -> dict:
    """Derive function / hunk pairs from the stored file-level pairs (no network,
    no re-mining). Derived pairs keep their LLM fields when unchanged; derived
    pairs that no longer follow from the file are removed (hand-edited ones are
    kept). A commit with more than ``max_functions`` changed functions is flagged
    tangled, as during mining. The splitting itself (parsing, diffing) runs in ``workers`` processes (None: the
    Settings value); the stored rows and the order of the writes are the same for any number. With ``ecosystems`` (a campaign's scope) only the
    pairs of those ecosystems are split and only their records are refreshed. ``should_stop`` (see ``stopping``) is asked before each chunk: the chunks done are kept
    (the tangled flags and the records follow for them) and the counts carry ``"paused": True``; a later split starts again from the first stored file pair, which is harmless.
    ``only_unsplit`` takes just the file pairs the splitter has not seen (no parse status), for a run that follows mining.

    A file pair records a signature (``extraction_meta.sig``: the splitter's version and parsers, the quality policy, both versions of the code, the diff and the fields the derived pairs
    inherit). A pair whose signature is still the current one is left alone (counted as ``unchanged``): its derived pairs cannot differ, so a paused or repeated run only does what is
    left. ``force`` splits every pair again whatever its signature."""
    report = progress or _noop
    policy = splitquality.default_policy()
    ids = codestore.file_pair_ids_to_split(ecosystems=ecosystems, unsplit_only=only_unsplit)
    if not ids:
        print("No stored file-level pairs to split.")
        report(state="done", label="Nothing to split", processed=0, total=0)
        return {"files": 0}
    print(f"splitting {len(ids):,} stored file-level pairs into functions / hunks")
    counts: Counter = Counter(files=0, functions=0, hunks=0, unchanged=0)
    commits: set[str] = set()
    t0 = time.time()
    paused = False
    with compute.Pool(workers) as pool:
        for i in range(0, len(ids), chunk):
            if stopping.stop(should_stop):
                paused = True
                break
            part = ids[i:i + chunk]
            rows = codestore.pairs_by_ids(part)
            new, stale, status, metas = [], [], [], []
            fps = []
            for fp in rows:
                fp = {k: fp.get(k) for k in ("pair_key", *codestore.PAIR_FIELDS)}
                fp.update(root_cause_llm=None, secure_pattern_llm=None, explanation_llm=None, llm_model=None,
                          llm_prompt_version=None, edited_at=None)
                fps.append(fp)
            todo = []
            for fp in fps:
                if not force and splitquality.stored_signature(fp.get("extraction_meta")) == splitquality.split_signature(fp, policy):
                    counts["unchanged"] += 1
                else:
                    todo.append(fp)
            for fp, (subs, parse_status, parse_reason, file_meta) in zip(todo, pool.map(_split_task, todo, chunksize=4, min_items=8) if todo else []):
                status.append((parse_status, parse_reason, fp["pair_key"]))
                metas.append((file_meta, fp["pair_key"]))
                new += subs
                prefix = fp["pair_key"][:-2] + ":"
                stale.append((prefix, [p["pair_key"] for p in subs], fp.get('commit_key')))
                counts["files"] += 1
                counts["functions"] += sum(p["granularity"] == "function" for p in subs)
                counts["hunks"] += sum(p["granularity"] == "hunk" for p in subs)
                counts["files_split"] += bool(subs)
                if fp.get("commit_key"):
                    commits.add(fp["commit_key"])
            for prefix, keep, source_key in stale:  # derived pairs of an older split
                keep_keys = set(keep)
                gone = [k for k in codestore.derived_pair_keys(prefix, commit_key=source_key) if k not in keep_keys]
                if gone:
                    codestore.delete_pairs_by_keys(gone)
                    counts["removed"] += len(gone)
            codestore.upsert_pairs(new)
            codestore.set_parse_status(status)
            codestore.set_file_meta(metas)
            done = min(i + chunk, len(ids))
            report(state="resplit", label="Splitting stored pairs", processed=done, total=len(ids),
                   counts={k: counts[k] for k in ("functions", "hunks")})
    counts["cross_file_pairs"] = _link_commits(sorted(commits))
    # tangled: as during mining, many changed functions in one commit
    keys = sorted(commits)
    for ch in codestore._chunks(keys):
        nfun = codestore.function_pair_counts(ch)
        tangled = [k for k in ch if nfun.get(k, 0) > max_functions]
        codestore.set_tangled(ch, tangled)
        counts["tangled_commits"] += len(tangled)
    counts["attributed"] = sum(attribution.refresh(sorted(commits), only_missing=True).values())
    n = codestore.refresh_records(None if ecosystems is None else [r["id"] for r in store.select_mineable(("id",), ecosystems=ecosystems)])
    print(f"done in {time.time() - t0:.0f}s: {counts['files']:,} files -> {counts['functions']:,} "
          f"function pairs and {counts['hunks']:,} hunk pairs ({counts['files_split']:,} files split; "
          f"{counts['removed']:,} outdated derived pairs removed); "
          f"{counts['tangled_commits']:,} commits flagged tangled (> {max_functions} functions)")
    print(f"{n:,} records updated")
    report(state="paused" if paused else "done", label="Paused" if paused else "Split complete", processed=counts["files"], total=len(ids),
           counts={k: counts[k] for k in ("functions", "hunks")})
    return {**counts, "paused": True} if paused else dict(counts)


def _link_commits(commit_keys: list[str]) -> int:
    """The cross-file pass of a re-split: for each commit with changed files in the same language whose splits left functions removed in one and added in another, link them and store
    the pairs (the earlier cross-file pairs of the commit were deleted with the other derived pairs of their file). Returns how many pairs were stored."""
    stored = 0
    for ch in codestore._chunks(commit_keys, 100):
        by_commit: dict[str, list[dict]] = {}
        for r in codestore.file_pair_rows(ch):
            by_commit.setdefault(r["commit_key"], []).append(r)
        new, metas = [], []
        for rows in by_commit.values():
            if len(rows) < 2:
                continue
            sides = [(_meta_sides(r.get("extraction_meta")), r.get("language")) for r in rows]
            if not any(a_side & {"removed"} and b_side & {"added"} and a_lang == b_lang and i != j
                       for i, (a_side, a_lang) in enumerate(sides) for j, (b_side, b_lang) in enumerate(sides)):
                continue                                            # no file lost a function while another gained one
            analyses = []
            for r in rows:
                try:
                    analyses.append(funcsplit.analyze(r.get("language") or "", r["file_path"], r["code_before"] or "", r["code_after"] or "", diff=r.get("diff")))
                except Exception:  # ignored: this file takes no part in the linking
                    analyses.append(None)
            made = cross_file_pairs(rows, analyses)
            new += made
            metas += [(r["extraction_meta"], r["pair_key"]) for r in rows if r.get("extraction_meta")]
        if new:
            codestore.upsert_pairs(new)
            stored += len(new)
        if metas:
            codestore.set_file_meta(metas)
    return stored


def _meta_sides(meta: str | None) -> set[str]:
    """Which sides (``removed`` / ``added``) of function the file-level meta lists as without a counterpart."""
    try:
        return {u["side"] for u in json.loads(meta or "{}").get("unpaired", [])}
    except (ValueError, TypeError, KeyError):
        return set()


def split_is_current(suffix: str = "") -> bool:
    """Whether the stored pairs were split by this splitter version with these parsers. The parsers are compared only when they were recorded (a store split before they were recorded is
    not split again for that alone). ``suffix`` is ``":<ecosystem>"`` for the per-ecosystem stamps a campaign keeps."""
    if store.get_meta(f"split_version{suffix}") != str(funcsplit.VERSION):
        return False
    recorded = store.get_meta(f"split_parsers{suffix}")
    return recorded is None or recorded == funcsplit.parser_fingerprint()


def stamp_split(suffix: str = "") -> None:
    """Record that the stored pairs were split by this splitter version with these parsers."""
    store.set_meta(f"split_version{suffix}", str(funcsplit.VERSION))
    store.set_meta(f"split_parsers{suffix}", funcsplit.parser_fingerprint())


def maintain(progress=None) -> None:
    """Keep stored mining results current without re-mining (runs after Mine and
    before Build): check fix commits not checked yet - or all of them once after
    the check changed (fixcheck.VERSION) - and re-split every stored file pair once
    after the splitting changed (funcsplit.VERSION). Nothing is downloaded again
    except cached registry release dates."""
    from app.backend.pipeline.ingest import fixcheck
    if store.get_meta("fixcheck_version") != str(fixcheck.VERSION):
        print(f"fix-commit check v{fixcheck.VERSION}: re-checking every mined commit once")
        fixcheck.verify_all(only_unchecked=False, progress=progress)
        store.set_meta("fixcheck_version", str(fixcheck.VERSION))
    else:
        fixcheck.verify_all(only_unchecked=True, progress=progress, quiet=True)
    if not split_is_current():
        print(f"function / hunk splitting v{funcsplit.VERSION} ({funcsplit.parser_fingerprint()}): re-splitting the stored pairs once")
        resplit(progress=progress)
        stamp_split()


def acquire_commit(row: dict, gh, git, *, before_git=None, git_scope=None):
    """Lease local readers only while executing Git calls, excluding HTTP work."""
    commit = None
    from app.backend.pipeline.acceleration import mining_policy, operations
    local_scope = git_scope if git_scope is not None else nullcontext
    fallback_reason = None
    if mining_policy.current().get('version') == 4:
        from app.backend.pipeline.acceleration import git_metadata
        from app.backend.pipeline.acceleration.repository_service import CompatibilityFallback
        if before_git is not None:
            before_git()
            before_git = None
        try:
            with local_scope():
                commit = git_metadata.acquire(row, git, gh)
        except (operations.DeadlineExpired, stopping.Paused, MemoryError):
            raise
        except (CompatibilityFallback, FetchError, OSError) as error:
            operations.check()
            fallback_reason = str(error)[:500]
            from app.backend.pipeline.acceleration.metrics import current
            current.count('git_snapshot_metadata_fallbacks')
    if commit is None and gh and row["host"] == "github.com":
        try:
            commit = gh.commit(row)
            if mining_policy.current().get('version') == 4:
                commit.acquisition = {'schema': 'mining-acquisition/1', 'transport': 'github-rest',
                    'commit': commit.sha, 'parent': commit.parent, 'fallback_reason': fallback_reason}
        except FetchError:
            commit = None                             # e.g. access restricted -> git
    if before_git is not None:
        before_git()
    if commit is None:
        with local_scope():
            commit = git.commit(row)
        if mining_policy.current().get('version') == 4:
            commit.acquisition = {'schema': 'mining-acquisition/1', 'transport': 'git-reference',
                'commit': commit.sha, 'parent': commit.parent, 'fallback_reason': fallback_reason}
    if mining_policy.optimized() and any(language_of(f.path) in ('javascript', 'typescript') for f in commit.files):
        original = commit.loader
        try:
            if isinstance(original, _GitLoader):
                cached = commit
            else:
                with local_scope():
                    cached = git.commit({**row, "sha": commit.sha})
        except FetchError as error:
            from app.backend.pipeline.acceleration.metrics import current
            current.count('git_transport_fallbacks')
            print(f'Git snapshot unavailable; retaining the original acquisition path: {str(error)[:180]}')
        else:
            if cached.sha != commit.sha or cached.parent != commit.parent:
                raise ValueError('Git and GitHub identify different before/after snapshots')
            commit.loader = _BatchedGitLoader(cached.loader.d, original)
    if gh and row["host"] == "github.com":
        try:                                          # the repository's license, recorded once (licenses.py)
            from app.backend.pipeline.ingest import licenses
            licenses.for_repository(gh, "github.com", row["owner"], row["repo"])
        except RateLimited:
            raise
        except Exception:  # ignored: licence lookup is best effort; the pair is built without it
            pass
    return commit


def mine_one(row: dict, gh: GitHubFetcher | None, git: GitFetcher, meta: MetaIndex,
             max_files: int, max_functions: int = MAX_FUNCTIONS, cpu_pool=None) -> MineResult:
    commit = acquire_commit(row, gh, git)
    return build_pairs(row, commit, meta.for_commit(row), max_files, max_functions, cpu_pool=cpu_pool)


def _noop(**_):
    pass


def _wait(until: float, state: str, reason: str, report, beat=None, should_stop=None, **ctx) -> None:
    """Sleep until ``until`` (epoch s), reporting the deadline once; the UI counts down. Raises ``stopping.Paused`` when ``should_stop`` says so."""
    secs = max(0, until - time.time())
    if secs <= 0:
        return
    m, sec = divmod(int(round(secs)), 60)
    print(f"{reason}: waiting {m}m {sec:02d}s until "
          f"{time.strftime('%H:%M:%S', time.localtime(until))}")
    report(state=state, until=until, reason=reason, **ctx)
    while (left := until - time.time()) > 0:
        time.sleep(min(1.0 if should_stop else 5.0, left))
        if beat:
            beat()
        stopping.check(should_stop)


def _fmt_secs(sec: float) -> str:
    m, s_ = divmod(int(sec), 60)
    return f"{m}m {s_:02d}s" if m else f"{s_}s"


def _mine_batch(queue: list[dict], gh, git, meta, max_files, max_functions,
                workers, counts: Counter, tick, commit_timeout: float = 600,
                heartbeat=None, should_stop=None, cpu_pool=None) -> float | None:
    """Mine one batch. Returns the resume time if GitHub rate-limited us; commits
    not finished then stay queued (they are never marked failed for it).

    When ``should_stop`` says yes the commits not started are dropped (they stay queued) and the ones in progress are finished and stored, then the batch ends.

    A watchdog keeps one stuck commit from stalling the run: a commit running
    longer than ``commit_timeout`` seconds is recorded as failed (retry later with
    "Retry previously failed commits") and the batch moves on without it."""
    reset = None
    started: dict[str, float] = {}
    from app.backend.pipeline.acceleration import operations, mining_policy

    def run(r):
        started[r["commit_key"]] = time.time()
        try:
            res = mine_one(r, gh, git, meta, int(max_files), int(max_functions), cpu_pool=cpu_pool)
            if res.status == "mined" and res.commit:
                if mining_policy.hydrate_context() and any(p.get('language') in ('javascript', 'typescript', 'tsx', 'jsx') for p in res.pairs):
                    from app.backend.pipeline import security_snapshots, fsio
                    from app.backend import paths
                    try:
                        operations.phase('hydrating_context')
                        cached = res.commit if isinstance(res.commit.loader, _GitLoader) else git.commit({**r, "sha": res.commit.sha})
                        hydrated = security_snapshots.hydrate_pairs(res.pairs, cached)
                    except (stopping.Paused, operations.DeadlineExpired):
                        raise
                    except Exception as error:
                        hydrated = {'schema': 'snapshot-hydration/1', 'fetched': 0, 'omissions': [{'reason': str(error)[:500]}]}
                    receipt = paths.interim_dir() / 'snapshot_hydration' / (hashlib.sha256(r['commit_key'].encode()).hexdigest() + '.json')
                    receipt.parent.mkdir(parents=True, exist_ok=True)
                    fsio.write_json(receipt, hydrated)
                try:                     # is it really the fix? (release dates; fixcheck.py)
                    operations.phase('checking_fix')
                    from app.backend.pipeline.ingest import fixcheck
                    res.fix_check = fixcheck.check_commit(r, res.commit.date)
                except (stopping.Paused, operations.DeadlineExpired):
                    raise
                except Exception as e:
                    res.fix_check = ("unknown", f"check failed: {str(e)[:80]}", None, None)
            return res
        finally:
            started.pop(r["commit_key"], None)

    if mining_policy.current().get('version') in (3, 4):
        from app.backend.pipeline.acceleration.staged_mining import active, StagedMiner
        if miner := active():
            miner.run(queue)
        else:
            with StagedMiner(gh,git,meta,max_files,max_functions,cpu_pool,counts,tick,
                             should_stop=should_stop or (lambda:False),service_seconds=commit_timeout) as miner:
                miner.run(queue)
        return None
    if mining_policy.optimized():
        from app.backend.pipeline.acceleration import mining_batch
        return mining_batch.run(queue, run, workers, counts, tick, commit_timeout=commit_timeout,
                                heartbeat=heartbeat, should_stop=should_stop)

    ceiling = max(1, int(workers))
    remaining = deque(queue)
    pending: dict[Future, dict] = {}
    ex = ThreadPoolExecutor(max_workers=ceiling)           # sized to the ceiling, not the live width, so a width raised mid-batch has somewhere to grow into
    width, last_note, stopping_now = ceiling, time.time(), False
    try:
        while remaining or pending:
            if should_stop and not stopping_now and should_stop():
                stopping_now = True
                for f in pending:
                    f.cancel()
                pending = {f: r for f, r in pending.items() if not f.cancelled() and r["commit_key"] in started}
                remaining.clear()
                print("pause requested: finishing the commits in progress, the rest of this batch stays queued")
            if not stopping_now and reset is None:
                new_width = compute.memory_adaptive_workers(ceiling)
                if abs(new_width - width) > 1:             # damping: a noisy reading a second apart should not thrash the width
                    width = new_width
                while remaining and len(pending) < width:
                    r = remaining.popleft()
                    pending[ex.submit(run, r)] = r
            if not pending:
                break
            all_pending = pending
            done, waiting = wait(list(all_pending), timeout=1.0 if should_stop else 15, return_when=FIRST_COMPLETED)
            pending = {f: all_pending[f] for f in waiting}
            now = time.time()
            if heartbeat:
                heartbeat()
            for fut in list(pending):                         # watchdog
                row = pending[fut]
                t0 = started.get(row["commit_key"])
                if t0 and now - t0 > commit_timeout:
                    del pending[fut]
                    tag = f"{row['owner']}/{row['repo']}@{row['sha'][:8]}"
                    codestore.mark_fix_commit(row["commit_key"], "failed",
                                              error=f"timed out after {int(commit_timeout)}s")
                    counts["failed"] += 1
                    print(f"{tag}: failed - timed out after {_fmt_secs(commit_timeout)}; moving on "
                          "(retry later with 'Retry previously failed commits')")
                    codestore.refresh_records(codestore.records_for_commit(row))
                    tick()
            if not done and now - last_note >= 60:            # say what is still running
                last_note = now
                busy = sorted(((now - started[row["commit_key"]], row) for row in pending.values()
                               if row["commit_key"] in started), key=lambda x: -x[0])
                if busy:
                    print("still working on " + ", ".join(
                        f"{r['owner']}/{r['repo']}@{r['sha'][:8]} ({_fmt_secs(t)})" for t, r in busy[:4]))
            for fut in done:
                row = all_pending[fut]
                tag = f"{row['owner']}/{row['repo']}@{row['sha'][:8]}"
                if fut.cancelled():              # dropped after a rate limit: still queued
                    continue
                try:
                    res = fut.result()
                except RateLimited as e:
                    if reset is None:
                        for f in pending:
                            f.cancel()
                        print(f"{tag}: {e}; the rest of this batch stays queued")
                    reset = max(reset or 0, e.reset)
                    continue
                except stopping.Paused:
                    for pending_future in pending:
                        pending_future.cancel()
                    raise
                except Exception as e:
                    from app.backend.pipeline import redact
                    codestore.mark_fix_commit(row["commit_key"], "failed", error=redact.text(e)[:300])
                    counts["failed"] += 1
                    print(f"{tag}: failed - {redact.text(e)[:120]}")
                    codestore.refresh_records(codestore.records_for_commit(row))
                    tick()
                    continue
                _store_result(row, res, counts, tag)
                tick()
            if reset is not None:
                pending = {f: r for f, r in pending.items() if not f.cancelled() and r["commit_key"] in started}
                remaining.clear()
    finally:
        # never wait for an abandoned (timed-out) thread
        ex.shutdown(wait=False, cancel_futures=True)
    return reset


def _store_result(row: dict, res: MineResult, counts: Counter, tag: str, *, log=print) -> None:
    # Stable keys preserve IDs, manual corrections and versioned context drafts
    # across retries. A failed retry must never erase an earlier successful mine.
    codestore.upsert_pairs(res.pairs)
    codestore.set_pair_quality(pairquality.rows(res.pairs))          # beside the pairs: the signals and the change key found while they were attributed
    codestore.replace_commit_files(row["commit_key"], res.files)
    usable = [p for p in res.pairs if p.get("code_available")]
    nfun = sum(p["granularity"] == "function" for p in usable)
    c = res.commit
    codestore.mark_fix_commit(
        row["commit_key"], res.status, error=res.reason,
        parent_sha=c.parent if c else None,
        commit_message=(c.message or "")[:2000] if c else None,
        commit_date=c.date if c else None,
        acquisition=c.acquisition if c else None,
        files_changed=len(c.files) if c else None,
        **({"parent_count": res.kind["parent_count"], "commit_kind": res.kind["kind"],
            "parent_selection_reason": res.kind["reason"]} if res.kind else {}),
        langs=json.dumps(res.langs) if res.langs else None,
        detected_langs=json.dumps(res.cve_langs) if res.cve_langs else None,
        pairs=len(usable), mined_at=datetime.datetime.utcnow().isoformat() + "Z",
        **(dict(zip(("fix_check", "fix_check_reason", "link", "fix_check_package"), res.fix_check))
           if res.fix_check else {"fix_check": None, "fix_check_reason": None,
                                  "fix_check_package": None}))
    counts[res.status] += 1
    counts["pairs"] += len(usable)
    counts["functions"] += nfun
    lang = f" [{'/'.join(res.cve_langs)}; {res.note}]" if res.cve_langs else ""
    log(f"{tag}: {res.status}"
          + (f" - {len(usable)} pairs ({nfun} functions)" if usable else f" - {res.reason}")
          + lang
          + (" [tangled: flagged, excluded from training]" if res.reason == "tangled_commit" else "")
          + (f" [suspect fix link: {res.fix_check[1]}]" if res.fix_check and res.fix_check[0] == "suspect"
             else ""))
    codestore.refresh_records(codestore.records_for_commit(row))   # live on the records


def mine(limit: int | None = None, workers: int = 4, retry_failed: bool = False,
         max_files: int = 40, max_functions: int = 20,
         retryable_only: bool = False, batch_size: int = 200, pause_seconds: float = 60, until_done: bool = True,
         commit_timeout: float = 600, progress=None, ecosystems: list[str] | None = None, should_stop=None) -> dict:
    """Mine queued fix commits in batches of ``batch_size``, pausing
    ``pause_seconds`` between batches and, with a GitHub token, waiting out the API
    rate limit (until its reset time) instead of failing. ``until_done`` keeps going
    until nothing mineable is left; ``limit`` caps the commits of this run. With
    ``ecosystems`` (a campaign's scope) only the commits tagged with them are mined.

    ``should_stop`` (see ``stopping``) is asked at the top of every batch, while waiting and while a batch runs: the commits in progress are finished and stored, the others stay
    queued, the run record is closed as "paused" and the counts carry ``"paused": True``. Mining the same scope again continues from the queue."""
    from app.backend.pipeline.acceleration.acquisition_hints import enabled as selection_hints_enabled
    use_selection_hints=selection_hints_enabled()
    report = progress or _noop
    if retry_failed:
        n = codestore.reset_failed_fix_commits(ecosystems=ecosystems, **({"retryable_only":True} if retryable_only else {}))
        print(f"re-queued {n} previously failed commits")
    total = codestore.count_mineable(ecosystems=ecosystems)
    if limit:
        total = min(total, int(limit))
    if not total:
        print("No pending fix commits"
              + ". Run 'Extract fix commits' (or ingest) first.")
        report(state="done", label="Nothing to mine", processed=0, total=0)
        return {"processed": 0}
    from app.backend.pipeline import mining_run
    params = {"workers": workers, "retry_failed": False, "max_files": max_files,
              "max_functions": max_functions, "batch_size": batch_size,
              "pause_seconds": pause_seconds, "until_done": until_done,
              "commit_timeout": commit_timeout, "limit": limit, "ecosystems": ecosystems}
    run = mining_run.start(params, total)          # refuses if another miner is alive
    if run.get("resumed_from"):
        print(f"resuming the run interrupted after starting {run['resumed_from']}: "
              f"{total:,} commits still queued")
    token = os.getenv("GITHUB_TOKEN")
    gh = GitHubFetcher(token) if token else None
    from app.backend.pipeline.acceleration import mining_policy
    git = GitFetcher(batched=mining_policy.optimized())
    batch_size = max(1, int(batch_size))
    print(f"mining {total:,} fix commits in batches of {batch_size} with "
          f"{'GitHub API (token) + git' if gh else 'git (no GitHub token)'}, {workers} workers, "
          "each CVE in the language detected from git"
          + (f", {pause_seconds:g}s pause between batches" if until_done else ""))
    from app.backend.pipeline.acceleration import mining_metadata, runtime
    # Legacy standalone callers retain MetaIndex; versioned campaigns use bounded storage.
    bounded_metadata = runtime.current_policy().get('stream_metadata', False)
    if bounded_metadata:
        disk_meta = mining_metadata.build(ecosystems)
        meta = MetaIndex.__new__(MetaIndex)
        meta.adv, meta.by_cve, meta.nvd = disk_meta.adv, disk_meta.by_cve, disk_meta.nvd
    else:
        disk_meta = None
        meta = MetaIndex(ecosystems=ecosystems)
    print(f"metadata: {len(meta.adv)} advisories, {len(meta.nvd)} NVD CVEs")
    counts: Counter = Counter()
    batch, started, last_tick = 0, time.time(), [0.0]

    def done() -> int:
        return sum(counts[k] for k in _DONE)

    def ctx() -> dict:
        return {"processed": done(), "total": total, "batch": batch,
                "batches": max(batch, -(-total // batch_size)),
                "counts": {k: counts[k] for k in ("mined", "pairs", "no_code",
                                                  "skipped", "failed") if counts[k]},
                "quota": gh.last_quota if gh else None}

    def beat() -> None:
        mining_run.beat(processed=done(), batch=batch, counts=dict(counts))

    def tick() -> None:
        beat()
        if time.time() - last_tick[0] >= 1.0:
            last_tick[0] = time.time()
            report(state="mining", **ctx())

    paused, pause_reason = False, None
    try:
        from app.backend.pipeline.acceleration.resource_control import active as active_resources, AdaptiveParserPool
        resources=active_resources() if mining_policy.current().get('version') == 4 else None
        parser_pool = (AdaptiveParserPool(resources, should_stop=should_stop or (lambda:False),progress=report) if resources else
                       compute.Pool(workers=mining_policy.current()['parser_workers'], force_process=True)
                       if mining_policy.optimized() else compute.Pool())
        from contextlib import ExitStack
        with parser_pool as cpu_pool, ExitStack() as stack:  # one parser pool and staged miner for the whole run
            if mining_policy.current().get('version') in (3, 4):
                from app.backend.pipeline.acceleration.staged_mining import StagedMiner
                miner=stack.enter_context(StagedMiner(gh,git,meta,max_files,max_functions,cpu_pool,counts,tick,
                    should_stop=should_stop or (lambda:False),service_seconds=commit_timeout))
                batch_size=256
                if use_selection_hints:
                    selected_count=max(0,min(total,256) if not until_done else total)
                    selector=hashlib.sha256(json.dumps(['pending-fix-commits/1',ecosystems],
                        sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()
                    miner.configure_selection(codestore.pending_fix_commit_identities(selected_count,ecosystems=ecosystems),
                        selected_count=selected_count,selector_fingerprint=selector,
                        validate=lambda candidates:codestore.revalidate_pending_fix_commit_identities(candidates,ecosystems=ecosystems))
            rolling=mining_policy.rolling_enabled()
            if rolling:
                # Keep a bounded active frontier instead of waiting for each
                # 256-item tail. The database remains the pending authority.
                selected=min(total,256)
                initial=codestore.pending_fix_commits(selected,ecosystems=ecosystems)
                initial=compute.interleave(initial,key=lambda r:(r['owner'],r['repo']))
                selected=len(initial);batch=1
                maximum=total if until_done else min(total,256)
                def refill(capacity,active):
                    nonlocal selected,batch
                    available=min(capacity,maximum-selected)
                    if available<=0:return []
                    rows=codestore.pending_fix_commits(available,ecosystems=ecosystems,
                        exclude=[row['commit_key'] for row in active])
                    selected+=len(rows);batch=max(1,-(-selected//256))
                    return compute.interleave(rows,key=lambda r:(r['owner'],r['repo']))
                miner.run(initial,refill=refill)
            while done() < total and not rolling:   # legacy batch policy retains its original pause and selection
                stopping.check(should_stop)
                if gh:
                    q = gh.quota()
                    if q and q["remaining"] < max(2 * int(workers), 5):
                        _wait(q["reset"] + 5, "rate_limited", "GitHub API quota used up", report, beat, should_stop, **ctx())
                queue = codestore.pending_fix_commits(min(batch_size, total - done()), ecosystems=ecosystems)
                if not queue:
                    break
                queue = compute.interleave(queue, key=lambda r: (r["owner"], r["repo"]))      # neighbours of one repository do not queue behind its lock
                batch += 1
                print(f"\nâ”€â”€ batch {batch}: {len(queue)} commits ({done():,}/{total:,} done) â”€â”€")
                report(state="mining", **ctx())
                reset = _mine_batch(queue, gh, git, meta, max_files, max_functions,
                                    workers, counts, tick, commit_timeout=commit_timeout, heartbeat=beat, should_stop=should_stop, cpu_pool=cpu_pool)
                stopping.check(should_stop)
                if reset:
                    _wait(reset + 5, "rate_limited", "GitHub rate limit reached", report, beat, should_stop, **ctx())
                    continue
                if not until_done:
                    break
                if done() < total and codestore.count_mineable(ecosystems=ecosystems) and pause_seconds > 0:
                    _wait(time.time() + float(pause_seconds), "pause",
                          f"pause between batches ({pause_seconds:g}s)", report, beat, should_stop, **ctx())
    except stopping.Paused as error:
        paused = True
        pause_reason = getattr(error, 'reason', None)
    finally:
        if disk_meta is not None:
            mining_metadata.close(disk_meta)
        if mining_policy.optimized():
            from app.backend.pipeline.acceleration.git_objects import close_all
            close_all()
    took = time.time() - started
    print(f"\ndone in {took:.0f}s: {counts['mined']} mined, {counts['no_code']} without code, "
          f"{counts['skipped']} skipped, "
          f"{counts['failed']} failed; {counts['pairs']} usable pairs "
          f"({counts['functions']} function-level)")
    left = codestore.count_mineable(ecosystems=ecosystems)
    mining_run.finish("paused" if paused else "finished", processed=done(), batch=batch, counts=dict(counts), left=left)
    if left:
        print(f"{left:,} commits still queued; run Mine again to continue")
    report(state="paused" if paused else "done", label="Paused" if paused else "Finished" if not left else f"Stopped: {left:,} commits still queued",
           **ctx())
    return {**counts, "paused": True, 'pause_reason': pause_reason} if paused else dict(counts)


_DONE = ("mined", "no_code", "skipped", "failed")
