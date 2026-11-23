"""Which language a CVE's vulnerable code is in, detected from git.

No fixed ecosystem table (a PyPI package can be fixed in C, a Python web app in
its TypeScript frontend). When the fix commit is fetched:

* the **repository profile** is the share of source per language in the
  repository: from the tree at the fix commit (``git ls-tree``, file counts) or,
  through the GitHub API, GitHub's own language statistics (bytes);
* the **fix languages** are the languages of the source files the fix changed
  (tests, docs and vendored files left out when anything else changed);
* the **CVE language(s)** are the fix languages that are also main languages of
  the repository (>= MAIN_SHARE of its source), most-changed first, or the fix
  languages themselves when none is. A stray script in another language in a
  Java fix is therefore ignored.
"""
from __future__ import annotations

MAIN_SHARE = 0.05

# GitHub (linguist) names -> the names used for mined files (codemine.LANG_BY_EXT)
_GH = {"c++": "cpp", "c#": "csharp", "objective-c++": "objective-c", "cython": "python",
       "jupyter notebook": None, "html": None, "css": None, "scss": None, "makefile": None,
       "dockerfile": None, "cmake": None, "batchfile": None, "powershell": None}


def normalise(name: str, known: set[str]) -> str | None:
    n = name.strip().lower()
    n = _GH.get(n, n)
    return n if n and n in known else None


def shares(counts: dict[str, float]) -> dict[str, float]:
    total = sum(counts.values())
    return {k: v / total for k, v in sorted(counts.items(), key=lambda kv: -kv[1])} if total else {}


def profile_from_paths(paths, language_of) -> dict[str, float]:
    counts: dict[str, int] = {}
    for p in paths:
        lang = language_of(p)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return shares(counts)


def profile_from_github(stats: dict[str, int], known: set[str]) -> dict[str, float]:
    counts: dict[str, float] = {}
    for name, n in (stats or {}).items():
        lang = normalise(name, known)
        if lang:
            counts[lang] = counts.get(lang, 0) + n
    return shares(counts)


def cve_languages(changed: dict[str, int], profile: dict[str, float]) -> list[str]:
    """The CVE's language(s): fix languages that are main repository languages."""
    ranked = sorted(changed, key=lambda l: (-changed[l], -profile.get(l, 0)))
    main = {l for l, s in profile.items() if s >= MAIN_SHARE}
    keep = [l for l in ranked if l in main]
    return keep or ranked


def describe(profile: dict[str, float], top: int = 3) -> str:
    return ", ".join(f"{l} {s:.0%}" for l, s in list(profile.items())[:top]) or "unknown"
