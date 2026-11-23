"""The tools an agent session may use (agentic mode, Part C).

Plan mode reuses ``chat_tools.TOOLS``/``Workspace`` unmodified -- zero new code, and the reason Plan mode never creates a sandbox at all. Manual and Auto mode read and write the
project's *copy* inside its agent sandbox instead of the host's own files: the same four read-only tools (``list_files``, ``read_file``, ``search_code``, ``scan_results``), now backed
by ``SandboxWorkspace`` and ``sandbox_files.py``, plus ``write_file``, ``run_command``, ``diff_so_far``, ``checkpoint`` and ``update_task_list``. ``project_graph`` is added once Part D
exists.

What ``write_file``/``run_command`` cannot reach: the *real* project tree. Nothing here is ``agent_apply.apply()`` (Part C's own, separate module) -- that is the only path that ever
writes to the real project, and it is deliberately absent from both ``TOOLS`` lists below, so no model call in any mode, including Auto, can ever reach it.

``SandboxWorkspace`` only rejects path traversal (a model-chosen path is still untrusted input); it does no credential filtering of its own (``copyin.py`` already excluded what looks
like credentials when the project was copied in) and no host-escape check (Docker's own containment -- no bind mount, no network -- already covers that).
"""
from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass

from app.backend import sandbox, sandbox_files
from app.backend.pipeline import agent_sessions, chat_tools, credentials, projects
from app.backend.pipeline.agentic import codeintel_tools, diagnostics, search_args, web_tools
from app.backend.pipeline.agentic.exec import plan_tools, sandbox_tools
from app.backend.pipeline.scan import agent_sandbox, code_rules

AGENT_EXEC_TIMEOUT = 120
AGENT_EXEC_MAX = 600
CONDENSE_HEAD = 4_000
CONDENSE_TAIL = 4_000
_LINE = re.compile(r"^(.*?):(\d+):(.*)$")


def condense_output(text: str, head: int = CONDENSE_HEAD, tail: int = CONDENSE_TAIL) -> str:
    """Long output kept at both ends (a build's noisy setup is at the start, a failure usually explains itself at the end), the middle dropped with a count of what that was --
    applied before the outer, uniform ``MAX_TOOL_CHARS`` cut every tool result already gets."""
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return text[:head] + f"\n... [{dropped:,} characters omitted] ...\n" + text[-tail:]


class SandboxWorkspace:
    """The agent sandbox's own copy of the project, as the write/exec tools see it: a label each when there are several roots, nothing outside them."""

    def __init__(self, container_id: str, roots: list[str]):
        self.container_id = container_id
        self._order = list(roots)
        self.roots = {posixpath.basename(r): r for r in roots}

    @property
    def labelled(self) -> bool:
        return len(self._order) > 1

    def root_of(self, label: str) -> str:
        if not self.labelled:
            return self._order[0]
        if label not in self.roots:
            raise chat_tools.ToolError(f"This project has several folders; give a path that starts with one of: {', '.join(self.roots)}.")
        return self.roots[label]

    def resolve(self, path: str | None) -> tuple[str, str, str]:
        """``(label, absolute container path, relative path)``; ``("", "", "")`` is the top of a project of several folders. Raises ``ToolError`` for a path that tries to leave the
        project -- the only check this makes; the copy was already hardened at copy-in time, and Docker's containment covers the rest."""
        text = str(path or "").replace("\\", "/").strip()
        if "\x00" in text or text.startswith("/"):
            raise chat_tools.ToolError("Give a path relative to the project, for example src/app.py.")
        parts = [p for p in text.split("/") if p and p != "."]
        if ".." in parts:
            raise chat_tools.ToolError("A path cannot go up out of the project.")
        if self.labelled:
            if not parts:
                return "", "", ""
            if parts[0] not in self.roots:
                raise chat_tools.ToolError(f"This project has several folders; start the path with one of: {', '.join(self.roots)}.")
            label, root, parts = parts[0], self.roots[parts[0]], parts[1:]
        else:
            label, root = "", self._order[0]
        rel = "/".join(parts)
        full = posixpath.join(root, rel) if rel else root
        return label, full, rel

    @staticmethod
    def shown(label: str, rel: str) -> str:
        return "/".join(p for p in (label, rel) if p)


@dataclass
class AgentContext:
    """Everything a tool call needs: which project and session it belongs to, and the workspace to run it against -- ``chat_tools.Workspace`` in Plan mode (``container_id`` is
    ``None``, and nothing here is ever reached: ``call`` hands straight to ``chat_tools.call``), ``SandboxWorkspace`` in Manual and Auto mode."""

    project_id: str
    session_id: str
    ws: object
    container_id: str | None = None
    v2: bool = False                         # the v2 engine: project_graph says which files the run has changed since the index was built, and scan_results carries the finding ids
    transcript: object | None = None         # a function that returns the session's transcript so far (the web tools number their pages from it)
    on_output: object | None = None          # a function (stream, text) that a running command gives its output to as it prints, for the page to show live
    cancel: object | None = None             # a threading.Event that is set when the owner stops the session, so that a running command is ended too


def _skip_dir(name: str) -> bool:
    return name in projects.SKIP_DIRS or (name.startswith(".") and name != ".github") or credentials.is_secret_dir(name)


def list_files(ctx: AgentContext, path: str = "") -> dict:
    ws = ctx.ws
    label, full, rel = ws.resolve(path)
    if ws.labelled and not full:
        return {"path": "", "entries": [{"name": lab, "type": "folder"} for lab in ws.roots], "summary": f"Listed the {len(ws.roots)} folders of the project"}
    doc = sandbox_files.listing(ws.container_id, full)
    entries, hidden = [], 0
    for e in doc["entries"]:
        if e["kind"] == "folder":
            if credentials.is_secret_dir(e["name"]):
                entries.append({"name": e["name"], "type": "folder", "kept_from_assistant": True})
            elif _skip_dir(e["name"]):
                hidden += 1
            else:
                entries.append({"name": e["name"], "type": "folder"})
        elif e["kind"] == "file":
            entries.append({"name": e["name"], "type": "file", **({"kept_from_assistant": True} if credentials.is_kept(e["name"]) else {"bytes": e["size"]})})
    shown = ws.shown(label, rel)
    out = {"path": shown, "entries": entries[:chat_tools.MAX_LIST], "summary": f"Listed {shown or 'the top of the project'}"}
    if len(entries) > chat_tools.MAX_LIST:
        out["truncated"] = f"{len(entries) - chat_tools.MAX_LIST} more entries are not shown"
    if hidden:
        out["not_listed"] = f"{hidden} dependency, build or hidden folders are not listed"
    return out


