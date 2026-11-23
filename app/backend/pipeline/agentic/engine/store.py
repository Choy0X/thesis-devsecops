"""Where agent sessions are kept: one interface, two implementations.

A session is one small document (``meta``: mode, status, settings, pending call, task list, grants, usage ...) and an ordered list of messages (the transcript). They are kept apart so that
adding a message does not rewrite the session: an append is one insert and one counter update, whatever the length of the conversation. ``commit`` is the single write the session functions
are built from; it is atomic (the messages and the field changes of one commit happen together or not at all) and it can be made conditional (``where``), which is what turns "approve this
call" into a compare-and-set: of two clicks, one matches and one does not.

``MongoSessionStore`` retains its compatibility name and uses embedded SQLite (collections ``agent_sessions`` and ``agent_messages`` of ``db/schema.py``, a transaction per commit). ``MemorySessionStore`` follows the same rules in
memory for isolated engine tests; contract tests run both implementations against the same expectations. Neither knows anything about agents: the meaning of a field is
``agent_sessions.py``'s. The stored form is the meta fields as they are (``created_at`` and ``updated_at`` as typed UTC dates, a field that is ``None`` is not stored), plus four internal
counters (``rev``, ``msg_seq``, ``msgs``, ``bytes``) that the store keeps and ``agent_sessions`` hides.
"""
from __future__ import annotations

import abc
import copy
import datetime
import json
import threading

from app.backend.pipeline.agentic.engine.errors import NotFound, SessionExists, SessionTooLarge

INTERNAL = ("msg_seq", "msgs", "bytes")          # counters the store keeps; ``rev`` is the fourth and is shown
DATE_FIELDS = ("created_at", "updated_at", "started_at", "recorded_at")
_MISSING = object()


JOURNAL_RESERVED = frozenset({"_id", "run_id", "sid", "pid", "seq", "recorded_at"})          # the store's own fields of a journal record: a record that uses one of these names is refused


def check_journal_record(record: dict) -> None:
    clash = sorted(JOURNAL_RESERVED & set(record))
    if clash:
        raise ValueError(f"a journal record cannot use the store's own field name(s) {clash}")


def message_bytes(message: dict) -> int:
    """What a message counts for towards a session's size."""
    return len(json.dumps(message, ensure_ascii=False).encode("utf-8"))


# -- paths and conditions (``where``, ``set_`` and ``unset`` name document fields by dotted path) ----------------------------------------------------------

