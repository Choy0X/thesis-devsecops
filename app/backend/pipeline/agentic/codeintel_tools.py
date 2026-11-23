"""``find_symbol`` and ``read_files``: the two tools of code intelligence, and the adapters that find and read the files for them.

* ``find_symbol(name, kind, path)``: where a function, class, variable or method is defined, imported and used, with the function each use is in (``codeintel.py`` does the reading). In Manual and
  Auto it reads the sandbox copy, so it sees what the agent has just changed; in Plan it reads the project on disk, read only.
* ``read_files(paths)``: up to six files (or line ranges, ``path:10-60``) in one call, each read exactly as ``read_file`` reads one, so that every protection of that tool applies. The point is
  rounds: a small model spends a whole model call on each file it reads.

Both are offered only to a session that runs the v2 engine (``EngineTurn``), so that the legacy loop stays what it was for the benchmark.
"""
from __future__ import annotations

import os
import re
import shlex
import time

from app.backend import sandbox, sandbox_files
from app.backend.pipeline import chat_tools, credentials
from app.backend.pipeline.agentic import codeintel, search_args
from app.backend.pipeline.scan import code_rules

MAX_FILES_READ = 40                  # files parsed for one find_symbol
MAX_FILE_BYTES = 400_000
CANDIDATE_LIMIT = 400                # files the sandbox's grep may name
SEARCH_SECONDS = 15.0
READ_FILES_MAX = 6
READ_FILES_LINES = 120               # lines of each file when the call names no range: six whole files would not fit a small model's context
KINDS = ("all", "definition", "import", "use")
_RANGE = re.compile(r"^(?P<path>.+?):(?P<a>\d+)(?:-(?P<b>\d+))?$")


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required}}}


FIND_SYMBOL_TOOL = _tool("find_symbol", "Find where a function, class, variable or method is defined, imported and used, in one call (each use says which function it is in). Use it before you rename or change "
                         "something, instead of several greps and reads. A name like Cart.total looks for total inside the class Cart.",
                         {"name": {"type": "string", "description": "The name, for example load_user or Cart.total"}, "kind": {"type": "string", "enum": list(KINDS)},
                          "path": {"type": "string", "description": "Optional folder or file to look in"}}, ["name"])
READ_FILES_TOOL = _tool("read_files", f"Read up to {READ_FILES_MAX} files in one call. Each entry is a path, or path:first-last for some lines (for example src/cart.py or src/cart.py:10-60).",
                        {"paths": {"type": "array", "items": {"type": "string"}}}, ["paths"])
TOOLS = [FIND_SYMBOL_TOOL, READ_FILES_TOOL]
NAMES = tuple(t["function"]["name"] for t in TOOLS)


# -- read_files ------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def parse_entry(entry) -> tuple[str, int, int | None]:
    """``(path, first line, last line or None)`` of an entry of ``read_files``."""
    text = str(entry or "").strip().strip("`'\"")
    if not text:
        raise chat_tools.ToolError("An entry is empty; give a path such as src/app.py or src/app.py:10-60.")
    m = _RANGE.match(text)
    if m and not re.match(r"^[A-Za-z]:[\\/]", text):
        a, b = int(m.group("a")), (int(m.group("b")) if m.group("b") else None)
        if b is not None and b < a:
            a, b = b, a
        return m.group("path"), max(1, a), b
    return text, 1, None


def read_many(read_one, paths) -> dict:
    """The result of ``read_files``: ``read_one(path, start_line, end_line)`` is the mode's own ``read_file``; an entry that fails is an error in its place, not the end of the call."""
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not paths:
        raise chat_tools.ToolError("Give paths: a list of up to 6 files, for example [\"src/app.py\", \"src/util.py:10-60\"].")
    entries, extra = paths[:READ_FILES_MAX], max(0, len(paths) - READ_FILES_MAX)
    files, failed = [], 0
    for entry in entries:
        try:
            path, start, end = parse_entry(entry)
            files.append(read_one(path, start, end if end is not None else start + READ_FILES_LINES - 1))
        except chat_tools.ToolError as e:
            failed += 1
            files.append({"path": str(entry), "error": str(e)})
        except (ValueError, OSError) as e:
            failed += 1
            files.append({"path": str(entry), "error": f"The file could not be read: {e}"})
    if failed == len(files):
        raise chat_tools.ToolError("None of the files could be read: " + "; ".join(f"{f['path']}: {f['error']}" for f in files)[:600])
    out = {"files": [{k: v for k, v in f.items() if k != "summary"} for f in files], "summary": f"Read {len(files) - failed} of {len(files)} file(s): " + ", ".join(str(f.get('path')) for f in files)[:200]}
    if extra:
        out["not_read"] = f"{extra} more path(s) were not read; at most {READ_FILES_MAX} at a time. Ask for them in the next call."
    return out


# -- find_symbol -----------------------------------------------------------------------------------------------------------------------------------------------------------------------

def _mask(files: list[codeintel.FileHits]) -> None:
    for fh in files:
        for h in fh.hits:
            h.text = code_rules.masked(h.text, chat_tools.LINE_CHARS)


def _skip_path(rel: str) -> bool:
    parts = rel.split("/")
    return any(chat_tools._skip_dir(p) for p in parts[:-1]) or credentials.is_kept(rel)


def _check(name: str, kind: str) -> tuple[str, str, str]:
    try:
        owner, last = codeintel.split_name(name)
    except ValueError as e:
        raise chat_tools.ToolError(str(e)) from None
    kind = str(kind or "all").lower()
    if kind not in KINDS:
        raise chat_tools.ToolError(f"kind must be one of {', '.join(KINDS)}.")
    return owner, last, kind


