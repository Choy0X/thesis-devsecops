"""Recover fix commits that an advisory does not link directly.

Of the advisories without a commit pointer, many name a pull request, an issue, or only a fixed
version of a package whose repository is known. Each of these can be followed to a commit, and a
recovered link says how it was found (``via``) and is labelled source ``recovered``:

* ``pr_merge_commit`` / ``pr_commit``    a merged pull request: its merge or squash commit, or, for a
  rebase merge (the tip commit is the PR's own last commit), the PR's commits (up to 10);
* ``issue_closed_by_commit``             the commit that closed a referenced issue;
* ``version_range``                      the tag of the fixed version against the previous stable
  release tag: accepted only when a single fix can be isolated (the one non-merge commit in the
  range, or the one whose message names the advisory); a range with many unrelated commits stays
  unresolved with the reason recorded. A release or compare URL alone is never taken for a fix.

All lookups go through an injectable ``api`` (``get(path, params) -> json | None``), so the logic is
tested without a network. Attempts are recorded in the derived store (``link_recovery``) so a record
is not asked about twice.
"""
from __future__ import annotations

import json
import time

from app.backend.pipeline import codestore, derived, store
from app.backend.pipeline.ecosystems import versions as ev
from app.backend.pipeline.ingest import fixrefs

GITHUB_API = "https://api.github.com"
PR_COMMIT_LIMIT = 10
TAG_PAGES = 3


