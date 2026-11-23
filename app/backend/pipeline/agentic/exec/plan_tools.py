"""The terminal tools of Plan mode: ``bash``, ``recall``, ``grep`` and ``glob``, over the read-only terminal of ``host_ro.py``.

``bash`` runs one command line (``rg``, ``git log``, ``cat``, a pipe of them) in the real project, read-only, and returns its output. ``recall`` gives back output that was cut or that rtk left
out. ``grep`` and ``glob`` are the structured form of searching and finding files (ripgrep behind them, the results in the shape the page and the model already know), and fall back to the
built-in search when ripgrep is not installed, so Plan mode works before the command tools are.
"""
from __future__ import annotations

import json
import os

from app.backend.pipeline import chat_tools
from app.backend.pipeline.agentic import outputs, search_args
from app.backend.pipeline.agentic.exec import builtins, host_ro, rtk, toolchain
from app.backend.pipeline.scan import code_rules

MAX_MATCHES = chat_tools.MAX_MATCHES
MAX_FILES = chat_tools.MAX_LIST
RECALL_CHARS = 24_000


def terminal(ctx) -> host_ro.HostReadOnly:
    return host_ro.HostReadOnly(ctx.ws, store=(ctx.project_id, ctx.session_id))


def bash(ctx, command: str, raw: bool = False) -> dict:
    """One command line, read-only. ``raw`` runs git and rg as they are, with nothing left out by rtk."""
    try:
        return terminal(ctx).run(command, raw=bool(raw))
    except builtins.Usage as e:
        raise chat_tools.ToolError(str(e)) from None


def recall(ctx, handle: str, start: int = 1, lines: int | None = None, grep: str | None = None) -> dict:
    """Output that was cut (a handle that starts with ``out_``) or that rtk left out (a hex code), from a line, up to a number of lines, or only the lines that match a regular expression."""
    handle = str(handle or "").strip().strip("[]")
    try:
        if handle.startswith(outputs.PREFIX):
            got = outputs.load(ctx.project_id, ctx.session_id, handle, start=int(start or 1), lines=int(lines) if lines else None, grep=grep or None)
            return {**got, "summary": f"Read lines {got['from']} to {got['to']} of {got['total_lines']} of {handle}" + (f"; {got['more']} more" if got["more"] else "")}
        term = terminal(ctx)
        if term.rtk is None:
            raise chat_tools.ToolError("rtk is not installed, so that handle cannot be read. Run the command again with raw set to true.")
        text = code_rules.mask(rtk.recall(term.rtk, handle, term.env, full=True, start=int(start) if start and int(start) > 1 else None, lines=int(lines) if lines else None, grep=grep or None))
    except (outputs.OutputError, toolchain.ToolchainError, ValueError) as e:
        raise chat_tools.ToolError(str(e)) from None
    cut = len(text) > RECALL_CHARS
    text = text[:RECALL_CHARS] if cut else text
    rows = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    out = {"handle": handle, "text": text, "summary": f"Read {rows} lines of {handle}"}
    if cut:
        out["truncated"] = f"Cut at {RECALL_CHARS:,} characters; ask for a later part with start, a number of lines with lines, or only some lines with grep."
    return out


def grep(ctx, pattern: str, path: str = "", ignore_case: bool = True, glob: str = "") -> dict:
    """Search the project's text files for a regular expression with ripgrep: ``{"pattern", "files_searched", "matches": [{"file", "line", "text"}], "summary"}``, the same as the built-in search."""
    term = terminal(ctx)
    if term.rg is None or ctx.ws.labelled:                                  # (a project of several folders is searched by the built-in search, which labels each folder)
        out = chat_tools.search_code(ctx.ws, pattern, path, ignore_case, glob)
        if term.rg is None:
            out["note"] = "ripgrep is not installed, so the built-in search was used (Settings, Engines, Command tools)."
        return out
    pattern = search_args.fix_regex(pattern)
    if not pattern.strip():
        raise chat_tools.ToolError("Give a pattern to search for.")
    if len(pattern) > chat_tools.MAX_PATTERN:
        raise chat_tools.ToolError(f"Use a pattern shorter than {chat_tools.MAX_PATTERN} characters.")
    folder, path_glob = search_args.split_path(path)
    label, full, rel = ctx.ws.resolve(folder)
    if full and not os.path.exists(full):
        raise chat_tools.ToolError(chat_tools._missing(ctx.ws, label, rel, path))
    args = ["--json", "-e", pattern] + (["-i"] if ignore_case else ["-s"])
    for g in (path_glob, glob):
        if g:
            args += ["-g", g]
    args += ["--", folder] if folder else []
    try:
        res = term.rg_json(args)
    except builtins.Usage as e:
        raise chat_tools.ToolError(str(e)) from None
    matches, searched, cut, errors = [], 0, False, []
    for line in res.text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind, data = event.get("type"), event.get("data", {})
        if kind == "match" and not cut:
            if len(matches) >= MAX_MATCHES:
                cut = True
                continue
            name = (data.get("path") or {}).get("text", "").replace("\\", "/")
            text = (data.get("lines") or {}).get("text", "").strip()
            shown = ctx.ws.shown(label if ctx.ws.labelled and not name.startswith(label + "/") else "", name.removeprefix("./"))
            matches.append({"file": shown, "line": data.get("line_number"), "text": code_rules.masked(text, chat_tools.LINE_CHARS, config=chat_tools._is_config(name))})
        elif kind == "summary":
            searched = int((data.get("stats") or {}).get("searches", 0))
    if res.code == 2 and not matches and "regex parse error" in res.err.lower():
        raise chat_tools.ToolError("That is not a valid regular expression: " + " ".join(res.err.split())[:240])
    where = ctx.ws.shown(label, rel) or "the project"
    if searched == 0:
        raise chat_tools.ToolError(f"Nothing was searched: no text file under {where}" + (f" matches {' and '.join(repr(g) for g in (path_glob, glob) if g)}" if (path_glob or glob) else "")
                                   + ". Check the path with list_files, or search from the top of the project.")
    out = {"pattern": pattern, "files_searched": searched, "matches": matches, "engine": "ripgrep", "summary": f"Searched {searched} files for {pattern!r}: {len(matches)} matches"}
    if cut:
        out["truncated"] = f"Stopped at {MAX_MATCHES} matches; narrow the pattern or the path."
    return out


