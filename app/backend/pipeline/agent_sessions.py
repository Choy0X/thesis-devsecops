"""Stored agent sessions (agentic mode, Part C).

A session is a small document (its mode, status, settings, pending call, task list, grants ...) and an ordered list of messages (the transcript), kept in local SQLite behind the store of
``agentic/engine/store.py`` (collections ``agent_sessions`` and ``agent_messages``). It is the *sole* source of truth for ``agent_loop.AgentTurn``: a Manual-mode pause ends the stream
with ``pending_call`` recorded here, and resuming is a fresh ``/run`` call that rebuilds the turn from this record, not a suspended generator tied to one still-alive thread (that design
ties resume to a process that may not survive to see the human's click; this one survives a restart of the app between the pause and the approval).

Every function here keeps the shape it had when a session was one file: it takes ``(project_id, session_id)``, and the session it returns is one dict with the transcript in it. What changed
is how it is kept: an append is one insert and no longer rewrites the conversation, a change is one atomic commit, there is no limit of 4 MB for the file (a session may hold
``MAX_SESSION_BYTES``, a message ``MAX_MESSAGE_BYTES``), and a change that must happen only if the session is still in a given state (answering the pending call) is a compare-and-set that two
clicks cannot both win. A session an earlier version left as a file (``data/projects/<project id>/agent_sessions/<session id>.json``) is read as it is and imported into the store the first
time it is changed (``agentic/engine/legacy_files.py``); the file stays.

Schema ``agent_session/1`` (the shape of the dict this module returns, unchanged): ``id``, ``project_id``, ``mode`` (``"plan"``, ``"manual"`` or ``"auto"``, chosen at creation but live after --
``update()`` changes it any time nothing is ``pending_call``, and ``agent_loop._workspace_for`` picks the real project or the project's one shared sandbox fresh on every ``run()`` from whatever
it currently is, so a session never has to be forked just to change mode), ``title`` (empty until an LLM suggests one after the first run finishes, or the owner renames it), ``instructions``
(added after the project's own system prompt, like a chat's ``params.system`` -- unlike it, free to change any time via ``update()``, not just at creation), ``params`` (``temperature``,
``top_p`` and ``max_tokens``: a number, or ``None`` for the model's own; ``autocompact``: default ``True``; ``web_search``: default ``False``; ``auto_approve``: Manual mode's write/run
confirmation pause is skipped when set, default ``False``), ``created_at``, ``updated_at``, ``status`` (``"idle"``: ready for ``/run``; ``"waiting_approval"``: ``pending_call`` is set;
``"done"``; ``"error"``; ``"stopped"``: a human cancelled it -- "running" is never persisted, only tracked in memory by ``claim``, the same way a project chat's turn is), ``transcript`` (the
messages and tool calls/results a round produced, each with ``id``, ``role``, ``created_at``; a ``role == "user"`` one started in Manual or Auto also carries ``snapshot``,
``{root: commit hash}`` taken just before it ran, so ``rewind()`` can hand it to ``agent_sandbox.rollback`` if that instruction is later edited away), ``task_list``
(``[{"text", "done"}, ...]``, written only by the ``update_task_list`` tool), ``pending_call`` (``{"id", "name", "arguments", "asked_at"}`` or ``None``: at most one at a time, since a
session pauses before running a second; while it is being answered it also carries ``executing``, the process that is running it), ``rounds`` (how many model rounds have run, for Auto mode's
cap), and ``rev`` (how many changes the stored session has had).
"""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import secrets
import threading
import time
import uuid

from app.backend.pipeline import projects
from app.backend.pipeline.agentic import thinking
from app.backend.pipeline.agentic.engine import legacy_files
from app.backend.pipeline.agentic.engine.errors import AgentSessionError, NotFound, SessionExists, SessionTooLarge   # the names this module has always had
from app.backend.pipeline.agentic.engine.store import INTERNAL, MemorySessionStore, MongoSessionStore, SessionStore, message_bytes

SCHEMA = "agent_session/1"
MAX_SESSION_BYTES = 64_000_000        # what one session may hold in all (the messages as JSON); a run that would pass it ends with a message, not with a dead stream
MAX_MESSAGE_BYTES = 2_000_000         # application limit for one message; tool results are cut far below this before storage
MAX_INSTRUCTIONS = 4_000
MAX_TITLE = 80
MAX_TOKENS_LIMIT = 65_536
MODES = ("plan", "manual", "auto")
STATUSES = ("idle", "waiting_approval", "done", "error", "stopped")
BOOT = uuid.uuid4().hex               # this process: a pending call marked as executing by another boot was running when that process ended, and may have partly run
_ID = re.compile(r"[0-9a-f]{12}")
_BUSY: set[tuple[str, str]] = set()
_BUSY_LOCK = threading.Lock()
_ACTIVE: dict[tuple[str, str], object] = {}
_LOCKS: dict[tuple[str, str], threading.RLock] = {}


# -- time -----------------------------------------------------------------------------------------------------------------------------------------------

_LAST = [0]


