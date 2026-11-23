"""Scan the source code of a project with the static rules of ``code_rules``. The files are only read, never run, never copied, and never followed out of the project's folders.

What the report promises, and what it does not:

* a match is a ``candidate``: a text pattern matched a line. It says what the pattern may mean and what to check; it does not say the program is vulnerable;
* a language with rules is checked in full, a language without is checked for hard-coded secrets only and says so, and file types that are not source code (configuration, shell,
  templates) are not read at all, so an empty result is never presented as a clean one;
* files that were skipped (too large, binary, minified, unreadable) are counted by the reason, and a scan that hit its limits says that it was cut short;
* a secret is masked in the evidence, so that the report does not become the place where it is copied to.

The report is written under ``data/projects/<id>/scans/<scan>/report.json`` and the run is appended to the project's audit log.
"""
from __future__ import annotations

import datetime
import os
import pathlib
import re
import secrets

from app.backend.pipeline import fsio, projects, util
from app.backend.pipeline.scan import code_rules as rules

SCHEMA = "project-scan/1"
ORDER = ["critical", "high", "medium", "low"]
MAX_FILE_BYTES = 1_000_000
MAX_LINE_CHARS = 2_000                      # longer lines are minified code or data: not matched
MAX_FILES = 50_000
MAX_FINDINGS = 2_000
MAX_PER_FILE_RULE = 20                      # one rule firing on every line of a file is one thing to look at, not two hundred
CONTEXT_LINES = 2
CONTEXT_CHARS = 200
SNIPPET_CHARS = 240
COMMENT_STARTS = {"Python": ("#",), "JavaScript": ("//", "/*", "*"), "TypeScript": ("//", "/*", "*")}
TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "e2e", "fixtures", "__mocks__", "testdata"}
TEST_NAME = re.compile(r"(^test_|_test\.py$|\.(test|spec)\.[cm]?[jt]sx?$|^conftest\.py$)")


def _is_test(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in TEST_DIRS for p in parts[:-1]) or bool(TEST_NAME.search(parts[-1]))


def walk(folder: str):
    """``(relative path, absolute path, language)`` for each source file of a folder, in a stable order, and ``skipped`` counts by reason. Dependency and build folders are not
    entered, a link that leaves the folder is not followed, and the walk stops at ``MAX_FILES`` (``truncated``)."""
    root = os.path.realpath(folder)
    skipped = {"too_large": 0, "binary": 0, "unreadable": 0, "links": 0}
    files, truncated = [], False
    for dirpath, dirs, names in os.walk(root, followlinks=False):
        keep = []
        for d in sorted(dirs):
            if d in projects.SKIP_DIRS or (d.startswith(".") and d != ".github"):
                continue
            if not projects.inside(root, os.path.join(dirpath, d)):         # a junction or link that leaves the folder
                skipped["links"] += 1
                continue
            keep.append(d)
        dirs[:] = keep
        for name in sorted(names):
            lang = projects.LANGUAGES.get(os.path.splitext(name)[1].lower())
            if not lang:
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not projects.inside(root, full):
                skipped["links"] += 1
                continue
            if len(files) >= MAX_FILES:
                truncated = True
                return files, skipped, truncated
            files.append((pathlib.PurePath(os.path.relpath(full, root)).as_posix(), full, lang))
    return files, skipped, truncated


def _read_lines(path: str, skipped: dict) -> list[str] | None:
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            skipped["too_large"] += 1
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        skipped["unreadable"] += 1
        return None
    if b"\x00" in data[:2048]:
        skipped["binary"] += 1
        return None
    return data.decode("utf-8", errors="replace").splitlines()


def scan_file(rel: str, text_lines: list[str], language: str) -> list[dict]:
    """The candidates found in one file; each carries its evidence (the line and the lines around it, secrets masked)."""
    applicable = rules.for_language(language)
    blob = None
    active = []
    for r in applicable:
        if r.requires is not None:
            blob = blob if blob is not None else "\n".join(text_lines)
            if not r.requires.search(blob):
                continue
        active.append(r)
    comment = COMMENT_STARTS.get(language, ())
    in_tests = _is_test(rel)
    found, per = [], {}
    for i, line in enumerate(text_lines):
        if len(line) > MAX_LINE_CHARS:
            continue
        stripped = line.lstrip()
        is_comment = bool(comment) and stripped.startswith(comment)
        for r in active:
            if is_comment and not r.in_comments:
                continue
            if not r.pattern.search(line) or (r.unless is not None and r.unless.search(line)):
                continue
            severity, note = r.severity, None
            if r.refine is not None:
                lowered = r.refine(line)
                if lowered:
                    severity, note = lowered
            n = per.get((r.id), 0)
            per[r.id] = n + 1
            if n >= MAX_PER_FILE_RULE:
                continue
            ctx = []
            if r.context:
                lo, hi = max(0, i - CONTEXT_LINES), min(len(text_lines), i + CONTEXT_LINES + 1)
                ctx = [{"line": j + 1, "text": rules.masked(text_lines[j], CONTEXT_CHARS), "match": j == i} for j in range(lo, hi)]
            found.append({"rule": r.id, "severity": severity, "cwe": r.cwe, "summary": r.summary, "why": r.why, "advice": r.advice, "state": "candidate",
                          "file": rel, "line": i + 1, "language": language, "in_tests": in_tests,
                          "snippet": rules.masked(stripped, SNIPPET_CHARS) if r.show_line else "(a private key header; the key itself is not shown)", "context": ctx, **({"note": note} if note else {})})
    for r in active:                                                        # said once per file when a rule had more matches than are listed
        extra = per.get(r.id, 0) - MAX_PER_FILE_RULE
        if extra > 0:
            for f in reversed(found):
                if f["rule"] == r.id:
                    f["more_in_file"] = extra
                    break
    return found