def glob(ctx, pattern: str, path: str = "") -> dict:
    """Find files by name or glob with ripgrep: ``{"pattern", "files", "summary"}``."""
    term = terminal(ctx)
    if term.rg is None or ctx.ws.labelled:
        out = chat_tools.find_files(ctx.ws, pattern, path)
        if term.rg is None:
            out["note"] = "ripgrep is not installed, so the built-in file search was used (Settings, Engines, Command tools)."
        return out
    pattern = str(pattern or "").strip()
    if not pattern:
        raise chat_tools.ToolError("Give a file name or pattern, for example *.py or src/**/test_*.py.")
    if len(pattern) > chat_tools.MAX_PATTERN:
        raise chat_tools.ToolError(f"Use a pattern shorter than {chat_tools.MAX_PATTERN} characters.")
    folder, path_glob = search_args.split_path(path)
    label, full, rel = ctx.ws.resolve(folder)
    if full and not os.path.exists(full):
        raise chat_tools.ToolError(chat_tools._missing(ctx.ws, label, rel, path))
    name = pattern if ("/" in pattern or "*" in pattern or "?" in pattern or "[" in pattern or "{" in pattern) else f"**/{pattern}"          # a bare name is a file of that name anywhere
    args = ["--files", "--glob", name] + (["--glob", path_glob] if path_glob else []) + (["--", folder] if folder else [])
    try:
        res = term.rg_json(args)
    except builtins.Usage as e:
        raise chat_tools.ToolError(str(e)) from None
    files = [line.strip().replace("\\", "/").removeprefix("./") for line in res.text.splitlines() if line.strip()]
    files = [ctx.ws.shown(label if ctx.ws.labelled and not f.startswith(label + "/") else "", f) for f in files]
    cut = len(files) > MAX_FILES
    out = {"pattern": pattern, "files": sorted(files)[:MAX_FILES], "engine": "ripgrep", "summary": f"Looked for files matching {pattern!r}: {min(len(files), MAX_FILES)} found"}
    if cut:
        out["truncated"] = f"Stopped at {MAX_FILES} files; narrow the pattern or the path."
    return out


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required}}}


BASH_TOOL = _tool("bash", "Run a read-only command in the project and read its output: rg (search), git status, diff, log, show and blame, cat, head, tail, ls, find, tree, wc, sort, uniq and diff, "
                          "joined with | && || or ;. Paths are relative to the project. It cannot write, install (npm, yarn, pnpm, pip), create a file, run another program or leave the project: a command that "
                          "needs one of those is for the owner, so name it in your answer. The output of git and rg is condensed; where something "
                          "was left out the end of the output says so, and recall(handle) gets it back, or run the command again with raw set to true.",
                  {"command": {"type": "string", "description": "One command line, for example: git log --oneline -20  or  rg -n \"def main\" src | head -20"},
                   "raw": {"type": "boolean", "description": "true: run git and rg as they are, with nothing left out"}}, ["command"])
RECALL_TOOL = _tool("recall", "Get back output that was cut or left out: give the handle printed after the word recall (out_ and twelve hex digits, or a short hex code). start is the first line, lines how many "
                              "(at most 400), grep keeps only the lines that match a regular expression.",
                    {"handle": {"type": "string"}, "start": {"type": "integer", "description": "First line to return, 1 is the first"}, "lines": {"type": "integer"},
                     "grep": {"type": "string", "description": "A regular expression: keep only the matching lines"}}, ["handle"])
GREP_TOOL = _tool("grep", "Search the project's text files for a regular expression with ripgrep; returns file, line and the matching line. path is a folder or one file (not a pattern); to limit the search "
                          "to some files give glob, for example *.py or src/**/*.{ts,tsx}. It skips dependency folders and files of credentials.",
                  {"pattern": {"type": "string", "description": "A regular expression, for example \\bpassword\\b|secret"}, "path": {"type": "string", "description": "Optional folder or file to search in"},
                   "glob": {"type": "string", "description": "Optional file filter, for example *.py or src/**/*.ts"}, "ignore_case": {"type": "boolean"}}, ["pattern"])
GLOB_TOOL = _tool("glob", "Find files of the project by name or glob pattern (for example *.py, test_*.py, Makefile or src/**/*.{ts,tsx}) with ripgrep. Returns their paths.",
                  {"pattern": {"type": "string"}, "path": {"type": "string", "description": "Optional folder to look in"}}, ["pattern"])

IMPLEMENTATIONS = {"bash": bash, "recall": recall, "grep": grep, "glob": glob}
