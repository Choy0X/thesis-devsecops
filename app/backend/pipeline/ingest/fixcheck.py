"""Is a fix commit really the fix? A release-date plausibility check.

Advisories link commits in two ways, and only some of the links say "this is the
fix":

* **declared** - an OSV ``GIT`` range ``fixed`` event, an OSV reference typed
  ``FIX``, an NVD reference tagged ``Patch``;
* **mentioned** - any other commit URL (OSV ``WEB`` references, untagged NVD
  references): often the fix, but also the change that introduced the bug, a
  related feature, or a later hardening.

Taking every link as the fix puts mislabelled pairs in the dataset. Example:
GHSA-5f7m-mmpc-qhh4 (mysql, fixed in 2.14.0) lists two WEB commits; ``192fe45``
from 2012 adds support for old MySQL servers, ``310c6a7`` from 2017 is the fix.

The check compares the commit date with the package's release dates. For every
affected range with a fixed version F, let L be the newest release the range
covers below F (the last vulnerable release). The fix belongs to F and not to L,
so a fix commit is *plausible* when

    date(L) - SLACK  <  commit date  <=  date(F) + SLACK

(either bound is skipped when that release has no date; a ``last_affected``
range gives L only). One plausible range is enough (fixes are backported).

Result per commit: ``verified`` (plausible for a range of a linked record),
``suspect`` (every checkable range rules it out; the reason says why) or
``unknown`` (no release dates: unknown registry, open range, no version data).
The check uses the commit date stored by the miner and the registry dates
cached by ``depversions``: nothing is re-mined.
"""
from __future__ import annotations
import datetime
import json
import time

from app.backend.pipeline import codestore, store, stopping
from app.backend.pipeline.acceleration import operations
from app.backend.pipeline.ingest import depversions as dv

VERSION = 2                  # bump when the check changes: every mined commit is re-checked once
DECLARED = {"osv_git_range", "osv_ref_fix", "nvd_ref_patch"}
SLACK = datetime.timedelta(days=7)     # tag vs registry upload, time zones


def link_strength(vias) -> str:
    vias = json.loads(vias) if isinstance(vias, str) else (vias or [])
    return "declared" if any(v in DECLARED for v in vias) else "mentioned"


def _day(v) -> datetime.datetime | None:
    if not v:
        return None
    try:
        d = datetime.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    if d.tzinfo:
        d = d.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return d


def _span(days: float) -> str:
    days = abs(days)
    if days >= 365:
        return f"{days / 365.25:.1f} years"
    if days >= 60:
        return f"{days / 30.4:.0f} months"
    return f"{days:.0f} days"


def _registry_version(eco, rel: dict, v: str) -> str | None:
    """The registry's spelling of version ``v`` (Go lists 'v1.2.3', OSV '1.2.3')."""
    if v in rel:
        return v
    k = dv.VKey(eco, v)
    return next((r for r in rel if dv.VKey(eco, r) == k), None)


def _in_range(eco, v: str, r: dict) -> bool:
    intro = r.get("introduced") or "0"
    return intro == "0" or not (dv.VKey(eco, v) < dv.VKey(eco, intro))


