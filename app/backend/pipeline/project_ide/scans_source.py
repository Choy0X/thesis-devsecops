"""Exact release source vault with bounded extraction and explicit coverage."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import tarfile
from urllib.parse import quote
import uuid
import zipfile

from . import workspace
from .scans_inventory import public_observation, safe_path

MAX_FILES = 4096
MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_RATIO = 100
TEXT_SUFFIXES = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".toml", ".yaml", ".yml", ".cfg", ".ini", ".txt", ".md", ".c", ".h", ".cpp", ".rs", ".go", ".html", ".css", ".sh"}
RESERVED = {"con", "prn", "aux", "nul", *{f"com{i}" for i in range(1, 10)}, *{f"lpt{i}" for i in range(1, 10)}}


def _name(name):
    if not name or "\\" in name or ":" in name or "\x00" in name or PureWindowsPath(name).drive or name.startswith("/"):
        raise ValueError("Unsafe archive path.")
    parts = PurePosixPath(name).parts
    if any(p in {".", ".."} or p.rstrip(" .") != p or p.split(".")[0].lower() in RESERVED for p in parts):
        raise ValueError("Unsafe archive path component.")
    return "/".join(parts)


def extract_archive(data, destination, kind, cancelled):
    from .scans import check_cancel
    if len(data) > MAX_TOTAL: raise ValueError("Compressed artifact exceeds 64 MiB.")
    destination = Path(destination)
    if destination.exists(): raise ValueError("Extraction destination must be new.")
    members, total, seen = [], 0, set()
    archive = zipfile.ZipFile(io.BytesIO(data)) if kind == "zip" else tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    try:
        for item in archive.infolist() if kind == "zip" else archive:
            check_cancel(cancelled)
            name = _name(item.orig_filename if kind == "zip" else item.name)
            if len(members) >= MAX_FILES: raise ValueError("Artifact exceeds 4096 members.")
            if name.lower() in seen: raise ValueError("Duplicate or case-colliding archive paths.")
            seen.add(name.lower())
            if kind == "zip":
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))): raise ValueError("Archive links and special files are forbidden.")
                directory, size = item.is_dir(), item.file_size
                if item.flag_bits & 1: raise ValueError("Encrypted archives are unsupported.")
                if size > max(1, item.compress_size) * MAX_RATIO: raise ValueError("Archive expansion ratio exceeds 100.")
            else:
                if not (item.isfile() or item.isdir()): raise ValueError("Archive links and special files are forbidden.")
                directory, size = item.isdir(), item.size
            total += size
            if size > MAX_FILE or total > MAX_TOTAL or total > max(1, len(data)) * MAX_RATIO: raise ValueError("Archive size/expansion limit exceeded.")
            members.append((name, item, directory, size))
        destination.mkdir(parents=True)
        for name, item, directory, size in members:
            check_cancel(cancelled)
            target = destination / name
            safe_path(target, destination)
            if directory: target.mkdir(parents=True, exist_ok=True); continue
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.open(item) if kind == "zip" else archive.extractfile(item)
            with stream:
                raw = stream.read(MAX_FILE + 1)
            if len(raw) != size: raise ValueError("Archive size changed or member exceeded its bound.")
            with target.open("xb") as out: out.write(raw)
            target.chmod(stat.S_IREAD)
        return destination
    finally:
        archive.close()


def _installed_files(observation, ecosystem, name):
    found, skipped = {}, []
    env = Path(observation["environment"])
    if ecosystem == "npm":
        root = Path(observation["source_dir"])
        safe_path(root, env)
        for folder, dirs, names in os.walk(root, followlinks=False):
            base = Path(folder)
            kept = []
            for child in dirs:
                relative = (base / child).relative_to(root).as_posix()
                if workspace.excluded(relative): skipped.append({"path": relative, "reason": "Excluded nested dependency/build/secret directory"}); continue
                try: safe_path(base / child, env); kept.append(child)
                except ValueError: skipped.append({"path": relative, "reason": "Link or junction"})
            dirs[:] = kept
            for file in names: found[(base / file).relative_to(root).as_posix()] = base / file
            if len(found) > MAX_FILES: break
        return found, skipped, env
    site, metadata = Path(observation["site_packages"]), Path(observation["metadata_dir"])
    record = metadata / "RECORD"
    if record.exists():
        safe_path(record, env)
        if record.stat().st_size > MAX_FILE: raise ValueError("Installed RECORD exceeds metadata limit.")
        for item in csv.reader(io.StringIO(record.read_text(encoding="utf-8"))):
            if not item: continue
            try: relative = _name(item[0])
            except ValueError: skipped.append({"path": item[0], "reason": "RECORD path outside site-packages or unsafe path"}); continue
            if ".dist-info/" in relative or workspace.excluded(relative): continue
            found[relative] = site / relative
            if len(found) > MAX_FILES: break
    else:
        top = metadata / "top_level.txt"
        roots = [name.replace("-", "_")]
        if top.exists():
            safe_path(top, env)
            if top.stat().st_size > MAX_FILE: raise ValueError("Installed top_level metadata exceeds limit.")
            roots = top.read_text(encoding="utf-8").splitlines()
        for module in roots:
            if not re.fullmatch(r"[A-Za-z_][\w]*", module): skipped.append({"path": module, "reason": "Unsupported top-level module"}); continue
            candidate = site / module
            if (site / (module + ".py")).is_file(): found[module + ".py"] = site / (module + ".py")
            if candidate.is_dir():
                safe_path(candidate, env)
                for folder, dirs, names in os.walk(candidate, followlinks=False):
                    base = Path(folder)
                    kept = []
                    for child in dirs:
                        relative = (base / child).relative_to(site).as_posix()
                        if workspace.excluded(relative):
                            skipped.append({"path": relative, "reason": "Excluded generated/secret directory"}); continue
                        try: safe_path(base / child, env); kept.append(child)
                        except ValueError: skipped.append({"path": relative, "reason": "Link or junction"})
                    dirs[:] = kept
                    for file in names: found[(base / file).relative_to(site).as_posix()] = base / file
                    if len(found) > MAX_FILES: break
    return found, skipped, env


def _snapshot(mapping, root, destination, cancelled):
    from .scans import check_cancel
    files, skipped, total = [], [], 0
    destination.mkdir(parents=True, exist_ok=False)
    for index, (relative, original) in enumerate(sorted(mapping.items())):
        check_cancel(cancelled)
        if index >= MAX_FILES:
            skipped.append({"path": relative, "reason": "4096 file limit reached"}); break
        if workspace.excluded(relative): skipped.append({"path": relative, "reason": "Excluded path"}); continue
        try:
            _name(relative); safe_path(original, root)
            before = original.stat()
            if not stat.S_ISREG(before.st_mode) or before.st_nlink > 1: raise ValueError("Nonregular or hard-linked file")
            if before.st_size > MAX_FILE or total + before.st_size > MAX_TOTAL: raise ValueError("File or total byte limit")
            raw = original.read_bytes()
            after = original.stat()
            if (before.st_mtime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino): raise ValueError("Source changed during snapshot")
            target = destination / relative; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw); target.chmod(stat.S_IREAD)
            total += len(raw)
            try:
                text = raw.decode("utf-8")
                status = "text" if "\x00" not in text and (Path(relative).suffix.lower() in TEXT_SUFFIXES or Path(relative).name in {"LICENSE", "Makefile"}) else "unsupported"
            except UnicodeError: status = "binary"
            files.append({"path": relative, "source_path": str(target), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "mtime_ns": before.st_mtime_ns, "status": status})
        except (OSError, ValueError) as exc: skipped.append({"path": relative, "reason": str(exc)[:300]})
    digest = hashlib.sha256(json.dumps([(f["path"], f["sha256"]) for f in files], separators=(",", ":")).encode()).hexdigest()
    return files, skipped, digest


def _published(service, row, version, destination, cancelled):
    from .scans import check_cancel, validate_url
    eco = row["ecosystem"]
    if eco not in {"PyPI", "npm"}:
        return None, {"status": "unavailable", "reason": "Exact published source retrieval is supported for PyPI and npm.", "version": version}
    for observation in row["observations"]:
        if observation.get("version") == version and not public_observation(eco, observation):
            return None, {"status": "unavailable", "reason": "Recorded dependency origin is a nonpublic registry, VCS, URL, or local source. Public release substitution is not permitted.", "version": version}
    name = quote(row["name"], safe=""); ver = quote(version, safe="")
    url = f"https://pypi.org/pypi/{name}/{ver}/json" if eco == "PyPI" else f"https://registry.npmjs.org/{name}/{ver}"
    metadata, lookup = service._lookup(url, cancelled=cancelled)
    if not metadata: return None, {"status": "unavailable", "lookup": lookup, "reason": "Exact published release metadata unavailable."}
    if eco == "PyPI":
        info = metadata.get("info", {})
        if info.get("version") != version or info.get("name") and info["name"].lower().replace("_", "-") != row["name"]: return None, {"status": "unavailable", "reason": "Published release identity mismatch", "lookup": lookup}
        artifacts = [a for a in metadata.get("urls", []) if a.get("packagetype") in {"sdist", "bdist_wheel"} and not a.get("yanked")]
        artifacts.sort(key=lambda a: (a.get("packagetype") != "sdist", a.get("filename", "")))
        artifact = artifacts[0] if artifacts else None
        if not artifact: return None, {"status": "unavailable", "lookup": lookup, "reason": "No exact source distribution or wheel is published."}
        artifact_url = artifact["url"]; expected = artifact.get("digests", {}).get("sha256")
        kind = "zip" if artifact.get("filename", "").endswith((".zip", ".whl")) else "tar"
    else:
        if metadata.get("name") != row["name"] or metadata.get("version") != version: return None, {"status": "unavailable", "reason": "Published release identity mismatch", "lookup": lookup}
        artifact = metadata.get("dist", {}); artifact_url = artifact.get("tarball")
        expected = artifact.get("integrity") or artifact.get("shasum"); kind = "tar"
    if not expected or not artifact_url: return None, {"status": "unavailable", "reason": "Published artifact lacks a digest or URL.", "lookup": lookup}
    validate_url(artifact_url); check_cancel(cancelled)
    raw = service.fetch_bytes(artifact_url); check_cancel(cancelled)
    if len(raw) > MAX_TOTAL: raise ValueError("Artifact exceeds 64 MiB.")
    if eco == "PyPI": valid = hashlib.sha256(raw).hexdigest() == expected
    elif "-" in expected:
        valid = False
        for token in expected.split():
            algo, encoded = token.split("-", 1)
            if algo in {"sha256", "sha384", "sha512"} and base64.b64encode(hashlib.new(algo, raw).digest()).decode() == encoded: valid = True
    else: valid = hashlib.sha1(raw).hexdigest() == expected
    if not valid: raise ValueError("Published artifact digest mismatch.")
    extract_archive(raw, destination, kind, cancelled)
    return destination, {"status": "verified_artifact", "lookup": lookup, "url": artifact_url, "sha256": hashlib.sha256(raw).hexdigest(), "expected_digest": expected, "version": version}


def _compare(installed, published_root):
    # Compare only matching release-relative paths. Missing counterparts remain explicit.
    lookup = {p.relative_to(published_root).as_posix(): p for p in published_root.rglob("*") if p.is_file()}
    matches, differs, missing = [], [], []
    for item in installed:
        candidates = [p for rel, p in lookup.items() if rel == item["path"] or rel.endswith("/" + item["path"])]
        if len(candidates) != 1: missing.append(item["path"]); continue
        (matches if hashlib.sha256(candidates[0].read_bytes()).hexdigest() == item["sha256"] else differs).append(item["path"])
    return {"status": "compared", "matching": matches, "different": differs, "not_comparable": missing, "limitation": "Content comparison of uniquely matched paths, not package authenticity or runtime equivalence."}


def review_sources(service, selected, inventory_result, cancelled, progress):
    from .scans import check_cancel, ScanCancelled
    run = service.cache / "sources" / uuid.uuid4().hex
    results = []
    for row in selected:
        versions = row["installed"] or row["resolved"]
        if not versions:
            results.append({"package_id": row["id"], "name": row["name"], "status": "inconclusive", "reason": "No exact installed or locked version is known.", "coverage": {"text_files": 0}})
        for version in versions:
            observations = [o for o in row["observations"] if o["kind"] == "installed" and o["version"] == version]
            # Distinct installed copies can contain local modifications even at the same version.
            for copy_index, observation in enumerate(observations or [None]):
                check_cancel(cancelled)
                if progress: progress({"phase": "source", "package_id": row["id"], "version": version, "completed": len(results)})
                identity = hashlib.sha256((row["id"] + version + str(copy_index)).encode()).hexdigest()[:20]
                base = run / identity
                base.mkdir(parents=True)
                result = {"package_id": row["id"], "id": row["id"], "name": row["name"], "ecosystem": row["ecosystem"], "version": version,
                          "installed_observation": observation, "advisory_context": service.advisory(row, version, cancelled), "files": [], "source_path": None, "origin": None}
                skipped = []
                try:
                    mapping = {}
                    if observation and row["ecosystem"] in {"PyPI", "npm"}:
                        mapping, skipped, root = _installed_files(observation, row["ecosystem"], row["name"])
                    if mapping:
                        path = base / "review"
                        files, omitted, digest = _snapshot(mapping, root, path, cancelled)
                        result.update({"files": files, "sha256": digest, "source_path": str(path), "origin": "installed"}); skipped.extend(omitted)
                        try:
                            published, proof = _published(service, row, version, base / "published", cancelled)
                            result["published_comparison"] = {"provenance": proof, **(_compare(files, published) if published else {"status": "unavailable"})}
                        except ScanCancelled: raise
                        except Exception as exc: result["published_comparison"] = {"status": "unavailable", "reason": str(exc)[:300]}
                    else:
                        published, proof = _published(service, row, version, base / "published", cancelled)
                        result["published_provenance"] = proof
                        if published:
                            mapping = {p.relative_to(published).as_posix(): p for p in published.rglob("*") if p.is_file()}
                            path = base / "review"
                            files, omitted, digest = _snapshot(mapping, published, path, cancelled)
                            result.update({"files": files, "sha256": digest, "source_path": str(path), "origin": "published_exact_release"}); skipped.extend(omitted)
                        else: skipped.append({"path": row["name"], "reason": proof["reason"]})
                        if observation: skipped.append({"path": observation["path"], "reason": "Installed source unavailable; exact release artifact fallback was attempted."})
                    result["status"] = "ready_for_review" if any(f["status"] == "text" for f in result["files"]) else "inconclusive"
                except ScanCancelled: raise
                except Exception as exc:
                    result["status"] = "inconclusive"; skipped.append({"path": row["name"], "reason": str(exc)[:300]})
                result["coverage"] = {"text_files": sum(f["status"] == "text" for f in result["files"]), "binary_files": sum(f["status"] == "binary" for f in result["files"]), "unsupported_files": sum(f["status"] == "unsupported" for f in result["files"]), "bytes": sum(f["bytes"] for f in result["files"]), "skipped": skipped, "limits": {"max_files": MAX_FILES, "max_file_bytes": MAX_FILE, "max_total_bytes": MAX_TOTAL}, "model_review": "pending", "static_analysis": "pending"}
                results.append(result)
    return {"schema": "project-ide-source/1", "analysis_only": True, "selected_package_ids": [r["id"] for r in selected], "packages": results,
            "scope": inventory_result["scope"], "provenance": inventory_result["provenance"], "problems": inventory_result["problems"], "vault": str(run)}
