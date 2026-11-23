"""Affected and safe versions of a vulnerable dependency, per CVE record.

For every package an advisory affects, this keeps:

* the affected version ranges (OSV ``ECOSYSTEM`` / ``SEMVER`` events, in order:
  ``introduced`` .. ``fixed`` (exclusive) or .. ``last_affected`` (inclusive), plus
  explicit version lists);
* the first and the latest fixed release;
* the **safe version**: the newest published release (from the package registry)
  that no range covers;
* a status:
  - ``fixed``       a fixed release exists (upgrade to the safe version);
  - ``safe_newer``  no fix event, but a release newer than every affected version
                    exists and is not affected;
  - ``unsafe``      no fix and no unaffected newer release: still vulnerable;
  - ``unknown``     no usable version data.

Versions are compared with the ecosystem's own ordering, never as strings:
``pipeline/ecosystems/versions.py`` (PEP 440, SemVer, Maven ComparableVersion, RubyGems,
Composer, NuGet). Registry requests are paced per host, retried with backoff, and an outage is
never cached as "no such package".
"""
from __future__ import annotations
from contextlib import contextmanager
import datetime
import math
import json
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlsplit

import requests

from app.backend import compute
from app.backend.pipeline import store, stopping
from app.backend.pipeline.acceleration import operations
from app.backend.pipeline.ecosystems import versions as _ev

CACHE_DAYS = 7
UA = {"User-Agent": "thesis-vuln-framework (research; dependency version check)"}
TIMEOUT = (10, 30)

# ── version ordering ─────────────────────────────────────────────────────────

def VKey(ecosystem: str | None, v):
    """The comparable version of one ecosystem (cached; see ecosystems/versions.py)."""
    return _ev.key(ecosystem, v)


vkey = VKey


def is_prerelease(ecosystem: str | None, v: str) -> bool:
    return _ev.is_prerelease(ecosystem, v)


# ── affected ranges ──────────────────────────────────────────────────────────

def affected_from_osv(rec: dict, ecosystem: str | None = None) -> list[dict]:
    """Per affected package: ordered intervals from ECOSYSTEM/SEMVER ranges and the
    explicit version list (GIT ranges are commits, handled as fix commits)."""
    out = []
    for aff in rec.get("affected", []) or []:
        pkg = aff.get("package") or {}
        eco = pkg.get("ecosystem")
        if ecosystem and (eco or "").split(":")[0].lower() != ecosystem.split(":")[0].lower():
            continue
        intervals = []
        for rng in aff.get("ranges", []) or []:
            if rng.get("type") == "GIT":
                continue
            cur = None
            for ev in rng.get("events", []) or []:
                if "introduced" in ev:
                    if cur:
                        intervals.append(cur)
                    cur = {"introduced": str(ev["introduced"])}
                elif "fixed" in ev or "last_affected" in ev:
                    cur = cur or {"introduced": "0"}
                    if "fixed" in ev:
                        cur["fixed"] = str(ev["fixed"])
                    else:
                        cur["last_affected"] = str(ev["last_affected"])
                    intervals.append(cur)
                    cur = None
            if cur:
                intervals.append(cur)              # open: no fix (yet)
        versions = [str(v) for v in (aff.get("versions") or [])]
        if pkg.get("name") and (intervals or versions):
            out.append({"ecosystem": eco, "package": pkg["name"], "ranges": intervals,
                        "versions": versions[:200], "versions_total": len(versions)})
    return out


def is_affected(ecosystem: str | None, version: str, entry: dict) -> bool:
    """Is ``version`` affected by one package entry of an advisory?"""
    if not version:
        return False
    listed = {_ev.normalize(ecosystem, x) for x in (entry.get("versions") or [])}
    if _ev.normalize(ecosystem, version) in listed:
        return True
    v = vkey(ecosystem, version)
    for r in entry.get("ranges") or []:
        intro = r.get("introduced") or "0"
        if intro != "0" and v < vkey(ecosystem, intro):
            continue
        if r.get("fixed"):
            if v < vkey(ecosystem, r["fixed"]):
                return True
        elif r.get("last_affected"):
            if not (vkey(ecosystem, r["last_affected"]) < v):
                return True
        else:
            return True                            # open range: every later version
    return False