def get_path(doc: dict, path: str):
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def set_path(doc: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = cur[part] = {}
        cur = nxt
    cur[parts[-1]] = value


def unset_path(doc: dict, path: str) -> None:
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        cur = cur.get(part)
        if not isinstance(cur, dict):
            return
    cur.pop(parts[-1], None)


def matches(doc: dict, where: dict) -> bool:
    """Document equality, ``$ne`` and ``$exists`` on dotted paths (the three conditions a commit uses). A field that is absent equals ``None``."""
    for path, cond in where.items():
        value = get_path(doc, path)
        if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
            for op, arg in cond.items():
                if op == "$ne":
                    same = (value is _MISSING and arg is None) or (value is not _MISSING and value == arg)
                    if same:
                        return False
                elif op == "$exists":
                    if (value is not _MISSING) != bool(arg):
                        return False
                else:
                    raise ValueError(f"the condition {op} is not supported")
        else:
            if cond is None:
                if value is not _MISSING and value is not None:
                    return False
            elif value is _MISSING or value != cond:
                return False
    return True


# -- the interface -------------------------------------------------------------------------------------------------------------------------------------------------

class SessionStore(abc.ABC):
    """Everything is addressed by ``(project id, session id)``. A store never returns the objects it keeps: what comes back is the caller's to change."""

    @abc.abstractmethod
    def insert(self, project_id: str, meta: dict) -> dict:
        """Add a new session (``meta`` has ``id``; no transcript). Raises ``SessionExists``. Returns the stored meta."""

    @abc.abstractmethod
    def meta(self, project_id: str, session_id: str) -> dict | None:
        """The session's meta, or None."""

    @abc.abstractmethod
    def messages(self, project_id: str, session_id: str) -> list[dict]:
        """The transcript, in order."""

    @abc.abstractmethod
    def list_meta(self, project_id: str) -> list[dict]:
        """The meta of every session of the project."""

    @abc.abstractmethod
    def commit(self, project_id: str, session_id: str, *, append=(), set_: dict | None = None, unset=(), inc: dict | None = None, where: dict | None = None,
               max_bytes: int | None = None) -> dict | None:
        """One atomic change: append ``append`` to the transcript, set the fields in ``set_`` (a value of ``None`` removes the field), remove those in ``unset``, add the numbers in ``inc``.
        With ``where`` the change happens only if the session matches it, and the answer is None when it does not (nothing changed). Always bumps ``rev``. Returns the new meta.
        Raises ``NotFound`` (no such session) and ``SessionTooLarge`` (the messages would take the session over ``max_bytes``)."""

    @abc.abstractmethod
    def insert_at(self, project_id: str, session_id: str, index: int, messages, set_: dict | None = None) -> dict:
        """Put ``messages`` into the transcript at ``index`` (the later messages move after them)."""

    @abc.abstractmethod
    def truncate(self, project_id: str, session_id: str, keep: int, set_: dict | None = None, unset=()) -> dict:
        """Keep the first ``keep`` messages and drop the rest."""

    @abc.abstractmethod
    def update_message(self, project_id: str, session_id: str, message_id: str, fields: dict) -> bool:
        """Add fields to one message. False when there is no such message."""

    @abc.abstractmethod
    def replace(self, project_id: str, doc: dict, max_bytes: int | None = None, only_if_absent: bool = False) -> dict:
        """Write a whole session (``doc`` has ``transcript``), replacing whatever is stored under its id -- or, with ``only_if_absent``, only when nothing is stored under it (importing a
        session from a file: two threads doing it at once leave one copy, and the second does not wipe what the first has added since). Returns the stored meta."""

    @abc.abstractmethod
    def delete(self, project_id: str, session_id: str) -> bool:
        """Remove the session and its messages. False when there was none."""

    @abc.abstractmethod
    def delete_project(self, project_id: str) -> int:
        """Remove every session of a project (and their runs and journals). Returns how many sessions."""

    # -- runs and their journal (``agentic/engine/run.py`` and ``journal.py``) --------------------------------------------------------------------------------------------

    @abc.abstractmethod
    def run_insert(self, doc: dict) -> None:
        """Add the record of a run (``doc`` has ``id`` and ``session_id``)."""

    @abc.abstractmethod
    def run_get(self, run_id: str) -> dict | None:
        """The record of a run, or None."""

    @abc.abstractmethod
    def run_update(self, run_id: str, *, set_: dict | None = None, inc: dict | None = None, unset=(), where: dict | None = None) -> dict | None:
        """Change a run's record atomically (the same ``set_``, ``inc``, ``unset`` and ``where`` as ``commit``). Returns the new record, or None when ``where`` did not match or there is no such run."""

    @abc.abstractmethod
    def runs_of(self, session_id: str) -> list[dict]:
        """The runs of a session, newest first."""

    @abc.abstractmethod
    def journal_append(self, run_id: str, record: dict) -> int:
        """Add a record to a run's journal. Returns its place (1, 2, 3 ... in the order they were added)."""

    @abc.abstractmethod
    def journal_read(self, run_id: str, after: int = 0, limit: int = 1000) -> list[dict]:
        """The records of a run's journal after place ``after``, in order, at most ``limit``."""


def _split(doc: dict) -> tuple[dict, list[dict]]:
    meta = {k: copy.deepcopy(v) for k, v in doc.items() if k not in ("transcript", "schema", "rev") and v is not None}
    return meta, list(doc.get("transcript") or [])


# -- in memory ----------------------------------------------------------------------------------------------------------------------------------------------------------

class MemorySessionStore(SessionStore):
    def __init__(self):
        self._lock = threading.RLock()
        self._meta: dict[tuple[str, str], dict] = {}
        self._msgs: dict[tuple[str, str], list[dict]] = {}
        self._runs: dict[str, dict] = {}
        self._journal: dict[str, list[dict]] = {}

    @staticmethod
    def _fresh(meta: dict, messages: list[dict]) -> dict:
        meta = {**meta, "rev": int(meta.get("rev", 0)), "msg_seq": len(messages), "msgs": len(messages), "bytes": sum(message_bytes(m) for m in messages)}
        return meta

    def insert(self, project_id, meta):
        with self._lock:
            key = (project_id, meta["id"])
            if key in self._meta:
                raise SessionExists()
            stored = self._fresh({k: copy.deepcopy(v) for k, v in meta.items() if k not in ("transcript", "schema", "rev") and v is not None}, [])
            self._meta[key], self._msgs[key] = stored, []
            return copy.deepcopy(stored)

    def meta(self, project_id, session_id):
        with self._lock:
            m = self._meta.get((project_id, session_id))
            return copy.deepcopy(m) if m is not None else None

    def messages(self, project_id, session_id):
        with self._lock:
            return copy.deepcopy(self._msgs.get((project_id, session_id), []))

    def list_meta(self, project_id):
        with self._lock:
            return [copy.deepcopy(m) for (pid, _), m in self._meta.items() if pid == project_id]

    def commit(self, project_id, session_id, *, append=(), set_=None, unset=(), inc=None, where=None, max_bytes=None):
        with self._lock:
            key = (project_id, session_id)
            meta = self._meta.get(key)
            if meta is None:
                raise NotFound()
            if where and not matches(meta, where):
                return None
            new = [copy.deepcopy(m) for m in append]
            total = sum(message_bytes(m) for m in new)
            if new and max_bytes is not None and meta["bytes"] + total > max_bytes:
                raise SessionTooLarge()
            self._msgs[key].extend(new)
            self._apply(meta, set_, unset, inc)
            meta["rev"] += 1
            if new:
                meta["msg_seq"] += len(new)
                meta["msgs"] += len(new)
                meta["bytes"] += total
            return copy.deepcopy(meta)

    @staticmethod
    def _apply(meta: dict, set_, unset, inc) -> None:
        for path, value in (set_ or {}).items():
            if value is None:
                unset_path(meta, path)
            else:
                set_path(meta, path, copy.deepcopy(value))
        for path in unset or ():
            unset_path(meta, path)
        for path, amount in (inc or {}).items():
            current = get_path(meta, path)
            set_path(meta, path, (0 if current is _MISSING else current) + amount)

    def _recount(self, key) -> None:
        meta, msgs = self._meta[key], self._msgs[key]
        meta["msgs"], meta["bytes"] = len(msgs), sum(message_bytes(m) for m in msgs)

    def insert_at(self, project_id, session_id, index, messages, set_=None):
        with self._lock:
            key = (project_id, session_id)
            if key not in self._meta:
                raise NotFound()
            new = [copy.deepcopy(m) for m in messages]
            self._msgs[key][index:index] = new
            self._meta[key]["msg_seq"] += len(new)
            self._apply(self._meta[key], set_, (), None)
            self._meta[key]["rev"] += 1
            self._recount(key)
            return copy.deepcopy(self._meta[key])

    def truncate(self, project_id, session_id, keep, set_=None, unset=()):
        with self._lock:
            key = (project_id, session_id)
            if key not in self._meta:
                raise NotFound()
            del self._msgs[key][keep:]
            self._apply(self._meta[key], set_, unset, None)
            self._meta[key]["rev"] += 1
            self._recount(key)
            return copy.deepcopy(self._meta[key])

    def update_message(self, project_id, session_id, message_id, fields):
        with self._lock:
            for m in self._msgs.get((project_id, session_id), []):
                if m.get("id") == message_id:
                    m.update(copy.deepcopy(fields))
                    self._recount((project_id, session_id))
                    return True
            return False

    def replace(self, project_id, doc, max_bytes=None, only_if_absent=False):
        with self._lock:
            meta, messages = _split(doc)
            if max_bytes is not None and sum(message_bytes(m) for m in messages) > max_bytes:
                raise SessionTooLarge()
            key = (project_id, doc["id"])
            if only_if_absent and key in self._meta:
                return copy.deepcopy(self._meta[key])
            rev = self._meta[key]["rev"] + 1 if key in self._meta else 0
            self._meta[key] = self._fresh({**meta, "rev": rev}, messages)
            self._msgs[key] = copy.deepcopy(messages)
            return copy.deepcopy(self._meta[key])

    def delete(self, project_id, session_id):
        with self._lock:
            key = (project_id, session_id)
            self._msgs.pop(key, None)
            self._drop_runs(lambda r: r["session_id"] == session_id)
            return self._meta.pop(key, None) is not None

    def _drop_runs(self, test) -> None:
        for rid in [rid for rid, r in self._runs.items() if test(r)]:
            self._runs.pop(rid, None)
            self._journal.pop(rid, None)

    def delete_project(self, project_id):
        with self._lock:
            keys = [k for k in self._meta if k[0] == project_id]
            for k in keys:
                self._meta.pop(k, None)
                self._msgs.pop(k, None)
            self._drop_runs(lambda r: r.get("project_id") == project_id)
            return len(keys)

    def run_insert(self, doc):
        with self._lock:
            self._runs[doc["id"]] = copy.deepcopy(doc)
            self._journal[doc["id"]] = []

    def run_get(self, run_id):
        with self._lock:
            r = self._runs.get(run_id)
            return copy.deepcopy(r) if r is not None else None

    def run_update(self, run_id, *, set_=None, inc=None, unset=(), where=None):
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or (where and not matches(run, where)):
                return None
            self._apply(run, set_, unset, inc)
            return copy.deepcopy(run)

    def runs_of(self, session_id):
        with self._lock:
            runs = [copy.deepcopy(r) for r in self._runs.values() if r["session_id"] == session_id]
            return sorted(runs, key=lambda r: (r.get("started_at", ""), r.get("n", 0), r["id"]), reverse=True)

    def journal_append(self, run_id, record):
        check_journal_record(record)
        with self._lock:
            if run_id not in self._runs:
                raise NotFound("There is no such run.")
            seq = len(self._journal[run_id]) + 1
            self._journal[run_id].append({**copy.deepcopy(record), "seq": seq, "recorded_at": _now_iso()})
            return seq

    def journal_read(self, run_id, after=0, limit=1000):
        with self._lock:
            return copy.deepcopy([r for r in self._journal.get(run_id, []) if r["seq"] > after][:limit])


# -- embedded SQLite document store (compatibility class name retained) --------------------------------------------------------------------------------------------------

_UTC = datetime.timezone.utc


def _now_iso() -> str:
    n = datetime.datetime.now(_UTC)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


def _to_dt(value):
    """``2026-10-18T10:00:00.123Z`` to a date (anything that does not read as one is kept as it is)."""
    if isinstance(value, str):
        try:
            return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=_UTC)
        except ValueError:
            return value
    return value


