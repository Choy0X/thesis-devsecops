"""The read-only tools the project chat may use: list the files of a folder, read part of a file, search the code, and read the project's scan results.

Every tool is scoped to the project's own folders: a path is made real and must stay inside one of them (a link that leaves a folder is refused), dependency and build folders are
not walked by a listing or a search, and what looks like credentials is kept from the assistant by one rule (``is_kept``) that every tool applies to the name that was asked for and
to the file it really is: files named like keys, tokens and environment files, and folders such as ``.git``, ``.ssh`` and ``.aws``. What is returned is masked the same way the code
scan masks its evidence, before it is shortened, so a secret that the project's code holds does not travel to the language model, or to the page. A pattern given to a search is
matched with a time limit, because a pattern written by a model can take for ever on a line. Nothing here writes, runs or opens a network connection. A tool that cannot do what it
was asked raises ``ToolError`` with a sentence, which is handed back to the model as the result, so it can correct itself.
"""
from __future__ import annotations

import inspect
import os
import pathlib
import re
import time

import regex

from app.backend.pipeline import projects
from app.backend.pipeline.agentic import search_args
from app.backend.pipeline.credentials import is_kept, is_secret_dir, plain as _plain
from app.backend.pipeline.scan import code_rules

MAX_FILE_BYTES = 1_000_000
READ_LINES = 200                  # lines returned when the model names none
MAX_READ_LINES = 400
MAX_READ_CHARS = 24_000
MAX_LIST = 200
MAX_MATCHES = 60
MAX_SEARCH_FILES = 20_000
MAX_PATTERN = 300
SEARCH_SECONDS = 15.0
FILE_SECONDS = 1.0                # a pattern matched against one whole file
LINE_SECONDS = 0.05               # one line matched against the model's pattern
LINE_CHARS = 400
FINDINGS_SHOWN = 25
LIST_HINT = 15                    # entries named when a path is wrong, so the next call can be right
TEXT_SUFFIXES = set(projects.LANGUAGES) | {".md", ".json", ".yml", ".yaml", ".toml", ".txt", ".html", ".css", ".scss", ".sh", ".bat", ".ps1", ".sql", ".xml", ".ini", ".cfg", ".gradle",
                                           ".properties", ".vue", ".svelte", ".c", ".h", ".cpp", ".hpp", ".swift", ".lock"}
# A search reads every file that is not known to be binary (a Makefile, a Dockerfile, a dotfile and a notebook are code too); what is left is told apart by looking inside it.
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war", ".class", ".exe", ".dll", ".so", ".dylib",
                   ".o", ".a", ".obj", ".pyc", ".pyo", ".whl", ".parquet", ".feather", ".npy", ".npz", ".pkl", ".pickle", ".bin", ".gguf", ".safetensors", ".onnx", ".pt", ".pth", ".db", ".sqlite",
                   ".mp3", ".mp4", ".mov", ".avi", ".wav", ".ogg", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".psd", ".iso"}
KEPT = "That is kept from the assistant: its name says it holds credentials."


class ToolError(Exception):
    """What went wrong, as a sentence the model can act on."""


