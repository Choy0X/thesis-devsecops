"""Small helpers that several pipeline modules used to define for themselves.

The three timestamp helpers differ on purpose, because the stored values differ and a reader of an old database or run file must still
parse them: ``now_utc`` is timezone-aware, ``now_z`` is naive UTC with a ``Z`` suffix, ``now_local`` is local time with no zone. A
module keeps the format it already wrote; none of them is a drop-in for another.
"""
from __future__ import annotations

import datetime
import difflib
import json
import pathlib
from typing import Any


def now_utc() -> str:
    """Timezone-aware UTC to the second, for example ``2026-10-08T12:00:00+00:00``."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def now_utc_precise() -> str:
    """Timezone-aware UTC with the existing context-history microsecond precision."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def now_z() -> str:
    """Naive UTC with microseconds and a ``Z`` suffix, for example ``2026-10-08T12:00:00.123456Z``."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def now_local() -> str:
    """Local time to the second with no zone, for example ``2026-10-08T14:00:00``."""
    return datetime.datetime.now().isoformat(timespec="seconds")


def now_zulu() -> str:
    """UTC to the second with a ``Z`` suffix, for example ``2026-10-08T12:00:00Z``: what Projects, their scans and their audit logs store."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_code(code: str | None) -> str:
    """Code with the outer blank lines and the trailing spaces of every line removed (so a re-indented or re-wrapped copy compares equal
    except for its indentation). None is the empty string."""
    return "\n".join(line.rstrip() for line in (code or "").strip().splitlines())


def udiff(before: str | None, after: str | None, a: str, b: str) -> str:
    """A unified diff in which every line ends in a newline. ``difflib`` leaves the last line of a text without a final newline bare, so a
    changed last line would glue its ``-`` and ``+`` lines into one; the git marker ``\\ No newline at end of file`` follows such a line."""
    out: list[str] = []
    for line in difflib.unified_diff((before or "").splitlines(True), (after or "").splitlines(True), fromfile=a, tofile=b):
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def read_json(path: str | pathlib.Path) -> Any:
    """The parsed JSON file, or None when it is missing, unreadable or not valid JSON."""
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
