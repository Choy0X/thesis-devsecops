"""Readers for the manifests and lock files of Go, Maven (and Gradle lock files), RubyGems, Packagist, crates.io, NuGet and Hex (P3f.8.1, P3f.8.2), in the shape of ``manifests.py``.

They are registered by ``manifests.py`` itself (its ``READERS`` table), so ``read`` and the dependency scan use them like the npm and Python ones. Nothing is run and nothing is installed;
the files are only read. What a file can say, and what it cannot, is stated per reader:

* ``go.mod`` names the minimum version of each module the project requires. The build may use a higher one that another module asks for, so the version is ``pinned``, not ``lock``.
* ``pom.xml`` gives a version, a property that stands for one (``${spring.version}``, resolved from the file's own ``<properties>`` and ``<dependencyManagement>``), a range, or nothing
  (the version is managed by a parent POM, which is not read). Only a plain version is exact; the rest is ``declared`` and the scan says the version in use is not known.
* ``gradle.lockfile`` is a lock file: every line is an exact version.
* ``Gemfile.lock`` is a lock file; ``Gemfile`` gives the ranges written next to each gem.
* ``composer.lock`` is a lock file; ``composer.json`` gives the constraints (``php`` and ``ext-*`` requirements are not packages and are left out).
* ``Cargo.lock`` is a lock file (registry crates only); in ``Cargo.toml`` a bare version is a caret range, so only ``=1.0.0`` is exact.
* ``packages.lock.json`` and ``packages.config`` are lock files; project files (``.csproj`` and the like) and ``Directory.Packages.props`` give versions or ranges, or none under central management.
* ``mix.lock`` is a lock file; in ``mix.exs`` a plain version is exactly that version and ``~>`` or ``>=`` are ranges.

A POM is XML that the project controls, so it is refused when it declares a document type or an entity (nothing a real POM needs), before it is parsed.
"""
from __future__ import annotations

import json
import pathlib
import re
import xml.etree.ElementTree as ET

MAX_RESOLVED = 2000                                       # the longest a version, group or artifact may become while its properties are substituted
_PROPERTY = re.compile(r"\$\{([^}]+)\}")
_EXACT = re.compile(r"^v?\d+(\.\d+){0,3}([-+][0-9A-Za-z.\-+]+)?$")


def _mf():
    from app.backend.pipeline.scan import manifests as mf
    return mf


def _exact(spec: str) -> bool:
    return bool(_EXACT.match(spec.strip()))


# -- Go ----------------------------------------------------------------------------------------------------------------------------------------------------------

def go_mod(path: pathlib.Path, rel: str) -> list[dict]:
    mf = _mf()
    out, in_block = [], False
    for raw in mf._text(path).splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if in_block:
            if line == ")":
                in_block = False
                continue
            parts = line.split()
        elif line.startswith("require"):
            rest = line[len("require"):].strip()
            if rest == "(":
                in_block = True
                continue
            parts = rest.split()
        else:
            continue
        if len(parts) >= 2 and parts[1].startswith("v"):
            out.append(mf._dep("Go", parts[0], parts[1].lstrip("v"), parts[1], rel, "pinned"))
    return out


# -- Maven and Gradle -----------------------------------------------------------------------------------------------------------------------------------------------

def _safe_xml(text: str, rel: str) -> ET.Element:
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise _mf().ManifestError(f"{rel} declares a document type or an entity, which a project file does not need, so it was not read.")
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise _mf().ManifestError(f"{rel} is not valid XML ({e}).") from None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in node if _local(c.tag) == name), None)