def read_file(ctx: AgentContext, path: str, start_line: int = 1, end_line: int | None = None) -> dict:
    ws = ctx.ws
    label, full, rel = ws.resolve(path)
    if not rel:
        raise chat_tools.ToolError("Name a file to read.")
    if credentials.is_kept(rel):                               # the sandbox copy leaves out the names it knows; this is the same list the tools of Plan mode refuse by
        raise chat_tools.ToolError(chat_tools.KEPT)
    try:
        _, content = sandbox_files.download(ws.container_id, full)
    except sandbox_files.FileError as e:
        raise chat_tools.ToolError(f"{e} Use list_files or find_files to find the right path." if str(e).endswith("is not there.") else str(e)) from None
    if b"\x00" in content[:2048]:
        raise chat_tools.ToolError("This is not a text file.")
    lines = content.decode("utf-8", errors="replace").splitlines()
    config = posixpath.splitext(credentials.plain(posixpath.basename(full)))[1].lower() not in projects.LANGUAGES
    total = len(lines)
    start = max(1, int(start_line or 1))
    if start > max(total, 1):
        raise chat_tools.ToolError(f"The file has {total} lines.")
    end = min(total, int(end_line) if end_line else start + chat_tools.READ_LINES - 1, start + chat_tools.MAX_READ_LINES - 1)
    body, chars, last = [], 0, start - 1
    for n in range(start, end + 1):
        text = code_rules.masked(lines[n - 1], chat_tools.LINE_CHARS, config=config)
        if chars + len(text) > chat_tools.MAX_READ_CHARS:
            break
        body.append(f"{n}: {text}")
        chars += len(text)
        last = n
    shown = ws.shown(label, rel)
    out = {"path": shown, "start_line": start, "end_line": last, "total_lines": total, "text": "\n".join(body), "summary": f"Read {shown} lines {start} to {last} of {total}"}
    if last < total:
        out["lines_after"] = total - last
    return out


def read_files(ctx: AgentContext, paths: list) -> dict:
    return codeintel_tools.read_many(lambda p, a, b: read_file(ctx, p, a, b), paths)


def find_symbol(ctx: AgentContext, name: str, kind: str = "all", path: str = "") -> dict:
    return codeintel_tools.find_symbol_sandbox(ctx, name, kind, path)


def search_code(ctx: AgentContext, pattern: str, path: str = "", ignore_case: bool = True, glob: str = "") -> dict:
    """Searches with POSIX extended regular expressions (``grep -E``), not the Python dialect Plan mode's ``search_code`` uses -- the price of running inside the sandbox rather
    than on the host. A plain word or a simple pattern behaves the same either way. ``path`` is a folder or a file; a glob in it (or in ``glob``) filters the matches by file."""
    ws = ctx.ws
    pattern = search_args.fix_regex(pattern)
    if not pattern.strip():
        raise chat_tools.ToolError("Give a pattern to search for.")
    if len(pattern) > chat_tools.MAX_PATTERN:
        raise chat_tools.ToolError(f"Use a pattern shorter than {chat_tools.MAX_PATTERN} characters.")
    folder, path_glob = search_args.split_path(path)
    filters = chat_tools._name_filters(path_glob, glob)
    label, full, rel = ws.resolve(folder)
    targets = [(lab, root, "") for lab, root in ws.roots.items()] if (ws.labelled and not full) else [(label, ws.root_of(label), rel)]
    matches, searched, cut = [], 0, ""
    flag = "-rHniI" if ignore_case else "-rHnI"
    for lab, root, sub in targets:
        target = sub or "."
        cmd = f"grep {flag} --exclude-dir=.git -E -m {chat_tools.MAX_MATCHES} -e {shlex.quote(pattern)} -- {shlex.quote(target)}"
        try:
            res = sandbox.exec_run(ws.container_id, cmd, workdir=root, timeout=AGENT_EXEC_TIMEOUT)
        except Exception as e:
            raise chat_tools.ToolError(f"The search could not run: {type(e).__name__}: {str(e)[:200]}.") from None
        if res["exit_code"] not in (0, 1):                                       # 0 = matches found, 1 = none found; anything else is a real error
            raise chat_tools.ToolError(f"grep could not search there: {res['output'].strip().splitlines()[-1][:300] if res['output'].strip() else 'unknown error'}")
        searched += 1
        for line in res["output"].splitlines():
            m = _LINE.match(line)
            if not m:
                continue
            fpath, lineno, content = m.group(1), m.group(2), m.group(3)
            fpath = fpath[2:] if fpath.startswith("./") else fpath
            if credentials.is_kept(fpath):
                continue
            if filters:
                from_base = fpath[len(sub) + 1:] if sub and fpath.startswith(sub + "/") else fpath
                if not all(f(from_base) or f(fpath) for f in filters):
                    continue
            matches.append({"file": ws.shown(lab, fpath), "line": int(lineno), "text": code_rules.masked(content.strip(), chat_tools.LINE_CHARS)})
            if len(matches) >= chat_tools.MAX_MATCHES:
                cut = f"Stopped at {chat_tools.MAX_MATCHES} matches; narrow the pattern or the path."
                break
        if cut:
            break
    out = {"pattern": pattern, "files_searched": searched, "matches": matches, "summary": f"Searched {searched} root(s) of the sandbox copy for {pattern!r}: {len(matches)} matches"}
    if cut:
        out["truncated"] = cut
    return out


