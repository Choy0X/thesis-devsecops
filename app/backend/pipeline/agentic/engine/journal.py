"""The journal of a run: what was about to happen, and what did, for every tool call.

Before a tool runs, an ``intent`` record is written (the call, its class, and for a file write the hash of the file as it is). When it has run, a ``result`` record is written (the result as
the model will be given it, and the hash of the file as it is then) BEFORE the result is added to the transcript. The transcript stays the source of truth for what the model saw; the journal
is what lets a restart tell what was in flight:

* a call with a result record but no result in the transcript was finished and not yet written down: the recorded result is written down, and the call is NOT run again;
* a call with an intent and no result may or may not have run: a read-only one is run again, a file write is compared with the hashes (not applied yet: run again; the file written is the file the call
  would have written: done; anything else: a result that says it may have run and the file is not what it was, for the model to look at), a command that cannot be shown to be read-only goes back to the
  owner as a call that may already have run, and a subagent is closed as interrupted;
* a call with neither never started, and is run as usual.

``decide`` is a pure function of the call, its records and two small helpers, so that every rule is tested without a database or a sandbox.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from app.backend.pipeline.agentic.tools import spec

READ_LIKE = ("read", "web", "scan", "meta")


def args_sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def class_of(tool: str) -> str:
    """How a call that may have run is treated on resume: ``read`` (run again), ``idempotent`` (run again), ``file_write`` (compare the file), ``exec`` (the owner decides), ``task`` (interrupted)."""
    if tool == "task":
        return "task"
    if tool == "checkpoint":
        return "idempotent"
    if tool in ("write_file", "edit_file"):
        return "file_write"
    kind = spec.kind_of(tool)[0]
    if kind in READ_LIKE:
        return "read"
    return "exec"                                                   # a command, an apply_patch, an MCP tool, a tool nobody knows


# -- commands that can safely be run again: the positive grammar of shellsafe.py

from app.backend.pipeline.agentic.engine.shellsafe import rerunnable  # noqa: E402,F401  (re-exported: the journal's rule for a command that may have run)


# -- what the records say ---------------------------------------------------------------------------------------------------------------------------------------------

def call_states(records: list[dict]) -> dict[str, dict]:
    """``{call id: {"intent": record or None, "result": record or None}}`` from the records of a run."""
    out: dict[str, dict] = {}
    for r in records:
        cid = r.get("call")
        if not cid or r.get("k") not in ("intent", "result"):
            continue
        out.setdefault(cid, {"intent": None, "result": None})[r["k"]] = r
    return out


@dataclass
class Decision:
    """What resuming does with one unresolved call: ``run`` (run it as usual), ``recovered`` (write down ``record``'s result, do not run), ``note`` (write down a result that says what is not
    known, do not run), ``ask`` (put it to the owner as a call that may already have run)."""
    action: str
    call: dict
    record: dict | None = None
    text: str = ""


def decide(call: dict, states: dict[str, dict], hash_of: Callable[[str], str | None]) -> Decision:
    """The rule for one unresolved call of the transcript (``call`` is the ``tool_calls`` entry: ``{"id", "function": {"name", "arguments"}}``)."""
    cid = call["id"]
    state = states.get(cid) or {}
    if state.get("result"):
        return Decision("recovered", call, state["result"])
    intent = state.get("intent")
    if intent is None:
        return Decision("run", call)                                  # it never started
    cls = intent.get("cls") or class_of(call["function"]["name"])
    if cls in ("read", "idempotent"):
        return Decision("run", call)
    if cls == "task":
        return Decision("note", call, text="The subagent was interrupted when the app stopped, so its work is not known. Start it again if it is still needed.")
    if cls == "file_write":
        pre = intent.get("pre") or {}
        path, before = next(iter(pre.items()), (None, None))
        try:
            wanted = json.loads(call["function"]["arguments"] or "{}")
        except ValueError:
            wanted = {}
        now = hash_of(path) if path else None
        if call["function"]["name"] == "write_file" and isinstance(wanted.get("content"), str):
            target = hashlib.sha256(wanted["content"].encode("utf-8")).hexdigest()
            if now == target:
                return Decision("recovered", call, {"ok": True, "summary": f"{path} already has exactly this content (the app stopped after writing it); nothing was written again",
                                                    "payload": json.dumps({"path": path, "unchanged": True, "summary": f"{path} already has exactly this content; nothing was written again"})})
        if path and now == before:
            return Decision("run", call)                              # the file is as it was: the call had not changed it
        return Decision("note", call, text=f"The app stopped while this call was running, and {path or 'the file'} is not as it was before it. It may have been changed by this call, wholly or in part. "
                                           f"Read the file to see what it holds now, then decide what is still to be done.")
    if cls == "exec":
        args = {}
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except ValueError:
            pass
        if call["function"]["name"] in ("bash", "run_command") and rerunnable(str(args.get("command") or "")):
            return Decision("run", call)
        return Decision("ask", call, text="The app stopped while this was running, so it may already have run, wholly or in part.")
    return Decision("ask", call, text="The app stopped while this was running, so it may already have run.")


# -- writing -----------------------------------------------------------------------------------------------------------------------------------------------------------

class Journal:
    """The writer for one run. Every method is a single insert; a failure to write the journal raises (a call must not run without its intent being on record)."""

    def __init__(self, store, run_id: str):
        self.store, self.run_id = store, run_id

    def append(self, kind: str, **fields) -> int:
        return self.store.journal_append(self.run_id, {"k": kind, **fields})

    def intent(self, call_id: str, tool: str, arguments: str, pre: dict | None = None, cls: str | None = None) -> int:
        return self.append("intent", call=call_id, tool=tool, cls=cls or class_of(tool), args_sha=args_sha(arguments), **({"pre": pre} if pre else {}))

    def result(self, call_id: str, tool: str, ok: bool, summary: str, payload: str, display: str | None = None, post: dict | None = None) -> int:
        return self.append("result", call=call_id, tool=tool, ok=bool(ok), summary=str(summary)[:500], payload=payload, **({"display": display} if display else {}), **({"post": post} if post else {}))

    def round(self, n: int, message_id: str | None = None) -> int:
        return self.append("round", n=n, **({"message": message_id} if message_id else {}))

    def note(self, what: str, **fields) -> int:
        return self.append("note", what=what, **fields)

    def stop(self, reason: str, state: str) -> int:
        return self.append("stop", reason=reason, state=state)

    def records(self, after: int = 0, limit: int = 2000) -> list[dict]:
        return self.store.journal_read(self.run_id, after, limit)

    PAGE = 1000
    MAX_RECORDS = 500_000                    # a run that wrote more than this is not read whole: resuming it asks the owner

    def all_records(self) -> list[dict]:
        """EVERY record of the run, oldest first, read page by page. A resume that read only the first page would not see the newest results of a long run, and would take a call that finished for one
        that never started."""
        out: list[dict] = []
        after = 0
        while len(out) < self.MAX_RECORDS:
            page = self.store.journal_read(self.run_id, after, self.PAGE)
            if not page:
                break
            out += page
            after = page[-1]["seq"]
            if len(page) < self.PAGE:
                break
        return out

    def state_of(self, call_id: str) -> dict:
        """``{"intent": record or None, "result": record or None}`` of one call, from every record of the run."""
        return call_states(self.all_records()).get(call_id) or {"intent": None, "result": None}
