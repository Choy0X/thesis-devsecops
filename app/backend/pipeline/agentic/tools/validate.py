"""A call's arguments checked against the tool's own schema, and made to fit it where that is safe.

A schema-constrained decision cannot get an argument's shape wrong, but a native call or a call written as text can: a number sent as a string, ``file_path`` where the tool says ``path``, a flag
as ``"true"``, a command as a list of words, an argument the tool does not have. ``coerce`` fixes what has one obvious reading and says what it did; what is left is checked with the schema
itself (``jsonschema``) and reported in a sentence that names the tool's real parameters, so that the model's next call can be right.
"""
from __future__ import annotations

import json
import re

import jsonschema

# What models call a parameter, by the name a tool really uses: applied only when the real name is declared and missing and the other name is not a parameter of its own.
SYNONYMS = {
    "path": ("file_path", "filepath", "file", "filename", "file_name", "directory", "dir", "folder"),
    "pattern": ("query", "regex", "search", "q", "search_pattern", "expression"),
    "command": ("cmd", "script", "shell_command"),
    "content": ("contents", "text", "body", "file_content"),
    "old": ("old_string", "old_str", "old_text", "find"),
    "new": ("new_string", "new_str", "new_text", "replace", "replacement"),
    "start_line": ("start", "from_line", "line_start"),
    "end_line": ("end", "to_line", "line_end"),
    "message": ("msg", "description"),
    "glob": ("include", "file_pattern", "file_glob"),
    "ignore_case": ("case_insensitive",),
    "query": ("q", "search", "text"),
    "url": ("link", "uri", "address"),
    "workdir": ("path", "cwd", "dir", "directory", "folder", "working_dir", "working_directory"),
    "background": ("detach", "async", "run_in_background", "bg"),
    "raw": ("no_filter", "unfiltered", "nofilter"),
    "job": ("job_id", "jobid", "pid", "id"),
    "handle": ("output_id", "hash", "recall_id", "id"),
    "patch": ("diff", "unified_diff", "patch_text"),
    "start": ("from", "offset", "first_line"),
    "lines": ("limit", "count", "n", "max_lines"),
    "timeout": ("timeout_seconds", "seconds", "time_limit"),
}
_TRUE, _FALSE = {"true", "yes", "1", "on"}, {"false", "no", "0", "off"}


def _coerce_value(value, declared):
    """``value`` as the declared type when there is one obvious way, else unchanged."""
    types = declared if isinstance(declared, list) else [declared]
    if value is None or any(t is None for t in types):
        return value
    if "integer" in types and isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        return int(value)
    if "integer" in types and isinstance(value, float) and value.is_integer():
        return int(value)
    if "number" in types and isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    if "boolean" in types and isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE
    if "string" in types and isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if "string" in types and isinstance(value, list) and value and all(isinstance(x, str) for x in value):
        return " ".join(value)                                                  # a command written as a list of words
    if "array" in types and isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else value
        except ValueError:
            return value
    if "object" in types and isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else value
        except ValueError:
            return value
    return value


def describe_parameters(schema: dict) -> str:
    """What a tool takes, for the sentence that tells a model: ``path (text), start_line? (whole number)``."""
    names = {"string": "text", "integer": "whole number", "number": "number", "boolean": "true or false", "array": "list", "object": "object"}
    required = set(schema.get("required") or [])
    parts = []
    for k, v in (schema.get("properties") or {}).items():
        kind = v.get("type")
        parts.append(f"{k}{'' if k in required else '?'} ({names.get(kind, kind) if isinstance(kind, str) else 'any'})")
    return ", ".join(parts) or "no arguments"


def coerce(name: str, args: dict, schema: dict) -> tuple[dict, list[str], str | None]:
    """``(arguments that fit, what was changed, a problem or None)``."""
    props = schema.get("properties") or {}
    out, notes = dict(args), []
    for prop, synonyms in SYNONYMS.items():
        if prop in props and prop not in out:
            for syn in synonyms:
                if syn in out and syn not in props:
                    out[prop] = out.pop(syn)
                    notes.append(f"{syn} read as {prop}")
                    break
    if schema.get("additionalProperties") is not True:
        for extra in [k for k in out if k not in props]:
            out.pop(extra)
            notes.append(f"{extra} is not an argument of {name}, and was left out")
    for k, v in list(out.items()):
        fixed = _coerce_value(v, (props.get(k) or {}).get("type"))
        if fixed != v or type(fixed) is not type(v):
            notes.append(f"{k} read as {type(fixed).__name__}")
            out[k] = fixed
    missing = [k for k in (schema.get("required") or []) if k not in out]
    if missing:
        return out, notes, f"{name} needs {', '.join(missing)}. It takes: {describe_parameters(schema)}."
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(out), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        where = ".".join(str(p) for p in e.path)
        return out, notes, f"The arguments of {name} do not fit: {where + ' ' if where else ''}{e.message}. It takes: {describe_parameters(schema)}."
    return out, notes, None
