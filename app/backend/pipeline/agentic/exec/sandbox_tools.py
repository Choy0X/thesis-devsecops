"""The terminal tools of Manual and Auto: ``bash`` (rtk in front, output as it arrives, background jobs), ``bash_output``, ``bash_kill``, ``recall`` and ``apply_patch``, over the sandbox terminal of
``sandbox_exec.py``. They run in the project's own copy in the sandbox container; nothing here reaches the real project (``agent_apply`` is the only way a change gets there, and the owner
starts it).
"""
from __future__ import annotations

import posixpath
import re
import shlex

from app.backend import sandbox, sandbox_files
from app.backend.pipeline import chat_tools, credentials
from app.backend.pipeline.agentic import diagnostics
from app.backend.pipeline.agentic import outputs
from app.backend.pipeline.agentic.exec import sandbox_exec as se
from app.backend.pipeline.scan import code_rules

MAX_PATCH = 400_000


def executor(ctx) -> se.SandboxExecutor:
    return se.SandboxExecutor(ctx.ws.container_id, [ctx.ws.root_of(label) for label in (ctx.ws.roots if ctx.ws.labelled else [""])], project_id=ctx.project_id, session_id=ctx.session_id)


def bash(ctx, command: str, workdir: str = "", timeout: int = se.DEFAULT_TIMEOUT, background: bool = False, raw: bool = False) -> dict:
    """Run a command in the project's copy. In the foreground the output comes back whole (and, while it runs, in pieces through ``ctx.on_output``); in the background the command is started
    detached and its job id comes back at once."""
    try:
        ex = executor(ctx)
        folder = ""
        if workdir:
            _, full, _ = ctx.ws.resolve(workdir)
            folder = full
        if background:
            return ex.start_job(command, workdir=folder or None, raw=bool(raw))
        return ex.run(command, workdir=folder or None, timeout=int(timeout or se.DEFAULT_TIMEOUT), raw=bool(raw), on_output=getattr(ctx, "on_output", None), cancel=getattr(ctx, "cancel", None))
    except se.SandboxExecError as e:
        raise chat_tools.ToolError(str(e)) from None


def bash_output(ctx, job: str, start: int = 1, lines: int = 400, grep: str | None = None) -> dict:
    """The log of a command that was started in the background, from a line, and whether it is still running."""
    try:
        return executor(ctx).job_output(job, start, lines, grep or None)
    except se.SandboxExecError as e:
        raise chat_tools.ToolError(str(e)) from None


def bash_kill(ctx, job: str) -> dict:
    """End a background command and everything it started."""
    try:
        return executor(ctx).job_kill(job)
    except se.SandboxExecError as e:
        raise chat_tools.ToolError(str(e)) from None


def recall(ctx, handle: str, start: int = 1, lines: int | None = None, grep: str | None = None) -> dict:
    """Output that was cut (``out_...``) or that rtk left out (a hex code), from a line, up to a number of lines, or only the lines that match."""
    handle = str(handle or "").strip().strip("[]")
    try:
        if handle.startswith(outputs.PREFIX):
            got = outputs.load(ctx.project_id, ctx.session_id, handle, start=int(start or 1), lines=int(lines) if lines else None, grep=grep or None)
            return {**got, "summary": f"Read lines {got['from']} to {got['to']} of {got['total_lines']} of {handle}" + (f"; {got['more']} more" if got["more"] else "")}
        text = executor(ctx).recall_rtk(handle, start=int(start) if start else None, lines=int(lines) if lines else None, grep=grep or None)
    except (outputs.OutputError, se.SandboxExecError, ValueError) as e:
        raise chat_tools.ToolError(str(e)) from None
    cut = len(text) > 24_000
    text = text[:24_000] if cut else text
    out = {"handle": handle, "text": text, "summary": f"Read {text.count(chr(10)) + (1 if text and not text.endswith(chr(10)) else 0)} lines of {handle}"}
    if cut:
        out["truncated"] = "Cut at 24,000 characters; ask for a later part with start, a number of lines with lines, or only some lines with grep."
    return out


_DIFF_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.M)


