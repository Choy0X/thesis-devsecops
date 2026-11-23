"""The ecosystem adapter: everything about an ecosystem that the pipeline must not hard-code.

The mined npm data is the template. Every other ecosystem flows through the same
stages (ingest, link, mine, extract, group, view) and must produce the same record
schema; what differs per ecosystem lives here: how a package name is canonicalised,
which file names are tests, vendored or generated code, and which files describe
dependencies and configuration (kept as evidence). Version semantics and registry
lookups are added in ``versions`` (one module per ecosystem family).

An adapter is data plus small functions, so adding an ecosystem is: write one adapter,
pass the shared conformance suite (``tests/test_ecosystems.py``), done.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, urlsplit

BUCKET = "https://osv-vulnerabilities.storage.googleapis.com/{eco}/all.zip"

# The global rules the miner always used; every ecosystem inherits them.
COMMON_TEST = re.compile(
    r"(^|/)(tests?|testing|spec|specs|__tests__|testdata|test_data|fixtures)/"
    r"|(^|/)test_[^/]+$|_test\.[a-z]+$|\.(spec|test)\.[jt]sx?$|(^|/)conftest\.py$", re.I)
COMMON_VENDOR = re.compile(
    r"(^|/)(docs?|vendor|vendored|third[_-]party|node_modules|dist|site-packages|\.github)/", re.I)
COMMON_GENERATED = re.compile(r"\.min\.(js|css)$|\.bundle\.js$|-min\.js$", re.I)


def _compile(patterns: tuple[str, ...]) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in patterns]


@dataclass(frozen=True)
class EcosystemAdapter:
    name: str                                    # the OSV ecosystem id (bucket path and record value)
    canon: Callable[[str], str]                  # canonical form of one package name
    manifest_files: tuple[str, ...] = ()         # dependency / configuration files (basename globs)
    test_patterns: tuple[str, ...] = ()
    vendor_patterns: tuple[str, ...] = ()
    generated_patterns: tuple[str, ...] = ()
    package_less: bool = False                   # advisories carry git ranges, not packages (GIT, Linux, OSS-Fuzz)

    def zip_url(self) -> str:
        return BUCKET.format(eco=quote(self.name))

    # -- identity -------------------------------------------------------------------------
    def canonical_package(self, name: str | None) -> str:
        return self.canon((name or "").strip())

    def package_key(self, packages: str | None) -> str | None:
        """'<ecosystem>:<first canonical package>' for a (comma-joined) package field."""
        names = sorted({self.canonical_package(p) for p in (packages or "").split(",") if p.strip()} - {""})
        return f"{self.name.lower()}:{names[0]}" if names else None

    # -- paths ------------------------------------------------------------------------------
    def _hit(self, which: str, path: str) -> bool:
        return any(rx.search(path) for rx in _cached(self, which))

    def is_test_path(self, path: str) -> bool:
        return bool(COMMON_TEST.search(path)) or self._hit("test", path)

    def is_vendored_path(self, path: str) -> bool:
        return bool(COMMON_VENDOR.search(path)) or self._hit("vendor", path)

    def is_generated_path(self, path: str) -> bool:
        return bool(COMMON_GENERATED.search(path)) or self._hit("generated", path)

    def is_manifest_path(self, path: str) -> bool:
        base = path.rsplit("/", 1)[-1]
        return any(fnmatch.fnmatchcase(base, g) for g in self.manifest_files)


_COMPILED: dict[tuple[str, str], list[re.Pattern]] = {}


def _cached(adapter: EcosystemAdapter, which: str) -> list[re.Pattern]:
    key = (adapter.name, which)
    if key not in _COMPILED:
        pats = {"test": adapter.test_patterns, "vendor": adapter.vendor_patterns,
                "generated": adapter.generated_patterns}[which]
        _COMPILED[key] = _compile(pats)
    return _COMPILED[key]


# -- repository identity (shared by every ecosystem) ---------------------------------------------

_MARKERS = {"-", "commit", "commits", "tree", "blob", "pull", "pulls", "issues", "releases", "tags",
            "compare", "merge_requests", "wiki"}
_TWO_SEGMENT_HOSTS = {"github.com", "bitbucket.org"}


def repository_identity(url: str | None) -> str | None:
    """``host/owner/repo`` in lower case, without ``.git``, ``www.`` or a commit/tree suffix.
    Forks differ by owner; the same repository named in different spellings is one identity."""
    if not isinstance(url, str) or not url.strip():
        return None
    u = url.strip()
    scp = re.match(r"^git@([^:/]+):(.+)$", u)
    if scp:
        host, path = scp.group(1), scp.group(2)
    elif "://" in u:
        parts = urlsplit(u)
        host, path = parts.hostname or "", parts.path
    else:
        return None
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    segs: list[str] = []
    for s in (x for x in path.split("/") if x):
        if s in _MARKERS:
            break
        segs.append(s)
    if len(segs) < 2 or not host:
        return None
    if host in _TWO_SEGMENT_HOSTS:
        segs = segs[:2]
    segs[-1] = re.sub(r"\.git$", "", segs[-1])
    return "/".join([host, *(s.lower() for s in segs)])
