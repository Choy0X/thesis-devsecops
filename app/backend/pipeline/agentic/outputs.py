"""The whole output of a command, kept beside the session so that a cut result can be read again.

A command's output that is longer than what the model is sent (and what the page shows at first) is cut. The cut part is not lost: the full output, with credentials masked, is kept
in a file next to the session and named by a handle (``out_`` and twelve hex digits of its hash), and the ``recall`` tool and the page's "Show all lines" read it back by that handle,
from a line, up to a number of lines, or only the lines that match. Masking happens before the text is written and again when it is read (the single place that decides what may be shown).
A handle that does not start with ``out_`` is one of rtk's own (what a filter left out) and is read by rtk, not from here.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import shutil

from app.backend.pipeline import fsio, projects
from app.backend.pipeline.scan import code_rules

PREFIX = "out_"
HANDLE = re.compile(r"^out_[0-9a-f]{12}$")
MAX_ENTRY = 4_000_000                 # characters kept of one output
MAX_SESSION = 64_000_000              # characters kept for one session; the oldest outputs go first
MAX_LINES = 400                       # lines one recall returns
MAX_CHARS = 24_000                    # characters one recall returns


class OutputError(ValueError):
    """The output asked for is not there; the message says what to do."""


def folder(project_id: str, session_id: str) -> pathlib.Path:
    projects.get(project_id)
    if not re.fullmatch(r"[0-9a-f]{12}", session_id or ""):
        raise OutputError("That is not a session.")
    return projects.ROOT / project_id / "agent_sessions" / f"{session_id}.outputs"


def save(project_id: str, session_id: str, text: str) -> str:
    """Keep ``text`` (masked, at most ``MAX_ENTRY`` characters) and return its handle."""
    clean = code_rules.mask(text)[:MAX_ENTRY]
    handle = PREFIX + hashlib.sha256(clean.encode("utf-8", errors="replace")).hexdigest()[:12]
    d = folder(project_id, session_id)
    path = d / f"{handle}.txt"
    if not path.exists():
        fsio.write_text(path, clean)
        _trim(d)
    return handle


def _trim(d: pathlib.Path) -> None:
    files = sorted(d.glob("out_*.txt"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    while files and total > MAX_SESSION:
        old = files.pop(0)
        total -= old.stat().st_size
        old.unlink(missing_ok=True)


def load(project_id: str, session_id: str, handle: str, *, start: int = 1, lines: int | None = None, grep: str | None = None) -> dict:
    """``{"handle", "total_lines", "from", "to", "text", "more"}``: the part of an output that was asked for, masked again, at most ``MAX_LINES`` lines and ``MAX_CHARS`` characters."""
    if not HANDLE.fullmatch(handle or ""):
        raise OutputError("That is not an output handle. A handle looks like out_0123456789ab.")
    path = folder(project_id, session_id) / f"{handle}.txt"
    try:
        text = code_rules.mask(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        raise OutputError("That output is no longer kept. Run the command again.") from None
    rows = text.split("\n")
    if rows and rows[-1] == "":
        rows.pop()
    total = len(rows)
    numbered = list(enumerate(rows, 1))
    if grep:
        try:
            rx = re.compile(grep)
        except re.error as e:
            raise OutputError(f"The pattern is not a valid regular expression: {e}.") from None
        numbered = [(n, row) for n, row in numbered if rx.search(row)]
    start = max(1, int(start or 1))
    numbered = [(n, row) for n, row in numbered if n >= start]
    cap = min(int(lines), MAX_LINES) if lines else MAX_LINES
    shown, used = [], 0
    for n, row in numbered[:cap]:
        used += len(row) + 1
        if used > MAX_CHARS and shown:
            break
        shown.append((n, row))
    more = len(numbered) - len(shown)
    return {"handle": handle, "total_lines": total, "from": shown[0][0] if shown else start, "to": shown[-1][0] if shown else start - 1,
            "text": "\n".join(row for _, row in shown), "more": more}


def remove(project_id: str, session_id: str) -> None:
    try:
        shutil.rmtree(folder(project_id, session_id), ignore_errors=True)
    except (OutputError, projects.ProjectError):
        pass
