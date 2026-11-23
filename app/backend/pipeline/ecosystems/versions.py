"""Version ordering per ecosystem. Versions are never compared as strings.

One family per ordering scheme:

* ``pep440``   PyPI, through ``packaging.version`` (epochs, ``.postN``, ``.devN``);
* ``semver``   npm, Go, crates.io, Hex and other SemVer 2.0 ecosystems (a leading ``v`` and
  build metadata are ignored; a Go pseudo-version is a pre-release);
* ``maven``    Apache Maven ``ComparableVersion``: ``alpha < beta < milestone < rc < snapshot <
  release (ga, final) < sp``; an unknown qualifier such as ``-jre`` or ``-redhat-1`` sorts AFTER the
  release, so ``31.1-jre`` is a release, not a pre-release;
* ``gem``      RubyGems ``Gem::Version`` (a letter segment makes a pre-release);
* ``composer`` Composer / Packagist stability order ``dev < alpha < beta < RC < stable < patch``;
* ``nuget``    NuGet SemVer 2.0 with an optional fourth number and case-insensitive labels;
* ``generic``  the old tolerant tokeniser, for other ecosystems and for any version that does not
  parse in its own scheme (marked ``valid=False``).

``key(ecosystem, version)`` never raises and is cached; ``compare`` and ``is_prerelease`` build on it.
"""
from __future__ import annotations

import functools
import re

from packaging.version import InvalidVersion, Version

_FAMILY = {"pypi": "pep440", "npm": "semver", "go": "semver", "crates.io": "semver", "hex": "semver",
           "pub": "semver", "swifturl": "semver", "maven": "maven", "rubygems": "gem",
           "packagist": "composer", "nuget": "nuget"}
_STRIP_V = {"semver", "composer", "nuget"}


def family(ecosystem: str | None) -> str:
    return _FAMILY.get((ecosystem or "").split(":")[0].strip().lower(), "generic")


def normalize(ecosystem: str | None, v) -> str:
    """The comparable text of a version: stripped, and without the ``v`` prefix where the
    ecosystem's registry and OSV disagree about it (Go lists ``v1.2.3``, OSV ``1.2.3``)."""
    s = str(v).strip()
    if family(ecosystem) in _STRIP_V and s[:1] in ("v", "V") and s[1:2].isdigit():
        s = s[1:]
    return s


# -- generic (the original tolerant key) ------------------------------------------------------------

_PRE = {"dev": 0, "snapshot": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2, "milestone": 3,
        "m": 3, "rc": 4, "cr": 4, "pre": 4, "preview": 4}
_RELEASE = {"ga", "final", "release", "r"}
_POST = {"sp", "post", "p", "pl", "patch"}


def _generic_parse(v: str) -> tuple:
    s = v.strip().lstrip("vV").split("+", 1)[0].lower()
    key = []
    for tok in re.findall(r"\d+|[a-z]+", s):
        if tok.isdigit():
            key.append((1, int(tok), ""))
        elif tok in _RELEASE:
            continue
        elif tok in _POST:
            key.append((2, 0, tok))
        else:
            key.append((0, _PRE.get(tok, 5), tok))
    return tuple(key)


def _generic_cmp(a: tuple, b: tuple) -> int:
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else (1, 0, "")      # a missing part is the ".0" of a release
        y = b[i] if i < len(b) else (1, 0, "")
        if x != y:
            return -1 if x < y else 1
    return 0


def _tuple_cmp(a, b) -> int:
    return (a > b) - (a < b)


# -- SemVer ----------------------------------------------------------------------------------------------

_SEMVER = re.compile(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.\-]+))?(?:\+[0-9A-Za-z.\-]+)?$")


def _semver_parse(v: str):
    m = _SEMVER.match(v.strip())
    if not m:
        return None
    major, minor, patch = (int(x or 0) for x in m.group(1, 2, 3))
    pre = m.group(4)
    if pre is None:
        return (major, minor, patch, 1, ()), False
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (major, minor, patch, 0, ids), True


_NUGET = re.compile(r"^[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.\-]+))?(?:\+.*)?$")


def _nuget_parse(v: str):
    m = _NUGET.match(v.strip())
    if not m:
        return None
    nums = tuple(int(x or 0) for x in m.group(1, 2, 3, 4))
    pre = m.group(5)
    if pre is None:
        return nums + (1, ()), False
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in pre.split("."))
    return nums + (0, ids), True


