"""Bounded, read-only inventory of chat manifests and associated environment metadata."""
from __future__ import annotations

from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from packaging.requirements import Requirement

from app.backend.pipeline.scan import manifests
from . import workspace

MAX_METADATA = 16 * 1024 * 1024
MAX_FILES = 50000
EXTRA_MANIFESTS = {"uv.lock", "pdm.lock", "Pipfile", "setup.cfg"}
UNRESOLVED_MANIFESTS = {"setup.py", "build.gradle", "build.gradle.kts", "bun.lock", "bun.lockb", "pubspec.lock", "Package.resolved"}
CRATES_REGISTRIES = {"registry+https://github.com/rust-lang/crates.io-index", "registry+https://index.crates.io/"}


def norm(eco, name):
    if eco in {"Go", "Maven"}:
        return name
    if eco in {"PyPI", "crates.io"}:
        return re.sub(r"[-_.]+" if eco == "PyPI" else r"_", "-", name.lower())
    return name.lower()


def public_observation(eco, observation):
    source = observation.get("source")
    if not source:
        return True
    expected = {"PyPI": "pypi.org", "npm": "registry.npmjs.org"}.get(eco)
    if not expected or not isinstance(source, dict) or set(source) != {"registry"}:
        return False
    try:
        address = urlsplit(str(source["registry"]))
        return (address.scheme == "https" and address.hostname == expected and not address.username and
                not address.password and address.port in (None, 443))
    except ValueError:
        return False


def _valid_name(eco, name):
    patterns = {
        "Maven": r"[A-Za-z0-9_][A-Za-z0-9_.-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*",
        "Go": r"[A-Za-z0-9][A-Za-z0-9.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.~-]*)*",
        "Packagist": r"[a-z0-9_][a-z0-9_.-]*/[a-z0-9_][a-z0-9_.-]*",
    }
    return bool(re.fullmatch(patterns.get(eco, r"(?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+"), name))


def _go_replacements(text):
    replacements = set()
    block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if line == "replace (":
            block = True
        elif line == ")":
            block = False
        elif "=>" in line and (block or line.startswith("replace ")):
            words = line.removeprefix("replace ").split("=>", 1)[0].split()
            if words:
                replacements.add(words[0].strip('"'))
    return replacements


def safe_path(path, root):
    """Reject links/junctions at every component, even if the target stays under root."""
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root):
        raise ValueError("Dependency path escapes the associated environment.")
    cursor = path
    while True:
        if cursor.exists() and workspace.linked(cursor):
            raise ValueError("Links and junctions are not dependency scan inputs.")
        if cursor == root:
            break
        cursor = cursor.parent
    return path


def read(path, root):
    path = safe_path(path, root)
    if not path.is_file() or path.stat().st_size > MAX_METADATA:
        raise ValueError("Metadata missing or exceeds 16 MiB.")
    raw = path.read_bytes()
    st = path.stat()
    return raw.decode("utf-8"), {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "mtime_ns": st.st_mtime_ns, "bytes": len(raw)}