def check_entry(commit_day: datetime.datetime, entry: dict) -> tuple[str, str, bool]:
    """(verdict, reason, bounded) of one commit against one affected package.
    ``bounded`` is true when a fixed release also bounded the date from above: a
    ``last_affected`` range alone only says the commit is newer than the last
    vulnerable release, which every later commit is."""
    eco, pkg = entry.get("ecosystem"), entry.get("package")
    ranges = [r for r in entry.get("ranges") or [] if r.get("fixed") or r.get("last_affected")]
    if not ranges or not pkg:
        return "unknown", "no fixed version", False
    rel = dv.release_dates(eco, pkg)
    if not rel:
        return "unknown", f"no release data for {pkg}", False
    k = lambda v: dv.VKey(eco, v)
    date = lambda v: _day(dv.release_date(eco, pkg, v)) if v else None
    fails, checked, weak_ok = [], 0, None
    for r in ranges:
        if r.get("fixed"):
            f_ver = _registry_version(eco, rel, r["fixed"])
            below = [v for v in rel if k(v) < k(r["fixed"]) and _in_range(eco, v, r)]
        else:
            f_ver = None
            below = [v for v in rel if not (k(r["last_affected"]) < k(v)) and _in_range(eco, v, r)]
        stable = [v for v in below if not dv.is_prerelease(eco, v)]
        l_ver = max(stable or below, key=k) if below else None
        lo, hi = date(l_ver), date(f_ver)
        if lo is None and hi is None:
            continue
        checked += 1
        if lo is not None and commit_day <= lo - SLACK:
            fails.append(((lo - commit_day).days,
                          f"committed {commit_day:%Y-%m-%d}, {_span((lo - commit_day).days)} before "
                          f"{l_ver} ({lo:%Y-%m-%d}), the last affected release"))
        elif hi is not None and commit_day > hi + SLACK:
            fails.append(((commit_day - hi).days,
                          f"committed {commit_day:%Y-%m-%d}, {_span((commit_day - hi).days)} after "
                          f"{f_ver} ({hi:%Y-%m-%d}), the fixed release"))
        else:
            span = " and ".join(x for x in (
                f"after {l_ver} ({lo:%Y-%m-%d})" if lo else "",
                f"by {f_ver} ({hi:%Y-%m-%d})" if hi else "") if x)
            why = f"{pkg}: committed {commit_day:%Y-%m-%d}, {span}"
            if hi is not None:
                return "verified", why, True
            weak_ok = weak_ok or why                    # keep looking for a bounded range
    if weak_ok:
        return "verified", weak_ok, False
    if not checked:
        return "unknown", f"no release dates for the affected versions of {pkg}", False
    return "suspect", f"{pkg}: " + max(fails)[1], False


def check(commit_date, entries_by_record: list[list[dict]]) -> tuple[str, str, str | None]:
    """(verdict, reason, package) over every linked record and package: verified
    if any range of any record verifies (``package`` is the one it verified for -
    e.g. a maintained fork rather than the abandoned original), suspect if some
    were checked and none verified."""
    day = _day(commit_date)
    if day is None:
        return "unknown", "no commit date", None
    suspect, unknown, weak = None, None, None
    for entries in entries_by_record:
        for e in entries:
            try:
                verdict, why, bounded = check_entry(day, e)
            except (MemoryError, stopping.Paused, operations.DeadlineExpired):
                raise
            except Exception as ex:
                from app.backend.pipeline.acceleration.failures import retryable,resource_pressure
                if operations.current() is not None and (resource_pressure(ex) or retryable(ex)):
                    raise
                verdict, why, bounded = "unknown", f"check failed: {str(ex)[:80]}", False
            if verdict == "verified" and bounded:
                return verdict, why, e.get("package")
            if verdict == "verified":
                weak = weak or (why, e.get("package"))     # a fixed-release match may follow
            elif verdict == "suspect":
                suspect = suspect or why
            else:
                unknown = unknown or why
    if weak:
        return "verified", weak[0], weak[1]
    return (("suspect", suspect, None) if suspect
            else ("unknown", unknown or "no affected versions", None))


def _entries(rec: dict) -> list[dict]:
    if not rec.get("affected") and rec.get("osv_id") and not rec.get("deps_checked_at"):
        try:                                      # records stored before 'affected' was kept
            return json.loads(dv.check_record(rec).get("affected") or "[]")
        except (MemoryError, stopping.Paused, operations.DeadlineExpired):
            raise
        except Exception as error:
            from app.backend.pipeline.acceleration.failures import retryable,resource_pressure
            if operations.current() is not None and (resource_pressure(error) or retryable(error)):
                raise
            return []
    return json.loads(rec.get("affected") or "[]")


