"""Stored conversations of the project chat.

A conversation is one file, ``data/projects/<project id>/chats/<chat id>.json``, written atomically (a reader or a restart sees the old file or the complete new one) and removed with
the project (the project's folder is removed as a whole). Plain, local, inspectable text, like the project's scans and its audit log; it can contain excerpts of the project's files that the
model read, so it stays on this computer and goes nowhere by itself.

Schema ``chat/1``: ``id``, ``title``, ``created_at``, ``updated_at``, ``params`` (``system``: extra instructions added after the fixed system prompt, ``temperature``, ``top_p`` and
``max_tokens``: a number, or ``None`` for the model's own, ``autocompact``: whether a long conversation is folded into a summary on its own, default ``True``, ``web_search``: whether a
question also searches the web, default ``False``) and ``messages`` (each:
``id``, ``role``, ``content``, ``created_at``; an answer also ``thinking``, ``tools``, ``sources``, ``notices``, ``usage``, ``model`` (the literal name a request to the model carries,
"llm" for the local gateway), ``model_label`` (what to show: the real GGUF file or external model name a result actually says the model was), ``error``, ``stopped``, ``trimmed``,
``compacted``).

Every limit is said in a sentence when it is hit, never by dropping something quietly: 200 conversations a project, 400 messages a conversation, 20,000 characters a message, 2 MB a file, a
title of 80 characters and 4,000 characters of system text. A file that cannot be read is left out of the list, counted there, and refused when opened; it is never repaired.
"""
from __future__ import annotations

import datetime
import json
import math
import pathlib
import re
import secrets
import threading
import time

from app.backend.pipeline import fsio, projects

SCHEMA = "chat/1"
MAX_CHATS = 200
MAX_MESSAGES = 400
MAX_MESSAGE_CHARS = 20_000
MAX_FILE_BYTES = 2_000_000
MAX_TITLE = 80
MAX_SYSTEM = 4_000
TITLE_FROM_QUESTION = 60
MAX_TOKENS_LIMIT = 65_536
_ID = re.compile(r"[0-9a-f]{12}")
_ROLES = ("user", "assistant")
_BUSY: set[tuple[str, str]] = set()
_BUSY_LOCK = threading.Lock()


class ChatStoreError(ValueError):
    """What is wrong with a conversation, as a sentence for the person, with the HTTP status a route answers with."""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.status, self.code = status, code


class NotFound(ChatStoreError):
    def __init__(self, message: str = "There is no such conversation."):
        super().__init__(message, 404, "not_found")


# -- time -----------------------------------------------------------------------------------------------------------------------------------------------

_LAST = [0]