def find_files(ctx: AgentContext, pattern: str, path: str = "") -> dict:
    """Files of the sandbox copy whose path matches a glob (``chat_tools.glob_matcher``). The list comes from git (``git ls-files``: tracked and untracked files that are not ignored), so
    a dependency folder or a build output that the project itself ignores is left out without a walk of the whole tree."""
    ws = ctx.ws
    pattern = str(pattern or "").strip()
    if not pattern:
        raise chat_tools.ToolError("Give a file name or pattern, for example *.py or src/**/test_*.py.")
    if len(pattern) > chat_tools.MAX_PATTERN:
        raise chat_tools.ToolError(f"Use a pattern shorter than {chat_tools.MAX_PATTERN} characters.")
    matches = chat_tools.glob_matcher(pattern)
    folder, path_glob = search_args.split_path(path)
    in_path = chat_tools.glob_matcher(path_glob) if path_glob else None
    label, full, rel = ws.resolve(folder)
    targets = [(lab, root, "") for lab, root in ws.roots.items()] if (ws.labelled and not full) else [(label, ws.root_of(label), rel)]
    found, cut = [], ""
    for lab, root, sub in targets:
        res = sandbox.exec_run(ws.container_id, "git ls-files -co --exclude-standard -z", workdir=root, timeout=AGENT_EXEC_TIMEOUT)
        if res["exit_code"] != 0:
            raise chat_tools.ToolError(f"The file list could not be read: {res['output'].strip()[:200]}")
        for name in sorted(n for n in res["output"].split("\0") if n):
            if sub and not (name == sub or name.startswith(sub + "/")):
                continue
            if any(_skip_dir(part) for part in name.split("/")[:-1]) or credentials.is_kept(name) or not matches(name):
                continue
            if in_path and not (in_path(name[len(sub) + 1:] if sub else name) or in_path(name)):
                continue
            found.append(ws.shown(lab, name))
            if len(found) >= chat_tools.MAX_LIST:
                cut = f"Stopped at {chat_tools.MAX_LIST} files; narrow the pattern or the path."
                break
        if cut:
            break
    out = {"pattern": pattern, "files": found, "summary": f"Looked for files matching {pattern!r} in the sandbox copy: {len(found)} found"}
    if cut:
        out["truncated"] = cut
    return out


DEPENDENCIES_SHOWN = 80
DEPENDENCY_NOTE = ("The dependency scan reads the manifests and lock files only (never the code of a dependency) and matches the versions in them against known advisories. It does not look up newer releases: "
                   "judge from your own knowledge whether a version is old, and say that this is your judgement and not a finding of the scan. 'inconclusive' only means that no lock file fixes the version "
                   "(normal for a project that has not been installed): report it, and name the install command as a step for the owner, which Manual or Auto can run in a sandbox copy. Never try to install anything yourself here. "
                   "'env_error' means that the advisories of that ecosystem are not on this computer, so nothing was checked: it is not a fault of the project or of its setup. Say that it was not checked, give the reason "
                   "in not_checked, and tell the owner that the advisories can be downloaded under CVE Collections; do not go looking for a cause in the project's files.")


def scan_results(ctx: AgentContext, kind: str = "code") -> dict:
    """The project's own, already-stored scan history (never the sandbox copy's files): identical in every mode, since nothing here reads file content."""
    if kind not in ("code", "dependencies"):
        raise chat_tools.ToolError("kind must be code or dependencies.")
    scans = [s for s in projects.scans(ctx.project_id) if s["kind"] == kind]
    if not scans:
        tool = "rescan_code" if kind == "code" else "rescan_dependencies"
        if kind == "dependencies":
            return {"available": False, "message": "This project has no dependency scan yet. Run one with rescan_dependencies only if the owner asked about dependencies, vulnerabilities or outdated packages (the owner is "
                                                   "asked to confirm it); for any other question carry on without it.", "summary": "Looked for a dependency scan: none yet"}
        return {"available": False, "message": f"This project has no {kind} scan yet. Run one yourself with {tool}, or carry on by reading the files; do not ask the owner to run it.",
                "summary": f"Looked for a {kind} scan: none yet"}
    r = projects.scan_report(ctx.project_id, scans[0]["scan_id"])
    out = {"available": True, "kind": kind, "scanned_at": r["finished"], "words": r["words"], "counts": r["summary"]}
    if kind == "code":
        keys = ("id", "rule", "severity", "cwe", "summary", "file", "line", "in_tests", "state") if ctx.v2 else ("rule", "severity", "cwe", "summary", "file", "line", "in_tests", "state")
        out["findings"] = [{k: f[k] for k in keys if k in f} for f in r["findings"][:chat_tools.FINDINGS_SHOWN]]
        out["languages"] = {k: v["coverage"] for k, v in r["languages"].items()}
        more = len(r["findings"]) - chat_tools.FINDINGS_SHOWN
    else:
        rows = [d for d in r["dependencies"] if d["state"] == "vulnerable"]
        out["vulnerable"] = [{"name": d["name"], "version": d["version"], "ecosystem": d["ecosystem"], "severity": d.get("severity"), "advisories": [a["id"] for a in d.get("advisories", [])],
                              "recommendation": d.get("recommendation")} for d in rows[:chat_tools.FINDINGS_SHOWN]]
        out["not_checked"] = {e: v["reason"] for e, v in r["ecosystems"].items() if not v.get("checked")}
        out["dependencies"] = [{k: d.get(k) for k in ("name", "declared", "version", "state") if d.get(k) is not None} for d in r["dependencies"][:DEPENDENCIES_SHOWN]]
        out["how_to_read_it"] = DEPENDENCY_NOTE
        more = len(rows) - chat_tools.FINDINGS_SHOWN
    if more > 0:
        out["truncated"] = f"{more} more are in the report and not shown here"
    out["summary"] = f"Read the newest {kind} scan of {r['finished'][:10]}"
    return out