class GitHubClient:
    """Minimal authenticated GitHub REST client that raises ``codemine.RateLimited``."""

    def __init__(self, token: str, session=None):
        import requests
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_maxsize=32)       # the default pool of 10 made more of run()'s workers wait for one connection
            session.mount("https://", adapter)
            session.mount("http://", adapter)
        from app.backend.pipeline.acceleration.http_cache import wrap
        self.s = wrap(session)
        self.s.headers.update({"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                               "X-GitHub-Api-Version": "2022-11-28"})

    def get(self, path: str, params: dict | None = None):
        from app.backend.pipeline.ingest import codemine
        r = self.s.get(f"{GITHUB_API}{path}", params=params, timeout=(15, 60))
        limited = codemine._rate_limit(r)
        if limited:
            raise limited
        if r.status_code in (404, 410, 422, 451):          # gone, unavailable, or a sha/ref that this repository does not know
            return None
        r.raise_for_status()
        return r.json()


def _ptr(owner: str, repo: str, sha: str, via: str, evidence: dict) -> dict:
    p = fixrefs._pointer("github.com", owner, repo, sha)
    p["via"] = via
    p["evidence"] = evidence
    return p


def _first_line(message) -> str:
    return (message or "").splitlines()[0] if message else ""


def resolve_pull(api, host: str, owner: str, repo: str, number: int) -> list[dict]:
    """The commit(s) of a merged pull request (GitHub only)."""
    if host != "github.com":
        return []
    pr = api.get(f"/repos/{owner}/{repo}/pulls/{number}")
    if not pr or not pr.get("merged_at"):
        return []
    ev_ = {"pull": number}
    commits = api.get(f"/repos/{owner}/{repo}/pulls/{number}/commits", {"per_page": 100}) or []
    merge_sha = pr.get("merge_commit_sha")
    if merge_sha:
        c = api.get(f"/repos/{owner}/{repo}/commits/{merge_sha}")
        if c:
            last = _first_line((commits[-1].get("commit") or {}).get("message")) if commits else None
            rebase = (len(c.get("parents") or []) == 1 and len(commits) > 1 and last is not None
                      and _first_line((c.get("commit") or {}).get("message")) == last)
            if not rebase:                       # merge commit, squash, or a single commit
                return [_ptr(owner, repo, merge_sha, "pr_merge_commit", ev_)]
    if 0 < len(commits) <= PR_COMMIT_LIMIT:
        return [_ptr(owner, repo, c["sha"], "pr_commit", ev_) for c in commits]
    return []


def resolve_issue(api, host: str, owner: str, repo: str, number: int) -> list[dict]:
    """The commit that closed an issue, if GitHub recorded one."""
    if host != "github.com":
        return []
    events = api.get(f"/repos/{owner}/{repo}/issues/{number}/events", {"per_page": 100}) or []
    out, seen = [], set()
    for e in events:
        sha = e.get("commit_id")
        if e.get("event") == "closed" and sha and sha not in seen:
            seen.add(sha)
            out.append(_ptr(owner, repo, sha, "issue_closed_by_commit", {"issue": number}))
    return out


def _tag_prefixes(package: str) -> list[str]:
    short = package.split("/")[-1]
    out = ["v", "", f"{package}@", f"{package}@v", f"{package}-", f"{package}-v"]
    if short != package:
        out += [f"{short}@", f"{short}@v", f"{short}-", f"{short}-v"]
    return out


def pick_tags(ecosystem: str, package: str, fixed_version: str, tags: list[dict]):
    """(tag of the fixed version, previous stable release tag of the same naming style)."""
    want = ev.normalize(ecosystem, fixed_version).lower()
    by_name = {t["name"].lower(): t for t in tags}
    for prefix in _tag_prefixes(package.lower()):
        fixed = by_name.get(prefix + want)
        if not fixed:
            continue
        target = ev.key(ecosystem, want)
        best, best_key = None, None
        for t in tags:
            name = t["name"].lower()
            if not name.startswith(prefix) or t is fixed:
                continue
            k = ev.key(ecosystem, name[len(prefix):])
            if not k.valid or k.prerelease or not (k < target):
                continue
            if best_key is None or best_key < k:
                best, best_key = t, k
        return fixed, best
    return None, None


def _aliases(rec: dict) -> list[str]:
    out = {rec.get("osv_id"), rec.get("cve_id")}
    try:
        out |= set(json.loads(rec.get("aliases") or "[]"))
    except ValueError:
        pass
    return sorted(a for a in out if a)


def _repo_of(rec: dict) -> tuple[str, str] | None:
    try:
        refs = json.loads(rec.get("source_urls") or "[]")
    except ValueError:
        return None
    best = None
    for ref in refs:
        c = fixrefs.classify_reference(ref.get("url", ""))
        if c["kind"] != "other" or c.get("host") == "github.com":
            if "owner" in c:
                if ref.get("type") == "PACKAGE":
                    return c["owner"], c["repo"]
                best = best or (c["owner"], c["repo"])
    return best


def recover_by_version(rec: dict, api) -> tuple[list[dict], str | None]:
    """Pointers for the fixed versions of a record, and why nothing was found otherwise."""
    repo = _repo_of(rec)
    if not repo:
        return [], "no repository named by the advisory"
    try:
        entries = json.loads(rec.get("affected") or "[]")
    except ValueError:
        entries = []
    fixed = sorted({r["fixed"] for e in entries for r in e.get("ranges") or [] if r.get("fixed")})
    if not fixed:
        return [], "no fixed version"
    owner, name = repo
    eco, pkg = rec.get("ecosystem"), rec.get("package") or name
    tags: list[dict] = []
    for page in range(1, TAG_PAGES + 1):
        got = api.get(f"/repos/{owner}/{name}/tags", {"per_page": 100, "page": page}) or []
        tags += got
        if len(got) < 100:
            break
    aliases = [a.lower() for a in _aliases(rec)]
    ptrs, reasons = [], []
    for f in fixed:
        ft, pt = pick_tags(eco, pkg, f, tags)
        if not ft:
            reasons.append(f"no tag for {f}")
            continue
        if not pt:
            reasons.append(f"no earlier release tag before {ft['name']}")
            continue
        cmp_ = api.get(f"/repos/{owner}/{name}/compare/{pt['name']}...{ft['name']}")
        if not cmp_:
            reasons.append(f"{ft['name']}: compare unavailable")
            continue
        commits = [c for c in cmp_.get("commits") or [] if len(c.get("parents") or []) <= 1]
        named = [c for c in commits if any(a in ((c.get("commit") or {}).get("message") or "").lower()
                                           for a in aliases)]
        if len(named) == 1:
            pick, how = named[0], "message_names_advisory"
        elif len(commits) == 1:
            pick, how = commits[0], "single_commit_in_range"
        else:
            reasons.append(f"{ft['name']}: range has {len(commits)} commits and none names the advisory")
            continue
        ptrs.append(_ptr(owner, name, pick["sha"], "version_range",
                         {"fixed_version": f, "from": pt["name"], "to": ft["name"], "isolation": how}))
    return ptrs, ("; ".join(reasons) or None) if not ptrs else None


def recover(rec: dict, api) -> dict:
    """{'outcome': recovered | unresolved, 'pointers', 'reason', 'method'} for one record."""
    try:
        refs = json.loads(rec.get("source_urls") or "[]")
    except ValueError:
        refs = []
    ptrs, seen = [], set()
    for ref in refs:
        c = fixrefs.classify_reference(ref.get("url", ""))
        if (str(c.get("owner") or "").lower(), str(c.get("repo") or "").lower()) == ("github", "advisory-database"):
            continue                                        # a pull request of the advisory database edits the advisory, it is not the fix
        found = []
        if c["kind"] == "pull":
            found = resolve_pull(api, c["host"], c["owner"], c["repo"], c["number"])
        elif c["kind"] == "issue":
            found = resolve_issue(api, c["host"], c["owner"], c["repo"], c["number"])
        for p in found:
            if p["commit_key"] not in seen:
                seen.add(p["commit_key"])
                ptrs.append(p)
    if ptrs:
        return {"outcome": "recovered", "pointers": ptrs, "reason": None, "method": "reference"}
    ptrs, why = recover_by_version(rec, api)
    if ptrs:
        return {"outcome": "recovered", "pointers": ptrs, "reason": None, "method": "version_range"}
    return {"outcome": "unresolved", "pointers": [], "method": None,
            "reason": why or "no pull request, issue or fixed version to follow"}


def run(api=None, limit: int | None = None, ecosystems: list[str] | None = None,
        sleep=time.sleep, progress=None, max_consecutive_errors: int = 25, should_stop=None, workers: int = 8) -> dict:
    """Try to recover a fix commit for every mineable record that has none and was not tried yet.

    ``recover`` for up to ``workers`` records runs at once (each one is its own handful of GitHub requests, which is latency, not CPU, so an
    ecosystem with many unresolved records no longer waits out every one of them in turn - it waits out the slowest of each batch of ``workers``
    instead). Only the network call runs off the main thread; every write (the attempt, a recovered link, a record's refreshed fix_commits) still
    happens here, one at a time, in whatever order the calls finish in, same as a single commit's writes always have been.

    An API error on one record (a gateway error, a timeout) is counted in ``errors`` and the record is left untried so that the next run asks
    again; ``max_consecutive_errors`` errors in a row (by when they are seen to finish, not the order they were started in, now that more than
    one can be in flight) stop the stage (a dead network must not look like "nothing found"). A rate limit hit by any of them pauses new requests
    (the ones already in flight are allowed to finish) until it resets, then carries on with what was still untried, workers included.

    ``should_stop`` (see ``stopping``) is asked before starting a new record and during a rate-limit wait; when it says yes the stage ends with
    ``"paused": True`` in its counts, and the records it did not reach (including any already submitted but not yet started) are still untried."""
    import collections
    import requests
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    from app.backend.pipeline import stopping
    from app.backend.pipeline.ingest import codemine
    report = progress or (lambda **_: None)
    dconn = derived.connect()
    mg = derived.mongo_of(dconn)
    tried = mg.link_recovery_tried(dconn)
    todo = [r for r in store.select_mineable(("id", "key", "osv_id", "cve_id", "ecosystem", "package", "aliases", "source_urls", "affected",
                                              "fix_commits"), no_fix=True, ecosystems=ecosystems or None) if r["key"] not in tried]
    if limit:
        todo = todo[:int(limit)]
    counts = {"attempted": 0, "recovered": 0, "unresolved": 0, "commits": 0, "errors": 0}
    in_a_row = 0
    processed = 0
    paused = False
    width = max(1, int(workers))
    remaining = collections.deque(todo)
    pending: dict = {}                              # future -> record
    ex = ThreadPoolExecutor(max_workers=width)
    try:
        while remaining or pending:
            if stopping.stop(should_stop):
                for fut in pending:
                    fut.cancel()
                paused = True
                break
            while remaining and len(pending) < width:
                rec = remaining.popleft()
                pending[ex.submit(recover, rec, api)] = rec
            if not pending:
                break
            done, _ = wait(list(pending), timeout=1.0, return_when=FIRST_COMPLETED)
            reset_at = None
            for fut in done:
                rec = pending.pop(fut)
                if fut.cancelled():
                    continue
                try:
                    out = fut.result()
                    in_a_row = 0
                except codemine.RateLimited as e:
                    remaining.appendleft(rec)
                    reset_at = max(reset_at or 0, e.reset)
                    continue
                except requests.RequestException:
                    counts["errors"] += 1
                    in_a_row += 1
                    processed += 1
                    report(state="recover", label="Recovering fix commits", processed=processed, total=len(todo), counts=dict(counts))
                    if in_a_row >= max_consecutive_errors:
                        raise
                    continue
                processed += 1
                counts["attempted"] += 1
                counts[out["outcome"]] += 1
                from app.backend.pipeline.acceleration.publication import atomic, session_database
                from app.backend.pipeline.db.derived_mongo import MongoDerived
                def persist():
                    if out["pointers"]:
                        store.set_fix_commits([(rec["id"], json.dumps(out["pointers"]))])
                        codestore.queue_fix_commits([{**p, "advisory_id": rec["osv_id"] or rec["cve_id"], "cve_id": rec["cve_id"],
                                                      "source": "recovered", "ecosystem": rec.get("ecosystem")} for p in out["pointers"]])
                        codestore.refresh_records([rec["id"]])
                    mg.link_recovery_save(MongoDerived(session_database(dconn.db)), rec["key"], out["outcome"], out["method"], out["reason"], out["pointers"])
                atomic(persist)
                if out["pointers"]:counts["commits"] += len(out["pointers"])
                report(state="recover", label="Recovering fix commits", processed=processed, total=len(todo), counts=dict(counts))
            if reset_at is not None:
                for fut in list(pending):                          # a request already in flight keeps running; only the ones not yet started wait
                    if fut.cancel():
                        remaining.appendleft(pending.pop(fut))
                wait_s = max(0.0, reset_at - time.time()) + 5
                report(state="rate_limited", label="GitHub rate limit reached", until=time.time() + wait_s)
                if stopping.sleep(min(wait_s, 3600), should_stop, nap=sleep):
                    for fut in list(pending):
                        fut.cancel()
                    paused = True
                    break
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    dconn.close()
    return {**counts, "paused": True} if paused else counts
