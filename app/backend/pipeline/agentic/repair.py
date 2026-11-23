"""Putting right what a small model gets slightly wrong in a tool call, instead of failing the round: the arguments that are almost JSON, a tool name that is almost a name.

What a model writes for a call is often one small slip away from valid: a backslash that starts no escape (``\\d`` in a pattern), a line break inside a string, single quotes, a trailing comma, ``True``
for ``true``, the whole object written as a string inside a string. Each slip has one safe repair, and the repair never changes what a valid text means, so it is tried in order and stops at the
first that parses. What cannot be repaired is reported as it is (the model is told the arguments were not JSON and tries again); in particular a text that is cut short is never closed for the model:
a call whose arguments end in the middle is not run, because a file written half way is worse than a call that failed.

A tool name is repaired the same way: a ``functions.`` prefix, the wrong case, a name the app used to have (the aliases), or the one tool whose name is a close match.
"""
from __future__ import annotations

import difflib
import json
import re

VALID_ESCAPES = set('"\\/bfnrtu')
PREFIXES = ("functions.", "functions/", "functions:", "tools.", "tool.", "call:", "default_api.")
_PY = (("True", "true"), ("False", "false"), ("None", "null"))


def _segments(text: str) -> list[tuple[str, bool]]:
    """``text`` cut into ``(piece, inside a double-quoted string)``, following escapes, so that a repair can be applied to the strings or to what is between them and never to the wrong one."""
    out, buf, in_str, i = [], [], False, 0
    while i < len(text):
        c = text[i]
        if in_str:
            buf.append(c)
            if c == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 1
            elif c == '"':
                out.append(("".join(buf), True))
                buf, in_str = [], False
        elif c == '"':
            if buf:
                out.append(("".join(buf), False))
            buf, in_str = ['"'], True
        else:
            buf.append(c)
        i += 1
    if buf:
        out.append(("".join(buf), in_str))
    return out


def _fix_string(piece: str) -> str:
    """The inside of one string made valid: a backslash that starts no escape is a backslash, and a raw control character is written as its escape."""
    body, out, i = piece, [], 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            nxt = body[i + 1] if i + 1 < len(body) else ""
            if nxt and nxt in VALID_ESCAPES and not (nxt == "u" and not re.match(r"[0-9a-fA-F]{4}", body[i + 2:i + 6])):
                out.append(c + nxt)
                i += 2
                continue
            out.append("\\\\")
        elif ord(c) < 32:
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(c, f"\\u{ord(c):04x}"))
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _fix_strings(text: str) -> str:
    """Every closed string of ``text`` made valid; a string that never closes (the text was cut short) is left exactly as it is."""
    out = []
    for seg, inside in _segments(text):
        closed = inside and len(seg) >= 2 and seg.startswith('"') and seg.endswith('"') and not seg.endswith('\\"')
        out.append('"' + _fix_string(seg[1:-1]) + '"' if closed else seg)
    return "".join(out)


def _repairs(text: str) -> list[tuple[str, str]]:
    """The candidate texts, cheapest repair first, each with what was done."""
    out: list[tuple[str, str]] = []
    t = text.strip()
    fenced = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", t, re.S)
    if fenced:
        t = fenced.group(1)
        out.append((t, "took the arguments out of a code fence"))
    fixed = _fix_strings(t)
    if fixed != t:
        out.append((fixed, "fixed a backslash or a line break inside a string"))
        t = fixed
    commas = re.sub(r",(\s*[}\]])", r"\1", t)
    if commas != t:
        out.append((commas, "removed a trailing comma"))
        t = commas
    if '"' not in t and "'" in t:
        t2 = t.replace("'", '"')
        out.append((t2, "turned single quotes into double quotes"))
        t = t2
    parts = _segments(t)
    py = "".join(seg if inside else _python_literals(seg) for seg, inside in parts)
    if py != t:
        out.append((py, "turned Python literals into JSON ones"))
    return out


def _python_literals(piece: str) -> str:
    for old, new in _PY:
        piece = re.sub(rf"\b{old}\b", new, piece)
    return piece


def repair_arguments(raw: str | None) -> tuple[dict | None, list[str]]:
    """The arguments of a call as the object the model meant, and what was put right. ``(None, [])`` when there is nothing that parses as an object (an empty string is an empty object: a
    call with no arguments is often written as nothing)."""
    text = "" if raw is None else str(raw)
    if not text.strip():
        return {}, []
    notes: list[str] = []
    candidates = [(text, "")] + _repairs(text)
    cumulative: list[str] = []
    for candidate, what in candidates:
        if what:
            cumulative.append(what)
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, str):                                   # an object written as a string inside the string
            try:
                inner = json.loads(value)
            except ValueError:
                continue
            if isinstance(inner, dict):
                return inner, [*cumulative, "unwrapped arguments that were written as a string"]
            continue
        if isinstance(value, dict):
            return value, cumulative
        notes = ["the arguments are not an object"]
    return None, notes


def repair_name(name: str, known: list[str], aliases: dict[str, str] | None = None) -> str | None:
    """The tool a name stands for, or None. Exact first, then without a prefix such as ``functions.``, then by case, then through the aliases (names the app used before), then the one known
    name that is a close match."""
    if name in known:
        return name
    raw = str(name or "").strip()
    for prefix in PREFIXES:
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):]
    raw = raw.strip("`'\" ").removesuffix("()")
    if raw in known:
        return raw
    lowered = {k.lower(): k for k in known}
    if raw.lower() in lowered:
        return lowered[raw.lower()]
    aliased = (aliases or {}).get(raw) or (aliases or {}).get(raw.lower())
    if aliased in known:
        return aliased
    close = difflib.get_close_matches(raw.lower(), list(lowered), n=2, cutoff=0.8)
    return lowered[close[0]] if len(close) == 1 else None