def _associated_metadata(source, directories, add, provenance, problems, cancelled):
    from .scans import check_cancel

    def document(path):
        text, proof = read(path, source)
        provenance.append({**proof, "origin": "installed_metadata"})
        return json.loads(text)

    for relative, kinds in sorted(directories.items()):
        check_cancel(cancelled)
        base = source / relative
        reports = []
        if kinds & {"composer.json", "composer.lock"}:
            reports.append(("Packagist", base / "vendor/composer/installed.json"))
        if any(Path(name).suffix in {".csproj", ".fsproj", ".vbproj"} for name in kinds) or "packages.lock.json" in kinds:
            reports.append(("NuGet", base / "obj/project.assets.json"))
        if "go.mod" in kinds:
            reports.append(("Go", base / "vendor/modules.txt"))
        for eco, path in reports:
            if not path.exists() and not path.is_symlink():
                continue
            try:
                if eco == "Go":
                    text, proof = read(path, source)
                    provenance.append({**proof, "origin": "vendored_metadata"})
                    for line in text.splitlines():
                        header = re.fullmatch(r"# (\S+) (v\S+)(?:\s+=>\s+(\S+)(?: (v\S+))?)?", line)
                        if header:
                            name, version, replacement, replacement_version = header.groups()
                            if replacement and not replacement_version:
                                continue
                            add(eco, replacement or name, "vendored", replacement_version or version, path=path)
                elif eco == "Packagist":
                    data = document(path)
                    for item in data["packages"] if isinstance(data, dict) else data:
                        version = str(item.get("version") or "")
                        if item.get("name") and version and item.get("type") != "metapackage" and not version.startswith("dev-"):
                            add(eco, item["name"], "installed", version, path=path)
                else:
                    for identity, item in document(path)["libraries"].items():
                        name, _, version = identity.partition("/")
                        if item.get("type") == "package" and version:
                            add(eco, name, "installed", version, path=path)
            except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
                problems.append({"path": str(path), "reason": str(exc)[:400], "status": "inconclusive"})
        if "pom.xml" not in kinds:
            continue
        copied = base / "target/dependency"
        if not copied.exists() and not copied.is_symlink():
            continue
        try:
            safe_path(copied, source)
            for jar in sorted(copied.glob("*.jar"))[:1000]:
                check_cancel(cancelled)
                try:
                    artifact = _jar_identity(safe_path(jar, source))
                    if artifact:
                        group, name, version = artifact
                        add("Maven", f"{group}:{name}", "installed", version, path=jar)
                except (OSError, ValueError, RuntimeError, BadZipFile) as exc:
                    problems.append({"path": str(jar), "reason": str(exc)[:400], "status": "inconclusive"})
        except (OSError, ValueError) as exc:
            problems.append({"path": str(copied), "reason": str(exc)[:400], "status": "inconclusive"})


def _jar_identity(jar):
    if jar.stat().st_size > 128 * 1024 * 1024:
        return None
    with ZipFile(jar) as archive:
        identities = set()
        for member in archive.infolist():
            path = re.fullmatch(r"META-INF/maven/([^/]+)/([^/]+)/pom\.properties", member.filename)
            if not path or member.file_size > 16 * 1024:
                continue
            fields = dict(re.findall(r"(?m)^(groupId|artifactId|version)\s*=\s*([^\r\n ]+)",
                                     archive.read(member).decode("utf-8", "replace")))
            group, name = path.groups()
            version = fields.get("version")
            if fields.get("groupId") == group and fields.get("artifactId") == name and version and jar.stem == f"{name}-{version}":
                identities.add((group, name, version))
        return next(iter(identities)) if len(identities) == 1 else None