def rescan_dependencies(ctx: AgentContext) -> dict:
    """Run a fresh dependency/CVE advisory scan of the real project (never the sandbox copy -- the advisory data lives only on the host) and read it back exactly as scan_results
    would, now that it is the newest one. The same thing a person's own "Scan Dependencies" button does; offline (never looks a registry up for outdated versions)."""
    try:
        from app.backend.pipeline.scan import project_deps
        project_deps.run(projects.get(ctx.project_id), check_outdated=False)
    except chat_tools.ToolError:
        raise
    except Exception as e:
        raise chat_tools.ToolError(f"The dependency scan could not run: {type(e).__name__}: {str(e)[:160]}") from None
    out = scan_results(ctx, "dependencies")
    out["summary"] = "Ran a fresh dependency/CVE scan of the real project just now. " + out["summary"]
    return _scan_note(ctx, out)


def rescan_code(ctx: AgentContext) -> dict:
    """Run a fresh code-pattern security scan of the real project (never the sandbox copy) and read it back exactly as scan_results would, now that it is the newest one. The
    same thing a person's own "Scan Code" button does."""
    try:
        from app.backend.pipeline.scan import project_code
        project_code.run(projects.get(ctx.project_id))
    except chat_tools.ToolError:
        raise
    except Exception as e:
        raise chat_tools.ToolError(f"The code scan could not run: {type(e).__name__}: {str(e)[:160]}") from None
    out = scan_results(ctx, "code")
    out["summary"] = "Ran a fresh code scan of the real project just now. " + out["summary"]
    return _scan_note(ctx, out)


SCAN_NOTE = ("This scan read the REAL project, not the sandbox copy where your changes are. It cannot show whether your change worked or fixed anything: for that run the project's tests, or verify_fix "
             "with a regression test.")


def _scan_note(ctx: AgentContext, out: dict) -> dict:
    return {**out, "note": SCAN_NOTE} if ctx.v2 else out


def project_graph(ctx: AgentContext, path: str = "") -> dict:
    """The project's structural graph, read from the project's own index (built against the real project, not the sandbox copy -- the graph describes what the project looks
    like, which is the same question in every mode). An empty path is an overview of every indexed file."""
    try:
        from app.backend.pipeline.db import mongo
        from app.backend.pipeline.rag import project_graph as pg
        out = pg.tool_result(mongo.database(), ctx.project_id, ctx.ws, path)
    except chat_tools.ToolError:
        raise
    except Exception as e:
        raise chat_tools.ToolError(f"The project graph could not be read: {type(e).__name__}: {str(e)[:160]}") from None
    return _mark_stale(ctx, out, path) if ctx.v2 and ctx.container_id else out


STALE_NOTE = "The graph was built from the project as it was; this file has been changed in the sandbox since, so the graph shows the OLD version. Use find_symbol or read_file for the current one."


def sandbox_changed(ctx: AgentContext) -> set[str]:
    """The files of the sandbox copy that differ from the copy the session started from (git's own view, in each root), as the paths the tools show. Empty when git cannot say."""
    ws, changed = ctx.ws, set()
    for lab, root in ws.roots.items():
        try:
            res = sandbox.exec_run(ws.container_id, "git status --porcelain -z", workdir=root, timeout=AGENT_EXEC_TIMEOUT)
        except Exception:  # ignored: a marker that cannot be made is left out; the result is still true of the index
            continue
        if res["exit_code"] != 0:
            continue
        for entry in (e for e in res["output"].split("\0") if len(e) > 3):
            changed.add(ws.shown(lab if ws.labelled else "", entry[3:]))
    return changed


def _mark_stale(ctx: AgentContext, out: dict, path: str) -> dict:
    changed = sandbox_changed(ctx)
    if not changed:
        return out
    if path and out.get("path") in changed and out.get("available", True) is not False:
        return {**out, "stale": True, "note": STALE_NOTE}
    if not path and isinstance(out.get("files"), list):
        stale = sorted(f["path"] for f in out["files"] if f.get("path") in changed)
        if stale:
            return {**out, "stale_files": stale[:20], "note": f"{len(stale)} file(s) listed here have been changed in the sandbox since the graph was built; for them the graph shows the old version."}
    return out


def write_file(ctx: AgentContext, path: str, content: str) -> dict:
    ws = ctx.ws
    label, full, rel = ws.resolve(path)
    if not rel:
        raise chat_tools.ToolError("Name a file to write, relative to the project.")
    if credentials.is_kept(rel):
        raise chat_tools.ToolError(chat_tools.KEPT)
    folder, name = posixpath.split(full)
    data = content.encode("utf-8")
    shown = ws.shown(label, rel)
    try:
        _, existing = sandbox_files.download(ws.container_id, full)
    except sandbox_files.FileError:
        existing = None
    if existing is not None and existing == data:                 # the same file again: nothing is written, and the loop guard is told that nothing changed (it would otherwise forget what it had seen)
        return {"path": shown, "bytes": len(data), "unchanged": True, "summary": f"{shown} already has exactly this content; nothing was written"}
    try:
        sandbox.exec_run(ws.container_id, f"mkdir -p -- {shlex.quote(folder)}", timeout=30)
        out = sandbox_files.upload(ws.container_id, folder, name, data, overwrite=True, uid=agent_sandbox.UID)
    except sandbox_files.FileError as e:
        raise chat_tools.ToolError(str(e)) from None
    return diagnostics.add_to({"path": shown, "bytes": out["bytes"], "summary": f"Wrote {out['bytes']:,} bytes to {shown}"}, ws.container_id, full, shown)


_READ_NUMBER = re.compile(r"^[ \t]*\d{1,6}: ?")                 # what read_file prints in front of every line ("12: text"); a model copies it into old


