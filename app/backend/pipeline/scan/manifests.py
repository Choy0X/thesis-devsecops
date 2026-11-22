"""Read the dependencies a project declares or pins, from the manifests and lock files of its ecosystems. Nothing is executed and nothing is installed: the files are only read.

Each dependency is ``{ecosystem, name, version, declared, manifest, source}``. ``version`` is the exact version in use when a file says it (a lock file, a ``==`` pin) and
None when the file gives only a range; ``declared`` is the range or specifier as written; ``source`` is ``lock``, ``pinned`` or ``declared``. A dependency known only by its range
cannot be matched against advisories exactly, and the scan says so instead of calling it fine.
"""
from __future__ import annotations

import json
import pathlib
import re

from packaging.requirements import InvalidRequirement, Requirement

MAX_BYTES = 40 * 1024 * 1024                      # a manifest larger than this is not read (a lock file of a very large monorepo is a few megabytes)
_EXACT = re.compile(r"^v?\d+(\.\d+){0,3}([-+][0-9A-Za-z.\-+]+)?$")


class ManifestError(ValueError):
    """A manifest that cannot be read, with a sentence for the person."""


def _text(path: pathlib.Path) -> str:
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ManifestError(f"{path.name} is larger than {MAX_BYTES // (1024 * 1024)} MB and was not read.")
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ManifestError(f"{path.name} could not be read ({e.strerror or type(e).__name__}).") from None


def _dep(eco: str, name: str, version, declared, manifest: str, source: str) -> dict:
    return {"ecosystem": eco, "name": name, "version": version or None, "declared": declared, "manifest": manifest, "source": source}


# -- npm ---------------------------------------------------------------------------------------------------------------------------------------------------

def npm_package_json(path: pathlib.Path, rel: str) -> list[dict]:
    try:
        doc = json.loads(_text(path))
    except ValueError as e:
        raise ManifestError(f"{rel} is not valid JSON ({e}).") from None
    out = []
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        for name, spec in (doc.get(key) or {}).items() if isinstance(doc.get(key), dict) else []:
            spec = str(spec)
            exact = bool(_EXACT.match(spec))
            out.append(_dep("npm", name, spec.lstrip("v") if exact else None, spec, rel, "pinned" if exact else "declared"))
    return out


def npm_package_lock(path: pathlib.Path, rel: str) -> list[dict]:
    try:
        doc = json.loads(_text(path))
    except ValueError as e:
        raise ManifestError(f"{rel} is not valid JSON ({e}).") from None
    out: list[dict] = []
    packages = doc.get("packages")
    if isinstance(packages, dict):                                         # lockfileVersion 2 and 3
        for key, info in packages.items():
            if not key or not isinstance(info, dict) or not info.get("version") or info.get("link"):
                continue
            name = info.get("name") or key.rsplit("node_modules/", 1)[-1]
            out.append(_dep("npm", name, str(info["version"]), None, rel, "lock"))
        return out

    def walk(deps: dict) -> None:                                           # lockfileVersion 1: a tree
        for name, info in deps.items():
            if isinstance(info, dict) and info.get("version") and not str(info["version"]).startswith(("file:", "git", "http")):
                out.append(_dep("npm", name, str(info["version"]), None, rel, "lock"))
            if isinstance(info, dict) and isinstance(info.get("dependencies"), dict):
                walk(info["dependencies"])
    if isinstance(doc.get("dependencies"), dict):
        walk(doc["dependencies"])
    return out


def npm_yarn_lock(path: pathlib.Path, rel: str) -> list[dict]:
    """yarn v1 (``version "1.2.3"``) and yarn berry (``version: 1.2.3``): a block starts at an unindented line ``"name@range", name@range:``."""
    out, names = [], []
    for line in _text(path).splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if not line.startswith((" ", "\t")) and line.rstrip().endswith(":"):
            names = []
            for part in line.rstrip()[:-1].split(","):
                spec = part.strip().strip('"').strip("'")
                at = spec.rfind("@")
                if at > 0:
                    names.append(spec[:at])
            continue
        m = re.match(r'^\s+version:?\s+"?([^"\s]+)"?\s*$', line)
        if m and names:
            out.append(_dep("npm", names[0], m.group(1), None, rel, "lock"))
            names = []
    return out


def npm_pnpm_lock(path: pathlib.Path, rel: str) -> list[dict]:
    try:
        import yaml
        doc = yaml.safe_load(_text(path)) or {}
    except Exception as e:
        raise ManifestError(f"{rel} could not be read as YAML ({type(e).__name__}).") from None
    out = []
    for key in (doc.get("packages") or {}) if isinstance(doc.get("packages"), dict) else []:
        k = str(key).strip("'\"").lstrip("/")
        base = re.sub(r"\(.*\)$", "", k)                                    # peer suffixes: name@1.2.3(react@18)
        m = re.match(r"^(@?[^@/]+(?:/[^@/]+)?)@(\d[^\s(]*)$", base) or re.match(r"^(@?[^@/]+(?:/[^@/]+)?)/(\d[^\s(]*)$", base)
        if m:
            out.append(_dep("npm", m.group(1), m.group(2), None, rel, "lock"))
    return out


# -- Python ------------------------------------------------------------------------------------------------------------------------------------------------