def inventory(workspace_root: Path, source_root: Path, environment: Path | None = None, *, _cancelled=None) -> dict:
    from .scans import check_cancel
    check_cancel(_cancelled)
    work, source = Path(workspace_root).absolute(), Path(source_root).absolute()
    if not work.is_dir() or not source.is_dir():
        raise ValueError("The chat workspace and associated source root must exist.")
    safe_path(work, work); safe_path(source, source)
    packages, problems, provenance = {}, [], []
    manifest_dirs = {Path("."): set()}
    scanned_environments = []

    def add(eco, name, kind, version=None, declared=None, path=None, **extra):
        if not isinstance(name, str) or not isinstance(version, (str, type(None))):
            problems.append({"path": str(path), "reason": "Invalid package identity", "status": "inconclusive"}); return
        registry_name, name = name, norm(eco, name)
        if not _valid_name(eco, name):
            problems.append({"path": str(path), "reason": "Invalid package name", "status": "inconclusive"}); return
        if version and eco in {"Go", "crates.io", "Packagist", "npm"}:
            version = version.removeprefix("v")
        row = packages.setdefault((eco, name), {"id": eco + ":" + name, "ecosystem": eco, "name": name,
                                                "registry_name": registry_name if eco == "crates.io" else name, "observations": []})
        row["observations"].append({"kind": kind, "version": version, "declared": declared, "path": str(path), **extra})

    def req(text, path, kind="declared", **extra):
        item = Requirement(text)
        pin = next((s.version for s in item.specifier if s.operator == "==" and "*" not in s.version), None)
        add("PyPI", item.name, kind, pin, str(item.specifier) or None, path, marker=str(item.marker) if item.marker else None, **extra)

    discovered = workspace.files(work)
    if len(discovered) > MAX_FILES:
        raise ValueError("Workspace inventory exceeds 50000 files.")
    for relative, path in sorted(discovered.items()):
        check_cancel(_cancelled)
        name = path.name
        if name not in EXTRA_MANIFESTS and not manifests.reader_for(name):
            if name in UNRESOLVED_MANIFESTS and not (name.startswith("build.gradle") and path.with_name("gradle.lockfile").is_file()):
                problems.append({"path": relative, "reason": "No static reader for this dependency input. Versions are unverified.", "status": "inconclusive"})
            continue
        try:
            text, proof = read(path, work); provenance.append({**proof, "origin": "workspace_manifest"})
            manifest_dirs.setdefault(path.parent.relative_to(work), set()).add(name)
            if name in {"uv.lock", "pdm.lock"}:
                for item in tomllib.loads(text).get("package", []):
                    if item.get("name") and item.get("version"):
                        add("PyPI", item["name"], "locked", item["version"], path=path, source=item.get("source"))
            elif name == "Pipfile":
                doc = tomllib.loads(text)
                for section in ("packages", "dev-packages"):
                    for package, value in doc.get(section, {}).items():
                        spec = value if isinstance(value, str) else value.get("version", "*")
                        add("PyPI", package, "declared", declared=spec, path=path)
            elif name == "setup.cfg":
                import configparser
                doc = configparser.ConfigParser(); doc.read_string(text)
                for item in doc.get("options", "install_requires", fallback="").splitlines():
                    if item.strip(): req(item, path)
            elif name == "Cargo.lock":
                for item in tomllib.loads(text).get("package", []):
                    source_url = item.get("source")
                    if source_url in CRATES_REGISTRIES:
                        add("crates.io", item["name"], "locked", item["version"], path=path)
                    elif source_url:
                        problems.append({"path": relative, "reason": "A Cargo dependency has a non-crates.io source.", "status": "inconclusive"})
            else:
                replacements = _go_replacements(text) if name == "go.mod" else set()
                for dep in manifests.read(work, relative):
                    if dep["name"] in replacements:
                        problems.append({"path": relative, "reason": f"{dep['name']} is replaced in go.mod. The original version is unverified.", "status": "inconclusive"})
                        continue
                    # An exact declaration remains a declaration; it is never evidence of installation.
                    add(dep["ecosystem"], dep["name"], "locked" if dep["source"] == "lock" else "declared", dep["version"], dep["declared"], path)
                if name == "pyproject.toml":
                    doc = tomllib.loads(text)
                    for items in (doc.get("project", {}).get("optional-dependencies", {}) or {}).values():
                        for item in items: req(item, path, optional=True)
                    for items in doc.get("dependency-groups", {}).values():
                        for item in items:
                            if isinstance(item, str): req(item, path, optional=True)
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            problems.append({"path": relative, "reason": str(exc)[:400], "status": "inconclusive"})

    _associated_metadata(source, manifest_dirs, add, provenance, problems, _cancelled)
    roots = []
    if environment is not None:
        roots.append((Path(environment).absolute(), "explicit"))
    else:
        for relative in sorted(manifest_dirs):
            parent = source / relative
            for name in (".venv", "venv", "env", "node_modules"):
                if (parent / name).exists(): roots.append((parent / name, "associated"))
    seen, file_count = set(), 0
    for env, origin in roots:
        check_cancel(_cancelled)
        if env in seen: continue
        seen.add(env)
        try:
            safe_path(env, source if origin == "associated" else env)
            if not env.is_dir(): raise ValueError("Selected environment is not a directory.")
            scanned_environments.append({"path": str(env), "origin": origin})
            for folder, dirs, names in os.walk(env, followlinks=False):
                check_cancel(_cancelled)
                base = Path(folder)
                kept = []
                for name in dirs:
                    if name in {".git", "__pycache__", ".cache", ".bin"}: continue
                    try: safe_path(base / name, env); kept.append(name)
                    except ValueError as exc: problems.append({"path": str(base / name), "reason": str(exc), "status": "skipped"})
                dirs[:] = kept
                file_count += len(names)
                if file_count > MAX_FILES: raise ValueError("Associated environment exceeds 50000 files; remaining metadata skipped.")
                if base.name.endswith(".dist-info") and "METADATA" in names:
                    text, proof = read(base / "METADATA", env); provenance.append({**proof, "origin": "installed_metadata"})
                    meta = Parser().parsestr(text)
                    if meta.get("Name") and meta.get("Version"):
                        add("PyPI", meta["Name"], "installed", meta["Version"], path=base / "METADATA", environment=str(env), site_packages=str(base.parent), metadata_dir=str(base), requires=meta.get_all("Requires-Dist", []))
                        for dep in meta.get_all("Requires-Dist", []):
                            try: req(dep, base / "METADATA", "transitive_declared", parent=norm("PyPI", meta["Name"]))
                            except ValueError: pass
                if "package.json" in names and ("node_modules" in base.parts or (origin == "explicit" and base != env)):
                    text, proof = read(base / "package.json", env)
                    doc = json.loads(text)
                    if doc.get("name") and doc.get("version"):
                        provenance.append({**proof, "origin": "installed_metadata"})
                        add("npm", doc["name"], "installed", str(doc["version"]), path=base / "package.json", environment=str(env), source_dir=str(base), requires=doc.get("dependencies", {}))
                        for package, spec in doc.get("dependencies", {}).items(): add("npm", package, "transitive_declared", declared=str(spec), path=base / "package.json", parent=doc["name"])
        except (OSError, ValueError, TypeError) as exc:
            problems.append({"path": str(env), "reason": str(exc)[:400], "status": "inconclusive"})

    for row in packages.values():
        obs = row["observations"]
        row["installed"] = sorted({o["version"] for o in obs if o["kind"] == "installed"})
        row["vendored"] = sorted({o["version"] for o in obs if o["kind"] == "vendored" and o["version"]})
        row["locked"] = sorted({o["version"] for o in obs if o["kind"] == "locked" and o["version"]})
        row["declared"] = sorted({o["declared"] for o in obs if o["kind"] == "declared" and o["declared"]})
        pinned = sorted({o["version"] for o in obs if o["kind"] == "declared" and o["version"] and "*" not in o["version"]})
        row["resolved"] = row["installed"] or row["vendored"] or row["locked"] or pinned
        row["resolution"] = ("installed" if row["installed"] else "vendored" if row["vendored"] else
                             "locked" if row["locked"] else ("minimum_declared" if row["ecosystem"] == "Go" else "declared")
                             if pinned else "unresolved")
        observed_versions = set(row["installed"] + row["vendored"] + row["locked"] + pinned)
        row["versions"] = [{"version": v, "kinds": sorted({o["kind"] for o in obs if o["version"] == v}), "provenance": [o for o in obs if o["version"] == v]} for v in sorted(observed_versions)]
        row["relationship"] = "direct" if any(o["kind"] == "declared" for o in obs) else "transitive"
        row["disagreements"] = []
        if row["installed"] and row["locked"] and set(row["installed"]) != set(row["locked"]): row["disagreements"].append("Installed and locked versions differ.")
        if len(row["resolved"]) > 1: row["disagreements"].append("Multiple exact versions are present.")
        from .scans import _satisfies
        for version in row["installed"] or row["vendored"] or row["locked"]:
            for spec in row["declared"]:
                if _satisfies(row["ecosystem"], version, spec) is False:
                    row["disagreements"].append(f"Exact version {version} does not satisfy declared range {spec}.")
        row["status"] = "resolved" if row["resolved"] else "inconclusive"
    return {"schema": "project-ide-inventory/1", "packages": sorted(packages.values(), key=lambda r: r["id"]), "problems": problems, "provenance": provenance,
            "scope": {"workspace_root": str(work), "source_root": str(source), "environments": scanned_environments, "manifest_files": len([p for p in provenance if p["origin"] == "workspace_manifest"]), "exclusions": sorted(workspace.EXCLUDED_DIRS | workspace.EXCLUDED_FILES), "policy": "Workspace dependency manifests and associated project metadata. No imports or project execution."}}