def without_read_numbers(text: str) -> str | None:
    """``text`` without the line numbers ``read_file`` prints, when it is plainly a copy of that output: at least two lines, every line that is not empty numbered, the numbers going up. Otherwise
    None (a file of code in which some lines start with a number is not touched). A model that copies read_file's lines into the text it writes puts the numbers into the file."""
    lines = text.split("\n")
    numbered = [l for l in lines if l.strip()]
    if len(numbered) < 2 or not all(_READ_NUMBER.match(l) for l in numbered):
        return None
    numbers = [int(re.match(r"[ \t]*(\d+)", l).group(1)) for l in numbered]
    if any(b <= a for a, b in zip(numbers, numbers[1:])):
        return None
    return "\n".join(_READ_NUMBER.sub("", l) for l in lines)


def _spans(text: str, old: str) -> list[tuple[int, int]]:
    out, at = [], text.find(old)
    while at >= 0 and old:
        out.append((at, at + len(old)))
        at = text.find(old, at + len(old))
    return out


def _reindent(new: str, delta: int, unit: str) -> str:
    """``new`` shifted right by ``delta`` characters of indentation (or left when it is negative, where it can be)."""
    if not delta:
        return new
    lines = new.split("\n")
    if delta > 0:
        return "\n".join((unit * delta + l) if l.strip() else l for l in lines)
    return "\n".join(l[min(-delta, len(l) - len(l.lstrip(" \t"))):] for l in lines)


def locate_edit(text: str, old: str, new: str) -> tuple[list[tuple[int, int]], str, str | None]:
    """``(places, what to put there, how it was found)`` for an edit. ``old`` is looked for exactly first; then, only when that finds nothing, in the ways a model's copy of a file is usually
    off and nothing else: a file with Windows line endings read as ``\\n``, the line numbers ``read_file`` prints in front of each line, and indentation or trailing spaces that differ. A way
    that finds the text in more than one place is not used to pick one: the caller says it is ambiguous."""
    spans = _spans(text, old)
    if spans:
        return spans, new, None
    if "\r\n" in text and "\r" not in old:
        crlf_old, crlf_new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
        spans = _spans(text, crlf_old)
        if spans:
            return spans, crlf_new, "the file's own line endings"
    lines = old.split("\n")
    if any(_READ_NUMBER.match(l) for l in lines):
        bare = "\n".join(_READ_NUMBER.sub("", l) for l in lines)
        bare_new = "\n".join(_READ_NUMBER.sub("", l) for l in new.split("\n")) if any(_READ_NUMBER.match(l) for l in new.split("\n")) else new
        for candidate, put in ((bare, bare_new), (bare.replace("\n", "\r\n"), bare_new.replace("\n", "\r\n"))):
            spans = _spans(text, candidate)
            if spans:
                return spans, put, "the line numbers of read_file left out"
        old, new, lines = bare, bare_new, bare.split("\n")
    if old.startswith("\n") and new.startswith("\n"):             # a blank line at either end of old is not part of what is matched, so it is not part of what is put there either
        new = new[1:]
    if old.endswith("\n") and new.endswith("\n"):
        new = new[:-1]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return [], new, None
    want = [l.strip() for l in lines]
    file_lines = text.split("\n")
    have = [l.strip() for l in file_lines]
    offsets, pos = [], 0
    for l in file_lines:
        offsets.append(pos)
        pos += len(l) + 1
    starts = [i for i in range(len(have) - len(want) + 1) if have[i:i + len(want)] == want]
    if not starts:
        return [], new, None
    spans = [(offsets[i], offsets[i + len(want) - 1] + len(file_lines[i + len(want) - 1].rstrip("\r"))) for i in starts]
    first = file_lines[starts[0]]
    shift = (len(first) - len(first.lstrip(" \t"))) - (len(lines[0]) - len(lines[0].lstrip(" \t")))
    put = _reindent(new, shift, "\t" if first.startswith("\t") else " ")
    return spans, put.replace("\n", "\r\n") if "\r\n" in text else put, "indentation and spaces at the ends of lines ignored"


MASK = "****"


def _masked_spans(text: str, old: str) -> list[tuple[int, int]]:
    """Where ``old`` is in ``text`` when ``old`` was copied out of read_file, which shows a credential as ``****``: each ``****`` stands for a run of characters that are not a quote or a line end."""
    parts = old.split(MASK)
    if len(parts) < 2:
        return []
    pattern = "[^\"'`\r\n]*".join(re.escape(p) for p in parts)
    return [(m.start(), m.end()) for m in re.finditer(pattern, text)]