def _text_of(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None and node.text else ""


def maven_pom(path: pathlib.Path, rel: str) -> list[dict]:
    mf = _mf()
    root = _safe_xml(mf._text(path), rel)
    props = {"project.version": _text_of(_child(root, "version")) or _text_of(_child(_child(root, "parent") or ET.Element("x"), "version")),
             "project.groupId": _text_of(_child(root, "groupId"))}
    for p in _child(root, "properties") if _child(root, "properties") is not None else []:
        props[_local(p.tag)] = (p.text or "").strip()[:MAX_RESOLVED]               # no property is longer than a version may become, whatever the file says

    def substitute(value: str) -> str | None:
        """One round of ``${name}`` substitution, counting the length as it is built and giving up (None) the moment it would pass ``MAX_RESOLVED``: the result is never built first and checked after."""
        out, pos, total = [], 0, 0
        for m in _PROPERTY.finditer(value):
            piece, replacement = value[pos:m.start()], props.get(m.group(1), m.group(0))
            total += len(piece) + len(replacement)
            if total > MAX_RESOLVED:
                return None
            out += [piece, replacement]
            pos = m.end()
        if total + len(value) - pos > MAX_RESOLVED:
            return None
        out.append(value[pos:])
        return "".join(out)

    def resolve(value: str) -> str:
        """Substitute ``${name}`` from the file's own properties, a few rounds deep. A value that would grow past ``MAX_RESOLVED`` characters is a property that expands into itself many times
        over (the entity-expansion trick in another form): it is left as it was before that round, unresolved."""
        value = value[:MAX_RESOLVED]
        for _ in range(5):
            replaced = substitute(value)
            if replaced is None:
                return value
            if replaced == value:
                break
            value = replaced
        return value

    def entries(container: ET.Element | None) -> list[tuple[str, str, str]]:
        found = []
        for dep in container if container is not None else []:
            if _local(dep.tag) != "dependency":
                continue
            group, artifact = resolve(_text_of(_child(dep, "groupId"))), resolve(_text_of(_child(dep, "artifactId")))
            if group and artifact:
                found.append((f"{group}:{artifact}", resolve(_text_of(_child(dep, "version"))), _text_of(_child(dep, "scope"))))
        return found

    managed_node = _child(root, "dependencyManagement")
    managed = {name: version for name, version, _ in entries(_child(managed_node, "dependencies") if managed_node is not None else None) if version}
    out = []
    for name, version, _scope in entries(_child(root, "dependencies")):
        version = version or managed.get(name, "")
        if version and "${" not in version and _exact(version):
            out.append(mf._dep("Maven", name, version.lstrip("v"), version, rel, "pinned"))
        else:
            out.append(mf._dep("Maven", name, None, version or None, rel, "declared"))
    return out


def gradle_lockfile(path: pathlib.Path, rel: str) -> list[dict]:
    mf = _mf()
    out = []
    for raw in mf._text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("empty="):
            continue
        coordinates = line.split("=", 1)[0]
        parts = coordinates.split(":")
        if len(parts) == 3 and parts[2]:
            out.append(mf._dep("Maven", f"{parts[0]}:{parts[1]}", parts[2], parts[2], rel, "lock"))
    return out


# -- RubyGems ---------------------------------------------------------------------------------------------------------------------------------------------------

def gem_lock(path: pathlib.Path, rel: str) -> list[dict]:
    mf = _mf()
    out, in_specs = [], False
    for raw in mf._text(path).splitlines():
        if raw and not raw.startswith(" "):
            in_specs = False
            continue
        if raw.strip() == "specs:":
            in_specs = True
            continue
        m = re.match(r"^ {4}(\S+) \(([^)]+)\)$", raw) if in_specs else None
        if m:
            version = m.group(2).split("-", 1)[0]                                       # "1.13.9-x86_64-linux": the platform is not part of the version
            out.append(mf._dep("RubyGems", m.group(1), version, m.group(2), rel, "lock"))
    return out


def gemfile(path: pathlib.Path, rel: str) -> list[dict]:
    mf = _mf()
    out = []
    for raw in mf._text(path).splitlines():
        m = re.match(r"""^\s*gem\s+["']([^"']+)["']\s*(?:,\s*(.*))?$""", raw.split("#", 1)[0])
        if not m:
            continue
        specs = re.findall(r"""["']([^"']+)["']""", (m.group(2) or "").split("=>")[0].split(":")[0] if m.group(2) else "")
        specs = [s for s in specs if re.match(r"^[~<>=!\s]*\d", s)]
        declared = ", ".join(specs) or None
        version = specs[0] if len(specs) == 1 and _exact(specs[0]) else (specs[0].lstrip("= ") if len(specs) == 1 and specs[0].startswith("=") and _exact(specs[0].lstrip("= ")) else None)
        out.append(mf._dep("RubyGems", m.group(1), version, declared, rel, "pinned" if version else "declared"))
    return out


# -- Packagist ----------------------------------------------------------------------------------------------------------------------------------------------------

def composer_lock(path: pathlib.Path, rel: str) -> list[dict]:
    mf = _mf()
    try:
        doc = json.loads(mf._text(path))
    except ValueError as e:
        raise mf.ManifestError(f"{rel} is not valid JSON ({e}).") from None
    out = []
    for key in ("packages", "packages-dev"):
        for p in doc.get(key) or [] if isinstance(doc.get(key), list) else []:
            name, version = p.get("name"), str(p.get("version") or "")
            if name and not version.startswith("dev-"):
                out.append(mf._dep("Packagist", name, version.lstrip("v"), version, rel, "lock"))
    return out


def composer_json(path: pathlib.Path, rel: str) -> list[dict]:
    mf = _mf()
    try:
        doc = json.loads(mf._text(path))
    except ValueError as e:
        raise mf.ManifestError(f"{rel} is not valid JSON ({e}).") from None
    out = []
    for key in ("require", "require-dev"):
        for name, spec in doc.get(key).items() if isinstance(doc.get(key), dict) else []:
            if name == "php" or name.startswith("ext-") or name.startswith("lib-"):
                continue
            spec = str(spec)
            out.append(mf._dep("Packagist", name, spec.lstrip("v") if _exact(spec) else None, spec, rel, "pinned" if _exact(spec) else "declared"))
    return out


# -- crates.io ----------------------------------------------------------------------------------------------------------------------------------------------------

def _toml(path: pathlib.Path, rel: str) -> dict:
    import tomllib
    mf = _mf()
    try:
        return tomllib.loads(mf._text(path))
    except tomllib.TOMLDecodeError as e:
        raise mf.ManifestError(f"{rel} is not valid TOML ({e}).") from None


def cargo_lock(path: pathlib.Path, rel: str) -> list[dict]:
    """Only crates that come from a registry: the workspace's own crates have no source, and a git dependency is not on crates.io."""
    mf = _mf()
    out = []
    for p in _toml(path, rel).get("package") or []:
        source = str(p.get("source") or "")
        if p.get("name") and p.get("version") and source.startswith("registry+"):
            out.append(mf._dep("crates.io", p["name"], p["version"], p["version"], rel, "lock"))
    return out


def cargo_toml(path: pathlib.Path, rel: str) -> list[dict]:
    """In Cargo a bare version such as "1.0" is a caret range, so only "=1.0.0" is exact. Path and git dependencies are not registry packages."""
    mf = _mf()
    doc = _toml(path, rel)
    tables = [doc.get(k) for k in ("dependencies", "dev-dependencies", "build-dependencies")] + [(doc.get("workspace") or {}).get("dependencies")]
    for target in (doc.get("target") or {}).values():
        tables += [target.get(k) for k in ("dependencies", "dev-dependencies", "build-dependencies")]
    out = []
    for table in tables:
        for name, spec in (table or {}).items():
            if isinstance(spec, dict):
                if "path" in spec or "git" in spec or spec.get("workspace") is True:
                    continue
                name, spec = spec.get("package", name), spec.get("version")
            if not isinstance(spec, str):
                continue
            exact = spec.startswith("=") and _exact(spec[1:].strip())
            out.append(mf._dep("crates.io", name, spec[1:].strip() if exact else None, spec, rel, "pinned" if exact else "declared"))
    return out


# -- NuGet ---------------------------------------------------------------------------------------------------------------------------------------------------------

def nuget_lock(path: pathlib.Path, rel: str) -> list[dict]:
    mf = _mf()
    try:
        doc = json.loads(mf._text(path))
    except ValueError as e:
        raise mf.ManifestError(f"{rel} is not valid JSON ({e}).") from None
    out = []
    for framework in (doc.get("dependencies") or {}).values():
        for name, info in framework.items() if isinstance(framework, dict) else []:
            if isinstance(info, dict) and info.get("resolved") and info.get("type") != "Project":
                out.append(mf._dep("NuGet", name, str(info["resolved"]), str(info.get("requested") or info["resolved"]), rel, "lock"))
    return out


def nuget_packages_config(path: pathlib.Path, rel: str) -> list[dict]:
    mf = _mf()
    root = _safe_xml(mf._text(path), rel)
    return [mf._dep("NuGet", p.get("id"), p.get("version"), p.get("version"), rel, "lock") for p in root.iter() if _local(p.tag) == "package" and p.get("id") and p.get("version")]


def nuget_project(path: pathlib.Path, rel: str) -> list[dict]:
    """``PackageReference`` in a project file and ``PackageVersion`` in Directory.Packages.props. A version is exact when it is a plain version; a range ``[1.0,2.0)``, a floating
    ``1.*`` or no version (central package management) is declared."""
    mf = _mf()
    root = _safe_xml(mf._text(path), rel)
    out = []
    for node in root.iter():
        if _local(node.tag) not in ("PackageReference", "PackageVersion"):
            continue
        name = node.get("Include") or node.get("Update")
        if not name:
            continue
        spec = node.get("Version") or node.get("VersionOverride") or _text_of(_child(node, "Version")) or None
        exact = bool(spec) and "$(" not in spec and _exact(spec)
        out.append(mf._dep("NuGet", name, spec if exact else None, spec, rel, "pinned" if exact else "declared"))
    return out


# -- Hex -----------------------------------------------------------------------------------------------------------------------------------------------------------

def mix_lock(path: pathlib.Path, rel: str) -> list[dict]:
    mf = _mf()
    out = []
    for raw in mf._text(path).splitlines():
        m = re.match(r'''^\s*"([^"]+)":\s*\{:hex,\s*:(\w+),\s*"([^"]+)"''', raw)
        if m:
            out.append(mf._dep("Hex", m.group(2), m.group(3), m.group(3), rel, "lock"))
    return out


def mix_exs(path: pathlib.Path, rel: str) -> list[dict]:
    """A Hex requirement written as a plain version ("1.2.3") means exactly that version; "~> 1.2" and ">= 1.0" are ranges. Dependencies from git or a path are left out."""
    mf = _mf()
    out = []
    for raw in mf._text(path).splitlines():
        line = raw.split("#", 1)[0]
        m = re.search(r"""\{:(\w+),\s*"([^"]+)"(.*)\}""", line)
        if not m or re.search(r"(?:^|[\s,])(?:git|github|path):", m.group(3)):
            continue
        spec = m.group(2).strip()
        exact = bool(re.fullmatch(r"=?=?\s*\d+(\.\d+){0,3}([-+][0-9A-Za-z.\-+]+)?", spec))
        out.append(mf._dep("Hex", m.group(1), re.sub(r"^=*\s*", "", spec) if exact else None, spec, rel, "pinned" if exact else "declared"))
    return out


READERS = {"go.mod": go_mod, "pom.xml": maven_pom, "gradle.lockfile": gradle_lockfile, "Gemfile.lock": gem_lock, "Gemfile": gemfile, "composer.lock": composer_lock, "composer.json": composer_json,
           "Cargo.lock": cargo_lock, "Cargo.toml": cargo_toml, "packages.lock.json": nuget_lock, "packages.config": nuget_packages_config, "Directory.Packages.props": nuget_project,
           "mix.lock": mix_lock, "mix.exs": mix_exs}
SUFFIX_READERS = {".csproj": nuget_project, ".fsproj": nuget_project, ".vbproj": nuget_project}