# -- Maven ComparableVersion --------------------------------------------------------------------------------

_QUALIFIERS = ["alpha", "beta", "milestone", "rc", "snapshot", "", "sp"]
_ALIASES = {"ga": "", "final": "", "release": "", "cr": "rc"}
_RELEASE_INDEX = str(_QUALIFIERS.index(""))


def _c(a, b) -> int:
    return (a > b) - (a < b)


class _Int:
    def __init__(self, v: int):
        self.v = v

    def is_null(self):
        return self.v == 0

    def cmp(self, o):
        if o is None:
            return 0 if self.v == 0 else 1
        if isinstance(o, _Int):
            return _c(self.v, o.v)
        return 1

    def canon(self):
        return ("i", self.v)


class _Str:
    def __init__(self, v: str, followed_by_digit: bool = False):
        if followed_by_digit and len(v) == 1:
            v = {"a": "alpha", "b": "beta", "m": "milestone"}.get(v, v)
        self.v = _ALIASES.get(v, v)

    def comparable(self) -> str:
        i = _QUALIFIERS.index(self.v) if self.v in _QUALIFIERS else -1
        return str(i) if i != -1 else f"{len(_QUALIFIERS)}-{self.v}"

    def is_null(self):
        return self.comparable() == _RELEASE_INDEX

    def cmp(self, o):
        if o is None:
            return _c(self.comparable(), _RELEASE_INDEX)
        if isinstance(o, _Int):
            return -1
        if isinstance(o, _Str):
            return _c(self.comparable(), o.comparable())
        return -1

    def canon(self):
        return ("s", self.comparable())


class _List(list):
    def is_null(self):
        return len(self) == 0

    def normalize(self):
        for i in range(len(self) - 1, -1, -1):
            last = self[i]
            if last.is_null():
                del self[i]
            elif not isinstance(last, _List):
                break

    def cmp(self, o):
        if o is None:
            return 0 if not self else self[0].cmp(None)
        if isinstance(o, _Int):
            return -1
        if isinstance(o, _Str):
            return 1
        for i in range(max(len(self), len(o))):
            left = self[i] if i < len(self) else None
            right = o[i] if i < len(o) else None
            res = (0 if right is None else -1 * right.cmp(left)) if left is None else left.cmp(right)
            if res != 0:
                return res
        return 0

    def canon(self):
        return ("l", tuple(x.canon() for x in self))


def _maven_item(is_digit: bool, text: str):
    return _Int(int(text)) if is_digit else _Str(text, False)


def _maven_parse(version: str) -> _List:
    version = version.strip().lower()
    root = lst = _List()
    stack = [lst]
    is_digit, start = False, 0
    for i, c in enumerate(version):
        if c in ".-":
            lst.append(_Int(0) if i == start else _maven_item(is_digit, version[start:i]))
            start = i + 1
            if c == "-":
                new = _List()
                lst.append(new)
                lst = new
                stack.append(lst)
        elif c.isdigit():
            if not is_digit and i > start:
                lst.append(_Str(version[start:i], True))
                start = i
                new = _List()
                lst.append(new)
                lst = new
                stack.append(lst)
            is_digit = True
        else:
            if is_digit and i > start:
                lst.append(_maven_item(True, version[start:i]))
                start = i
                new = _List()
                lst.append(new)
                lst = new
                stack.append(lst)
            is_digit = False
    if len(version) > start:
        lst.append(_maven_item(is_digit, version[start:]))
    while stack:
        stack.pop().normalize()
    return root


def _maven_pre(item) -> bool:
    if isinstance(item, _Str):
        return item.v in _QUALIFIERS[:5]
    if isinstance(item, _List):
        return any(_maven_pre(x) for x in item)
    return False


# -- RubyGems -------------------------------------------------------------------------------------------------

_GEM = re.compile(r"^[0-9]+(\.[0-9a-zA-Z]+)*(-[0-9A-Za-z\-.]+)?$")


def _gem_parse(v: str):
    s = v.strip()
    if not _GEM.match(s):
        return None
    segs = [int(x) if x.isdigit() else x.lower() for x in re.findall(r"[0-9]+|[a-zA-Z]+", s.replace("-", ".pre."))]
    i = next((k for k, x in enumerate(segs) if isinstance(x, str)), len(segs))
    num, rest = segs[:i], segs[i:]
    while num and num[-1] == 0:
        num.pop()
    while rest and rest[-1] == 0:
        rest.pop()
    return tuple(num + rest), any(isinstance(x, str) for x in segs)