def edit_file(ctx: AgentContext, path: str, old: str, new: str) -> dict:
    """Replace one piece of text in a file of the sandbox copy: ``old`` must be in the file exactly once, so a change is as small as the model can make it and an invented ``old`` is refused
    rather than guessed at (``locate_edit`` says which small slips of a copy are put right). An empty ``old`` adds ``new`` at the end of an existing file. ``write_file`` makes a new file or
    rewrites one."""
    ws = ctx.ws
    label, full, rel = ws.resolve(path)
    if not rel:
        raise chat_tools.ToolError("Name a file to edit, relative to the project.")
    if credentials.is_kept(rel):
        raise chat_tools.ToolError(chat_tools.KEPT)
    old, new = str(old or ""), str(new if new is not None else "")
    bare_new = without_read_numbers(new)                       # a new text copied out of read_file's output, line numbers and all: the numbers are not code
    numbers_left_out = bare_new is not None
    if bare_new is not None:
        new = bare_new
    if old and old == new:
        raise chat_tools.ToolError("old and new are the same, so nothing would change.")
    if not old and not new:
        raise chat_tools.ToolError("Give the text to add as new, or the exact text to replace as old.")
    try:
        _, content = sandbox_files.download(ws.container_id, full)
    except sandbox_files.FileError as e:
        raise chat_tools.ToolError(f"{e} To create a file use write_file." if not old else str(e)) from None
    if b"\x00" in content[:2048]:
        raise chat_tools.ToolError("This is not a text file.")
    text = content.decode("utf-8", errors="replace")
    how = None
    if not old:
        eol = "\r\n" if "\r\n" in text else "\n"
        joined = text + ("" if not text or text.endswith("\n") else eol) + new.replace("\r\n", "\n").replace("\n", eol)
        line = text.count("\n") + 1
        result_text, removed, added, how = joined, 0, new.count("\n") + 1, "added at the end of the file (old was empty)"
    else:
        spans, put, how = locate_edit(text, old, new)
        if not spans and ctx.v2 and MASK in old:               # the model copied a line with a credential on it as read_file showed it: the file has the real value
            if MASK in new:
                raise chat_tools.ToolError("new contains ****, which stands for a value that is kept from you. Write the code you want without it (for example read the value from an environment variable).")
            spans, put, how = _masked_spans(text, old), new, "**** standing for the value that read_file keeps from you"
        if not spans:
            raise chat_tools.ToolError("That text is not in the file. Read the file again and copy the lines exactly, indentation included (leave the line numbers out).")
        if len(spans) > 1:
            raise chat_tools.ToolError(f"That text is in the file {len(spans)} times. Include a few more lines around it so that it matches only once.")
        start, end = spans[0]
        line = text[:start].count("\n") + 1
        result_text, removed, added = text[:start] + put + text[end:], text[start:end].count("\n") + 1, put.count("\n") + 1
    folder, name = posixpath.split(full)
    try:
        out = sandbox_files.upload(ws.container_id, folder, name, result_text.encode("utf-8"), overwrite=True, uid=agent_sandbox.UID)
    except sandbox_files.FileError as e:
        raise chat_tools.ToolError(str(e)) from None
    shown = ws.shown(label, rel)
    result = {"path": shown, "line": line, "removed_lines": removed, "added_lines": added, "bytes": out["bytes"],
              "summary": f"Edited {shown} at line {line}: {removed} line(s) became {added}" if old else f"Added {added} line(s) to the end of {shown}"}
    if how:
        result["note"] = f"The text was found with {how}." if old else f"Done: {how}."
    if numbers_left_out:
        result["note"] = (result.get("note", "") + " The numbers read_file prints in front of each line are not part of the file; they were left out of the new text. Do not copy them.").strip()
    return diagnostics.add_to(result, ws.container_id, full, shown)


def run_command(ctx: AgentContext, command: str, path: str = "", timeout: int = AGENT_EXEC_TIMEOUT) -> dict:
    """The old name of ``bash`` (a call that still uses it is read as ``bash`` before it gets here; a direct call is the same thing)."""
    return sandbox_tools.bash(ctx, command, workdir=path, timeout=timeout)


def grep(ctx: AgentContext, pattern: str, path: str = "", ignore_case: bool = True, glob: str = "") -> dict:
    return search_code(ctx, pattern, path, ignore_case, glob)


def glob(ctx: AgentContext, pattern: str, path: str = "") -> dict:
    return find_files(ctx, pattern, path)


def diff_so_far(ctx: AgentContext) -> dict:
    """A compact summary of what has changed in the sandbox copy since the session started or its last checkpoint -- ``git diff --stat`` in each root; the human reviews the full
    diff through the session's own ``/diff`` route, not through this tool."""
    ws = ctx.ws
    roots = {}
    for lab, root in (ws.roots.items() if ws.labelled else [("", ws.root_of(""))]):
        res = sandbox.exec_run(ws.container_id, "git diff --stat", workdir=root, timeout=30)
        roots[lab or "."] = res["output"].strip() or "No changes since the last checkpoint."
    return {"roots": roots, "summary": "Read the changes since the last checkpoint in " + (f"{len(roots)} root(s)" if ws.labelled else "the project")}


def checkpoint(ctx: AgentContext, message: str) -> dict:
    """Commit the current state of every root as a checkpoint (a real ``git commit``), so it can be reviewed or rolled back to later."""
    ws = ctx.ws
    message = str(message or "").strip() or "Checkpoint"
    commits = {}
    for lab, root in (ws.roots.items() if ws.labelled else [("", ws.root_of(""))]):
        res = sandbox.exec_run(ws.container_id, f"git add -A && git commit -q -m {shlex.quote(message)} --allow-empty && git rev-parse --short HEAD", workdir=root, timeout=60)
        if res["exit_code"] != 0:
            raise chat_tools.ToolError(f"Could not checkpoint {lab or 'the project'}: {res['output'].strip()[:300]}")
        commits[lab or "."] = res["output"].strip()
    agent_sandbox.touch(ctx.project_id)
    return {"message": message, "commits": commits, "summary": f"Checkpointed as {message!r}"}


def git_status(ctx: AgentContext) -> dict:
    """What has changed in the sandbox copy right now (``git status``, not ``diff_so_far``'s ``git diff --stat``): new, modified and untracked files, per root."""
    ws = ctx.ws
    roots = {}
    for lab, root in (ws.roots.items() if ws.labelled else [("", ws.root_of(""))]):
        res = sandbox.exec_run(ws.container_id, "git status --porcelain=v1 -b", workdir=root, timeout=30)
        roots[lab or "."] = res["output"].strip() or "Nothing changed since the last checkpoint."
    return {"roots": roots, "summary": "Read the working-tree status of " + (f"{len(roots)} root(s)" if ws.labelled else "the project")}


def git_log(ctx: AgentContext, limit: int = 20) -> dict:
    """The sandbox copy's own commit history (the initial copy, then every checkpoint), newest first across every root."""
    ws = ctx.ws
    n = max(1, min(int(limit or 20), 100))
    commits = []
    for lab, root in (ws.roots.items() if ws.labelled else [("", ws.root_of(""))]):
        res = sandbox.exec_run(ws.container_id, f"git log --format=%h%x09%ct%x09%s -n {n}", workdir=root, timeout=30)
        for line in res["output"].splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[1].isdigit():
                commits.append({"root": lab or ".", "hash": parts[0], "at": int(parts[1]), "message": parts[2]})
    commits.sort(key=lambda c: c["at"], reverse=True)
    commits = commits[:n]
    return {"commits": commits, "summary": f"Read the last {len(commits)} commit(s) of the sandbox copy"}