def _stamp() -> str:
    """UTC to the millisecond with a ``Z`` suffix, and never the same twice in a row: the list is ordered by it."""
    with _BUSY_LOCK:
        now = max(time.time_ns() // 1_000_000, _LAST[0] + 1)
        _LAST[0] = now
    seconds, milliseconds = divmod(now, 1000)
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


# -- where and how -------------------------------------------------------------------------------------------------------------------------------------

_STORE: SessionStore | None = None


def store() -> SessionStore:
    """The embedded SQLite session store, or the explicitly selected in-memory implementation when ``ZTSA_AGENT_STORE=memory`` (engine tests)."""
    global _STORE
    if _STORE is None:
        _STORE = MemorySessionStore() if os.environ.get("ZTSA_AGENT_STORE", "mongo").strip().lower() == "memory" else MongoSessionStore()
    return _STORE


def use_store(new: SessionStore | None) -> None:
    """Use another store from now on (None: the default again). For tests."""
    global _STORE
    _STORE = new


def _lock(project_id: str, session_id: str) -> threading.RLock:
    """One lock per session for the changes that read a value and write it back (a counter, a list): two threads of this process do not lose each other's change."""
    with _BUSY_LOCK:
        return _LOCKS.setdefault((project_id, session_id), threading.RLock())


def _check(project_id: str, session_id: str) -> None:
    if not _ID.fullmatch(session_id or ""):
        raise NotFound()
    projects.get(project_id)                                         # a project that is not there has no sessions: NotFound of the project


def _meta(project_id: str, session_id: str) -> dict:
    """The stored meta of a session, importing it first when it is still a file of an earlier version. NotFound when there is none."""
    _check(project_id, session_id)
    meta = store().meta(project_id, session_id)
    if meta is not None:
        return meta
    doc = legacy_files.read(project_id, session_id)
    if doc is None:
        raise NotFound()
    heal_transcript(doc["transcript"], (doc.get("pending_call") or {}).get("id"))
    store().replace(project_id, doc, max_bytes=MAX_SESSION_BYTES, only_if_absent=True)      # idempotent: a second thread finds it there
    return store().meta(project_id, session_id) or {}


def _guard(message: dict) -> None:
    if message_bytes(message) > MAX_MESSAGE_BYTES:
        raise SessionTooLarge()


def heal_transcript(transcript: list[dict], pending_id: str | None = None) -> bool:
    """Turns the decision JSON that an earlier version stored as an assistant's *text* into what it always meant: a call (the thought as the narration before it, the call as a real
    ``tool_calls`` entry, matched to the result that follows by that result's own id) or a plain answer (the thought kept as ``thought``). Those sessions showed the raw
    ``{"thought": ..., "action": ...}`` as the answer and replayed a result with no call before it. Done in place, only to a message that parses as a whole decision object, and read-time:
    the file itself is rewritten the next time the session is. A *call* is restored only where an old session really had one: followed by its own result, or the last message with the session's
    pending call (``pending_id``). Words that merely look like a call, anywhere else (a model's answer that quotes one, text a project's file talked it into writing), stay words, because a
    restored call is one the next run executes. Returns whether anything changed."""
    changed = False
    for i, m in enumerate(transcript):
        if m.get("role") != "assistant" or m.get("tool_calls"):
            continue
        text = str(m.get("content") or "").strip()
        if not text.startswith("{"):
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        action = obj.get("action") if isinstance(obj, dict) else None
        if not isinstance(action, dict) or not isinstance(obj.get("thought"), str):
            continue
        if action.get("kind") == "call" and isinstance(action.get("name"), str) and isinstance(action.get("arguments"), dict):
            nxt = transcript[i + 1] if i + 1 < len(transcript) else None
            if nxt is not None and nxt.get("role") == "tool" and nxt.get("tool_call_id"):
                cid = nxt["tool_call_id"]                      # the old writer always followed the call it ran with that call's result
            elif nxt is None and pending_id:
                cid = pending_id                               # or left it as the session's one pending call
            else:
                continue                                       # anything else was never a call that ran or waited: words, left as words, never turned into something that can run
            m["content"] = obj["thought"]
            m["tool_calls"] = [{"id": cid, "type": "function", "function": {"name": action["name"], "arguments": json.dumps(action["arguments"], ensure_ascii=False)}}]
            changed = True
        elif action.get("kind") == "answer" and isinstance(action.get("text"), str):
            m["content"] = action["text"]
            if obj["thought"]:
                m["thought"] = obj["thought"]                  # what the model wrote beside its answer; read back to it, never shown as reasoning
            changed = True
    return changed


_CORE = ("id", "project_id", "mode", "title", "instructions", "params", "created_at", "updated_at", "status", "task_list", "pending_call", "rounds")


def _assemble(meta: dict, transcript: list[dict]) -> dict:
    """The dict every caller has always been given: the meta fields in their old order, the transcript, whatever optional fields the session has, and ``rev``."""
    doc = {"schema": SCHEMA, "id": meta["id"], "project_id": meta.get("project_id", ""), "mode": meta.get("mode", "plan"), "title": meta.get("title", ""), "instructions": meta.get("instructions", ""),
           "params": clean_params(meta.get("params")), "created_at": meta.get("created_at", ""), "updated_at": meta.get("updated_at", ""), "status": meta.get("status", "idle"),
           "transcript": transcript, "task_list": meta.get("task_list", []), "pending_call": meta.get("pending_call"), "rounds": meta.get("rounds", 0)}
    for key, value in meta.items():
        if key not in _CORE and key not in INTERNAL and key not in ("rev", "schema", "lease"):          # the lease names a process of this computer: not for the page
            doc[key] = value
    doc["rev"] = meta.get("rev", 0)
    return doc


def _summary(doc: dict) -> dict:
    return {"id": doc["id"], "mode": doc["mode"], "status": doc["status"], "created_at": doc.get("created_at", ""), "updated_at": doc.get("updated_at", ""),
            "rounds": doc.get("rounds", 0), "message_count": len(doc["transcript"]), "pending_call": doc.get("pending_call"), "title": doc.get("title", "")}


def _summary_of_meta(meta: dict) -> dict:
    return {"id": meta["id"], "mode": meta.get("mode", "plan"), "status": meta.get("status", "idle"), "created_at": meta.get("created_at", ""), "updated_at": meta.get("updated_at", ""),
            "rounds": meta.get("rounds", 0), "message_count": meta.get("msgs", 0), "pending_call": meta.get("pending_call"), "title": meta.get("title", "")}


def _write(project_id: str, doc: dict) -> None:
    """Write a whole session, transcript included, in place of whatever is stored (an old session as an earlier version left it, in a test). The functions below never need it."""
    for m in doc.get("transcript", []):
        _guard(m)
    store().replace(project_id, doc, max_bytes=MAX_SESSION_BYTES)


# -- parameters ------------------------------------------------------------------------------------------------------------------------------------------
# Mirrors project_chats.py's own title/number/bool/clean_params closely (duplicated, not imported: every other piece of this twin module -- _stamp, _read, _write, claim -- is
# already independently duplicated between the two rather than shared).

def _title(text: str, limit: int = MAX_TITLE) -> str:
    """One line, cut with three dots when it is longer than ``limit``."""
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[: limit - 3].rstrip() + "..."


def _number(value, name: str, low: float, high: float, integer: bool = False):
    """A parameter: None for empty (the model's own), else a number inside the limits."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise AgentSessionError(f"{name} must be a number, or empty for the model's own.")
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise AgentSessionError(f"{name} must be a number, or empty for the model's own.") from None
    if not math.isfinite(n):
        raise AgentSessionError(f"{name} must be a number, or empty for the model's own.")
    if integer and n != int(n):
        raise AgentSessionError(f"{name} must be a whole number.")
    if not low <= n <= high:
        raise AgentSessionError(f"{name} must be between {low:g} and {high:g}.")
    return int(n) if integer else n


def _bool(value, name: str) -> bool:
    if not isinstance(value, bool):
        raise AgentSessionError(f"{name} must be true or false.")
    return value


def clean_params(params: dict | None, current: dict | None = None) -> dict:
    """A session's own model settings, checked: ``{"temperature", "top_p", "max_tokens", "autocompact", "web_search", "auto_approve", "effort"}``. A key that is not given keeps its value
    in ``current``. ``auto_approve`` (Manual mode only, harmless if set for another) skips the write/run confirmation pause. ``effort`` is how much the model thinks before it acts (off, low, medium,
    high, max: ``agentic/thinking.py``)."""
    given, old = params or {}, current or {}
    out = {"temperature": old.get("temperature"), "top_p": old.get("top_p"), "max_tokens": old.get("max_tokens"),
           "autocompact": old.get("autocompact", True), "web_search": old.get("web_search", False), "auto_approve": old.get("auto_approve", False), "effort": thinking.clean(old.get("effort")),
           "engine": old.get("engine"), "budget": old.get("budget"), "flags": old.get("flags")}
    if "temperature" in given:
        out["temperature"] = _number(given["temperature"], "The temperature", 0, 2)
    if "top_p" in given:
        out["top_p"] = _number(given["top_p"], "Top p", 0, 1)
    if "max_tokens" in given:
        out["max_tokens"] = _number(given["max_tokens"], "The maximum answer length", 1, MAX_TOKENS_LIMIT, integer=True)
    if "autocompact" in given:
        out["autocompact"] = _bool(given["autocompact"], "Automatic compaction")
    if "web_search" in given:
        out["web_search"] = _bool(given["web_search"], "Web search")
    if "auto_approve" in given:
        out["auto_approve"] = _bool(given["auto_approve"], "Auto-approve")
    if "effort" in given:
        if str(given["effort"]).strip().lower() not in thinking.EFFORTS:
            raise AgentSessionError(f"The thinking effort must be one of {', '.join(thinking.EFFORTS)}.")
        out["effort"] = thinking.clean(given["effort"])
    if "engine" in given:
        if given["engine"] not in (None, "", "legacy", "v2"):
            raise AgentSessionError("The engine must be legacy or v2.")
        out["engine"] = given["engine"] or None
    if "budget" in given:
        out["budget"] = _clean_budget(given["budget"])
    if "flags" in given:
        out["flags"] = _clean_flags(given["flags"])
    return out


FLAGS = ("fence_results", "codeintel", "remediation", "protect", "gate", "ledger", "roles", "scope")


def _clean_flags(raw) -> dict | None:
    """The engine's options a session can switch (``fence_results``: tool results between markers; ``codeintel``, ``remediation``: the extra tools; ``protect``, ``gate``, ``ledger``, ``scope``: the parts of the task
    flow, each on unless switched off, so that a benchmark can measure what each adds): booleans under known names, or None for none."""
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict) or set(raw) - set(FLAGS) or any(not isinstance(v, bool) for v in raw.values()):
        raise AgentSessionError(f"The flags are {', '.join(FLAGS)}, each true or false.")
    return dict(raw)


def _clean_budget(raw) -> dict | None:
    """What a run may spend, as the session names it: any of ``rounds``, ``seconds``, ``tool_calls``, ``tokens`` (a number; ``0`` or empty means no limit for the last three), or None for the mode's own."""
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict) or set(raw) - {"rounds", "seconds", "tool_calls", "tokens"}:
        raise AgentSessionError("The budget names rounds, seconds, tool_calls and tokens, and nothing else.")
    out = {}
    for key, high in (("rounds", 1000), ("seconds", 86_400), ("tool_calls", 100_000), ("tokens", 100_000_000)):
        if key in raw:
            value = _number(raw[key], f"The budget's {key}", 0, high)
            out[key] = value if value is not None else 0
    return out or None