def summarise(findings: list[dict], files: int, languages: dict, skipped: dict, truncated: bool) -> dict:
    sev = {s: sum(1 for f in findings if f["severity"] == s) for s in ORDER}
    by_rule: dict[str, int] = {}
    for f in findings:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    return {"files": files, "findings": len(findings), "severity": sev, "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
            "in_tests": sum(1 for f in findings if f["in_tests"]), "secrets_only_languages": sorted(k for k, v in languages.items() if v["coverage"] == "secrets_only"),
            "skipped": sum(skipped.values()), "cut_short": truncated}


def run(project: dict, *, progress=lambda *a, **k: None, write: bool = True) -> dict:
    """Scan the code of ``project`` and write the report; returns it. Refused when the project has no confirmation that it may be tested. With ``write`` false nothing is written and
    the project's last scan is left alone: the full scan (``deep_scan``) uses the findings as one of its parts and writes its own report."""
    projects.require_authorized(project)
    scan_id = f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    started = util.now_zulu()
    has_rules = rules.languages_with_rules()
    findings, languages = [], {}
    skipped = {"too_large": 0, "binary": 0, "unreadable": 0, "links": 0}
    scanned, truncated = 0, False
    for folder in project["folders"]:
        t0 = util.now_zulu()
        progress(state="walk", label=f"Listing the source files in {folder['path']}")
        files, skip, cut = walk(folder["path"])
        truncated = truncated or cut
        for k, v in skip.items():
            skipped[k] += v
        before, read_before = len(findings), scanned
        for n, (rel, full, lang) in enumerate(files):
            if len(findings) >= MAX_FINDINGS:
                truncated = True
                break
            if n % 100 == 0:
                progress(state="rules", label=f"Reading {rel}", processed=n, total=len(files))
            info = languages.setdefault(lang, {"files": 0, "coverage": "rules" if lang in has_rules else "secrets_only", "scanned": 0})
            info["files"] += 1
            lines = _read_lines(full, skipped)
            if lines is None:
                continue
            info["scanned"] += 1
            scanned += 1
            findings.extend({**f, "folder": folder["path"]} for f in scan_file(rel, lines, lang))
        projects.audit(project["id"], tool="static rules", summary=f"{scanned - read_before} of {len(files)} source files read, {len(findings) - before} candidates; rules: {len(rules.ALL)}",
                       target=folder["path"], started=t0, ended=util.now_zulu(), exit_status=0 if not cut else 1)
    findings.sort(key=lambda f: (ORDER.index(f["severity"]), f["in_tests"], f["file"], f["line"]))              # serious first, test code after real code
    for i, f in enumerate(findings):
        f["id"] = f"code-{i + 1}"
    for lang, info in languages.items():
        info["reason"] = (f"There are no vulnerability rules for {lang} yet, so these files were checked for hard-coded secrets only." if info["coverage"] == "secrets_only" else "")
    summary = summarise(findings, scanned, languages, skipped, truncated)
    words = ("Every entry is a candidate: a text pattern matched a line, which is a place to look, not a proof that the program is vulnerable. "
             "No entry does not mean no problem: only the languages and patterns listed were checked, and files of other kinds were not read.")
    if truncated:
        words += f" The scan stopped at its limits ({MAX_FILES:,} files or {MAX_FINDINGS:,} candidates), so more may exist."
    report = {"schema": SCHEMA, "kind": "code", "scan_id": scan_id, "project_id": project["id"], "started": started, "finished": util.now_zulu(), "words": words, "rules": len(rules.ALL),
              "languages": languages, "skipped": skipped, "findings": findings, "summary": summary}
    if write:
        folder = projects.ROOT / project["id"] / "scans" / scan_id
        folder.mkdir(parents=True, exist_ok=True)
        fsio.write_json(folder / "report.json", report, indent=2)
        projects.record_scan(project["id"], {"at": report["finished"], "scan_id": scan_id, "kind": "code", "summary": _line(summary)})
    return report


def _line(s: dict) -> str:
    sev = s["severity"]
    return f"{s['files']} files: {s['findings']} candidates ({sev['critical']} critical, {sev['high']} high, {sev['medium']} medium, {sev['low']} low)"