def update_task_list(ctx: AgentContext, tasks: list[dict]) -> dict:
    doc = agent_sessions.set_task_list(ctx.project_id, ctx.session_id, tasks or [])
    return {"task_list": doc["task_list"], "summary": f"Updated the task list: {sum(1 for t in doc['task_list'] if t['done'])} of {len(doc['task_list'])} done"}


def propose_plan(ws, plan: str) -> dict:
    """Plan mode only: hands a finished plan to the owner for review. Trivial and non-destructive -- the real consequence (starting a new Manual or Auto session with the
    plan) happens entirely client-side, once the owner accepts it through /approve; this just records that a plan was proposed. ``ws`` is unused but required by
    chat_tools.call()'s ``fn(ws, **arguments)`` dispatch shape."""
    return {"ok": True, "summary": "Proposed a plan for the owner's review.", "plan": str(plan or "")}


PROPOSE_PLAN_TOOL = {"type": "function", "function": {
    "name": "propose_plan",
    "description": "Hand a finished plan to the owner for review, once you are done investigating and have a concrete plan -- not while you still need to read more with the "
                    "other tools. The owner accepts it (switching this same session into Manual or Auto to carry it out) or asks for changes, which you will see as feedback to act on.",
    "parameters": {"type": "object", "properties": {"plan": {"type": "string", "description": "The full plan: what to change, in which files, and why."}}, "required": ["plan"]}}}


def ask_question(ws, questions: list, answers: list | None = None) -> dict:
    """Plan mode only, like propose_plan: pauses for the owner's answer(s) instead of recording a finished plan. ``answers`` is never filled in by the model -- it arrives only in
    the owner's own ``edited_arguments`` on ``/approve``, lined up with ``questions`` by index."""
    return {"ok": True, "summary": f"Answered {len(answers or [])} of {len(questions or [])} question(s).", "questions": questions or [], "answers": answers or []}


ASK_QUESTION_TOOL = {"type": "function", "function": {
    "name": "ask_question",
    "description": "Pause and ask the owner to choose between real alternatives that only they can decide, before you continue to investigate or propose a plan. Give each question 2 to 4 "
                    "options; the owner can still type their own answer instead of picking one. Never ask for advice or a recommendation you could work out yourself from the project.",
    "parameters": {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "object", "properties": {
        "question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}, "description": "The 2 to 4 alternatives to choose between."}}, "required": ["question"]}}},
        "required": ["questions"]}}}
def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required}}}


WRITE_FILE_TOOL = _tool("write_file", "Create a new text file of the project's copy in the sandbox, or overwrite one completely. To change part of an existing file use edit_file instead.",
                        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])
EDIT_FILE_TOOL = _tool("edit_file", "Replace one piece of text in a file of the project's copy in the sandbox. old must appear in the file exactly once (copy it from read_file, indentation included, "
                       "without the line numbers); new is what replaces it. Leave old empty to add new at the end of the file. Read the file first.", {"path": {"type": "string"}, "old": {"type": "string", "description": "The exact text to replace, found once in the file"},
                                                                          "new": {"type": "string", "description": "The text to put in its place"}}, ["path", "old", "new"])
RUN_COMMAND_TOOL = _tool("run_command", "Run a shell command in the project's copy in the sandbox (no network, a process and memory limit). Long output is shown from both ends, the middle noted as dropped.",
                         {"command": {"type": "string"}, "path": {"type": "string", "description": "Folder to run it in, relative to the project"},
                          "timeout": {"type": "integer", "description": f"Seconds, up to {AGENT_EXEC_MAX}"}}, ["command"])
DIFF_TOOL = _tool("diff_so_far", "A compact summary of what has changed in the project's copy since the session started or its last checkpoint.", {}, [])
CHECKPOINT_TOOL = _tool("checkpoint", "Commit the current state of the project's copy as a checkpoint (a real git commit), to be reviewed or rolled back to later.", {"message": {"type": "string"}}, ["message"])
GIT_STATUS_TOOL = _tool("git_status", "The project's copy's own git status: new, modified and untracked files, per root.", {}, [])
GIT_LOG_TOOL = _tool("git_log", "The project's copy's own commit history (the initial copy, then every checkpoint), newest first.", {"limit": {"type": "integer", "description": "How many commits, up to 100 (default 20)"}}, [])
RESCAN_DEPENDENCIES_TOOL = _tool("rescan_dependencies", "Run a fresh dependency/CVE advisory scan of the real project (not the sandbox copy) and read it. Use it only when the owner asks about dependencies, vulnerabilities or outdated "
                                                        "or insecure packages: a question about how to improve or understand the app is not a request for it. The owner is asked to confirm each time. It reads the "
                                                        "manifests and lock files and matches their versions against known advisories; it never reads the code of a dependency and does not look up newer releases (judge "
                                                        "from your own knowledge whether a version is old).", {}, [])
RESCAN_CODE_TOOL = _tool("rescan_code", "Run a fresh code-pattern security scan of the real project (not the sandbox copy) and read it. Use it when scan_results says there is no scan yet.", {}, [])
UPDATE_TASK_LIST_TOOL = _tool("update_task_list", "Replace the session's task list, shown to the person watching. Make one at the start of any task with several steps and tick items off as you finish them.",
                              {"tasks": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}, "done": {"type": "boolean"}}, "required": ["text"]}}}, ["tasks"])

# Plan mode's own tool list: the read-only tools of a normal chat, the scans and the task list (they change no file of the project), and the two tools that end a Plan turn. Neither
# propose_plan nor ask_question is ever added to chat_tools.TOOLS itself, so a normal chat turn is never offered them.
# search_code and find_files are retired from what Plan mode is offered: grep and glob are the same tools on ripgrep (a call that still uses the old names is read as the new ones), and bash and
# recall are the read-only terminal. The built-in search stays under them for a computer where ripgrep is not installed yet.
_RETIRED = {"search_code": plan_tools.GREP_TOOL, "find_files": plan_tools.GLOB_TOOL}
PLAN_TOOLS = ([_RETIRED.get(t["function"]["name"], t) for t in chat_tools.TOOLS] + [plan_tools.BASH_TOOL, plan_tools.RECALL_TOOL]
              + [RESCAN_DEPENDENCIES_TOOL, RESCAN_CODE_TOOL, UPDATE_TASK_LIST_TOOL, PROPOSE_PLAN_TOOL, ASK_QUESTION_TOOL])