def apply_patch(ctx, patch: str, root: str = "") -> dict:
    """Apply a unified diff (the kind ``git diff`` writes) to the project's copy with ``git apply``: all of it or none of it. It is checked first, so a patch that does not fit changes nothing and
    the answer says where it does not."""
    ws = ctx.ws
    patch = str(patch or "")
    if not patch.strip():
        raise chat_tools.ToolError("Give the patch, as a unified diff (diff --git a/file b/file, ---, +++, @@ ...).")
    if len(patch) > MAX_PATCH or "\x00" in patch:
        raise chat_tools.ToolError(f"A patch is at most {MAX_PATCH:,} characters, and is text.")
    for m in _DIFF_FILE.finditer(patch):
        for name in m.groups():
            if name.startswith("/") or ".." in name.split("/"):
                raise chat_tools.ToolError(f"The patch names {name}, which is outside the project.")
            if credentials.is_kept(name):
                raise chat_tools.ToolError(f"The patch names {name}. {chat_tools.KEPT}")
            if ".git" in name.split("/"):
                raise chat_tools.ToolError("A patch cannot change files inside .git.")
    label, full, _ = ws.resolve(root)
    folder_root = full or ws.root_of("")
    if not patch.endswith("\n"):
        patch += "\n"
    path = f"{se.STATE}/patches"
    name = "p" + re.sub(r"\W", "", str(abs(hash(patch))))[:12] + ".patch"
    try:
        sandbox.exec_run(ws.container_id, f"mkdir -p {path}", timeout=15)
        sandbox_files.upload(ws.container_id, path, name, patch.encode("utf-8"), overwrite=True, uid=se.UID)
    except sandbox_files.FileError as e:
        raise chat_tools.ToolError(str(e)) from None
    target = f"{path}/{name}"
    stat = sandbox.exec_run(ws.container_id, f"git apply --check --verbose {shlex.quote(target)}", workdir=folder_root, timeout=60)
    if stat["exit_code"] != 0:
        raise chat_tools.ToolError("The patch does not fit and nothing was changed: " + " ".join(code_rules.mask(stat["output"], config=True).split())[:600])
    done = sandbox.exec_run(ws.container_id, f"git apply --whitespace=nowarn --numstat --summary {shlex.quote(target)} && git apply --whitespace=nowarn {shlex.quote(target)}", workdir=folder_root, timeout=60)
    sandbox.exec_run(ws.container_id, f"rm -f {shlex.quote(target)}", timeout=15)
    if done["exit_code"] != 0:
        raise chat_tools.ToolError("The patch could not be applied: " + " ".join(done["output"].split())[:600])
    files = []
    for line in done["output"].splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            add, rem, fname = parts
            files.append({"path": ws.shown(label, fname), "added": int(add) if add.isdigit() else None, "removed": int(rem) if rem.isdigit() else None})
    from app.backend.pipeline.scan import agent_sandbox
    agent_sandbox.touch(ctx.project_id)
    result = {"files": files, "summary": f"Applied a patch to {len(files)} file{'s' if len(files) != 1 else ''}: " + ", ".join(f["path"] for f in files[:6]) + ("..." if len(files) > 6 else "")}
    found = []
    for line in done["output"].splitlines():                       # a quick look at whether each file (the first few) still parses; the sandbox's own output names them
        parts = line.split("	")
        if len(parts) == 3 and len(found) < diagnostics.MAX_PATCH_FILES:
            problems = diagnostics.check(ws.container_id, posixpath.join(folder_root, parts[2]))
            if problems:
                found += [{"file": ws.shown(label, parts[2]), **p} for p in problems[:2]]
    if found:
        result["problems"] = found
        result["summary"] += f"; but {len({f['file'] for f in found})} of them do not parse"
        result["note"] = "A file the patch changed does not parse: " + "; ".join(f"{f['file']}{(' line ' + str(f['line'])) if f.get('line') else ''}: {f['message']}" for f in found[:3]) + ". Fix it before going on."
    return result


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required}}}


BASH_TOOL = _tool("bash", "Run a command in the project's copy in the sandbox (a closed container: no network, a process and memory limit) and read what it prints. Where the last command ended is where the next "
                          "starts (a cd and an export last). rtk condenses the output of git, tests, builds and listings; where something was left out the end of the output says so, and recall(handle) gets it back, "
                          "or run the command again with raw set to true. A command that keeps running (a server, a watcher) is started with background set to true: you get a job id, read it with "
                          "bash_output and end it with bash_kill.",
                  {"command": {"type": "string"}, "workdir": {"type": "string", "description": "Folder to run it in, relative to the project (default: where the last command ended)"},
                   "timeout": {"type": "integer", "description": f"Seconds, up to {se.MAX_TIMEOUT} (default {se.DEFAULT_TIMEOUT})"},
                   "background": {"type": "boolean", "description": "true: start it detached and return a job id"}, "raw": {"type": "boolean", "description": "true: nothing is left out by rtk"}}, ["command"])
BASH_OUTPUT_TOOL = _tool("bash_output", "Read the log of a command started with background true, from a line (default the first), and whether it is still running or how it finished.",
                         {"job": {"type": "string", "description": "The job id bash returned"}, "start": {"type": "integer"}, "lines": {"type": "integer", "description": "At most 400"},
                          "grep": {"type": "string", "description": "A regular expression: keep only the matching lines"}}, ["job"])
BASH_KILL_TOOL = _tool("bash_kill", "End a command started with background true, and everything it started.", {"job": {"type": "string", "description": "The job id bash returned"}}, ["job"])
RECALL_TOOL = _tool("recall", "Get back output that was cut or left out: give the handle printed after the word recall (out_ and twelve hex digits, or a short hex code). start is the first line, lines how many "
                              "(at most 400), grep keeps only the lines that match a regular expression.",
                    {"handle": {"type": "string"}, "start": {"type": "integer", "description": "First line to return, 1 is the first"}, "lines": {"type": "integer"},
                     "grep": {"type": "string", "description": "A regular expression: keep only the matching lines"}}, ["handle"])
APPLY_PATCH_TOOL = _tool("apply_patch", "Apply a unified diff (the kind git diff writes, with a diff --git a/file b/file line for each file) to the project's copy: all of it or none of it, checked first. "
                                        "Use it for a change that touches several places or files; edit_file is for one exact piece of text.",
                         {"patch": {"type": "string", "description": "The unified diff"}, "root": {"type": "string", "description": "For a project of several folders: which one the paths are relative to"}}, ["patch"])

IMPLEMENTATIONS = {"bash": bash, "bash_output": bash_output, "bash_kill": bash_kill, "recall": recall, "apply_patch": apply_patch}
STREAMING = {"bash"}