def _finish(files: list[codeintel.FileHits], name: str, kind: str, looked: int, cut: str, roots: int) -> dict:
    _mask(files)
    out = codeintel.collect(files, name, kind)
    out["files_read"] = looked
    if cut:
        out["truncated"] = (out.get("truncated", "") + " " + cut).strip()
    out["summary"] += f" ({looked} file(s) read in {roots} root(s))"
    return out


def _small_enough(container_id: str, root: str, names: list[str]) -> list[str]:
    """``names`` without the files over ``MAX_FILE_BYTES``, by their size in the container (a file is not downloaded to find out it is too big). If the sizes cannot be read, every name stays."""
    if not names:
        return names
    command = "stat -c '%s %n' -- " + " ".join(shlex.quote(n) for n in names)
    try:
        res = sandbox.exec_run(container_id, command, workdir=root, timeout=60)
    except Exception:  # ignored: without sizes the size is checked after the download, as before
        return names
    sizes = {}
    for line in (res.get("output") or "").splitlines():
        size, _, name = line.partition(" ")
        if size.isdigit() and name:
            sizes[name] = int(size)
    if not sizes:
        return names
    return [n for n in names if sizes.get(n, 0) <= MAX_FILE_BYTES]


def find_symbol_sandbox(ctx, name: str, kind: str = "all", path: str = "") -> dict:
    """``find_symbol`` on the sandbox copy: the files that hold the name are named by ``grep``, read from the container and parsed here."""
    owner, last, kind = _check(name, kind)
    ws = ctx.ws
    from app.backend.pipeline import agent_tools
    folder, path_glob = search_args.split_path(path)
    label, full, rel = ws.resolve(folder)
    targets = [(lab, root, "") for lab, root in ws.roots.items()] if (ws.labelled and not full) else [(label, ws.root_of(label), rel)]
    files: list[codeintel.FileHits] = []
    looked, cut, deadline = 0, "", time.monotonic() + SEARCH_SECONDS
    for lab, root, sub in targets:
        cmd = f"grep -rlwIF --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=__pycache__ -e {shlex.quote(last)} -- {shlex.quote(sub or '.')}"
        try:
            res = sandbox.exec_run(ws.container_id, cmd, workdir=root, timeout=agent_tools.AGENT_EXEC_TIMEOUT)
        except Exception as e:
            raise chat_tools.ToolError(f"The search could not run: {type(e).__name__}: {str(e)[:200]}.") from None
        listed = [line[2:] if line.startswith("./") else line for line in res["output"].splitlines() if line.strip()]
        if res["exit_code"] not in (0, 1) and not listed:                      # grep exits 2 when it could not read some file: what it did list is still the answer
            raise chat_tools.ToolError(f"grep could not search there: {res['output'].strip().splitlines()[-1][:300] if res['output'].strip() else 'unknown error'}")
        names = [n for n in sorted(set(listed)) if not _skip_path(n)]               # dependency folders and kept files are left out BEFORE the list is cut
        if path_glob:
            matcher = chat_tools.glob_matcher(path_glob)
            names = [n for n in names if matcher(n.rsplit("/", 1)[-1]) or matcher(n)]
        names = _small_enough(ws.container_id, root, names[:CANDIDATE_LIMIT])
        # source files first, tests after: the definition is what a rename starts from
        names.sort(key=lambda n: (bool(re.search(r"(^|/)(tests?|spec)(/|_)|_test\.|\.test\.|\.spec\.", n)), n))
        for n in names:
            if looked >= MAX_FILES_READ or time.monotonic() > deadline:
                cut = f"Stopped after {looked} files; give a path to look in fewer."
                break
            try:
                _, content = sandbox_files.download(ws.container_id, f"{root.rstrip('/')}/{n}")
            except sandbox_files.FileError:
                continue
            looked += 1
            if len(content) > MAX_FILE_BYTES or b"\x00" in content[:2048]:
                continue
            found = codeintel.analyze(content.decode("utf-8", errors="replace"), ws.shown(lab, n), last, owner)
            if found:
                files.append(found)
        if cut:
            break
    return _finish(files, name, kind, looked, cut, len(targets))


def find_symbol_host(ws, name: str, kind: str = "all", path: str = "") -> dict:
    """``find_symbol`` on the project on disk (Plan mode): read only, through the same walker ``search_code`` uses."""
    owner, last, kind = _check(name, kind)
    folder, path_glob = search_args.split_path(path)
    label, full, rel = ws.resolve(folder)
    targets = chat_tools._targets(ws, label, full, rel)
    matcher = chat_tools.glob_matcher(path_glob) if path_glob else None
    files: list[codeintel.FileHits] = []
    looked, cut, deadline, seen = 0, "", time.monotonic() + SEARCH_SECONDS, 0
    for lab, root, sub in targets:
        for fp in chat_tools._files_under(root, sub, text_only=True):
            seen += 1
            if looked >= MAX_FILES_READ or seen > chat_tools.MAX_SEARCH_FILES or time.monotonic() > deadline:
                cut = "Stopped early; give a path to look in fewer files."
                break
            rel_path = chat_tools._rel(fp, root)
            if matcher and not (matcher(rel_path.rsplit("/", 1)[-1]) or matcher(rel_path)):
                continue
            try:
                if os.path.getsize(fp) > MAX_FILE_BYTES:
                    continue
                with open(fp, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            if b"\x00" in data[:2048]:
                continue
            text = data.decode("utf-8", errors="replace")
            if not codeintel.word_in(text, last):
                continue
            looked += 1
            found = codeintel.analyze(text, ws.shown(lab, rel_path), last, owner)
            if found:
                files.append(found)
        if cut:
            break
    return _finish(files, name, kind, looked, cut, len(targets))
