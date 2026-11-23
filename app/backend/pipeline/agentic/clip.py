"""A tool result that fits a budget and is still a whole JSON document.

The loop used to cut ``json.dumps(result)[:budget]``, which ends a result in the middle of a string, loses the fields that come last (``summary``, ``truncated``, ``lines_after``), and hands
the page text that no longer parses, so it showed raw JSON. ``fit_json`` shrinks the biggest value instead (a long text at a line boundary, a list to fewer items), says in ``truncated``
what it left out, and keeps the small fields that tell the model what it is looking at. Whatever comes out parses.
"""
from __future__ import annotations

import copy
import json
import re

KEEP = {"summary", "truncated", "path", "pattern", "start_line", "end_line", "total_lines", "lines_after", "exit_code", "timed_out", "error", "files_searched", "ok", "available", "kind", "note", "recovery", "condensed", "job", "state"}
_NUMBERED = re.compile(r"^(\d+): ", re.M)


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _size(value) -> int:
    return len(_dump(value))


def _cut_text(text: str, target: int) -> str:
    """The start of ``text``, at most ``target`` characters, ended at a line boundary when there is one in its last half."""
    if target <= 0:
        return ""
    head = text[:target]
    at = head.rfind("\n")
    return head[:at] if at >= target // 2 else head


def _cut_list(items: list, target: int) -> list:
    """The first items of ``items`` whose serialised size fits ``target`` (at least one when the target allows any)."""
    kept, used = [], 2
    for item in items:
        used += _size(item) + 2
        if used > target:
            break
        kept.append(item)
    return kept


def _shrink(value, target: int):
    """``value`` made at most about ``target`` characters long, or the same value when it is not something that can be cut. Returns ``(new value, what was kept, what there was, unit)``."""
    if isinstance(value, str):
        new = _cut_text(value, target)
        return new, len(new), len(value), "characters"
    if isinstance(value, list):
        new = _cut_list(value, target)
        return new, len(new), len(value), "items"
    return value, 0, 0, ""


def _renumber(data: dict) -> None:
    """After the lines of a file read were cut, the range it reports is the range it still holds."""
    text = data.get("text")
    if not (isinstance(text, str) and isinstance(data.get("start_line"), int) and "end_line" in data):
        return
    numbers = _NUMBERED.findall(text)
    if not numbers:
        return
    data["end_line"] = int(numbers[-1])
    if isinstance(data.get("total_lines"), int):
        after = data["total_lines"] - data["end_line"]
        if after > 0:
            data["lines_after"] = after
        else:
            data.pop("lines_after", None)


def fit_json(result: dict, budget: int) -> str:
    """``result`` as JSON text of at most ``budget`` characters (best effort: a result whose small fields alone are larger comes back as a short notice) that always parses. The largest
    value that is not one of the fields that describe the result is shrunk until it fits, and ``truncated`` says what was left out."""
    text = _dump(result)
    if len(text) <= budget:
        return text
    data = copy.deepcopy(result)
    notes: list[str] = []
    for _ in range(80):
        text = _dump(data)
        if len(text) <= budget:
            break
        candidates = [(k, _size(v)) for k, v in data.items() if k not in KEEP and isinstance(v, (str, list))]
        if not candidates:
            break
        key, size = max(candidates, key=lambda c: c[1])
        over = len(text) - budget
        target = max(0, size - over - 160)              # room for the note that says what was cut
        new, kept, had, unit = _shrink(data[key], target)
        if had and kept >= had:
            break
        if not unit:
            break
        data[key] = new
        notes = [n for n in notes if not n.startswith(f"{key}: ")] + [f"{key}: kept {kept:,} of {had:,} {unit}"]
        data["truncated"] = _joined(result.get("truncated"), notes)
    _renumber(data)
    text = _dump(data)
    if len(text) > budget:
        short = {"summary": str(result.get("summary") or "The result was too large to show.")[:300], "truncated": "The result was too large to fit; ask for less (a narrower path, a shorter range)."}
        text = _dump(short)
    return text


def _joined(existing, notes: list[str]) -> str:
    parts = ([str(existing)] if existing else []) + ["; ".join(notes) + " (cut to fit)"]
    return ". ".join(parts)


def clip_arguments(arguments: str, limit: int) -> str:
    """The arguments of a call as the page is told them: whole when they fit, otherwise the long string values (a file's content, a long command) shortened with a count, and still JSON."""
    if len(arguments) <= limit:
        return arguments
    try:
        data = json.loads(arguments)
    except ValueError:
        return arguments[:limit]
    if not isinstance(data, dict):
        return arguments[:limit]
    strings = [k for k, v in data.items() if isinstance(v, str)]
    per = max(200, limit // max(1, len(strings)))
    for k in strings:
        v = data[k]
        if len(v) > per:
            data[k] = _cut_text(v, per) + f"\n... [{len(v) - per:,} more characters]"
    out = _dump(data)
    return out if len(out) <= limit * 2 else _dump({"arguments": out[:limit] + " ..."})


_RUN = re.compile(r"\S{%d,}")
RUN_LIMIT = 2_000


def shorten_runs(text: str, limit: int = RUN_LIMIT) -> str:
    """``text`` with every run of characters that has no white space in it and is longer than ``limit`` replaced by a note of how long it was (a minified file, a base64 blob, a line of
    a generated bundle). Nothing can be read from such a run, and masking credentials in it takes seconds per hundred kilobytes (the pattern looks back along the word from every position), so it is
    cut before that is done, which also means a secret inside one is not shown."""
    if len(text) <= limit:
        return text
    return re.sub(r"\S{%d,}" % (limit + 1), lambda m: f"[{len(m.group(0)):,} characters without a break left out]", text)