def _linked_records(row: dict) -> list[dict]:
    ids = codestore.records_for_commit(row)
    if not ids:
        return []
    return store.records_by_ids(ids, ("id", "osv_id", "cve_id", "ecosystem", "affected", "deps_checked_at"))


def check_commit(row: dict, commit_date) -> tuple[str, str, str, str | None]:
    """(fix_check, reason, link, package verified for) of one queued fix commit."""
    verdict, why, pkg = check(commit_date, [_entries(r) for r in _linked_records(row)])
    return verdict, why, link_strength(row.get("via")), pkg


def verify_all(only_unchecked: bool = False, workers: int = 8, progress=None,
               quiet: bool = False, ecosystems: list[str] | None = None, should_stop=None) -> dict:
    """Check every mined fix commit (no re-mining: commit dates are stored) and
    update the records, whose pair counts and best pair skip suspect commits.
    With ``ecosystems`` (a campaign's scope) only the commits tagged with them are checked, and only their records are refreshed. ``should_stop`` (see ``stopping``) is asked
    after each commit: the checks done are stored, the others are dropped, and the result carries ``"paused": True`` (the records are refreshed by the run that finishes the job)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    report = progress or (lambda **_: None)
    rows = codestore.commits_by_status("mined", only_unchecked=only_unchecked, ecosystems=ecosystems)
    if not rows:
        if not quiet:
            print("No mined fix commits to verify.")
        report(state="done", label="Nothing to verify", processed=0, total=0)
        return {"checked": 0}
    print(f"verifying {len(rows):,} mined fix commits against package release dates")
    counts = {"verified": 0, "suspect": 0, "unknown": 0}
    by_link = {}
    t0, last = time.time(), 0.0
    paused, checked = False, 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(check_commit, r, r.get("commit_date")): r for r in rows}
        for i, fut in enumerate(as_completed(futs), 1):
            if should_stop is not None and should_stop():
                paused = True
                for f in futs:
                    f.cancel()
                break
            checked = i
            row = futs[fut]
            try:
                verdict, why, link, pkg = fut.result()
            except Exception as e:
                verdict, why, link, pkg = ("unknown", f"check failed: {str(e)[:80]}",
                                           link_strength(row.get("via")), None)
            codestore.set_fix_check(row["commit_key"], verdict, why, link, pkg)
            counts[verdict] += 1
            by_link[f"{link}/{verdict}"] = by_link.get(f"{link}/{verdict}", 0) + 1
            if verdict == "suspect":
                print(f"  suspect {row['owner']}/{row['repo']}@{row['sha'][:8]}: {why}")
            if time.time() - last >= 1 or i == len(rows):
                last = time.time()
                report(state="verify", label="Verifying fix commits", processed=i,
                       total=len(rows), counts=dict(counts))
    if paused:
        print(f"paused after {checked:,} of {len(rows):,} commits")
        report(state="paused", label="Paused", processed=checked, total=len(rows), counts=dict(counts))
        return {"checked": checked, **counts, "paused": True}
    n = codestore.refresh_records(None if ecosystems is None else [r["id"] for r in store.select_mineable(("id",), ecosystems=ecosystems)])
    try:                                       # what the commit and the advisory say, beside the dates
        from app.backend.pipeline.ingest import linkevidence
        linkevidence.refresh([r["commit_key"] for r in rows])
    except Exception as e:
        print(f"link evidence skipped: {e}")
    print(f"\n{len(rows):,} commits in {time.time() - t0:.0f}s: {counts['verified']:,} verified, "
          f"{counts['suspect']:,} suspect, {counts['unknown']:,} unknown")
    print("by link type: " + ", ".join(f"{k} {v:,}" for k, v in sorted(by_link.items())))
    print(f"{n:,} records updated (pairs of suspect commits no longer count)")
    report(state="done", label="Verification complete", processed=len(rows), total=len(rows),
           counts=dict(counts))
    return {"checked": len(rows), **counts}