def _to_iso(value):
    if isinstance(value, datetime.datetime):
        value = value.replace(tzinfo=_UTC) if value.tzinfo is None else value.astimezone(_UTC)
        return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
    return value


def _pack_value(path: str, value):
    return _to_dt(value) if path in DATE_FIELDS else value


def _drop_none(value):
    """No ``null`` is stored (the data model's rule: absent means null): a key with ``None`` in a sub-document is left out."""
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    return value


def _unpack_meta(raw: dict) -> dict:
    meta = {k: v for k, v in raw.items() if k != "_id"}
    meta["id"] = raw["_id"]
    for f in DATE_FIELDS:
        if f in meta:
            meta[f] = _to_iso(meta[f])
    meta.setdefault("rev", 0)
    return meta


def _message_doc(project_id: str, session_id: str, seq: int, message: dict) -> dict:
    doc = {"_id": f"{session_id}:{seq:010d}", "pid": project_id, "sid": session_id, "seq": seq, "mid": message.get("id"), "m": message}
    if doc["mid"] is None:
        del doc["mid"]
    return doc


class MongoSessionStore(SessionStore):
    """The collections ``agent_sessions`` and ``agent_messages``. The database handle is asked for on every call (``db/legacy.handle()``, which sets the schema up once per database), so the
    store follows the application's database setting and a test's own database."""

    _ready: set[str] = set()                    # the databases whose two collections have been set up in this process
    _setup = threading.Lock()

    @classmethod
    def _db(cls):
        """The application's database with this store's two collections set up (once per process and database). The rest of the schema is the application's to set up at its start; a session
        does not wait for 35 other collections."""
        from app.backend.pipeline.db import mongo
        db = mongo.database()
        if db.name not in cls._ready:
            with cls._setup:
                if db.name not in cls._ready:
                    mongo.require()
                    mongo.ensure_schema(db, search=False, only={"agent_sessions", "agent_messages", "agent_runs", "agent_journal"})
                    cls._ready.add(db.name)
        return db

    @staticmethod
    def _client():
        from app.backend.pipeline.db import mongo
        return mongo.client()

    def _transaction(self, body):
        with self._client().start_session() as s:
            return s.with_transaction(body)

    def insert(self, project_id, meta):
        from app.backend.pipeline.db.operations import DuplicateKeyError
        raw = {"_id": meta["id"], **{k: _drop_none(_pack_value(k, v)) for k, v in meta.items() if k not in ("id", "transcript", "schema", "rev") and v is not None},
               "rev": 0, "msg_seq": 0, "msgs": 0, "bytes": 0}
        try:
            self._db()["agent_sessions"].insert_one(raw)
        except DuplicateKeyError:
            raise SessionExists() from None
        return _unpack_meta(raw)

    def meta(self, project_id, session_id):
        raw = self._db()["agent_sessions"].find_one({"_id": session_id, "project_id": project_id})
        return _unpack_meta(raw) if raw else None

    def messages(self, project_id, session_id):
        return [d["m"] for d in self._db()["agent_messages"].find({"sid": session_id}, sort=[("seq", 1)])]

    def list_meta(self, project_id):
        return [_unpack_meta(r) for r in self._db()["agent_sessions"].find({"project_id": project_id})]

    def commit(self, project_id, session_id, *, append=(), set_=None, unset=(), inc=None, where=None, max_bytes=None):
        from app.backend.pipeline.db.operations import ReturnDocument
        db = self._db()
        sessions, messages = db["agent_sessions"], db["agent_messages"]
        new = list(append)
        total = sum(message_bytes(m) for m in new)
        base = {"_id": session_id, "project_id": project_id}
        flt = {**base, **(where or {})}
        sets, unsets = {}, list(unset or ())
        for path, value in (set_ or {}).items():
            if value is None:
                unsets.append(path)
            else:
                sets[path] = _drop_none(_pack_value(path, value))
        upd: dict = {"$inc": {"rev": 1, **(inc or {})}}
        if sets:
            upd["$set"] = sets
        if unsets:
            upd["$unset"] = {p: "" for p in unsets}
        if new:
            upd["$inc"].update({"msg_seq": len(new), "msgs": len(new), "bytes": total})
            if max_bytes is not None:
                flt["bytes"] = {"$lte": max_bytes - total}

        def body(s):
            doc = sessions.find_one_and_update(flt, upd, return_document=ReturnDocument.AFTER, session=s)
            if doc is None:
                if sessions.find_one(base, {"_id": 1}, session=s) is None:
                    raise NotFound()
                if where and sessions.count_documents({**base, **where}, limit=1, session=s) == 0:
                    return None
                raise SessionTooLarge()
            if new:
                first = doc["msg_seq"] - len(new) + 1
                messages.insert_many([_message_doc(project_id, session_id, first + i, m) for i, m in enumerate(new)], session=s)
            return doc
        raw = self._transaction(body)
        return _unpack_meta(raw) if raw else None

    def _rewrite(self, s, project_id: str, session_id: str, kept: list[dict], set_: dict | None, unset=()):
        """Inside a transaction: the messages become ``kept`` (renumbered after the highest number used), the counters follow, the fields change."""
        db = self._db()
        sessions, messages = db["agent_sessions"], db["agent_messages"]
        base = {"_id": session_id, "project_id": project_id}
        doc = sessions.find_one(base, session=s)
        if doc is None:
            raise NotFound()
        messages.delete_many({"sid": session_id}, session=s)
        start = doc["msg_seq"]
        if kept:
            messages.insert_many([_message_doc(project_id, session_id, start + 1 + i, m) for i, m in enumerate(kept)], session=s)
        sets = {k: _drop_none(_pack_value(k, v)) for k, v in (set_ or {}).items() if v is not None}
        unsets = {k: "" for k in [*unset, *[k for k, v in (set_ or {}).items() if v is None]]}
        upd: dict = {"$inc": {"rev": 1}, "$set": {**sets, "msg_seq": start + len(kept), "msgs": len(kept), "bytes": sum(message_bytes(m) for m in kept)}}
        if unsets:
            upd["$unset"] = unsets
        from app.backend.pipeline.db.operations import ReturnDocument
        return sessions.find_one_and_update(base, upd, return_document=ReturnDocument.AFTER, session=s)

    def insert_at(self, project_id, session_id, index, messages, set_=None):
        def body(s):
            current = [d["m"] for d in self._db()["agent_messages"].find({"sid": session_id}, sort=[("seq", 1)], session=s)]
            current[index:index] = list(messages)
            return self._rewrite(s, project_id, session_id, current, set_)
        return _unpack_meta(self._transaction(body))

    def truncate(self, project_id, session_id, keep, set_=None, unset=()):
        def body(s):
            current = [d["m"] for d in self._db()["agent_messages"].find({"sid": session_id}, sort=[("seq", 1)], session=s)]
            return self._rewrite(s, project_id, session_id, current[:keep], set_, unset)
        return _unpack_meta(self._transaction(body))

    def update_message(self, project_id, session_id, message_id, fields):
        res = self._db()["agent_messages"].update_one({"sid": session_id, "mid": message_id}, {"$set": {f"m.{k}": v for k, v in fields.items()}})
        return res.matched_count == 1

    def replace(self, project_id, doc, max_bytes=None, only_if_absent=False):
        meta, messages = _split(doc)
        if max_bytes is not None and sum(message_bytes(m) for m in messages) > max_bytes:
            raise SessionTooLarge()
        sid = doc["id"]

        def body(s):
            db = self._db()
            sessions, msgs = db["agent_sessions"], db["agent_messages"]
            old = sessions.find_one({"_id": sid}, session=s)
            if old is not None and only_if_absent:
                return old
            msgs.delete_many({"sid": sid}, session=s)
            raw = {"_id": sid, **{k: _drop_none(_pack_value(k, v)) for k, v in meta.items() if k != "id"}, "rev": (old["rev"] + 1) if old else 0, "msg_seq": len(messages),
                   "msgs": len(messages), "bytes": sum(message_bytes(m) for m in messages)}
            sessions.replace_one({"_id": sid}, raw, upsert=True, session=s)
            if messages:
                msgs.insert_many([_message_doc(project_id, sid, i + 1, m) for i, m in enumerate(messages)], session=s)
            return raw
        return _unpack_meta(self._transaction(body))

    def delete(self, project_id, session_id):
        def body(s):
            db = self._db()
            db["agent_messages"].delete_many({"sid": session_id}, session=s)
            db["agent_journal"].delete_many({"sid": session_id}, session=s)
            db["agent_runs"].delete_many({"session_id": session_id}, session=s)
            return db["agent_sessions"].delete_one({"_id": session_id, "project_id": project_id}, session=s).deleted_count == 1
        return self._transaction(body)

    def delete_project(self, project_id):
        def body(s):
            db = self._db()
            db["agent_messages"].delete_many({"pid": project_id}, session=s)
            db["agent_journal"].delete_many({"pid": project_id}, session=s)
            db["agent_runs"].delete_many({"project_id": project_id}, session=s)
            return db["agent_sessions"].delete_many({"project_id": project_id}, session=s).deleted_count
        return self._transaction(body)

    # -- runs and their journal
    @staticmethod
    def _unpack(raw: dict) -> dict:
        out = {k: v for k, v in raw.items() if k != "_id"}
        out["id"] = raw["_id"]
        for f in DATE_FIELDS:
            if f in out:
                out[f] = _to_iso(out[f])
        return out

    def run_insert(self, doc):
        raw = {"_id": doc["id"], **{k: _drop_none(_pack_value(k, v)) for k, v in doc.items() if k != "id" and v is not None}}
        self._db()["agent_runs"].insert_one(raw)

    def run_get(self, run_id):
        raw = self._db()["agent_runs"].find_one({"_id": run_id})
        return self._unpack(raw) if raw else None

    def run_update(self, run_id, *, set_=None, inc=None, unset=(), where=None):
        from app.backend.pipeline.db.operations import ReturnDocument
        sets, unsets = {}, list(unset or ())
        for path, value in (set_ or {}).items():
            if value is None:
                unsets.append(path)
            else:
                sets[path] = _drop_none(_pack_value(path, value))
        upd: dict = {}
        if sets:
            upd["$set"] = sets
        if unsets:
            upd["$unset"] = {p: "" for p in unsets}
        if inc:
            upd["$inc"] = dict(inc)
        if not upd:
            return self.run_get(run_id)
        raw = self._db()["agent_runs"].find_one_and_update({"_id": run_id, **(where or {})}, upd, return_document=ReturnDocument.AFTER)
        return self._unpack(raw) if raw else None

    def runs_of(self, session_id):
        return [self._unpack(r) for r in self._db()["agent_runs"].find({"session_id": session_id}, sort=[("started_at", -1), ("n", -1), ("_id", -1)])]

    def journal_append(self, run_id, record):
        from app.backend.pipeline.db.operations import ReturnDocument
        check_journal_record(record)
        db = self._db()
        run = db["agent_runs"].find_one_and_update({"_id": run_id}, {"$inc": {"jseq": 1}}, return_document=ReturnDocument.AFTER, projection={"jseq": 1, "session_id": 1, "project_id": 1})
        if run is None:
            raise NotFound("There is no such run.")
        seq = run["jseq"]
        db["agent_journal"].insert_one({**record, "_id": f"{run_id}:{seq:08d}", "run_id": run_id, "sid": run["session_id"], "pid": run.get("project_id"), "seq": seq,
                                        "recorded_at": datetime.datetime.now(_UTC)})
        return seq

    def journal_read(self, run_id, after=0, limit=1000):
        out = []
        for raw in self._db()["agent_journal"].find({"run_id": run_id, "seq": {"$gt": after}}, sort=[("seq", 1)], limit=limit):
            rec = {k: v for k, v in raw.items() if k not in ("_id", "run_id", "sid", "pid")}
            if "recorded_at" in rec:
                rec["recorded_at"] = _to_iso(rec["recorded_at"])
            out.append(rec)
        return out