# -- sessions ------------------------------------------------------------------------------------------------------------------------------------------

def list_sessions(project_id: str) -> dict:
    """``{"sessions": [summary, ...], "damaged": n}``, the newest first; ``damaged`` counts the session files (an earlier version's) that could not be read."""
    projects.get(project_id)
    metas = store().list_meta(project_id)
    sessions = [_summary_of_meta(m) for m in metas if not m.get("parent")]          # the session a subagent worked in is part of the task call that started it, not a session of its own
    known = {m["id"] for m in metas}
    files, damaged = legacy_files.list_all(project_id)
    for doc in files:
        if doc.get("id") not in known and not doc.get("parent"):
            sessions.append(_summary(doc))
    sessions.sort(key=lambda s: (s["updated_at"], s["id"]), reverse=True)
    return {"sessions": sessions, "damaged": damaged}


def _model_ref(ref) -> str | None:
    """The model a session names (``provider/model``), or None for the one the settings name. Whether the provider exists is asked when a run starts, not here."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    if len(ref) > 240 or "/" not in ref.strip("/"):
        raise AgentSessionError("A model is named provider/model, for example myserver/qwen3-8b.")
    return ref



def create(project_id: str, mode: str, instructions: str = "", params: dict | None = None, model_ref: str | None = None) -> dict:
    if mode not in MODES:
        raise AgentSessionError(f"The mode must be one of {', '.join(MODES)}.")
    instructions = str(instructions or "")
    if len(instructions) > MAX_INSTRUCTIONS:
        raise AgentSessionError(f"The instructions are {len(instructions):,} characters; the limit is {MAX_INSTRUCTIONS:,}.")
    projects.get(project_id)
    now = _stamp()
    for _ in range(5):
        meta = {"id": secrets.token_hex(6), "project_id": project_id, "mode": mode, "title": "", "instructions": instructions, "params": clean_params(params),
                "created_at": now, "updated_at": now, "status": "idle", "task_list": [], "rounds": 0}
        if _model_ref(model_ref):
            meta["model_ref"] = _model_ref(model_ref)
        try:
            return _assemble(store().insert(project_id, meta), [])
        except SessionExists:
            continue                                         # a twelve-digit id that is taken: another
    raise AgentSessionError("No free session id was found.", 500, "no_id")


def get(project_id: str, session_id: str) -> dict:
    _check(project_id, session_id)
    meta = store().meta(project_id, session_id)
    if meta is None:
        doc = legacy_files.read(project_id, session_id)                 # still a file of an earlier version: shown as it is; it is imported when it is first changed
        if doc is None:
            raise NotFound()
        heal_transcript(doc["transcript"], (doc.get("pending_call") or {}).get("id"))
        return doc
    doc = _assemble(meta, store().messages(project_id, session_id))
    heal_transcript(doc["transcript"], (doc.get("pending_call") or {}).get("id"))
    return doc


def update(project_id: str, session_id: str, *, title: str | None = None, instructions: str | None = None, params: dict | None = None, mode: str | None = None,
           model_ref: str | None = None) -> dict:
    """Rename a session, change its instructions (free after the session has started, unlike at creation), its settings and/or its mode. A title that is empty after trimming is
    refused. ``mode`` is live, not fixed at creation: Plan reads the real project read-only, Manual/Auto share one sandbox per *project* (``agent_sandbox.ensure``, never recreated),
    so switching between them mid-session costs nothing and loses nothing -- refused only while a ``pending_call`` is waiting, since that call was asked under the mode about to be
    left behind."""
    meta = _meta(project_id, session_id)
    changes: dict = {}
    guarded = False                                            # a change that is refused while a call waits for the owner is made only if no call waits (checked in the commit itself)
    if title is not None:
        new = _title(title)
        if not new:
            raise AgentSessionError("A session needs a name.")
        changes["title"] = new
    if instructions is not None:
        instructions = str(instructions or "")
        if len(instructions) > MAX_INSTRUCTIONS:
            raise AgentSessionError(f"The instructions are {len(instructions):,} characters; the limit is {MAX_INSTRUCTIONS:,}.")
        changes["instructions"] = instructions
    if params is not None:
        changes["params"] = clean_params(params, meta.get("params"))
    unset: list[str] = []
    if model_ref is not None:                                  # "" goes back to the model the settings name
        if meta.get("pending_call"):
            raise AgentSessionError("Answer the pending call before changing the model.", 409, "pending_call")
        chosen = _model_ref(model_ref)
        guarded = True
        if chosen:
            changes["model_ref"] = chosen
        else:
            unset.append("model_ref")
    if mode is not None:
        if mode not in MODES:
            raise AgentSessionError(f"The mode must be one of {', '.join(MODES)}.")
        if meta.get("pending_call"):
            raise AgentSessionError("Answer the pending call before changing the mode.", 409, "pending_call")
        guarded = True
        changes["mode"] = mode
    changes["updated_at"] = _stamp()
    done = store().commit(project_id, session_id, set_=changes, unset=unset, where={"pending_call": None} if guarded else None)
    if done is None:
        raise AgentSessionError("Answer the pending call before changing the mode or the model.", 409, "pending_call")
    return get(project_id, session_id)


def delete(project_id: str, session_id: str) -> None:
    _check(project_id, session_id)
    if store().meta(project_id, session_id) is None and not legacy_files.path(project_id, session_id).is_file():
        raise NotFound()
    children = children_of(project_id, session_id)
    if any(is_busy(project_id, sid) for sid in [session_id, *children]):
        raise AgentSessionError("This session is running. Stop it or wait for it to finish before deleting it.", 409, "busy")
    from app.backend.pipeline.agentic import outputs
    for sid in [*children, session_id]:                          # the sessions its subagents worked in go with it
        legacy_files.remove(project_id, sid)                     # the file of an earlier version first: when it cannot be removed the session is still whole, and the owner is told
        store().delete(project_id, sid)
        outputs.remove(project_id, sid)                          # the whole outputs of its commands go with it
    with _BUSY_LOCK:
        _LOCKS.pop((project_id, session_id), None)


def delete_project(project_id: str) -> int:
    """Every session of a project goes with it (the files of an earlier version go with the project's folder). Returns how many the store held."""
    return store().delete_project(project_id)


def children_of(project_id: str, session_id: str) -> list[str]:
    """The ids of the sessions subagents worked in for ``session_id``."""
    projects.get(project_id)
    out = [m["id"] for m in store().list_meta(project_id) if (m.get("parent") or {}).get("session") == session_id]
    files, _ = legacy_files.list_all(project_id)
    out += [d["id"] for d in files if (d.get("parent") or {}).get("session") == session_id and d["id"] not in out]
    return sorted(out)


def create_child(project_id: str, parent_session: str, call_id: str, agent: str, mode: str, params: dict | None, model_ref: str | None, prompt: str, *, tainted: bool = False,
                 inherited_urls: list[str] | None = None) -> dict:
    """The session a subagent works in: hidden from the list, tied to the call that started it, with the task as its first message. Returns the session.

    The task was written by the model that started the subagent, not by the owner, so the message says so (``origin: agent``) and what is in it is not an address the owner wrote. What the parent has
    taken in from the web (``tainted``) and the addresses it may fetch (``inherited_urls``) are the child's too: a subagent starts with the parent's trust, no more and no less."""
    child = create(project_id, mode, "", params, model_ref)
    changes: dict = {"parent": {"session": parent_session, "call": call_id, "agent": agent}}
    if tainted:
        changes["tainted"] = True
    if inherited_urls:
        changes["inherited_urls"] = list(inherited_urls)[:500]
    store().commit(project_id, child["id"], set_=changes)
    return append_message(project_id, child["id"], new_message("user", prompt, origin="agent"))


def new_message(role: str, content: str = "", **extra) -> dict:
    return {"id": secrets.token_hex(6), "role": role, "content": str(content or ""), "created_at": _stamp(), **extra}



def close_dangling_calls(project_id: str, session_id: str) -> dict:
    """Gives each call of the transcript's last model round that never got a result (the app stopped between the model asking and the call running) a result saying so, right after the
    results it did get. Without it, a new instruction would either resume a call from before it (running something the owner has since moved on from) or send the model a call with no
    result, which a server refuses. A call that is the session's pending one is left alone: it is waiting for the owner, not lost. Returns the session."""
    _meta(project_id, session_id)
    doc = get(project_id, session_id)
    t = doc["transcript"]
    last = next((i for i in range(len(t) - 1, -1, -1) if t[i]["role"] == "assistant" and t[i].get("tool_calls")), None)
    if last is None:
        return doc
    end = last + 1
    while end < len(t) and t[end]["role"] == "tool":
        end += 1
    answered = {m.get("tool_call_id") for m in t[last + 1:end]}
    waiting = (doc.get("pending_call") or {}).get("id")
    missing = [c for c in t[last]["tool_calls"] if c["id"] not in answered and c["id"] != waiting]
    if not missing:
        return doc
    note = "This call was interrupted before it ran, so it did not run."
    store().insert_at(project_id, session_id, end, [new_message("tool", json.dumps({"error": note}), tool_call_id=c["id"], ok=False, name=c["function"]["name"], summary=note) for c in missing],
                      set_={"updated_at": _stamp()})
    return get(project_id, session_id)


def append_message(project_id: str, session_id: str, message: dict) -> dict:
    """Add one entry to the transcript (a message, a tool call or a tool result). Returns the session."""
    _meta(project_id, session_id)
    _guard(message)
    store().commit(project_id, session_id, append=[message], set_={"updated_at": _stamp()}, max_bytes=MAX_SESSION_BYTES)
    return get(project_id, session_id)


def fork(project_id: str, session_id: str, message_id: str) -> dict:
    """A new session that holds this one up to the end of the turn that ``message_id`` belongs to (an instruction, its steps and its answer): the same mode, instructions, settings and model, a
    copy of the conversation, a title that says where it came from. The original is not touched, and neither is the sandbox (the project's sandbox is shared by its sessions, so the fork
    starts from whatever state it is in); a call that was still waiting for the owner is closed with a note, not carried over, and the checkpoints kept with the owner's messages are left out so that
    editing an instruction in the fork can never roll the shared sandbox back past the point the fork was made from."""
    doc = get(project_id, session_id)
    t = doc["transcript"]
    at = next((i for i, m in enumerate(t) if m.get("id") == message_id), None)
    if at is None:
        raise AgentSessionError("There is no such message in this session.", 404, "no_message")
    end = at + 1
    while end < len(t) and t[end].get("role") != "user":
        end += 1
    kept = json.loads(json.dumps(t[:end]))
    for m in kept:
        m.pop("snapshot", None)
    child = create(project_id, doc["mode"], doc.get("instructions", ""), doc.get("params"), doc.get("model_ref"))
    store().commit(project_id, child["id"], append=kept, max_bytes=MAX_SESSION_BYTES,
                   set_={"title": ("Fork of " + (doc.get("title") or "an agent session"))[:120], "task_list": json.loads(json.dumps(doc.get("task_list", []))),
                         "forked_from": {"session": session_id, "message": message_id}, "rounds": sum(1 for m in kept if m.get("role") == "assistant"), "updated_at": _stamp()})
    return close_dangling_calls(project_id, child["id"])


def set_message_fields(project_id: str, session_id: str, message_id: str, **fields) -> dict:
    """Adds fields to one message of the transcript (the files attached to the owner's message, once they have been read). Returns the session."""
    _meta(project_id, session_id)
    if not store().update_message(project_id, session_id, message_id, fields):
        raise AgentSessionError("There is no such message in this session.", 404, "no_message")
    store().commit(project_id, session_id, set_={"updated_at": _stamp()})
    return get(project_id, session_id)


def set_status(project_id: str, session_id: str, status: str, *, rounds: int | None = None, stop_reason: str | None = None) -> dict:
    """Set the session's status; ``stop_reason`` (why the run ended, ``agentic/engine/budget.py``) is kept with it, and cleared by any status that is not an ending."""
    if status not in STATUSES:
        raise AgentSessionError(f"The status must be one of {', '.join(STATUSES)}.")
    _meta(project_id, session_id)
    changes: dict = {"status": status, "updated_at": _stamp(), "stop_reason": stop_reason}
    if rounds is not None:
        changes["rounds"] = rounds
    store().commit(project_id, session_id, set_=changes)
    return get(project_id, session_id)


def set_pending_call(project_id: str, session_id: str, call: dict | None) -> dict:
    """Record the one tool call waiting for a human's decision (Manual mode), or clear it (``None``) once it is answered. Setting one also marks the session ``waiting_approval``;
    clearing it leaves the status to whoever calls next (the loop sets ``idle`` or ``done`` itself)."""
    _meta(project_id, session_id)
    changes: dict = {"pending_call": call, "updated_at": _stamp()}
    if call is not None:
        changes["status"] = "waiting_approval"
    store().commit(project_id, session_id, set_=changes)
    return get(project_id, session_id)


def begin_pending_call(project_id: str, session_id: str, call_id: str) -> dict:
    """The owner has approved the pending call ``call_id`` and this process is about to run it: mark it so, in one conditional change that only one caller can win. A second answer to the same
    call, from a double click or from another thread, finds the mark and is refused (409). The mark names this process (``BOOT``): after a restart the call is shown as one that may have partly
    run (``in_doubt``) and can be answered again. Returns the session."""
    _meta(project_id, session_id)
    done = store().commit(project_id, session_id, set_={"pending_call.executing": BOOT, "pending_call.executing_at": _stamp()},
                          where={"pending_call.id": call_id, "pending_call.executing": {"$ne": BOOT}})
    if done is None:
        pending = (get(project_id, session_id).get("pending_call") or {})
        if pending.get("id") == call_id:
            raise AgentSessionError("This call is already being answered.", 409, "busy")
        raise AgentSessionError("There is no such pending call waiting on this session.", 409, "no_pending_call")
    return get(project_id, session_id)


def resolve_pending_call(project_id: str, session_id: str, call_id: str, message: dict, *, status: str = "idle") -> dict:
    """The result of the pending call ``call_id`` is written and the call cleared in ONE change: there is never a result with the call still pending, nor a cleared call with no result. Refused
    (409) when ``call_id`` is no longer the pending call. Returns the session."""
    if status not in STATUSES:
        raise AgentSessionError(f"The status must be one of {', '.join(STATUSES)}.")
    _meta(project_id, session_id)
    _guard(message)
    done = store().commit(project_id, session_id, append=[message], set_={"status": status, "updated_at": _stamp(), "stop_reason": None}, unset=["pending_call"], where={"pending_call.id": call_id},
                          max_bytes=MAX_SESSION_BYTES)
    if done is None:
        raise AgentSessionError("There is no such pending call waiting on this session.", 409, "no_pending_call")
    return get(project_id, session_id)


def in_doubt(pending: dict | None) -> bool:
    """Whether a pending call was being run by a process that has ended since: it may have partly run, and the owner decides again."""
    return bool(pending) and bool(pending.get("executing")) and pending["executing"] != BOOT


def runs(project_id: str, session_id: str) -> list[dict]:
    """The runs of a session, newest first (``agentic/engine/run.py``): an empty list for a session of the legacy engine."""
    _meta(project_id, session_id)
    return [{k: v for k, v in r.items() if k != "jseq"} for r in store().runs_of(session_id)]


def run_events(project_id: str, session_id: str, run_id: str | None = None, after: int = 0, limit: int = 500) -> dict:
    """The journal of a run of this session after place ``after`` (the newest run when none is named): ``{"run", "records", "next"}``, where ``next`` is the place to ask from next time. This is
    what a page that lost its stream asks for: the live stream is only a preview, and everything that happened is here and in the session."""
    all_runs = runs(project_id, session_id)
    run = next((r for r in all_runs if r["id"] == run_id), None) if run_id else (all_runs[0] if all_runs else None)
    if run_id and run is None:
        raise NotFound("There is no such run in this session.")
    if run is None:
        return {"run": None, "records": [], "next": 0}
    start = max(0, min(int(after), 2 ** 53))                     # a number the database cannot hold is not an error of the database
    records = store().journal_read(run["id"], start, max(1, min(int(limit), 2000)))
    return {"run": run, "records": records, "next": records[-1]["seq"] if records else start}


MAX_GRANTS = 100


def add_grants(project_id: str, session_id: str, rules: list[dict]) -> dict:
    """Remember "Allow always" for this session: ``rules`` are ``{tool, pattern, action: "allow"}``. A grant only ever turns a question into a yes (``agentic/permissions.py``); it is
    kept with the session, so a reload and the next run still have it, and it goes with the session when that is deleted."""
    with _lock(project_id, session_id):
        have = [dict(g) for g in _meta(project_id, session_id).get("grants", [])]
        for r in rules:
            entry = {"tool": str(r["tool"]), "pattern": str(r["pattern"]), "action": "allow"}
            if entry not in have:
                have.append(entry)
        if len(have) > MAX_GRANTS:
            raise AgentSessionError(f"A session keeps at most {MAX_GRANTS} grants.", 409, "too_many_grants")
        store().commit(project_id, session_id, set_={"grants": have, "updated_at": _stamp()})
    return get(project_id, session_id)


def clear_grants(project_id: str, session_id: str) -> dict:
    _meta(project_id, session_id)
    store().commit(project_id, session_id, set_={"grants": [], "updated_at": _stamp()})
    return get(project_id, session_id)


def rewind(project_id: str, session_id: str, message_id: str) -> tuple[dict, dict | None]:
    """Undo the last instruction as if it had never been sent: ``message_id`` must name the transcript's own *last* ``role == "user"`` entry (refused otherwise -- the frontend only
    ever offers Edit on that one, but this is enforced here too, not just there), and everything from it onward (its answer, tool calls, a still-pending call) is discarded. Returns
    the updated session and that message's own ``snapshot`` (the sandbox commits taken just before it ran, or ``None`` for a Plan-mode instruction, which never had one) so the
    caller -- which knows about the sandbox, unlike this module -- can roll it back. Clears ``pending_call`` and returns to ``idle`` regardless of what it was."""
    _meta(project_id, session_id)
    transcript = get(project_id, session_id)["transcript"]
    at = next((i for i, m in enumerate(transcript) if m["id"] == message_id), None)
    if at is None or transcript[at]["role"] != "user":
        raise AgentSessionError("There is no such instruction to edit.", 404, "not_found")
    if any(m["role"] == "user" for m in transcript[at + 1:]):
        raise AgentSessionError("Only the last instruction can be edited.", 409, "not_last")
    snapshot = transcript[at].get("snapshot")
    store().truncate(project_id, session_id, at, set_={"status": "idle", "updated_at": _stamp()}, unset=["pending_call"])
    return get(project_id, session_id), snapshot


WEB_QUERIES_KEPT = 200


def note_web_query(project_id: str, session_id: str, query: str) -> tuple[int, int]:
    """Counts a query as sent, before it is: ``(how many queries this session has now made, how many characters of them)``. Kept in the session itself and not read back from the results, so a query
    that was refused, failed or got no answer is counted too (what leaves the app is what is limited, not what came back)."""
    with _lock(project_id, session_id):
        meta = _meta(project_id, session_id)
        asked = [str(q) for q in meta.get("web_queries", [])] + [str(query)]
        count, chars = int(meta.get("web_query_count", 0)) + 1, int(meta.get("web_query_chars", 0)) + len(str(query))
        store().commit(project_id, session_id, set_={"web_queries": asked[-WEB_QUERIES_KEPT:], "web_query_count": count, "web_query_chars": chars})
    return count, chars


USAGE_SUMS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens", "seconds")


def add_usage(project_id: str, session_id: str, usage: dict) -> dict:
    """Adds what one model round cost (the tokens the server counted, the seconds it took) to the session's total, kept in the session itself so it survives a restart and a reload; a subagent's round is also
    added to the session that started it (the owner paid for it too). The prompt tokens of the round are what the request weighed, so the total keeps the newest of those as ``last_prompt_tokens`` as well."""
    ids = [session_id]
    root = (_meta(project_id, session_id).get("parent") or {}).get("session")
    if root and root != session_id:
        ids.append(root)
    out = {}
    for sid in ids:
        with _lock(project_id, sid):
            total = dict(_meta(project_id, sid).get("usage_total") or {})
            for key in USAGE_SUMS:
                value = usage.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    total[key] = round(total.get(key, 0) + value, 2) if key == "seconds" else int(total.get(key, 0) + value)
            if isinstance(usage.get("prompt_tokens"), int):
                total["last_prompt_tokens"] = usage["prompt_tokens"]
            total["rounds"] = int(total.get("rounds", 0)) + 1
            store().commit(project_id, sid, set_={"usage_total": total})
            out = total
    return out


def set_task_list(project_id: str, session_id: str, tasks: list[dict]) -> dict:
    clean = [{"text": str(t.get("text") or "")[:300], "done": bool(t.get("done"))} for t in (tasks or [])][:50]
    _meta(project_id, session_id)
    store().commit(project_id, session_id, set_={"task_list": clean, "updated_at": _stamp()})
    return get(project_id, session_id)


# -- one run at a time ----------------------------------------------------------------------------------------------------------------------------------

def is_busy(project_id: str, session_id: str) -> bool:
    """Whether a round is being run into this session right now."""
    with _BUSY_LOCK:
        return (project_id, session_id) in _BUSY


def project_busy(project_id: str) -> bool:
    """Whether any session of this project is running a round right now (the sandbox is shared by the project's sessions, so what resets or rewrites it must wait for all of them)."""
    with _BUSY_LOCK:
        return any(key[0] == project_id for key in _BUSY)


def running_sessions(project_id: str) -> set[str]:
    """The ids of the sessions of this project that are running a round right now."""
    with _BUSY_LOCK:
        return {key[1] for key in _BUSY if key[0] == project_id}


def stop(project_id: str, session_id: str) -> bool:
    """Ask the run in progress to end at the next round boundary. False when none is running."""
    with _BUSY_LOCK:
        turn = _ACTIVE.get((project_id, session_id))
    if turn is None:
        return False
    turn.cancel()
    return True


class claim:
    """``with agent_sessions.claim(project, session) as c:`` keeps a second ``/run`` from entering the same session at once (409 ``busy``). ``c.attach(turn)`` says what is running,
    so that ``stop`` can reach it (the turn needs a ``cancel()``)."""

    def __init__(self, project_id: str, session_id: str):
        self.key = (project_id, session_id)

    def __enter__(self):
        with _BUSY_LOCK:
            if self.key in _BUSY:
                raise AgentSessionError("This session is still running. Stop it or wait for it to finish.", 409, "busy")
            _BUSY.add(self.key)
        return self

    def attach(self, turn) -> None:
        with _BUSY_LOCK:
            _ACTIVE[self.key] = turn

    def __exit__(self, *exc):
        with _BUSY_LOCK:
            _BUSY.discard(self.key)
            _ACTIVE.pop(self.key, None)
        return False