def py_requirements(path: pathlib.Path, rel: str) -> list[dict]:
    out = []
    for raw in _text(path).splitlines():
        line = raw.split(" #", 1)[0].split("#", 1)[0].strip() if not raw.lstrip().startswith("#") else ""
        if not line or line.startswith(("-", "git+", "http")):
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            m = re.match(r"([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.\-]+)", line)
            if m:
                out.append(_dep("PyPI", m.group(1).lower(), m.group(2), f"=={m.group(2)}", rel, "pinned"))
            continue
        pinned = next((s.version for s in req.specifier if s.operator in ("==", "===")), None)
        out.append(_dep("PyPI", req.name.lower(), pinned, str(req.specifier) or None, rel, "pinned" if pinned else "declared"))
    return out


def py_poetry_lock(path: pathlib.Path, rel: str) -> list[dict]:
    out, name = [], None
    for line in _text(path).splitlines():
        line = line.strip()
        m = re.match(r'name\s*=\s*"([^"]+)"', line)
        if m:
            name = m.group(1).lower()
        v = re.match(r'version\s*=\s*"([^"]+)"', line)
        if v and name:
            out.append(_dep("PyPI", name, v.group(1), None, rel, "lock"))
            name = None
    return out


def py_pipfile_lock(path: pathlib.Path, rel: str) -> list[dict]:
    try:
        doc = json.loads(_text(path))
    except ValueError as e:
        raise ManifestError(f"{rel} is not valid JSON ({e}).") from None
    out = []
    for section in ("default", "develop"):
        for name, info in (doc.get(section) or {}).items():
            v = str((info or {}).get("version") or "")
            out.append(_dep("PyPI", name.lower(), v.lstrip("=") or None, v or None, rel, "lock" if v else "declared"))
    return out


def py_pyproject(path: pathlib.Path, rel: str) -> list[dict]:
    import tomllib
    try:
        doc = tomllib.loads(_text(path))
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"{rel} is not valid TOML ({e}).") from None
    out = []
    for spec in (doc.get("project") or {}).get("dependencies") or []:
        try:
            req = Requirement(str(spec))
        except InvalidRequirement:
            continue
        pinned = next((s.version for s in req.specifier if s.operator in ("==", "===")), None)
        out.append(_dep("PyPI", req.name.lower(), pinned, str(req.specifier) or None, rel, "pinned" if pinned else "declared"))
    for name, spec in ((doc.get("tool") or {}).get("poetry") or {}).get("dependencies", {}).items():
        if name.lower() == "python":
            continue
        text = spec if isinstance(spec, str) else (spec or {}).get("version") if isinstance(spec, dict) else None
        exact = bool(text and re.fullmatch(r"=?=?\s*\d+(\.\d+)*", str(text)))
        out.append(_dep("PyPI", name.lower(), str(text).lstrip("= ") if exact else None, str(text) if text else None, rel, "pinned" if exact else "declared"))
    return out


READERS = {
    "package.json": npm_package_json, "package-lock.json": npm_package_lock, "yarn.lock": npm_yarn_lock, "pnpm-lock.yaml": npm_pnpm_lock,
    "poetry.lock": py_poetry_lock, "Pipfile.lock": py_pipfile_lock, "pyproject.toml": py_pyproject,
}


# the readers of Go, Maven, RubyGems, Packagist, crates.io, NuGet and Hex live in their own module and use the helpers above when they are called
from app.backend.pipeline.scan import manifests_more as _more  # noqa: E402

READERS.update(_more.READERS)


def reader_for(name: str):
    if name in READERS:
        return READERS[name]
    suffix = pathlib.PurePosixPath(name).suffix.lower()
    if suffix in _more.SUFFIX_READERS:
        return _more.SUFFIX_READERS[suffix]
    if re.fullmatch(r"requirements[-_.\w]*\.txt", name) or name == "constraints.txt":
        return py_requirements
    return None


def read(root: pathlib.Path, relative: str) -> list[dict]:
    """The dependencies of one manifest, given its path relative to the project folder. Raises ``ManifestError`` when it cannot be read; a manifest of a kind not read yet gives none."""
    reader = reader_for(pathlib.PurePosixPath(relative).name)
    return reader(pathlib.Path(root) / relative, relative) if reader else []


def merge(deps: list[dict]) -> list[dict]:
    """One row per (ecosystem, package, version in use). A package known by an exact version (lock or pin) is not also listed by its range; a package known only by ranges is
    listed once per manifest. The manifests that name a row are collected in ``manifests``."""
    exact: dict[tuple, dict] = {}
    declared: dict[tuple, dict] = {}
    for d in deps:
        if d["version"]:
            key = (d["ecosystem"], d["name"].lower(), d["version"])
            row = exact.setdefault(key, {**d, "manifests": []})
            if d["manifest"] not in row["manifests"]:
                row["manifests"].append(d["manifest"])
            if d["source"] == "lock":
                row["source"] = "lock"
        else:
            key = (d["ecosystem"], d["name"].lower(), d["declared"] or "")
            row = declared.setdefault(key, {**d, "manifests": []})
            if d["manifest"] not in row["manifests"]:
                row["manifests"].append(d["manifest"])
    have_exact = {(e, n) for e, n, _ in exact}
    rows = list(exact.values()) + [r for (e, n, _), r in declared.items() if (e, n.lower()) not in have_exact]
    return sorted(rows, key=lambda r: (r["ecosystem"], r["name"].lower(), r["version"] or ""))