class _Skip(ToolError):
    """A file that a search passes over, with the reason (``too_large`` or ``binary``) so that the search can say how many it skipped and why."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


class Workspace:
    """The folders of one project as the assistant sees them: a label each when there are several, and nothing outside them."""

    def __init__(self, project: dict):
        self.project = project
        seen: dict[str, int] = {}
        self.folders: list[tuple[str, str]] = []
        for f in project["folders"]:
            real = os.path.realpath(f["path"])
            base = os.path.basename(real.rstrip("\\/")) or "project"
            seen[base] = seen.get(base, 0) + 1
            self.folders.append((base if seen[base] == 1 else f"{base}-{seen[base]}", real))
        self.roots = dict(self.folders)

    @property
    def labelled(self) -> bool:
        return len(self.folders) > 1

    def root_of(self, label: str) -> str:
        if not self.labelled:
            return self.folders[0][1]
        if label not in self.roots:
            raise ToolError(f"This project has several folders; give a path that starts with one of: {', '.join(self.roots)}.")
        return self.roots[label]

    def resolve(self, path: str | None) -> tuple[str, str, str]:
        """``(label, absolute path, relative path)`` for a path the model gave; ``("", "", "")`` is the top of a project of several folders. Raises ToolError when the path is not
        inside the project, or is one that is kept from the assistant (by the name asked for or by the file it really is)."""
        text = str(path or "").replace("\\", "/").strip()
        if "\x00" in text or re.match(r"^[A-Za-z]:", text) or text.startswith("/"):
            raise ToolError("Give a path relative to the project, for example src/app.py.")
        parts = [p for p in text.split("/") if p and p != "."]
        if ".." in parts:
            raise ToolError("A path cannot go up out of the project.")
        if self.labelled:
            if not parts:
                return "", "", ""
            if parts[0] not in self.roots:
                raise ToolError(f"This project has several folders; start the path with one of: {', '.join(self.roots)}.")
            label, root, parts = parts[0], self.roots[parts[0]], parts[1:]
        else:
            label, root = "", self.folders[0][1]
        full = os.path.realpath(os.path.join(root, *parts)) if parts else root
        try:
            inside = os.path.commonpath([root, full]) == root
        except ValueError:
            inside = False
        if not inside:
            raise ToolError("That path is outside the project.")
        rel = "/".join(parts)
        real_rel = pathlib.PurePath(os.path.relpath(full, root)).as_posix()
        if is_kept(rel) or (real_rel != "." and is_kept(real_rel)):
            raise ToolError(KEPT)
        return label, full, rel

    @staticmethod
    def shown(label: str, rel: str) -> str:
        return "/".join(p for p in (label, rel) if p)


def _skip_dir(name: str) -> bool:
    return name in projects.SKIP_DIRS or (name.startswith(".") and name != ".github") or is_secret_dir(name)


def _is_config(path: str) -> bool:
    """A file that is not code: its lines are literals, so credentials in them are masked whatever they look like."""
    return os.path.splitext(_plain(os.path.basename(path)))[1].lower() not in projects.LANGUAGES


def _lines(path: str) -> list[str]:
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise _Skip(f"This file is larger than {MAX_FILE_BYTES // 1000} kB; search it instead of reading it whole.", "too_large")
    with open(path, "rb") as f:
        data = f.read()
    if b"\x00" in data[:2048]:
        raise _Skip("This is not a text file.", "binary")
    return data.decode("utf-8", errors="replace").splitlines()


def _missing(ws: Workspace, label: str, rel: str, asked: str, what: str = "folder or file") -> str:
    """The sentence for a path that is not there: what was asked for, and what the nearest folder that does exist holds, so that the next call can name something real."""
    root = ws.root_of(label)
    parts = [p for p in rel.split("/") if p]
    while parts and not os.path.exists(os.path.join(root, *parts)):
        parts.pop()
    near = os.path.join(root, *parts) if parts else root
    try:
        names = [n + ("/" if os.path.isdir(os.path.join(near, n)) else "") for n in sorted(os.listdir(near)) if not is_kept(n) and not (os.path.isdir(os.path.join(near, n)) and _skip_dir(n))]
    except OSError:
        names = []
    where = (ws.shown(label, "/".join(parts)) + "/") if parts or label else "The top of the project"
    holds = f" {where} holds: {', '.join(names[:LIST_HINT])}{' ...' if len(names) > LIST_HINT else ''}." if names else ""
    return f"There is no such {what}: {asked!r}.{holds}"


def list_files(ws: Workspace, path: str = "") -> dict:
    label, full, rel = ws.resolve(path)
    if ws.labelled and not full:
        return {"path": "", "entries": [{"name": lab, "type": "folder"} for lab in ws.roots], "summary": f"Listed the {len(ws.roots)} folders of the project"}
    if not os.path.isdir(full):
        if os.path.exists(full):
            raise ToolError("That is a file, not a folder.")
        hint = " list_files takes one folder; to find files by a pattern use find_files." if search_args.has_glob(path) else ""
        raise ToolError(_missing(ws, label, rel, path, "folder") + hint)
    entries, hidden = [], 0
    for name in sorted(os.listdir(full), key=lambda n: (not os.path.isdir(os.path.join(full, n)), n.lower())):
        p = os.path.join(full, name)
        if os.path.isdir(p):
            if is_secret_dir(name):
                entries.append({"name": name, "type": "folder", "kept_from_assistant": True})
            elif _skip_dir(name):
                hidden += 1
            else:
                entries.append({"name": name, "type": "folder"})
        else:
            entries.append({"name": name, "type": "file", "bytes": os.path.getsize(p), **({"kept_from_assistant": True} if is_kept(name) else {})})
    shown = ws.shown(label, rel)
    out = {"path": shown, "entries": entries[:MAX_LIST], "summary": f"Listed {shown or 'the top of the project'}"}
    if len(entries) > MAX_LIST:
        out["truncated"] = f"{len(entries) - MAX_LIST} more entries are not shown"
    if hidden:
        out["not_listed"] = f"{hidden} dependency, build or hidden folders are not listed"
    return out


def read_file(ws: Workspace, path: str, start_line: int = 1, end_line: int | None = None) -> dict:
    label, full, rel = ws.resolve(path)
    if not rel:
        raise ToolError("Name a file to read.")
    if os.path.isdir(full):
        raise ToolError("That is a folder; use list_files.")
    if not os.path.isfile(full):
        raise ToolError(_missing(ws, label, rel, path, "file") + " Use list_files or find_files to find the right path.")
    lines = _lines(full)
    config = _is_config(full)
    total = len(lines)
    start = max(1, int(start_line or 1))
    if start > max(total, 1):
        raise ToolError(f"The file has {total} lines.")
    end = min(total, int(end_line) if end_line else start + READ_LINES - 1, start + MAX_READ_LINES - 1)
    body, chars, last = [], 0, start - 1
    for n in range(start, end + 1):
        text = code_rules.masked(lines[n - 1], LINE_CHARS, config=config)
        if chars + len(text) > MAX_READ_CHARS:
            break
        body.append(f"{n}: {text}")
        chars += len(text)
        last = n
    shown = ws.shown(label, rel)
    out = {"path": shown, "start_line": start, "end_line": last, "total_lines": total, "text": "\n".join(body), "summary": f"Read {shown} lines {start} to {last} of {total}"}
    if last < total:
        out["lines_after"] = total - last
    return out


def _files_under(root: str, rel: str, *, text_only: bool = True, scope: dict | None = None):
    """The files below ``rel`` of ``root`` (or the one file), skipping dependency folders, folders and files kept from the assistant, and links. ``text_only`` also passes over the files whose
    name says they are binary. ``scope`` is filled with what was passed over (``folders``, ``kept``, ``binary``), so that a search can say what it did not look at."""
    scope = scope if scope is not None else {}
    base = os.path.join(root, *rel.split("/")) if rel else root
    if os.path.isfile(base):
        yield base
        return
    stack = [base]
    while stack:                                                           # the files of a folder in name order, then its folders in name order (what a person reads as a listing)
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        subfolders = []
        for e in entries:
            try:
                is_dir, link = e.is_dir(follow_symlinks=False), _is_link(e)
            except OSError:
                continue
            if is_dir:
                if link or _skip_dir(e.name):                              # a link to a folder is never followed: it may lead out of the project
                    scope["folders"] = scope.get("folders", 0) + 1
                else:
                    subfolders.append(e.path)
            elif link or is_kept(e.name):
                scope["kept"] = scope.get("kept", 0) + 1
            elif text_only and os.path.splitext(_plain(e.name))[1].lower() in BINARY_SUFFIXES:
                scope["binary"] = scope.get("binary", 0) + 1
            else:
                yield e.path
        stack.extend(reversed(subfolders))


def _rel(path: str, root: str) -> str:
    """``path`` below ``root`` as a forward-slash path, by slicing (every file the walker yields starts with the folder it was given), not ``os.path.relpath``, which costs a call to the
    file system per file."""
    return path[len(root):].lstrip("\/").replace("\\", "/")


def _is_link(entry: os.DirEntry) -> bool:
    """A symbolic link, or on Windows any reparse point (a junction is one): what the directory listing already says, with no further call to the file system."""
    if entry.is_symlink():
        return True
    return bool(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)


_SKIPPED_WORDS = {"folders": "dependency, build or hidden folders", "kept": "files kept from the assistant", "binary": "binary files", "too_large": "files over 1 MB"}


def _skipped(scope: dict) -> dict:
    return {_SKIPPED_WORDS[k]: v for k, v in scope.items() if v and k in _SKIPPED_WORDS}


def _count_files(targets: list, seconds: float = 2.0, *, text_only: bool = True) -> int:
    """How many files a search would have looked at, counted without reading them (for how much of the project a cut search did not reach)."""
    deadline, n = time.monotonic() + seconds, 0
    for _lab, root, sub in targets:
        for _ in _files_under(root, sub, text_only=text_only):
            n += 1
            if time.monotonic() > deadline:
                return n
    return n


def _targets(ws: Workspace, label: str, full: str, rel: str) -> list:
    return [(lab, root, "") for lab, root in ws.folders] if (ws.labelled and not full) else [(label, ws.root_of(label), rel)]


def _name_filters(path_glob: str, glob: str) -> list:
    """The glob matchers a search must satisfy: the one that was in the path, and the ``glob`` argument. Both are written by a model, so both are matched without a regular expression."""
    return [glob_matcher(g) for g in (path_glob, str(glob or "").strip()) if g]


def _passes(filters: list, fp: str, root: str, sub: str, single: bool = False) -> bool:
    """Whether a file passes every glob: matched against its path from the folder that was searched, and (a model often writes it from the top of the project) from the project folder.
    ``single`` is whether what was searched is one file (its path from itself is its name)."""
    if not filters:
        return True
    top = _rel(fp, root)
    base = os.path.join(root, *sub.split("/")) if sub else root
    from_base = os.path.basename(fp) if single else _rel(fp, base)
    return all(f(from_base) or f(top) for f in filters)


def _is_single_file(root: str, sub: str) -> bool:
    return bool(sub) and os.path.isfile(os.path.join(root, *sub.split("/")))


def search_code(ws: Workspace, pattern: str, path: str = "", ignore_case: bool = True, glob: str = "") -> dict:
    pattern = search_args.fix_regex(pattern)
    if not pattern.strip():
        raise ToolError("Give a pattern to search for.")
    if len(pattern) > MAX_PATTERN:
        raise ToolError(f"Use a pattern shorter than {MAX_PATTERN} characters.")
    flags = regex.I if ignore_case else 0
    try:
        rx, rx_whole = regex.compile(pattern, flags), regex.compile(pattern, flags | regex.M)
    except regex.error as e:
        raise ToolError(f"That is not a valid regular expression: {e}.") from None
    folder, path_glob = search_args.split_path(path)                       # a pattern given as the path is the folder it starts in and a glob
    filters = _name_filters(path_glob, glob)
    label, full, rel = ws.resolve(folder)
    if full and not os.path.exists(full):
        raise ToolError(_missing(ws, label, rel, path))
    targets = _targets(ws, label, full, rel)
    scope: dict = {}
    matches, searched, deadline, cut = [], 0, time.monotonic() + SEARCH_SECONDS, ""
    for lab, root, sub in targets:
        single = _is_single_file(root, sub)
        for fp in _files_under(root, sub, scope=scope):
            if searched >= MAX_SEARCH_FILES or time.monotonic() > deadline:
                cut = "limits"
                break
            if not _passes(filters, fp, root, sub, single):
                continue
            if is_kept(_rel(fp, root)):                                    # links are never walked, so a file's own path says what it is
                scope["kept"] = scope.get("kept", 0) + 1
                continue
            try:
                text = _lines(fp)
            except _Skip as e:
                scope[e.reason] = scope.get(e.reason, 0) + 1
                continue
            except OSError:
                continue
            searched += 1
            try:
                if not rx_whole.search("\n".join(text), timeout=FILE_SECONDS):          # one look at the whole file rules out most files before any line is read
                    continue
            except TimeoutError:
                raise ToolError("That pattern takes too long to match; use a simpler one, or a plain word.") from None
            config = _is_config(fp)
            for n, line in enumerate(text, 1):
                if len(line) > 2000:
                    continue
                try:
                    hit = rx.search(line, timeout=LINE_SECONDS)
                except TimeoutError:
                    raise ToolError("That pattern takes too long to match; use a simpler one, or a plain word.") from None
                if hit:
                    matches.append({"file": ws.shown(lab, _rel(fp, root)), "line": n, "text": code_rules.masked(line.strip(), LINE_CHARS, config=config)})
                    if len(matches) >= MAX_MATCHES:
                        cut = "matches"
                        break
            if cut:
                break
        if cut:
            break
    where = ws.shown(label, rel) or "the project"
    if searched == 0:                                                       # a search that looked at nothing is a mistake to correct, never a result that says "no matches"
        why = ", ".join(f"{n} {what}" for what, n in _skipped(scope).items())
        raise ToolError(f"Nothing was searched: no text file under {where}" + (f" matches {' and '.join(repr(g) for g in (path_glob, glob) if g)}" if filters else "") + (f" (passed over: {why})" if why else "")
                        + ". Check the path with list_files, or search from the top of the project.")
    out = {"pattern": pattern, "files_searched": searched, "matches": matches, "summary": f"Searched {searched} files for {pattern!r}: {len(matches)} matches"}
    skipped = _skipped(scope)
    if skipped:
        out["skipped"] = skipped
    if cut == "matches":
        out["truncated"] = f"Stopped at {MAX_MATCHES} matches; narrow the pattern or the path."
    elif cut:
        total = _count_files(targets)
        out["truncated"] = f"The search stopped at its limits after {searched:,} of about {max(total, searched):,} files; narrow the path or add a glob such as *.py."
    return out


def _segment_matches(pat: str, text: str) -> bool:
    """One path segment against a pattern of ``*`` (any run of characters) and ``?`` (one character), with no regular expression: the usual two-pointer walk that remembers only the last
    ``*``, so the work is never more than the product of the two lengths, however many stars a pattern has."""
    p = t = mark = 0
    star = -1
    while t < len(text):
        if p < len(pat) and pat[p] == "*":
            star, mark = p, t
            p += 1
        elif p < len(pat) and (pat[p] == "?" or pat[p] == text[t]):
            p += 1
            t += 1
        elif star != -1:
            p, mark = star + 1, mark + 1
            t = mark
        else:
            return False
    while p < len(pat) and pat[p] == "*":
        p += 1
    return p == len(pat)


def glob_matcher(pattern: str):
    """A function from a relative file path to whether it matches the pattern: ``*`` is any run of characters inside one folder or file name, ``**`` as a whole segment any number of
    folders (also none), ``?`` one character, ``{a,b}`` either. A pattern without a ``/`` matches the file name in any folder, so ``*.py`` finds every Python file, the way a person expects.
    The pattern comes from the model, so nothing here can backtrack without bound: segments are matched by ``_segment_matches`` and ``**`` by a memoised walk over (pattern segment, path
    segment)."""
    alternatives = [_one_glob(p) for p in search_args.expand_braces(pattern)]
    return lambda name: any(m(name) for m in alternatives)


def _one_glob(pattern: str):
    pat = str(pattern).replace("\\", "/").strip().removeprefix("./").lower()
    if "/" not in pat:                                                     # no folder in it: the file name alone is matched, in any folder
        return lambda name: _segment_matches(pat, name.rsplit("/", 1)[-1].lower())
    segs = [x for x in pat.split("/") if x]

    def matches(name: str) -> bool:
        parts = name.lower().split("/")
        seen: dict[tuple[int, int], bool] = {}

        def walk(i: int, j: int) -> bool:
            if (i, j) not in seen:
                if i == len(segs):
                    seen[(i, j)] = j == len(parts)
                elif segs[i] == "**":
                    seen[(i, j)] = walk(i + 1, j) or (j < len(parts) and walk(i, j + 1))
                else:
                    seen[(i, j)] = j < len(parts) and _segment_matches(segs[i], parts[j]) and walk(i + 1, j + 1)
            return seen[(i, j)]
        return walk(0, 0)
    return matches


def find_files(ws: Workspace, pattern: str, path: str = "") -> dict:
    pattern = str(pattern or "").strip()
    if not pattern:
        raise ToolError("Give a file name or pattern, for example *.py or src/**/test_*.py.")
    if len(pattern) > MAX_PATTERN:
        raise ToolError(f"Use a pattern shorter than {MAX_PATTERN} characters.")
    folder, path_glob = search_args.split_path(path)
    filters = _name_filters(path_glob, pattern)
    label, full, rel = ws.resolve(folder)
    if full and not os.path.exists(full):
        raise ToolError(_missing(ws, label, rel, path))
    targets = _targets(ws, label, full, rel)
    found, cut = [], ""
    for lab, root, sub in targets:
        single = _is_single_file(root, sub)
        for fp in _files_under(root, sub, text_only=False):                  # every file by name, a binary one too
            if _passes(filters, fp, root, sub, single):
                found.append(ws.shown(lab, _rel(fp, root)))
                if len(found) >= MAX_LIST:
                    cut = f"Stopped at {MAX_LIST} files; narrow the pattern or the path."
                    break
        if cut:
            break
    out = {"pattern": pattern, "files": found, "summary": f"Looked for files matching {pattern!r}: {len(found)} found"}
    if cut:
        out["truncated"] = cut
    return out


def scan_results(ws: Workspace, kind: str = "code") -> dict:
    if kind not in ("code", "dependencies"):
        raise ToolError("kind must be code or dependencies.")
    scans = [s for s in projects.scans(ws.project["id"]) if s["kind"] == kind]
    if not scans:
        return {"available": False, "message": f"This project has no {kind} scan yet. The person can run it from the project page.", "summary": f"Looked for a {kind} scan: none yet"}
    r = projects.scan_report(ws.project["id"], scans[0]["scan_id"])
    out = {"available": True, "kind": kind, "scanned_at": r["finished"], "words": r["words"], "counts": r["summary"]}
    if kind == "code":
        out["findings"] = [{k: f[k] for k in ("rule", "severity", "cwe", "summary", "file", "line", "in_tests", "state")} for f in r["findings"][:FINDINGS_SHOWN]]
        out["languages"] = {k: v["coverage"] for k, v in r["languages"].items()}
        more = len(r["findings"]) - FINDINGS_SHOWN
    else:
        rows = [d for d in r["dependencies"] if d["state"] == "vulnerable"]
        out["vulnerable"] = [{"name": d["name"], "version": d["version"], "ecosystem": d["ecosystem"], "severity": d.get("severity"), "advisories": [a["id"] for a in d.get("advisories", [])],
                              "recommendation": d.get("recommendation")} for d in rows[:FINDINGS_SHOWN]]
        out["not_checked"] = {e: v["reason"] for e, v in r["ecosystems"].items() if not v.get("checked")}
        more = len(rows) - FINDINGS_SHOWN
    if more > 0:
        out["truncated"] = f"{more} more are in the report and not shown here"
    out["summary"] = f"Read the newest {kind} scan of {r['finished'][:10]}"
    return out


def project_graph(ws: Workspace, path: str = "") -> dict:
    """The project's structural graph (module, imports, constants, types and functions), already read when the project index was built -- never parses a file on the spot. An
    empty path is an overview of every indexed file; a failure to reach it (the index is not built, the database is unreachable) is reported as a tool failure, not a crashed turn."""
    try:
        from app.backend.pipeline.db import mongo
        from app.backend.pipeline.rag import project_graph as pg
        return pg.tool_result(mongo.database(), ws.project["id"], ws, path)
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(f"The project graph could not be read: {type(e).__name__}: {str(e)[:160]}") from None


TOOLS = [
    {"type": "function", "function": {"name": "list_files", "description": "List the files and folders directly inside a folder of the project. Use an empty path for the top level.",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Folder, relative to the project"}}, "required": []}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read lines of one text file of the project (up to 400 lines at a time). Credentials are masked.",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "search_code", "description": "Search the project's text files for a regular expression; returns file, line and the matching line. path is a folder or one file "
                                                                              "(not a pattern); to limit the search to some files give glob, for example *.py or src/**/*.{ts,tsx}.",
                                      "parameters": {"type": "object", "properties": {"pattern": {"type": "string", "description": "A regular expression, for example \\bpassword\\b|secret"},
                                                                                       "path": {"type": "string", "description": "Optional folder or file to search in"},
                                                                                       "glob": {"type": "string", "description": "Optional file filter, for example *.py or src/**/*.ts"}, "ignore_case": {"type": "boolean"}},
                                                      "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "find_files", "description": "Find files of the project by name or glob pattern (for example *.py, test_*.py, Makefile or src/**/*.{ts,tsx}). Returns their paths.",
                                      "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string", "description": "Optional folder to look in"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "scan_results", "description": "Read the newest scan of the project: the candidates found by the code rules, or the vulnerable dependencies.",
                                      "parameters": {"type": "object", "properties": {"kind": {"type": "string", "enum": ["code", "dependencies"]}}, "required": []}}},
    {"type": "function", "function": {"name": "project_graph", "description": "The project's structural graph: one file's module, imports, constants, types and functions, or an overview of every indexed file when no path is given.",
                                      "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "A file, or empty for an overview of the whole project"}}, "required": []}}},
]
IMPLEMENTATIONS = {"list_files": list_files, "read_file": read_file, "search_code": search_code, "find_files": find_files, "scan_results": scan_results, "project_graph": project_graph}


def call(ws: Workspace, name: str, arguments: dict, *, extra: dict | None = None) -> dict:
    """Run one tool. Returns the result (with a ``summary`` sentence); raises ToolError for a request that cannot be served. ``extra`` is additive, Plan mode's own seam for a
    tool it offers that a normal chat turn never does (agent_tools.PLAN_EXTRA_IMPLEMENTATIONS): checked first, but never changing what a normal chat turn can reach, since
    normal chat never passes one and TOOLS/IMPLEMENTATIONS above stay exactly as they were."""
    fn = (extra or {}).get(name) or IMPLEMENTATIONS.get(name)
    if fn is None:
        raise ToolError(f"There is no tool called {name}. Use one of: {', '.join({**IMPLEMENTATIONS, **(extra or {})})}.")
    if not isinstance(arguments, dict):
        raise ToolError("The arguments must be an object.")
    try:
        return fn(ws, **arguments)
    except TypeError as e:
        raise ToolError(f"The arguments do not fit the tool: {e}. {name} takes: {accepted(fn)}.") from None
    except (ValueError, OSError) as e:
        raise ToolError(f"The tool could not do that: {e}.") from None


def accepted(fn) -> str:
    """The argument names a tool takes (without the workspace or context it is handed first), for the sentence that tells a model which names are right."""
    try:
        return ", ".join(list(inspect.signature(fn).parameters)[1:]) or "no arguments"
    except (TypeError, ValueError):
        return "see the tool's description"