def _gem_cmp(a, b) -> int:
    for i in range(max(len(a), len(b))):
        left = a[i] if i < len(a) else 0
        right = b[i] if i < len(b) else 0
        if left == right:
            continue
        if isinstance(left, str) and isinstance(right, int):
            return -1
        if isinstance(left, int) and isinstance(right, str):
            return 1
        return -1 if left < right else 1
    return 0


# -- Composer ---------------------------------------------------------------------------------------------------

_COMPOSER = re.compile(r"^v?(\d+(?:\.\d+){0,3})(?:[-_.]?(dev|alpha|a|beta|b|rc|stable|patch|pl|p)[-_.]?(\d*))?$", re.I)
_STABILITY = {"dev": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2, "rc": 3, "stable": 4, "patch": 5, "pl": 5, "p": 5}


def _composer_parse(v: str):
    m = _COMPOSER.match(v.strip())
    if not m:
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums = nums + (0,) * (4 - len(nums))
    rank = _STABILITY[m.group(2).lower()] if m.group(2) else 4
    return nums + (rank, int(m.group(3) or 0)), rank < 4


# -- the key -----------------------------------------------------------------------------------------------------

@functools.total_ordering
class VersionKey:
    """A comparable version of one ecosystem family."""
    __slots__ = ("raw", "family", "valid", "prerelease", "_k", "_cmp", "_canon")

    def __init__(self, raw, fam, valid, prerelease, k, cmp, canon):
        self.raw, self.family, self.valid, self.prerelease = raw, fam, valid, prerelease
        self._k, self._cmp, self._canon = k, cmp, canon

    def _compare(self, o: "VersionKey") -> int:
        if self.family == o.family and self.valid and o.valid:
            return self._cmp(self._k, o._k)
        return _generic_cmp(_generic_parse(self.raw), _generic_parse(o.raw))      # an unparseable side

    def __eq__(self, o):
        return isinstance(o, VersionKey) and self._compare(o) == 0

    def __lt__(self, o):
        return self._compare(o) < 0

    def __hash__(self):
        return hash((self.family, self._canon))

    def __repr__(self):
        return f"VersionKey({self.family}:{self.raw!r})"


@functools.lru_cache(maxsize=1_000_000)
def _key(fam: str, raw: str) -> VersionKey:
    s = raw.strip()
    try:
        if fam == "pep440":
            v = Version(s)
            return VersionKey(raw, fam, True, v.is_prerelease, v, _tuple_cmp, v)
        if fam == "semver":
            got = _semver_parse(s)
            if got:
                return VersionKey(raw, fam, True, got[1], got[0], _tuple_cmp, got[0])
        elif fam == "nuget":
            got = _nuget_parse(s)
            if got:
                return VersionKey(raw, fam, True, got[1], got[0], _tuple_cmp, got[0])
        elif fam == "maven" and s:
            tree = _maven_parse(s)
            return VersionKey(raw, fam, True, _maven_pre(tree), tree, lambda a, b: a.cmp(b), tree.canon())
        elif fam == "gem":
            got = _gem_parse(s)
            if got:
                return VersionKey(raw, fam, True, got[1], got[0], _gem_cmp, got[0])
        elif fam == "composer":
            got = _composer_parse(s)
            if got:
                return VersionKey(raw, fam, True, got[1], got[0], _tuple_cmp, got[0])
    except InvalidVersion:
        pass
    g = _generic_parse(s)
    return VersionKey(raw, "generic" if fam != "generic" else fam, fam == "generic", any(p[0] == 0 for p in g),
                      g, _generic_cmp, g)


def key(ecosystem: str | None, v) -> VersionKey:
    """The comparable key of a version in an ecosystem (cached; never raises)."""
    return _key(family(ecosystem), normalize(ecosystem, v))


def compare(ecosystem: str | None, a, b) -> int:
    ka, kb = key(ecosystem, a), key(ecosystem, b)
    return -1 if ka < kb else (1 if kb < ka else 0)


def is_prerelease(ecosystem: str | None, v) -> bool:
    return key(ecosystem, v).prerelease
