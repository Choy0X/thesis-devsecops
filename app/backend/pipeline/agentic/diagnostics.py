"""After a file is written or edited in the sandbox copy, a quick look at whether it still parses, so that a model's slip (a missing bracket, a stray line number, a cut-off file) comes back with the
result of the call it was made in, and not three rounds later when a test fails for it.

Only syntax, with what the sandbox image already has and nothing to install: Python through its own parser (no ``.pyc`` is written), JSON, TOML, shell (``bash -n``), and JavaScript and TypeScript through
the ESLint of the scan toolkit, of which only a file that does not parse is reported (its security rules are the scan's business, not an edit's). A type checker or a linter would need the project's own packages
and settings; it is left to the agent's own commands. A check that cannot run (a program missing, too slow) says nothing: no news is not reported as good news, and a missing check is never an error of the edit.

A check must not run the project's code: it is started with Python's isolated mode (no working folder on the module path, no PYTHON variable, no user site), from the root of the container and not from the
folder that holds the project, with no shell start-up files; a project folder that happens to be called ``json`` or ``ast`` and holds an ``__init__.py`` is therefore never imported in place of the language's own.
"""
from __future__ import annotations

import json
import posixpath
import re
import shlex

from app.backend import sandbox
from app.backend.pipeline.scan import code_rules

TIMEOUT = 20
NEUTRAL = "/"                      # where a check runs: not in the project's folder or its parent, where a file or folder of the project's own could be found before the language's own modules
MAX_PROBLEMS = 5
MAX_PATCH_FILES = 3                # of the files a patch changed, how many are looked at
MESSAGE_CHARS = 220
_PY = "import ast,sys; p=sys.argv[-1]; ast.parse(open(p,'rb').read(), p)"
_JSON = "import json,sys; json.load(open(sys.argv[-1], encoding='utf-8'))"
_TOML = "import tomllib,sys; tomllib.load(open(sys.argv[-1],'rb'))"
ESLINT = "node /opt/toolkit/js/node_modules/eslint/bin/eslint.js -c /opt/toolkit/js/eslint.config.mjs -f json --no-error-on-unmatched-pattern"
SCRIPT_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")


def command_for(path: str) -> tuple[str, str] | None:
    """``(which check, the command)`` for a file by its name, or None when there is no check for it."""
    quoted = shlex.quote(path)
    suffix = posixpath.splitext(path)[1].lower()
    if suffix in (".py", ".pyi"):
        return "python", f"python3 -I -c {shlex.quote(_PY)} -- {quoted}"
    if suffix == ".json":
        return "json", f"python3 -I -c {shlex.quote(_JSON)} -- {quoted}"
    if suffix == ".toml":
        return "toml", f"python3 -I -c {shlex.quote(_TOML)} -- {quoted}"
    if suffix in (".sh", ".bash"):
        return "shell", f"bash --noprofile --norc -n -- {quoted}"
    if suffix in SCRIPT_SUFFIXES:
        return "script", f"{ESLINT} -- {quoted}"
    return None


def _clean(text: str) -> str:
    return " ".join(code_rules.mask(str(text or ""), config=True).split())[:MESSAGE_CHARS]


def parse(kind: str, exit_code: int, output: str) -> list[dict]:
    """The problems a check reported, ``[{line, message}]`` (empty when it found none or its answer cannot be read)."""
    if kind == "script":
        try:
            files = json.loads(output[output.index("["):])
        except ValueError:
            return []
        out = []
        for f in files if isinstance(files, list) else []:
            for m in f.get("messages") or []:
                if m.get("fatal"):                                       # a parsing error: the file does not read as the language it is named for
                    out.append({"line": m.get("line"), "message": _clean(m.get("message"))})
        return out[:MAX_PROBLEMS]
    if exit_code == 0:
        return []
    text = output.strip()
    if not text:
        return []
    if kind == "python":
        tail = text.splitlines()[-1] if text.splitlines() else text
        line = re.findall(r'File ".*?", line (\d+)', text)
        return [{"line": int(line[-1]) if line else None, "message": _clean(tail)}]
    if kind == "json":
        tail = text.splitlines()[-1]
        m = re.search(r"line (\d+) column", tail)
        return [{"line": int(m.group(1)) if m else None, "message": _clean(tail)}]
    if kind == "toml":
        tail = text.splitlines()[-1]
        m = re.search(r"line (\d+)", tail)
        return [{"line": int(m.group(1)) if m else None, "message": _clean(tail)}]
    if kind == "shell":
        out = []
        for line in text.splitlines():
            m = re.search(r"line (\d+): (.*)", line)
            if m and m.group(2).startswith("`"):
                continue                                                  # bash repeats the line of code it choked on; the line of words before it already says it
            out.append({"line": int(m.group(1)) if m else None, "message": _clean(m.group(2) if m else line)})
        return out[:MAX_PROBLEMS]
    return []


def check(container_id: str, full_path: str, workdir: str = NEUTRAL) -> list[dict] | None:
    """The problems found in the file at ``full_path`` of the container: a list (empty when it parses), or None when no check applies or it could not run."""
    found = command_for(full_path)
    if found is None:
        return None
    kind, command = found
    try:
        res = sandbox.exec_run(container_id, command, workdir=workdir, timeout=TIMEOUT)
    except Exception:  # ignored: a check that cannot run says nothing; the edit itself already succeeded
        return None
    if res.get("timed_out") or (kind == "script" and res.get("exit_code") not in (0, 1)) or (kind != "script" and res.get("exit_code") in (126, 127)):
        return None                                                      # the program is not there, or it ran out of time
    return parse(kind, int(res.get("exit_code") or 0), str(res.get("output") or ""))


def add_to(result: dict, container_id: str, full_path: str, shown: str, workdir: str = NEUTRAL) -> dict:
    """``result`` of a write or an edit with what a check of the file found: ``problems`` (and the words in ``summary``) when it does not parse. A file that parses, or has no check, is left as it is."""
    problems = check(container_id, full_path, workdir)
    if problems:
        result["problems"] = problems
        where = ", ".join(f"line {p['line']}" if p.get("line") else "somewhere" for p in problems[:3])
        result["summary"] = f"{result.get('summary', '')}; but {shown} does not parse ({where})".strip("; ")
        result["note"] = (result.get("note", "") + " The file you just changed does not parse: " + "; ".join(f"{('line ' + str(p['line']) + ': ') if p.get('line') else ''}{p['message']}" for p in problems[:3])
                          + ". Fix it before going on.").strip()
    return result