def _stamp() -> str:
    """UTC to the millisecond with a ``Z`` suffix (``2026-10-13T10:00:00.123Z``), and never the same twice in a row: the list is ordered by it, and two things done within a second are in order."""
    with _BUSY_LOCK:
        now = max(time.time_ns() // 1_000_000, _LAST[0] + 1)
        _LAST[0] = now
    seconds, milliseconds = divmod(now, 1000)
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{milliseconds:03d}Z"


# -- where and how -------------------------------------------------------------------------------------------------------------------------------------

def _folder(project_id: str) -> pathlib.Path:
    projects.get(project_id)                                         # a project that is not there has no conversations: NotFound of the project
    return projects.ROOT / project_id / "chats"


def _path(project_id: str, chat_id: str) -> pathlib.Path:
    if not _ID.fullmatch(chat_id or ""):
        raise NotFound()
    return _folder(project_id) / f"{chat_id}.json"


def _read(path: pathlib.Path) -> dict:
    try:
        chat = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise NotFound() from None
    except ValueError:
        raise ChatStoreError("This conversation's file is damaged and cannot be read. It was left as it is; delete it to remove it.", 422, "damaged") from None
    if not isinstance(chat, dict) or chat.get("schema") != SCHEMA or not isinstance(chat.get("messages"), list):
        raise ChatStoreError("This conversation's file is not in a format this version reads. It was left as it is; delete it to remove it.", 422, "damaged")
    return chat


def _write(project_id: str, chat: dict) -> None:
    text = json.dumps(chat, ensure_ascii=False, indent=1)
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ChatStoreError("This conversation has grown too large to keep (2 MB). Start a new one; this one stays as it is.", 413, "too_large")
    fsio.write_text(_path(project_id, chat["id"]), text)


def _title(text: str, limit: int = MAX_TITLE) -> str:
    """One line, cut with three dots when it is longer than ``limit``."""
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[: limit - 3].rstrip() + "..."


def _preview(chat: dict) -> str:
    for m in reversed(chat["messages"]):
        line = " ".join(str(m.get("content") or "").split())
        if line:
            return line[:120]
    return ""


def _summary(chat: dict) -> dict:
    return {"id": chat["id"], "title": chat.get("title") or "New conversation", "created_at": chat.get("created_at", ""), "updated_at": chat.get("updated_at", ""),
            "message_count": len(chat["messages"]), "preview": _preview(chat)}


# -- parameters ----------------------------------------------------------------------------------------------------------------------------------------

def _number(value, name: str, low: float, high: float, integer: bool = False):
    """A parameter: None for empty (the model's own), else a number inside the limits."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ChatStoreError(f"{name} must be a number, or empty for the model's own.")
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ChatStoreError(f"{name} must be a number, or empty for the model's own.") from None
    if not math.isfinite(n):
        raise ChatStoreError(f"{name} must be a number, or empty for the model's own.")
    if integer and n != int(n):
        raise ChatStoreError(f"{name} must be a whole number.")
    if not low <= n <= high:
        raise ChatStoreError(f"{name} must be between {low:g} and {high:g}.")
    return int(n) if integer else n


def _bool(value, name: str) -> bool:
    if not isinstance(value, bool):
        raise ChatStoreError(f"{name} must be true or false.")
    return value


def clean_params(params: dict | None, current: dict | None = None) -> dict:
    """The parameters of a conversation, checked: ``{"system", "temperature", "top_p", "max_tokens", "autocompact", "web_search"}``. A key that is not given keeps its value in ``current``."""
    given, old = params or {}, current or {}
    out = {"system": old.get("system", ""), "temperature": old.get("temperature"), "top_p": old.get("top_p"), "max_tokens": old.get("max_tokens"),
           "autocompact": old.get("autocompact", True), "web_search": old.get("web_search", False)}
    if "system" in given:
        system = str(given["system"] or "")
        if len(system) > MAX_SYSTEM:
            raise ChatStoreError(f"The instructions are {len(system):,} characters; the limit is {MAX_SYSTEM:,}.")
        out["system"] = system
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
    return out


# -- conversations -------------------------------------------------------------------------------------------------------------------------------------

def list_chats(project_id: str) -> dict:
    """``{"chats": [summary, ...], "damaged": n}``, the newest first; ``damaged`` counts the files that could not be read."""
    folder = _folder(project_id)
    chats, damaged = [], 0
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            chats.append(_summary(_read(path)))
        except ChatStoreError:
            damaged += 1
    chats.sort(key=lambda c: (c["updated_at"], c["id"]), reverse=True)
    return {"chats": chats, "damaged": damaged}


def create(project_id: str, title: str = "", params: dict | None = None) -> dict:
    folder = _folder(project_id)
    kept = sum(1 for _ in folder.glob("*.json")) if folder.is_dir() else 0
    if kept >= MAX_CHATS:
        raise ChatStoreError(f"This project has {MAX_CHATS} conversations, the most that are kept. Delete one first.", 409, "too_many")
    now = _stamp()
    chat = {"schema": SCHEMA, "id": secrets.token_hex(6), "title": _title(title), "created_at": now, "updated_at": now, "params": clean_params(params), "messages": []}
    _write(project_id, chat)
    return chat


def get(project_id: str, chat_id: str) -> dict:
    """The conversation as it is saved. An answer that was being written when nobody was writing it any more (a closed window, a power cut) was cut short: it is shown as stopped."""
    chat = _read(_path(project_id, chat_id))
    if (project_id, chat_id) not in _BUSY:
        for m in chat["messages"]:
            if m.get("streaming"):
                m.pop("streaming")
                m["stopped"] = True
    return chat


def update(project_id: str, chat_id: str, *, title: str | None = None, params: dict | None = None) -> dict:
    """Rename a conversation and/or change its parameters. A title that is empty after trimming is refused."""
    chat = get(project_id, chat_id)
    if title is not None:
        new = _title(title)
        if not new:
            raise ChatStoreError("A conversation needs a name.")
        chat["title"] = new
    if params is not None:
        chat["params"] = clean_params(params, chat.get("params"))
    chat["updated_at"] = _stamp()
    _write(project_id, chat)
    return chat


def delete(project_id: str, chat_id: str) -> None:
    path = _path(project_id, chat_id)
    if not path.is_file():
        raise NotFound()
    path.unlink()                                                    # also a damaged file: it is not read first


def new_message(role: str, content: str = "", **extra) -> dict:
    """A message ready to be stored: an id and a time, the words, and for an answer whatever else the turn found."""
    if role not in _ROLES:
        raise ChatStoreError("A message is from the person or from the assistant.")
    return {"id": secrets.token_hex(6), "role": role, "content": str(content or ""), "created_at": _stamp(), **extra}


def append(project_id: str, chat_id: str, message: dict) -> dict:
    """Add a message at the end; the first question of a conversation without a name becomes its title. Returns the conversation."""
    chat = get(project_id, chat_id)
    if len(chat["messages"]) >= MAX_MESSAGES:
        raise ChatStoreError(f"This conversation has {MAX_MESSAGES} messages, the most that are kept. Start a new one.", 409, "too_long")
    if len(message.get("content") or "") > MAX_MESSAGE_CHARS:
        raise ChatStoreError(f"A message can be {MAX_MESSAGE_CHARS:,} characters at most; this one is {len(message['content']):,}.", 413, "message_too_long")
    chat["messages"].append(message)
    if not chat.get("title") and message["role"] == "user":
        chat["title"] = _title(message["content"], TITLE_FROM_QUESTION) or "New conversation"
    chat["updated_at"] = _stamp()
    _write(project_id, chat)
    return chat


def replace_last(project_id: str, chat_id: str, message: dict) -> dict:
    """Replace the last message (the answer that was saved while it streamed, by the one that ended the turn)."""
    chat = get(project_id, chat_id)
    if not chat["messages"]:
        raise ChatStoreError("There is no message to replace.")
    chat["messages"][-1] = message
    chat["updated_at"] = _stamp()
    _write(project_id, chat)
    return chat


def truncate_from(project_id: str, chat_id: str, message_id: str) -> tuple[dict, dict]:
    """Cut the conversation at a message: it and every message after it are removed. Returns ``(the conversation, the message that was cut first)``."""
    chat = get(project_id, chat_id)
    at = next((i for i, m in enumerate(chat["messages"]) if m.get("id") == message_id), None)
    if at is None:
        raise NotFound("That message is not in this conversation.")
    cut = chat["messages"][at]
    chat["messages"] = chat["messages"][:at]
    chat["updated_at"] = _stamp()
    _write(project_id, chat)
    return chat, cut


def clear(project_id: str, chat_id: str) -> dict:
    """Empty a conversation's messages, keeping its id, title and params (unlike ``delete``, which removes the conversation itself)."""
    chat = get(project_id, chat_id)
    if not chat["messages"]:
        return chat
    chat, _ = truncate_from(project_id, chat_id, chat["messages"][0]["id"])
    return chat


# -- one turn at a time --------------------------------------------------------------------------------------------------------------------------------

_ACTIVE: dict[tuple[str, str], object] = {}


def is_busy(project_id: str, chat_id: str) -> bool:
    """Whether an answer is being written into this conversation right now."""
    with _BUSY_LOCK:
        return (project_id, chat_id) in _BUSY


def stop(project_id: str, chat_id: str) -> bool:
    """Ask the answer that is being written into this conversation to end at the next word (it keeps what it has written). False when none is being written."""
    with _BUSY_LOCK:
        turn = _ACTIVE.get((project_id, chat_id))
    if turn is None:
        return False
    turn.cancel()
    return True


class claim:
    """``with project_chats.claim(project, chat) as c:`` keeps a second answer from being written into the same conversation at once (409 ``busy``). ``c.attach(turn)`` says what
    answers, so that ``stop`` can reach it (the turn needs a ``cancel()``)."""

    def __init__(self, project_id: str, chat_id: str):
        self.key = (project_id, chat_id)

    def __enter__(self):
        with _BUSY_LOCK:
            if self.key in _BUSY:
                raise ChatStoreError("This conversation is still being answered. Stop it or wait for it to finish.", 409, "busy")
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
