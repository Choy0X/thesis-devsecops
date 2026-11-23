"""Bounded read-only static candidates for managed source and selected snapshots.

No project runner, importer, build command, legacy scan writer, or network is used.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import time

from app.backend.pipeline import projects
from app.backend.pipeline.scan import code_rules, project_code
from . import workspace
from .scans import check_cancel

MAX_FILES = 50000
MAX_FILE_BYTES = project_code.MAX_FILE_BYTES
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FINDINGS = project_code.MAX_FINDINGS
EXCLUDED_DIRS = workspace.EXCLUDED_DIRS | projects.SKIP_DIRS | {
    "site-packages", "dist-packages", "bower_components", "third_party", "third-party",
    "external", "deps", "dependencies", "generated", "downloads", "downloaded", ".yarn", ".pnpm-store",
}
GENERATED = re.compile(r"(?:@generated|automatically generated|auto-generated|do not edit)", re.I)
WORDS = ("Static pattern matches are candidates requiring source and data-flow review. "
         "No candidates does not establish safety. Only the reported files, languages and patterns were checked. "
         "Source was read without execution or modification.")


def _logical(name):
    if (not isinstance(name, str) or not name or any(c in name for c in "\\:\x00\r\n")
            or PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive
            or ".." in PurePosixPath(name).parts):
        raise ValueError("Invalid selected source path.")
    return name


def _regular(path):
    for parent in (path, *path.parents):
        if workspace.linked(parent):
            raise ValueError("Links and junctions are excluded.")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
        raise ValueError("Nonregular and hard-linked files are excluded.")
    return info


def analyze(root: Path, cancelled, progress=None, exclude_roots=()) -> dict:
    """Scan only own source beneath root. Excluded directories are not entered."""
    root = Path(root).absolute()
    check_cancel(cancelled)
    if not root.is_dir() or workspace.linked(root):
        raise ValueError("An existing regular workspace directory is required.")
    root = root.resolve()
    exclusions = tuple(Path(p).resolve() for p in exclude_roots)
    if progress:
        progress({"phase": "inventory"})
    rows, omitted, limited = [], [], False
    for folder, dirs, names in os.walk(root, followlinks=False):
        check_cancel(cancelled)
        base = Path(folder)
        kept = []
        for name in sorted(dirs):
            path = base / name
            rel = path.relative_to(root).as_posix()
            reason = None
            if name.lower() in EXCLUDED_DIRS or workspace.excluded(rel):
                reason = "excluded_directory"
            elif any(path.resolve().is_relative_to(p) for p in exclusions):
                reason = "selected_dependency_root"
            elif workspace.linked(path):
                reason = "links"
            if reason:
                omitted.append({"path": rel, "status": reason, "unit": "directory"})
            else:
                kept.append(name)
            if len(rows) + len(omitted) >= MAX_FILES:
                limited = True
                break
        if limited:
            break
        dirs[:] = kept
        for name in sorted(names):
            check_cancel(cancelled)
            path = base / name
            rel = path.relative_to(root).as_posix()
            reason = "excluded_file" if workspace.excluded(rel) else None
            if any(path.resolve().is_relative_to(p) for p in exclusions):
                reason = "selected_dependency_root"
            if len(rows) + len(omitted) >= MAX_FILES:
                limited = True
                break
            if reason:
                omitted.append({"path": rel, "status": reason, "unit": "file"})
            else:
                rows.append({"path": rel, "source_path": str(path), "status": "text"})
        if limited:
            break
    result = _analyze(rows, cancelled, progress, require_hash=False, own_source=True)
    result["workspace"] = str(root)
    result["scope"] = "project_code"
    result["files"] = omitted + result["files"]
    result["coverage"]["inventory_limited"] = limited
    result["coverage"]["excluded_directories"] = sorted(EXCLUDED_DIRS)
    result["coverage"]["exclude_roots"] = [str(p) for p in exclusions]
    _summarize(result)
    return result


def analyze_files(files, cancelled, progress=None) -> dict:
    """Review an explicit trusted source-vault file list, checking every digest.

    Caller selects the package and snapshots it first. A supplied source_path is
    never interpreted as a command, URL, import, or request to discover siblings.
    """
    return _analyze(files, cancelled, progress, require_hash=True, own_source=False)


def _analyze(rows, cancelled, progress, *, require_hash, own_source):
    findings, files, languages, total = [], [], {}, 0
    limited = False
    known = code_rules.languages_with_rules()
    scanned = 0
    next_update = 0.0
    file_total = min(len(rows), MAX_FILES) if own_source and progress else None
    for index, row in enumerate(rows):
        check_cancel(cancelled)
        if index >= MAX_FILES:
            limited = True
            break
        if not isinstance(row, dict):
            raise ValueError("Selected source files must be metadata objects.")
        if file_total is not None and (now := time.monotonic()) >= next_update:
            progress({"phase": "static_analysis", "completed": index, "total": file_total,
                      "path": row["path"], "scanned": scanned, "skipped": index - scanned, "findings": len(findings)})
            next_update = now + 0.25
            check_cancel(cancelled)
        item = {"path": str(row.get("path", "")), "status": "unreadable"}
        if row.get("sha256"):
            item["expected_sha256"] = row["sha256"]
        files.append(item)
        try:
            relative = _logical(row.get("path"))
            if workspace.excluded(relative):
                item["status"] = "excluded_file"
                continue
            if row.get("status", "text") not in {"text", "ready", "selected", "available"}:
                item["status"] = str(row.get("status"))[:80]
                continue
            supplied = row.get("source_path")
            if not isinstance(supplied, (str, Path)) or not Path(supplied).is_absolute():
                raise ValueError("Selected sources require an absolute source_path.")
            path = Path(supplied)
            before = _regular(path)
            item["bytes"] = before.st_size
            if before.st_size > MAX_FILE_BYTES:
                item["status"] = "too_large"
                continue
            if total + before.st_size > MAX_TOTAL_BYTES:
                item["status"] = "total_byte_limit"
                limited = True
                continue
            expected = row.get("sha256")
            if require_hash and not re.fullmatch(r"[0-9a-f]{64}", expected or ""):
                item["status"] = "missing_hash"
                continue
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            total += len(raw)
            after = _regular(path)
            actual = hashlib.sha256(raw).hexdigest()
            item["sha256"] = actual
            if len(raw) > MAX_FILE_BYTES or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                item["status"] = "source_changed"
                continue
            if expected is not None and expected != actual:
                item["status"] = "hash_mismatch"
                continue
            if b"\x00" in raw:
                item["status"] = "binary"
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeError:
                item["status"] = "unsupported_encoding"
                continue
            language = projects.LANGUAGES.get(path.suffix.lower())
            if not language:
                item["status"] = "unsupported_type"
                continue
            if own_source and (path.name.endswith((".min.js", ".min.ts", ".generated.py", ".g.ts", ".d.ts")) or GENERATED.search(text[:2048])):
                item["status"] = "generated"
                continue
            check_cancel(cancelled)
            if progress and not own_source:
                progress({"phase": "static_analysis", "completed": index, "path": relative})
            check_cancel(cancelled)
            lines = text.splitlines()
            long_lines = sum(len(line) > project_code.MAX_LINE_CHARS for line in lines)
            item.update(status="scanned", language=language, skipped_long_lines=long_lines,
                        coverage="rules" if language in known else "secrets_only")
            info = languages.setdefault(language, {"files": 0, "coverage": item["coverage"]})
            info["files"] += 1
            candidates = project_code.scan_file(relative, lines, language)
            check_cancel(cancelled)
            available = MAX_FINDINGS - len(findings)
            if len(candidates) > available:
                limited = True
            for candidate in candidates[:available]:
                findings.append({**candidate, "sha256": actual, "validation_state": "not_validated",
                                 "evidence": {"path": relative, "sha256": actual, "line": candidate["line"], "quote": candidate["snippet"]}})
            scanned += 1
            if len(findings) >= MAX_FINDINGS:
                limited = True
                break
        except (OSError, ValueError, RuntimeError) as exc:
            # Cancellation must propagate even though ScanCancelled is RuntimeError.
            from .scans import ScanCancelled
            if isinstance(exc, ScanCancelled):
                raise
            item["reason"] = str(exc)[:200]
            item["status"] = "unsafe_source" if isinstance(exc, ValueError) else "unreadable"
    check_cancel(cancelled)
    if file_total is not None:
        progress({"phase": "finalizing", "completed": len(files), "total": file_total,
                  "scanned": scanned, "skipped": len(files) - scanned, "findings": len(findings)})
    findings.sort(key=lambda f: (project_code.ORDER.index(f["severity"]), f["file"], f["line"], f["rule"]))
    check_cancel(cancelled)
    for index, finding in enumerate(findings, 1):
        finding["id"] = "candidate-" + str(index)
    result = {"schema": "project-ide-static-analysis/1", "scope": "dependency_source", "files": files,
              "findings": findings, "languages": languages, "words": WORDS,
              "coverage": {"partial": True, "limits_reached": limited, "bytes_read": total,
                           "max_files": MAX_FILES, "max_file_bytes": MAX_FILE_BYTES,
                           "max_total_bytes": MAX_TOTAL_BYTES, "max_findings": MAX_FINDINGS,
                           "rules": len(code_rules.ALL), "rule_languages": sorted(known), "execution_verified": False,
                           "dynamic_behavior": "not_checked", "data_flow": "not_checked"}}
    _summarize(result)
    return result


def _summarize(result):
    skipped = Counter(row["status"] for row in result["files"] if row["status"] != "scanned")
    result["skipped"] = dict(skipped)
    result["coverage"]["skipped_long_lines"] = sum(row.get("skipped_long_lines", 0) for row in result["files"])
    result["coverage"]["unsupported_extensions"] = dict(Counter(Path(row["path"]).suffix.lower() or "(no extension)"
        for row in result["files"] if row["status"] in {"unsupported_type", "unsupported"}))
    result["coverage"]["secrets_only_languages"] = sorted(name for name, info in result["languages"].items() if info["coverage"] == "secrets_only")
    result["summary"] = {"scanned_files": sum(row["status"] == "scanned" for row in result["files"]),
                         "candidates": len(result["findings"]), "skipped_entries": sum(skipped.values()),
                         "status": "candidates" if result["findings"] else "inconclusive",
                         "cut_short": result["coverage"]["limits_reached"] or result["coverage"].get("inventory_limited", False)}