def describe_ranges(entry: dict) -> str:
    parts = []
    for r in entry.get("ranges") or []:
        lo = "" if (r.get("introduced") or "0") == "0" else f">={r['introduced']}, "
        if r.get("fixed"):
            parts.append(f"{lo}<{r['fixed']}")
        elif r.get("last_affected"):
            parts.append(f"{lo}<={r['last_affected']}")
        else:
            parts.append(f"{lo or '>=0, '}no fix".replace(", no fix", " (no fix)"))
    if entry.get("versions_total") and not parts:
        n = entry["versions_total"]
        parts.append(f"{n} listed version{'s' if n != 1 else ''}")
    return " · ".join(parts) or "—"


# ── registries ───────────────────────────────────────────────────────────────

_local = threading.local()


def _session() -> requests.Session:
    """One keep-alive session per thread (a new TLS handshake per request was a
    large part of the time)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _local.session = s
    return s


class RegistryUnavailable(Exception):
    """A registry could not answer (rate limit, server error, timeout): not the same as
    'no such package', so nothing is cached for it."""

    def __init__(self, message, *, retry_at=None):
        super().__init__(message)
        self.retry_at = retry_at


class _PackageMissing(Exception):
    """Only the primary package lookup can supply authoritative absence."""
    def __init__(self, status):
        self.status = status
        super().__init__(f'Package endpoint returned HTTP {status}')


RETRIES = 3
OUTAGE_COOLDOWN = 60.0                      # seconds a host is left alone after repeated failures
DEFAULT_INTERVAL = 0.05                     # at most 20 requests per second per host
MIN_INTERVAL = {"crates.io": 1.0, "hex.pm": 0.7}     # crates.io asks for at most one request per second; hex.pm allows 100 requests a minute without a key
_NEXT_OK: dict[str, float] = {}
_DOWN_UNTIL: dict[str, float] = {}
_PACE_LOCK = threading.Lock()
_sleep = time.sleep
_mono = time.monotonic


def _wait(seconds):
    if operations.current() is None:
        _sleep(seconds)
        return
    left = seconds
    while left > 0:
        operations.check()
        step = min(left, operations.timeout(.1))
        _sleep(step)
        left -= step
    operations.check()


def _pace(host: str) -> None:
    """Space requests to one host by its policy (shared by every worker thread)."""
    with _PACE_LOCK:
        now = _mono()
        slot = max(now, _NEXT_OK.get(host, 0.0))
        _NEXT_OK[host] = slot + MIN_INTERVAL.get(host, DEFAULT_INTERVAL)
    if slot > now:
        _wait(slot - now)


def _unavailable(host, message):
    left = max(0, _DOWN_UNTIL.get(host, 0) - _mono())
    return RegistryUnavailable(message, retry_at=time.time() + left if left else None)


def _get(url: str, **kw):
    """Return 200 or optional-page absence; all other failures remain unavailable.

    Primary package lookups request a typed 404/410 proof. Secondary date pages
    retain None for absence and can never create a negative package cache row.
    """
    primary = kw.pop('_package_lookup', False)
    host = urlsplit(url).hostname or ""
    operations.check()
    if _mono() < _DOWN_UNTIL.get(host, 0.0):
        raise _unavailable(host, f"{host} is cooling down after repeated failures")
    headers = {**UA, **kw.pop("headers", {})}
    last, retry_after = "no attempt", 0.0
    for attempt in range(RETRIES):
        _pace(host)
        operations.check()
        retry_after = 0.0
        try:
            timeout = tuple(operations.timeout(value) for value in TIMEOUT) if operations.current() else TIMEOUT
            r = _session().get(url, headers=headers, timeout=timeout, **kw)
            operations.check()
        except requests.RequestException as error:
            last = type(error).__name__
        else:
            if r.status_code == 200:
                return r
            if r.status_code in (404, 410):
                if primary:
                    raise _PackageMissing(r.status_code)
                return None
            if r.status_code != 429 and r.status_code < 500:
                raise RegistryUnavailable(f"{host}: HTTP {r.status_code}")
            last = f"HTTP {r.status_code}"
            try:
                retry_after = float(r.headers.get("Retry-After", 0) or 0)
                if not math.isfinite(retry_after) or retry_after < 0:
                    retry_after = 0.0
            except (TypeError, ValueError):
                retry_after = 0.0
        if attempt < RETRIES - 1:
            _wait(max(retry_after, 2 ** attempt + random.uniform(0, 1)))
    _DOWN_UNTIL[host] = _mono() + max(OUTAGE_COOLDOWN, retry_after)
    raise _unavailable(host, f"{host}: {last} after {RETRIES} attempts")


def _go_escape(module: str) -> str:
    return "".join(f"!{c.lower()}" if c.isupper() else c for c in module)


def _iso(v) -> str | None:
    """A registry timestamp as ``YYYY-MM-DDTHH:MM:SS`` (UTC when the zone is given)."""
    if not v:
        return None
    try:
        if isinstance(v, (int, float)):                          # epoch milliseconds
            d = datetime.datetime.fromtimestamp(v / 1000, datetime.timezone.utc)
        else:
            d = datetime.datetime.fromisoformat(str(v).strip().replace("Z", "+00:00").replace(" ", "T"))
        if d.tzinfo:
            d = d.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return d.isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None


_MAVEN_DIR = re.compile(r'<a href="([^"/]+)/"[^>]*>[^<]*</a>\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})')


def _container(value, kind, label):
    if not isinstance(value, kind):
        raise RegistryUnavailable('Malformed registry container: ' + label)
    return value


def _member(document, name, kind):
    return _container(_container(document, dict, 'document').get(name), kind, name)


def _records(value):
    rows = _container(value, list, 'records')
    if any(not isinstance(row, dict) for row in rows):
        raise RegistryUnavailable('Malformed registry record')
    return rows


def _strings(value):
    values = _container(value, list, 'versions')
    if any(not isinstance(v, str) or not v for v in values):
        raise RegistryUnavailable('Malformed registry version')
    return values


def _nuget_dates(package: str) -> dict[str, str]:
    r = _get(f"https://api.nuget.org/v3/registration5-gz-semver2/{package.lower()}/index.json")
    if not r:
        return {}
    out = {}
    for page in _records(_member(r.json(), 'items', list)):
        leaves = page.get("items")
        if leaves is None and isinstance(page.get("@id"), str):
            pr = _get(page["@id"])
            leaves = _member(pr.json(), 'items', list) if pr else []
        for leaf in _records(leaves):
            ce = _member(leaf, 'catalogEntry', dict)
            pub = _iso(ce.get("published"))
            if ce.get("version") and pub and not pub.startswith("1900"):   # 1900 = unlisted
                out[ce["version"]] = pub
    return out


# registries whose version list carries the release dates at no extra cost
_DATES_FREE = {"pypi", "crates.io", "rubygems", "packagist", "go", "hex"}


def _fetch(ecosystem: str, package: str, dates: bool = True, *, prove_missing: bool = False) -> dict[str, str | None] | None:
    """Published (non-yanked) versions of a package with their release dates
    ({version: ISO date or None}), or None if the registry has no such package.
    ``dates=False`` skips the extra cost of dates where there is one (npm's full
    document, Maven's directory listing, NuGet's registration): collecting versions
    does not need them, only the fix-commit check does."""
    eco = (ecosystem or "").split(":")[0].lower()
    def primary(url, **kwargs):
        return _get(url, _package_lookup=prove_missing, **kwargs)
    if eco == "pypi":
        r = primary(f"https://pypi.org/pypi/{quote(package)}/json")
        if not r:
            return None
        out = {}
        for v, files in _member(r.json(), 'releases', dict).items():
            files = [f for f in _records(files) if not f.get("yanked")]
            if files:
                out[v] = min((_iso(f.get("upload_time_iso_8601") or f.get("upload_time")) or "9999"
                              for f in files))
                out[v] = None if out[v] == "9999" else out[v]
        return out
    if eco == "npm" and not dates:                    # small "corgi" document, no dates
        r = primary(f"https://registry.npmjs.org/{quote(package, safe='@')}",
                 headers={"Accept": "application/vnd.npm.install-v1+json"})
        return {v: None for v in _member(r.json(), 'versions', dict)} if r else None
    if eco == "npm":
        r = primary(f"https://registry.npmjs.org/{quote(package, safe='@')}")   # @scope%2Fname
        if not r:
            return None
        doc = r.json()
        times = _member(doc, 'time', dict)
        return {v: _iso(times.get(v)) for v in _member(doc, 'versions', dict)}
    if eco == "maven":
        if ":" not in package:
            return None
        group, artifact = package.split(":", 1)
        base = f"https://repo1.maven.org/maven2/{group.replace('.', '/')}/{artifact}/"
        r = primary(base + "maven-metadata.xml")
        if not r:
            return None
        document = ET.fromstring(r.content)
        if document.tag != 'metadata':
            raise RegistryUnavailable('Malformed Maven metadata')
        out = {e.text: None for e in document.iter("version") if e.text}
        if not dates:
            return out
        try:                                  # the directory listing carries upload dates
            d = _get(base)
            for v, ts in (_MAVEN_DIR.findall(d.text) if d else []):
                if v in out:
                    out[v] = _iso(ts)
        except (RegistryUnavailable, MemoryError, stopping.Paused, operations.DeadlineExpired):
            raise
        except Exception as error:
            raise RegistryUnavailable('Registry date response failed: ' + type(error).__name__) from error
        return out
    if eco == "crates.io":
        r = primary(f"https://crates.io/api/v1/crates/{quote(package)}/versions")
        return ({v["num"]: _iso(v.get("created_at")) for v in _records(_member(r.json(), 'versions', list))
                 if not v.get("yanked")} if r else None)
    if eco == "rubygems":
        r = primary(f"https://rubygems.org/api/v1/versions/{quote(package)}.json")
        if not r:
            return None
        out = {}
        for v in _records(r.json()):          # one entry per platform build: earliest wins
            d = _iso(v.get("created_at"))
            if v["number"] not in out or (d and (out[v["number"]] or "9999") > d):
                out[v["number"]] = d
        return out
    if eco == "packagist":
        r = primary(f"https://repo.packagist.org/p2/{package}.json")
        if not r:
            return None
        packages = _member(r.json(), 'packages', dict)
        return {v["version"]: _iso(v.get("time"))
                for v in _records(packages.get(package)) if v.get("version")}
    if eco == "hex":                          # hex.pm lists every release of a package with the time it was published
        r = primary(f"https://hex.pm/api/packages/{quote(package)}")
        if not r:
            return None
        return {v["version"]: _iso(v.get("inserted_at")) for v in _records(_member(r.json(), 'releases', list)) if v.get("version")}
    if eco == "go":                           # dates per version on demand (release_date)
        r = primary(f"https://proxy.golang.org/{_go_escape(package)}/@v/list")
        return {l.strip(): None for l in r.text.splitlines() if l.strip()} if r else None
    if eco == "nuget":
        r = primary(f"https://api.nuget.org/v3-flatcontainer/{package.lower()}/index.json")
        if not r:
            return None
        out = {v: None for v in _strings(_member(r.json(), 'versions', list))}
        if not dates:
            return out
        try:
            for v, d in _nuget_dates(package).items():
                if v in out or v.lower() in out:
                    out[v if v in out else v.lower()] = d
        except (RegistryUnavailable, MemoryError, stopping.Paused, operations.DeadlineExpired):
            raise
        except Exception as error:
            raise RegistryUnavailable('Registry date response failed: ' + type(error).__name__) from error
        return out
    return None


def _fetch_releases(ecosystem: str, package: str) -> list[str] | None:
    got = _fetch(ecosystem, package)
    return None if got is None else list(got)


_mem: dict[tuple, dict | None] = {}
_mem_lock = threading.Lock()
_stats = {"fetched": 0}
_fetch_locks = [threading.Lock() for _ in range(64)]


def preload_cache() -> int:
    """All fresh ``package_versions`` rows into memory with one query (a long run
    otherwise opens a connection per package lookup)."""
    now = datetime.datetime.utcnow()
    n = 0
    rows = store.registry_rows()
    with _mem_lock:
        for r in rows:
            try:
                age = now - datetime.datetime.fromisoformat(r["fetched_at"][:19])
            except (TypeError, ValueError):
                continue
            if age >= datetime.timedelta(days=CACHE_DAYS if r["ok"] else 1):
                continue
            if not r["ok"] and not (type(r.get("negative_status")) is int and r["negative_status"] in (404, 410)):
                continue
            val = None
            if r["ok"]:
                dates = json.loads(r["dates"]) if r["dates"] else {}
                val = {"map": {v: dates.get(v) for v in json.loads(r["versions"])},
                       "dated": r["dates"] is not None}
            _mem[(r["ecosystem"], r["package"])] = val
            n += 1
    return n


@contextmanager
def _package_fetch(key):
    # Fixed stripes bound coordination memory; no global cache lock spans I/O.
    lock = _fetch_locks[hash(key) % len(_fetch_locks)]
    if operations.current() is not None:
        with operations.acquire(lock):
            yield
    else:
        if not lock.acquire(timeout=180):
            raise RegistryUnavailable('Registry package lookup is busy')
        try:
            yield
        finally:
            lock.release()


def _registry(ecosystem: str, package: str, need_dates: bool = False) -> dict | None:
    """Coalesce package retrieval; only proven 404/410 responses cache absence."""
    key = ((ecosystem or "").lower(), package)
    try:
        with _package_fetch(key):
            return _registry_locked(ecosystem, package, key, need_dates)
    except RegistryUnavailable:
        if operations.current() is not None:
            raise
        return None


def _registry_locked(ecosystem, package, key, need_dates):
    operations.check()
    with _mem_lock:
        if key in _mem and (_mem[key] is None or not need_dates or _mem[key]["dated"]):
            return _mem[key]
    row = store.registry_row(*key)
    if row:
        age = datetime.datetime.utcnow() - datetime.datetime.fromisoformat(row["fetched_at"][:19])
        fresh = age < datetime.timedelta(days=CACHE_DAYS if row["ok"] else 1)
        proven = row["ok"] or type(row.get("negative_status")) is int and row["negative_status"] in (404, 410)
        if fresh and proven and (not row["ok"] or row["dates"] is not None or not need_dates):
            val = None
            if row["ok"]:
                dates = json.loads(row["dates"]) if row["dates"] else {}
                val = {"map": {v: dates.get(v) for v in json.loads(row["versions"])},
                       "dated": row["dates"] is not None}
            with _mem_lock:
                _mem[key] = val
            return val
    dated = need_dates or key[0].split(":")[0] in _DATES_FREE
    negative_status = None
    try:
        got = _fetch(ecosystem, package, dates=dated, prove_missing=True)
    except _PackageMissing as error:
        got, negative_status = None, error.status
    except (MemoryError, stopping.Paused, operations.DeadlineExpired, RegistryUnavailable):
        raise
    except Exception as error:
        raise RegistryUnavailable('Registry response failed: ' + type(error).__name__) from error
    operations.check()
    if got is None and negative_status is None:
        return None  # Unsupported registry or unproven absence is never negative evidence.
    val = None if got is None else {"map": got, "dated": dated}
    _stats["fetched"] += 1
    store.registry_save(*key, json.dumps(list(got or [])), int(got is not None), datetime.datetime.utcnow().isoformat(),
                        json.dumps({v: d for v, d in (got or {}).items() if d}) if dated else None,
                        **({'negative_status': negative_status} if negative_status is not None else {}))
    with _mem_lock:
        _mem[key] = val
    return val


def releases(ecosystem: str, package: str) -> list[str] | None:
    """Published releases of a package (registry), or None if unavailable."""
    got = _registry(ecosystem, package)
    return None if got is None else list(got["map"])


def release_dates(ecosystem: str, package: str) -> dict[str, str | None] | None:
    """{version: ISO release date or None} of a package, or None if unavailable."""
    got = _registry(ecosystem, package, need_dates=True)
    return None if got is None else got["map"]


def release_date(ecosystem: str, package: str, version: str) -> str | None:
    """Release date of one version (Go: fetched per version from the module proxy)."""
    dates = release_dates(ecosystem, package) or {}
    if dates.get(version):
        return dates[version]
    if (ecosystem or "").split(":")[0].lower() != "go" or version not in dates:
        return None
    try:
        r = _get(f"https://proxy.golang.org/{_go_escape(package)}/@v/{quote(version)}.info")
        d = _iso(r.json().get("Time")) if r else None
    except (MemoryError, stopping.Paused, operations.DeadlineExpired):
        raise
    except RegistryUnavailable:
        if operations.current() is not None:
            raise
        d = None
    except Exception as error:
        if operations.current() is not None:
            raise RegistryUnavailable('Registry date response failed: ' + type(error).__name__) from error
        d = None
    if d:
        key = ((ecosystem or "").lower(), package)
        with _mem_lock:
            if _mem.get(key):
                _mem[key]["map"][version] = d
            known = {v: x for v, x in (_mem.get(key) or {"map": {}})["map"].items() if x}
        store.registry_set_dates(*key, json.dumps(known))
    return d


# ── evaluation ───────────────────────────────────────────────────────────────

def evaluate(entry: dict, rel: list[str] | None) -> dict:
    """Fixed / safe versions and status of one affected package."""
    eco = entry.get("ecosystem")
    k = lambda v: vkey(eco, v)
    ranges = entry.get("ranges") or []
    fixed = sorted({r["fixed"] for r in ranges if r.get("fixed")}, key=k)
    open_range = any(not r.get("fixed") and not r.get("last_affected") for r in ranges)
    bounds = [r.get("fixed") or r.get("last_affected") for r in ranges
              if r.get("fixed") or r.get("last_affected")] + list(entry.get("versions") or [])
    highest = max(bounds, key=k) if bounds else None
    out = {"first_fixed": fixed[0] if fixed else None, "latest_fixed": fixed[-1] if fixed else None,
           "highest_affected": highest, "open_range": open_range,
           "registry": rel is not None, "latest_version": None, "safe_version": None,
           "safe_is_prerelease": False}
    if not ranges and not entry.get("versions"):
        return {**out, "status": "unknown"}
    if rel:
        stable = [v for v in rel if not is_prerelease(eco, v)]
        out["latest_version"] = max(stable or rel, key=k)
        safe = [v for v in rel if not is_affected(eco, v, entry)]
        # never recommend a downgrade below the vulnerability: with a fix, at least
        # the first fixed release; without one, newer than every affected version
        if open_range:
            safe = []
        elif fixed:
            safe = [v for v in safe if not (k(v) < k(fixed[0]))]
        elif not highest:
            safe = []
        else:
            safe = [v for v in safe if k(highest) < k(v)]
        safe_stable = [v for v in safe if not is_prerelease(eco, v)]
        if safe_stable or safe:
            out["safe_version"] = max(safe_stable or safe, key=k)
            out["safe_is_prerelease"] = not safe_stable
    elif fixed:
        out["safe_version"] = fixed[-1]                  # best known without the registry
    if open_range:
        # an introduced event without fix/last_affected covers every later release:
        # still vulnerable at the top, whatever older branches were fixed
        out["safe_version"], out["safe_is_prerelease"] = None, False
        status = "unsafe"
    elif fixed:
        status = "fixed"
    elif out["safe_version"]:
        status = "safe_newer"
    else:
        status = "unsafe"
    return {**out, "status": status}


def merge_packages(entries: list[dict]) -> list[dict]:
    """One entry per (ecosystem, package): an advisory lists a package once per
    affected branch, and a safe version must be outside *all* of its ranges."""
    out: dict[tuple, dict] = {}
    for e in entries:
        key = ((e.get("ecosystem") or "").lower(), e.get("package"))
        if key not in out:
            out[key] = {"ecosystem": e.get("ecosystem"), "package": e.get("package"),
                        "ranges": [], "versions": [], "versions_total": 0}
        m = out[key]
        m["ranges"] += [r for r in e.get("ranges") or [] if r not in m["ranges"]]
        m["versions"] += [v for v in e.get("versions") or [] if v not in m["versions"]]
        m["versions_total"] += int(e.get("versions_total") or 0)
    return list(out.values())


def summarise(entries: list[dict], ecosystem: str | None = None) -> dict:
    """Record-level columns. The headline is the advisory's main package (OSV lists
    it first; the record's ecosystem preferred); other packages - e.g. repackaged
    forks - are counted in the note, and all of them stay in ``affected``."""
    if not entries:
        return {"affected_versions": None, "fixed_version": None, "safe_version": None,
                "dependency_status": "unknown"}
    same = [e for e in entries if (e.get("ecosystem") or "").lower() == (ecosystem or "").lower()]
    pick = (same or entries)[0]
    others = [e for e in entries if e is not pick]
    note = ""
    if others:
        unsafe = sum(e.get("status") == "unsafe" for e in others)
        note = f" (+{len(others)} other package{'s' if len(others) > 1 else ''}" \
               + (f", {unsafe} unsafe" if unsafe else "") + ")"
    return {"affected_versions": describe_ranges(pick) + note,
            "fixed_version": pick.get("first_fixed"), "safe_version": pick.get("safe_version"),
            "dependency_status": pick.get("status")}


# ── per record ───────────────────────────────────────────────────────────────

_zips: dict[str, object] = {}
_zip_lock = threading.Lock()


def _local_osv(osv_id: str, ecosystem: str | None) -> dict | None:
    """The advisory from the OSV bulk dump already downloaded by Ingest
    (data/raw/osv_<ecosystem>.zip), without any network request."""
    import pathlib
    import zipfile
    from app.backend.pipeline.ingest.osv import RAW, _slug
    paths = ([RAW / f"osv_{_slug(ecosystem)}.zip"] if ecosystem else []) + sorted(RAW.glob("osv_*.zip"))
    for path in dict.fromkeys(paths):
        if not pathlib.Path(path).exists():
            continue
        with _zip_lock:                       # one ZipFile per dump, reads serialised
            z = _zips.get(str(path))
            if z is None:
                try:
                    z = _zips[str(path)] = zipfile.ZipFile(path)
                except (OSError, zipfile.BadZipFile):
                    continue
            try:
                return json.loads(z.read(f"{osv_id}.json"))
            except KeyError:
                continue
            except (MemoryError, stopping.Paused, operations.DeadlineExpired):
                raise
            except Exception as error:
                if isinstance(error, RegistryUnavailable) and operations.current() is not None:
                    raise
                return None  # A damaged archive entry can still use the remote fallback.

    return None


def _osv_record(osv_id: str, ecosystem: str | None) -> dict | None:
    """An advisory's OSV JSON, for records stored before 'affected' was kept: the
    local bulk dump first, then GCS, then the OSV API."""
    local = _local_osv(osv_id, ecosystem)
    if local is not None:
        return local
    urls = []
    if ecosystem:
        urls.append(f"https://storage.googleapis.com/osv-vulnerabilities/{quote(ecosystem)}/{quote(osv_id)}.json")
    urls.append(f"https://api.osv.dev/v1/vulns/{quote(osv_id)}")
    for u in urls:
        try:
            r = _get(u)
            if r:
                return r.json()
        except (MemoryError, stopping.Paused, operations.DeadlineExpired):
            raise
        except Exception as error:
            if operations.current() is not None:
                if isinstance(error, RegistryUnavailable):
                    raise
                raise RegistryUnavailable('OSV response failed: ' + type(error).__name__) from error
            continue
    return None


_eval_cache: dict[str, dict] = {}
_eval_lock = threading.Lock()


def _evaluated(entry: dict) -> dict:
    """``evaluate`` of one package entry, memoised: many advisories share a
    package and the same ranges (e.g. every GHSA / PYSEC pair of one CVE)."""
    key = json.dumps([entry.get("ecosystem"), entry.get("package"), entry.get("ranges"),
                      entry.get("versions")], sort_keys=True)
    with _eval_lock:
        hit = _eval_cache.get(key)
    if hit is not None:
        return hit
    res = evaluate(entry, releases(entry.get("ecosystem"), entry["package"]))
    with _eval_lock:
        if len(_eval_cache) > 200_000:
            _eval_cache.clear()
        _eval_cache[key] = res
    return res


def record_fields(rec: dict) -> dict:
    """The dependency-version fields of one record (no database write)."""
    entries = json.loads(rec["affected"]) if rec.get("affected") else None
    if entries is None and rec.get("osv_id"):
        osv = _osv_record(rec["osv_id"], rec.get("ecosystem"))
        entries = affected_from_osv(osv) if osv else []
    entries = merge_packages(entries or [])
    for e in entries:
        e.update(_evaluated(e))
    return {"affected": json.dumps(entries) if entries else None,
            **summarise(entries, rec.get("ecosystem")),
            "deps_checked_at": datetime.datetime.utcnow().isoformat() + "Z"}


def _write(rows: list[tuple[dict, int]]) -> None:
    if not rows:
        return
    store.update_records([{**f, "id": rid} for f, rid in rows])


def check_record(rec: dict) -> dict:
    """Compute and store the dependency-version fields of one record."""
    fields = record_fields(rec)
    _write([(fields, rec["id"])])
    return fields


def records_to_check(max_age_days: int = CACHE_DAYS, limit: int | None = None,
                     only_mined: bool = False, ecosystems: list[str] | None = None) -> list[dict]:
    """Records not checked in the last ``max_age_days``; with ``only_mined``, only
    those whose fix was mined (``code_status = 'mined'``); with ``ecosystems``, only those of the ecosystems."""
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=max_age_days)).isoformat()
    rows = store.select_mineable(("id", "osv_id", "cve_id", "ecosystem", "affected"), only_mined=only_mined, deps_stale_before=cutoff,
                                 ecosystems=ecosystems, order="ecosystem", limit=limit)
    return compute.interleave(rows, key=lambda r: r["ecosystem"])         # every registry host works at once; each paces its own requests


def count_to_check(max_age_days: int = CACHE_DAYS, only_mined: bool = False, ecosystems: list[str] | None = None) -> int:
    """How many records ``records_to_check`` would return (without loading them)."""
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=max_age_days)).isoformat()
    return store.count_mineable_records(only_mined=only_mined, deps_stale_before=cutoff, ecosystems=ecosystems)


def _fmt_eta(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    return f"{h}h {rem // 60:02d}m" if h else f"{rem // 60}m {rem % 60:02d}s"


def collect(workers: int = 24, limit: int | None = None, progress=None, beat=None,
            batch: int = 500, only_mined: bool = False, ecosystems: list[str] | None = None, should_stop=None) -> dict:
    """Dependency versions for the records not checked in the last CACHE_DAYS days:
    with ``only_mined`` (what Mine does, after mining) only the records whose fix
    was mined, otherwise every record with an affected package.

    Fast path: advisories come from the local OSV dumps, registry versions are
    fetched once per package over keep-alive connections (without release dates)
    and cached, evaluations are shared between records of the same package, and
    results are written in batches. ``should_stop`` (see ``stopping``) is asked after each record: the results so far are written, the others are dropped (they are checked
    again by the next run) and the result carries ``"paused": True``."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    report = progress or (lambda **_: None)
    todo = records_to_check(limit=limit, only_mined=only_mined, ecosystems=ecosystems)
    what = "mined records" if only_mined else "records"
    if not todo:
        print(f"dependency versions: all {what} are up to date")
        return {"checked": 0}
    cached = preload_cache()
    _stats["fetched"] = 0
    print(f"dependency versions: checking {len(todo):,} {what} (affected ranges -> fixed / safe "
          f"version) with {workers} workers; {cached:,} packages already cached")
    counts: dict[str, int] = {}
    pending: list[tuple[dict, int]] = []
    t0, last, last_log = time.time(), 0.0, 0
    paused, handled = False, 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(record_fields, r): r["id"] for r in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            if should_stop is not None and should_stop():
                paused = True
                for f in futs:
                    f.cancel()
                break
            handled = i
            try:
                fields = fut.result()
                pending.append((fields, futs[fut]))
                st = fields["dependency_status"]
            except Exception as e:
                st = "error"
                print(f"  dependency check failed: {str(e)[:120]}")
            counts[st] = counts.get(st, 0) + 1
            if len(pending) >= batch:
                _write(pending)
                pending = []
            now = time.time()
            if now - last >= 1 or i == len(todo):
                last = now
                rate = i / max(now - t0, 1e-6)
                report(state="deps", label=f"Checking dependency versions · {rate:,.0f}/s · "
                       f"ETA {_fmt_eta((len(todo) - i) / max(rate, 1e-6))}", processed=i,
                       total=len(todo), counts={**counts, "packages fetched": _stats["fetched"]})
                if beat:
                    beat()
            if i - last_log >= 5000:
                last_log = i
                print(f"  {i:,}/{len(todo):,} records, {i / max(now - t0, 1e-6):,.0f}/s, "
                      f"{_stats['fetched']:,} packages fetched")
    _write(pending)
    if paused:
        print(f"dependency versions: paused after {handled:,} of {len(todo):,} {what}")
        return {"checked": handled, **counts, "paused": True}
    took = time.time() - t0
    print(f"dependency versions: {len(todo):,} {what} in {took:.0f}s ({len(todo) / max(took, 1e-6):,.0f}/s; "
          f"{_stats['fetched']:,} packages fetched, the rest cached): "
          + ", ".join(f"{k} {v:,}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
    return {"checked": len(todo), **counts}