PLAN_EXTRA_IMPLEMENTATIONS = {"propose_plan": propose_plan, "ask_question": ask_question}


def _plan_extra(ctx: AgentContext) -> dict:
    """Plan mode's own implementations, each bound to this call's context: the scan and task tools of Manual and Auto take a context (the project and the session) where a plain chat
    tool takes the workspace, and in Plan mode ``scan_results`` is theirs too, so that "no scan yet" tells the agent to run one instead of telling it to ask the owner."""
    return {**PLAN_EXTRA_IMPLEMENTATIONS,
            "scan_results": lambda ws, kind="code": scan_results(ctx, kind),
            "rescan_dependencies": lambda ws: rescan_dependencies(ctx),
            "rescan_code": lambda ws: rescan_code(ctx),
            "update_task_list": lambda ws, tasks=None: update_task_list(ctx, tasks),
            "bash": lambda ws, command, raw=False: plan_tools.bash(ctx, command, raw),
            "recall": lambda ws, handle, start=1, lines=None, grep=None: plan_tools.recall(ctx, handle, start, lines, grep),
            "websearch": lambda ws, query, count=5: web_tools.websearch(ctx, query, count),
            "webfetch": lambda ws, url: web_tools.webfetch(ctx, url),
            "read_files": lambda ws, paths: codeintel_tools.read_many(lambda p, a, b: chat_tools.read_file(ws, p, a, b), paths),
            "find_symbol": lambda ws, name, kind="all", path="": codeintel_tools.find_symbol_host(ws, name, kind, path),
            "grep": lambda ws, pattern, path="", ignore_case=True, glob="": plan_tools.grep(ctx, pattern, path, ignore_case, glob),
            "glob": lambda ws, pattern, path="": plan_tools.glob(ctx, pattern, path)}


def _named(tool: dict, name: str) -> dict:
    return {"type": "function", "function": {**tool["function"], "name": name}}


_BASE = {t["function"]["name"]: t for t in chat_tools.TOOLS}
# What Manual and Auto are offered: the reads of Plan mode (search and find under the names Plan mode uses), the writes, the terminal and the checkpoint. run_command, git_status, git_log and
# diff_so_far are not offered any more (bash with rtk does what they did; a call that still uses run_command, search_code or find_files is read as bash, grep or glob), but they still work.
TOOLS = [_BASE["list_files"], _BASE["read_file"], _named(_BASE["search_code"], "grep"), _named(_BASE["find_files"], "glob"), _BASE["scan_results"], _BASE["project_graph"],
         WRITE_FILE_TOOL, EDIT_FILE_TOOL, sandbox_tools.APPLY_PATCH_TOOL, sandbox_tools.BASH_TOOL, sandbox_tools.BASH_OUTPUT_TOOL, sandbox_tools.BASH_KILL_TOOL, sandbox_tools.RECALL_TOOL,
         CHECKPOINT_TOOL, RESCAN_DEPENDENCIES_TOOL, RESCAN_CODE_TOOL, UPDATE_TASK_LIST_TOOL]
IMPLEMENTATIONS = {"list_files": list_files, "read_file": read_file, "read_files": read_files, "find_symbol": find_symbol, "search_code": search_code, "find_files": find_files, "grep": grep, "glob": glob, "scan_results": scan_results,
                   "project_graph": project_graph, "write_file": write_file, "edit_file": edit_file, "run_command": run_command, "diff_so_far": diff_so_far, "checkpoint": checkpoint,
                   "git_status": git_status, "git_log": git_log, "rescan_dependencies": rescan_dependencies, "rescan_code": rescan_code, "update_task_list": update_task_list,
                   **sandbox_tools.IMPLEMENTATIONS, **web_tools.IMPLEMENTATIONS}
WEB_TOOLS = web_tools.TOOLS                          # offered on top of a mode's own tools when the session's Web search switch is on
STREAMING = sandbox_tools.STREAMING                  # the tools whose output is given to the page as it arrives


def call(ctx: AgentContext, name: str, arguments: dict) -> dict:
    """Run one tool. Plan mode (``ctx.container_id`` is ``None``) hands to ``chat_tools.call`` with its own implementations (``_plan_extra``) taking precedence; Manual and Auto
    dispatch to the sandbox-backed tools above."""
    if ctx.container_id is None:
        return chat_tools.call(ctx.ws, name, arguments, extra=_plan_extra(ctx))
    if str(name).startswith("mcp__"):                    # a tool of one of the owner's MCP servers, run by the bridge in the sandbox
        from app.backend.pipeline.agentic.mcp import tools as mcp_tools
        return mcp_tools.call(ctx, name, arguments if isinstance(arguments, dict) else {})
    fn = IMPLEMENTATIONS.get(name)
    if fn is None:
        raise chat_tools.ToolError(f"There is no tool called {name}. Use one of: {', '.join(IMPLEMENTATIONS)}.")
    if not isinstance(arguments, dict):
        raise chat_tools.ToolError("The arguments must be an object.")
    try:
        agent_sandbox.touch(ctx.project_id)                 # a call is use: the idle clock restarts, so a long run is not stopped under itself
    except Exception:  # ignored: the clock is a convenience; a call is not refused because it could not be written down
        pass
    try:
        return fn(ctx, **arguments)
    except TypeError as e:
        raise chat_tools.ToolError(f"The arguments do not fit the tool: {e}. {name} takes: {chat_tools.accepted(fn)}.") from None
    except (ValueError, OSError) as e:
        raise chat_tools.ToolError(f"The tool could not do that: {e}.") from None
