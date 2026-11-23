"""A session written out as a file the owner keeps: Markdown to read, or JSON to process. Everything in it is masked for credentials once more on the way out (what is stored was masked as it was
read, but a file leaves the app and the care is repeated), a result is cut to its first lines, and what the owner said and what the agent did stay in the order they happened.
"""
from __future__ import annotations

import json

from app.backend.pipeline.scan import code_rules

RESULT_CHARS = 1_500
FORMATS = ("md", "json")


def _mask(text) -> str:
    return code_rules.mask(str(text or ""), config=True)


def _walk(value):
    """``value`` with every string in it masked (a transcript is nested JSON)."""
    if isinstance(value, str):
        return _mask(value)
    if isinstance(value, list):
        return [_walk(v) for v in value]
    if isinstance(value, dict):
        return {k: _walk(v) for k, v in value.items()}
    return value


def _call_line(name: str, arguments: str) -> str:
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        return f"{name} {arguments[:200]}"
    if isinstance(args, dict) and isinstance(args.get("command"), str):
        return f"$ {args['command']}"
    shown = ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False)[:120]}" for k, v in (args.items() if isinstance(args, dict) else []))
    return f"{name}({shown})"


def markdown(session: dict) -> str:
    """The session as a Markdown document: a header, then each instruction, the agent's thinking, steps and answers, in order."""
    out = [f"# {session.get('title') or 'Agent session'}", "",
           f"Mode: {session.get('mode')}. Started {session.get('created_at', '')}. {len([m for m in session.get('transcript', []) if m.get('role') == 'user'])} instruction(s).", ""]
    results = {m.get("tool_call_id"): m for m in session.get("transcript", []) if m.get("role") == "tool"}
    for m in session.get("transcript", []):
        role = m.get("role")
        if role == "user":
            out += ["## You", "", m.get("content") or ""]
            for a in m.get("attachments") or []:
                out.append(f"- attached: `{a.get('path')}`{' (the start of it)' if a.get('cut') else ''}")
            out.append("")
        elif role == "assistant":
            if m.get("thinking"):
                out += ["> " + line for line in str(m["thinking"]).strip().split("\n")] + [""]
            if m.get("content") and not m.get("tool_calls"):
                out += ["## Agent", "", m["content"], ""]
            elif m.get("content"):
                out += [f"_{m['content'].strip()}_", ""]
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                out += [f"**{fn.get('name')}**", "", "```", _call_line(fn.get("name") or "", fn.get("arguments") or ""), "```"]
                r = results.get(c.get("id"))
                if r is not None:
                    body = str(r.get("display") or r.get("content") or "")
                    out += [f"{'Result' if r.get('ok') else 'Failed'}: {r.get('summary') or ''}".rstrip(), ""]
                    if body.strip():
                        out += ["```", body[:RESULT_CHARS] + (f"\n... ({len(body) - RESULT_CHARS:,} more characters)" if len(body) > RESULT_CHARS else ""), "```"]
                out.append("")
    return _mask("\n".join(out).rstrip() + "\n")


def as_json(session: dict) -> str:
    """The session as JSON: its own fields and the transcript, masked."""
    keep = {k: session.get(k) for k in ("schema", "id", "project_id", "mode", "title", "instructions", "params", "model_ref", "created_at", "updated_at", "status", "task_list", "rounds") if k in session}
    return json.dumps(_walk({**keep, "transcript": session.get("transcript", [])}), ensure_ascii=False, indent=2) + "\n"


def render(session: dict, fmt: str) -> tuple[str, str, str]:
    """``(text, media type, file extension)`` for ``fmt`` (``md`` or ``json``)."""
    if fmt == "json":
        return as_json(session), "application/json", "json"
    return markdown(session), "text/markdown", "md"
