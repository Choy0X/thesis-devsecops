"""A short live test of how a model takes tools, run once per model.

What a chat template promises and what a model does are different things (measured on the model this app runs: its template takes tools, its calls were written as prose and never made). So
each protocol is tried for real with the same two small tasks, at temperature 0 and a few tokens each:

* ``read``: "read the first lines of README.md" must become one call to ``read_file`` for that file;
* ``continue``: after a search whose result is in the conversation, the model must call ``read_file`` on the file the search found, not describe what it would do. This is the step where a
  model that can make a call once but then talks gives itself away.

A protocol's score is the share of tasks it got right. The protocol chosen is the best score, and a tie goes to the earlier of native tools, the schema and a written call: native tools allow thinking and several calls in a reply, and the
schema cannot fail to be a valid call, which a call written as text can (it is the way for a server that takes neither tools nor a schema). A model for which nothing worked gets the schema.
"""
from __future__ import annotations

import json
import time

from app.backend.pipeline import decision
from app.backend.pipeline.agentic import leak
from app.backend.pipeline.agentic.profiles.model_profile import PROTOCOLS, SUITE_VERSION

MAX_TOKENS = 200
PREFERENCE = ("native", "schema", "text")           # native tools first (they allow thinking and several calls in a reply), then the schema (which cannot fail to be a call), then a written call


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required}}}


TOOLS = [
    _tool("read_file", "Read lines of one text file of the project.", {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
    _tool("search", "Search the project's files for a pattern; returns file, line and the matching line.", {"pattern": {"type": "string"}}, ["pattern"]),
    _tool("list_files", "List the files of a folder.", {"path": {"type": "string"}}, []),
]
FIRST = "Read the first 5 lines of README.md."
SECOND = "Find where main is defined and show me that function."
FOUND = "src/app.py:12: def main():"
TASKS = [
    {"id": "read", "turns": [{"role": "user", "content": FIRST}], "name": "read_file", "contains": "readme.md"},
    {"id": "continue", "turns": [{"role": "user", "content": SECOND}, {"call": ("search", {"pattern": "def main"}), "result": FOUND}], "name": "read_file", "contains": "app.py"},
]


def _native_messages(task: dict) -> list[dict]:
    out: list[dict] = []
    for i, t in enumerate(task["turns"]):
        if "call" in t:
            name, args = t["call"]
            cid = f"call_{i}"
            out += [{"role": "assistant", "content": None, "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]},
                    {"role": "tool", "tool_call_id": cid, "content": t["result"]}]
        else:
            out.append(t)
    return out


def _schema_messages(task: dict) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": decision.catalogue(TOOLS)}]
    for t in task["turns"]:
        if "call" in t:
            name, args = t["call"]
            out += [{"role": "assistant", "content": json.dumps({"thought": "", "action": {"kind": "call", "name": name, "arguments": args}})}, {"role": "tool", "content": t["result"]}]
        else:
            out.append(t)
    return out


def _right(name: str | None, arguments, task: dict) -> bool:
    return name == task["name"] and task["contains"] in json.dumps(arguments).lower()


def _try_native(client, model: str, task: dict) -> tuple[bool, str]:
    r = client.chat.completions.create(model=model, messages=_native_messages(task), tools=TOOLS, temperature=0, max_tokens=MAX_TOKENS, stream=False)
    message = r.choices[0].message
    calls = getattr(message, "tool_calls", None) or []
    if not calls:
        return False, "no call: " + str(getattr(message, "content", "") or "")[:80]
    try:
        args = json.loads(calls[0].function.arguments or "{}")
    except ValueError:
        return False, "arguments were not JSON"
    return _right(calls[0].function.name, args, task), calls[0].function.name


def _try_schema(client, model: str, task: dict) -> tuple[bool, str]:
    r = client.chat.completions.create(model=model, messages=_schema_messages(task), temperature=0, max_tokens=MAX_TOKENS, stream=False,
                                       response_format={"type": "json_schema", "json_schema": {"name": "decision", "schema": decision.build_schema(TOOLS), "strict": True}})
    decided = decision.parse_decision(r.choices[0].message.content or "", TOOLS)
    if not decided["calls"]:
        return False, "no call"
    call = decided["calls"][0]
    return _right(call["name"], json.loads(call["arguments"]), task), call["name"]


def _text_messages(task: dict) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": leak.INSTRUCTIONS + chr(10) + chr(10) + decision.catalogue(TOOLS)}]
    for t in task["turns"]:
        if "call" in t:
            name, args = t["call"]
            out += [{"role": "assistant", "content": "<tool_call>" + chr(10) + json.dumps({"name": name, "arguments": args}) + chr(10) + "</tool_call>"},
                    {"role": "user", "content": f"<tool_response name=\"{name}\">" + chr(10) + t["result"] + chr(10) + "</tool_response>"}]
        else:
            out.append(t)
    return out


def _try_text(client, model: str, task: dict) -> tuple[bool, str]:
    r = client.chat.completions.create(model=model, messages=_text_messages(task), temperature=0, max_tokens=MAX_TOKENS, stream=False)
    visible, calls, dropped = leak.scan(r.choices[0].message.content or "", [t["function"]["name"] for t in TOOLS])
    if not calls:
        return False, "no call: " + visible[:80]
    try:
        args = json.loads(calls[0]["arguments"] or "{}")
    except ValueError:
        return False, "arguments were not JSON"
    return _right(calls[0]["name"], args, task), calls[0]["name"]


ATTEMPTS = {"native": _try_native, "schema": _try_schema, "text": _try_text}


def run(client, model: str, *, protocols: tuple[str, ...] = ("native", "schema", "text")) -> dict:
    """The result of trying each protocol on each task: ``{"suite", "protocols": {name: {"score", "tasks": {id: {"ok", "got"}}, "seconds"}}, "tested_at"}``. A protocol the server refuses (a server
    that does not take tools, say) scores 0 with its reason; nothing here raises for a model's behaviour."""
    results: dict[str, dict] = {}
    for protocol in protocols:
        attempt = ATTEMPTS.get(protocol)
        if attempt is None:
            continue
        t0, tasks = time.monotonic(), {}
        for task in TASKS:
            try:
                ok, got = attempt(client, model, task)
            except Exception as e:  # noqa: BLE001 - whatever the server says is the finding: it does not take this protocol
                ok, got = False, f"{type(e).__name__}: {str(e)[:120]}"
            tasks[task["id"]] = {"ok": bool(ok), "got": got}
        results[protocol] = {"score": sum(1 for t in tasks.values() if t["ok"]) / len(TASKS), "tasks": tasks, "seconds": round(time.monotonic() - t0, 1)}
    return {"suite": SUITE_VERSION, "protocols": results, "tested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def choose(result: dict, enabled: tuple[str, ...] = ("native", "schema", "text")) -> str:
    """The protocol to use: the best score among those enabled and tried, a tie to the more natural one, and the schema when nothing scored."""
    tried = [p for p in PREFERENCE if p in enabled and p in (result.get("protocols") or {})]
    if not tried:
        return "schema"
    best = max(tried, key=lambda p: (result["protocols"][p]["score"], -PREFERENCE.index(p)))
    return best if result["protocols"][best]["score"] > 0 else "schema"


def describe(result: dict, chosen: str) -> str:
    """The result in a sentence, for the notes of a profile and the page."""
    parts = [f"{p}: {round(r['score'] * 100)}%" for p, r in (result.get("protocols") or {}).items() if p in PROTOCOLS]
    return f"Live test ({', '.join(parts)}); asked with the {chosen} protocol."
