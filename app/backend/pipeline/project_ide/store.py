"""Durable Projects IDE identities, idempotent work queues, and activity journal."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from .agent_progress import unsettled_effects

STATES = {"queued", "running", "waiting_approval", "waiting_input", "completed", "failed", "interrupted"}
ACTIVE = ("running", "waiting_approval", "waiting_input")
TABLES = {"projects", "chats", "runs"}


class StoreError(ValueError):
    pass


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def record(row):
    if row is None:
        raise StoreError("The requested IDE record does not exist.")
    result = dict(row)
    if "pinned" in result:
        result["pinned"] = bool(result["pinned"])
    for name in ("settings", "model", "payload", "result"):
        if name in result and result[name] is not None:
            result[name] = json.loads(result[name])
    return result


def _turn_title(payload: dict) -> str:
    text = payload.get("text")
    lines = text.strip().splitlines() if isinstance(text, str) else []
    title = " ".join(lines[0].split()) if lines else ""
    return title[:57].rstrip() + "..." if len(title) > 60 else title


class Store:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL UNIQUE,
                    settings TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL,
                    model TEXT NOT NULL, workspace TEXT, native_session TEXT, archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS chats_project ON chats(project_id);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id), request_key TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('turn','scan')), state TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, payload TEXT NOT NULL, result TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(chat_id, request_key));
                CREATE INDEX IF NOT EXISTS runs_queue ON runs(chat_id,state,created_at);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id),
                    chat_id TEXT NOT NULL REFERENCES chats(id), run_id TEXT REFERENCES runs(id),
                    turn_id TEXT, scan_id TEXT, type TEXT NOT NULL, timestamp TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_chat ON events(chat_id,sequence);
                CREATE TABLE IF NOT EXISTS agent_checkpoints (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
                    data TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_tool_checks (
                    profile TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA user_version").fetchone()[0] < 1:
                rows = connection.execute(
                    """SELECT c.id, (SELECT r.payload FROM runs r WHERE r.chat_id=c.id AND r.kind='turn'
                                     ORDER BY r.created_at,r.rowid LIMIT 1) AS payload
                       FROM chats c WHERE c.title='New chat' AND NOT EXISTS
                       (SELECT 1 FROM events e, json_each(e.payload, '$.fields') f
                        WHERE e.chat_id=c.id AND e.type='chat.updated' AND f.value='title')"""
                ).fetchall()
                for row in rows:
                    title = _turn_title(json.loads(row["payload"])) if row["payload"] else ""
                    if title and title != "New chat":
                        connection.execute("UPDATE chats SET title=? WHERE id=?", (title, row["id"]))
                        self._event(connection, row["id"], "chat.updated", {"fields": ["title"]})
                connection.execute("PRAGMA user_version=1")
            if connection.execute("PRAGMA user_version").fetchone()[0] < 2:
                if "pinned" not in {row["name"] for row in connection.execute("PRAGMA table_info(chats)")}:
                    connection.execute("ALTER TABLE chats ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
                connection.execute("PRAGMA user_version=2")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, table: str, identity: str) -> dict:
        if table not in TABLES:
            raise StoreError("Unsupported IDE resource family.")
        with self.connect() as connection:
            return record(connection.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone())

    def projects(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT p.*, EXISTS (
                       SELECT 1 FROM chats c JOIN runs r ON r.chat_id=c.id
                       WHERE c.project_id=p.id AND r.state IN (?,?,?,?)
                   ) AS has_active_run FROM projects p ORDER BY p.created_at,p.id""",
                ("queued", *ACTIVE),
            )
            return [{**record(row), "has_active_run": bool(row["has_active_run"])} for row in rows]

    def project(self, name: str, source: str, settings: dict | None = None) -> dict:
        if not name.strip() or len(name) > 120:
            raise StoreError("A project name of at most 120 characters is required.")
        identity = uuid.uuid4().hex
        with self.connect() as connection:
            try:
                connection.execute("INSERT INTO projects VALUES(?,?,?,?,?)", (identity, name.strip(), str(Path(source).resolve()), encode(settings or {}), stamp()))
            except sqlite3.IntegrityError as exc:
                raise StoreError("That source folder is already registered.") from exc
        return self.get("projects", identity)

    def chat(self, project_id: str, title: str, model: dict) -> dict:
        self.get("projects", project_id)
        if not title.strip() or len(title) > 160 or not model.get("model"):
            raise StoreError("A chat title and explicit model configuration are required.")
        identity = uuid.uuid4().hex
        with self.connect() as connection:
            connection.execute("INSERT INTO chats(id,project_id,title,model,created_at) VALUES(?,?,?,?,?)", (identity, project_id, title.strip(), encode(model), stamp()))
            self._event(connection, identity, "chat.created", {"model": model})
        return self.get("chats", identity)

    def chats(self, project_id: str) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT c.*, COALESCE(MAX(r.updated_at), c.created_at) AS last_active_at
                   FROM chats c LEFT JOIN runs r ON r.chat_id=c.id
                   WHERE c.project_id=? GROUP BY c.id ORDER BY c.pinned DESC,last_active_at DESC,c.id""",
                (project_id,),
            )
            return [record(row) for row in rows]

    def attach_workspace(self, chat_id: str, directory: str, provenance: dict) -> None:
        """Publish a newly provisioned workspace, even when its first run is active."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute("UPDATE chats SET workspace=? WHERE id=? AND workspace IS NULL", (directory, chat_id))
            if updated.rowcount != 1:
                raise StoreError("The chat already has a workspace or is unavailable.")
            self._event(connection, chat_id, "chat.updated", {"fields": ["workspace"]})
            self._event(connection, chat_id, "workspace.created", provenance)

    def update_chat(self, chat_id: str, **fields) -> dict:
        if not fields or set(fields) - {"title", "model", "workspace", "native_session", "archived", "pinned"}:
            raise StoreError("Unsupported chat configuration change.")
        if "title" in fields:
            fields["title"] = fields["title"].strip()
            if not 1 <= len(fields["title"]) <= 160:
                raise StoreError("Enter a conversation title of 1 to 160 characters.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record(connection.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone())
            active = connection.execute("SELECT 1 FROM runs WHERE chat_id=? AND state IN ('running','waiting_approval','waiting_input')", (chat_id,)).fetchone()
            if fields.get("archived") and connection.execute("SELECT 1 FROM runs WHERE chat_id=? AND state IN ('queued','running','waiting_approval','waiting_input')", (chat_id,)).fetchone():
                raise StoreError("Cancel queued or active work before archiving this chat.")
            if active and set(fields) & {"model", "workspace"}:
                raise StoreError("Execution configuration can change only between runs.")
            values = [encode(value) if key == "model" else value for key, value in fields.items()]
            connection.execute("UPDATE chats SET " + ",".join(key + "=?" for key in fields) + " WHERE id=?", (*values, chat_id))
            self._event(connection, chat_id, "chat.updated", {"fields": list(fields)})
        return self.get("chats", chat_id)

    def delete_chat(self, chat_id: str) -> None:
        """Delete saved conversation records without touching its filesystem checkout."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record(connection.execute("SELECT id FROM chats WHERE id=?", (chat_id,)).fetchone())
            if connection.execute("SELECT 1 FROM runs WHERE chat_id=? AND state IN (?,?,?,?)",
                                  (chat_id, "queued", *ACTIVE)).fetchone():
                raise StoreError("Cancel queued or active work before deleting this conversation.")
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_sources'").fetchone():
                connection.execute("DELETE FROM chat_sources WHERE chat_id=?", (chat_id,))
            connection.execute("DELETE FROM events WHERE chat_id=?", (chat_id,))
            connection.execute("DELETE FROM runs WHERE chat_id=?", (chat_id,))
            connection.execute("DELETE FROM chats WHERE id=?", (chat_id,))

    def submit(self, chat_id: str, request_key: str, kind: str, payload: dict) -> dict:
        if kind not in ("turn", "scan") or not request_key or len(request_key) > 128:
            raise StoreError("A valid work kind and submission idempotency key are required.")
        fingerprint = hashlib.sha256(encode({"kind": kind, "payload": payload}).encode()).hexdigest()
        identity, now = uuid.uuid4().hex, stamp()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            chat = record(connection.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone())
            if chat["archived"]:
                raise StoreError("Archived chats cannot start work.")
            prior = connection.execute("SELECT * FROM runs WHERE chat_id=? AND request_key=?", (chat_id, request_key)).fetchone()
            if prior:
                result = record(prior)
                if result["fingerprint"] != fingerprint:
                    raise StoreError("The submission key was already used for different work.")
                return result
            if kind == "turn" and chat["title"] == "New chat":
                title = _turn_title(payload)
                if title and title != chat["title"]:
                    changed = connection.execute(
                        """UPDATE chats SET title=? WHERE id=? AND NOT EXISTS
                           (SELECT 1 FROM runs WHERE chat_id=? AND kind='turn')""",
                        (title, chat_id, chat_id),
                    )
                    if changed.rowcount:
                        self._event(connection, chat_id, "chat.updated", {"fields": ["title"]})
            connection.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)", (identity, chat_id, request_key, kind, "queued", fingerprint, encode(payload), None, now, now))
            self._event(connection, chat_id, "run.queued", {"kind": kind}, identity)
        return self.get("runs", identity)

    def claim(self, chat_id: str, expected_id: str | None = None) -> dict | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM runs WHERE chat_id=? AND state IN ('running','waiting_approval','waiting_input')", (chat_id,)).fetchone():
                return None
            row = connection.execute("SELECT * FROM runs WHERE chat_id=? AND state='queued' ORDER BY created_at,rowid LIMIT 1", (chat_id,)).fetchone()
            if row is None or (expected_id is not None and row["id"] != expected_id):
                return None
            connection.execute("UPDATE runs SET state='running',updated_at=? WHERE id=?", (stamp(), row["id"]))
            self._event(connection, chat_id, "run.running", {}, row["id"])
            result = record(row)
            result["state"] = "running"
            return result

    def transition(self, run_id: str, state: str, result: dict | None = None) -> dict:
        if state not in STATES:
            raise StoreError("Unknown execution state.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = record(connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            allowed = {"queued": {"running", "interrupted", "failed"}, "running": set(ACTIVE) | {"completed", "failed", "interrupted"},
                       "waiting_approval": set(ACTIVE) | {"failed", "interrupted"}, "waiting_input": set(ACTIVE) | {"failed", "interrupted"}}
            if state not in allowed.get(run["state"], set()):
                raise StoreError("The execution state cannot transition from " + run["state"] + " to " + state)
            connection.execute("UPDATE runs SET state=?,result=?,updated_at=? WHERE id=?", (state, encode(result) if result is not None else None, stamp(), run_id))
            self._event(connection, run["chat_id"], "run." + state, result or {}, run_id)
        return self.get("runs", run_id)

    def _event(self, connection, chat_id: str, event_type: str, payload: dict, run_id: str | None = None):
        chat = record(connection.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone())
        run = record(connection.execute("SELECT * FROM runs WHERE id=? AND chat_id=?", (run_id, chat_id)).fetchone()) if run_id else None
        connection.execute("INSERT INTO events(project_id,chat_id,run_id,turn_id,scan_id,type,timestamp,payload) VALUES(?,?,?,?,?,?,?,?)",
                           (chat["project_id"], chat_id, run_id, run_id if run and run["kind"] == "turn" else None,
                            run_id if run and run["kind"] == "scan" else None, event_type, stamp(), encode(payload)))

    def event(self, chat_id: str, event_type: str, payload: dict, run_id: str | None = None) -> None:
        with self.connect() as connection:
            self._event(connection, chat_id, event_type, payload, run_id)

    def latest_sequence(self, chat_id: str) -> int:
        with self.connect() as connection:
            return connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM events WHERE chat_id=?", (chat_id,)).fetchone()[0]

    def events(self, chat_id: str, after: int = 0) -> list[dict]:
        with self.connect() as connection:
            return [record(row) for row in connection.execute("SELECT * FROM events WHERE chat_id=? AND sequence>? ORDER BY sequence LIMIT 1000", (chat_id, after))]

    def runs(self, chat_id: str) -> list[dict]:
        with self.connect() as connection:
            return [record(row) for row in connection.execute("SELECT * FROM runs WHERE chat_id=? ORDER BY created_at,rowid", (chat_id,))]

    @staticmethod
    def _checkpoint(connection, run_id):
        row = connection.execute("SELECT data,updated_at FROM agent_checkpoints WHERE run_id=?", (run_id,)).fetchone()
        return {**json.loads(row["data"]), "saved_at": row["updated_at"]} if row else None

    def checkpoint(self, run_id):
        with self.connect() as connection:
            return self._checkpoint(connection, run_id)

    def model_tool_status(self, profile):
        with self.connect() as connection:
            row = connection.execute("SELECT status FROM model_tool_checks WHERE profile=?", (encode(profile),)).fetchone()
            return row["status"] if row else "unchecked"

    def record_model_tool_status(self, profile, status):
        with self.connect() as connection:
            connection.execute("""INSERT INTO model_tool_checks VALUES(?,?,?)
                ON CONFLICT(profile) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at""",
                               (encode(profile), status, stamp()))

    def recent_decisions(self, chat_id):
        with self.connect() as connection:
            rows = connection.execute("""SELECT json_extract(c.data,'$.decisions') AS decisions
                FROM agent_checkpoints c JOIN runs r ON r.id=c.run_id WHERE r.chat_id=?
                AND json_array_length(c.data,'$.decisions') > 0 ORDER BY r.rowid DESC LIMIT 3""", (chat_id,)).fetchall()
        return [item for row in reversed(rows) for item in json.loads(row["decisions"])][-3:]

    @staticmethod
    def _save_checkpoint(connection, run_id, data):
        data.pop("saved_at", None)
        connection.execute("""INSERT INTO agent_checkpoints VALUES(?,?,?)
            ON CONFLICT(run_id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at""",
                           (run_id, encode(data), stamp()))

    def save_checkpoint(self, run_id, **fields):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            data = self._checkpoint(connection, run_id) or {}
            self._save_checkpoint(connection, run_id, {**data, **fields})

    def save_question(self, run_id, question):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = record(connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            data = self._checkpoint(connection, run_id)
            if not data or data.get("question") or run["state"] != "running":
                raise StoreError("This run cannot ask another question now.")
            self._save_checkpoint(connection, run_id, {**data, "question": question})
            connection.execute("UPDATE runs SET state='waiting_input',updated_at=? WHERE id=?", (stamp(), run_id))
            self._event(connection, run["chat_id"], "agent.question", {"id": question["id"]}, run_id)

    def answer_question(self, run_id, request_id, answers):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = record(connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            data = self._checkpoint(connection, run_id) or {}
            question = data.get("question")
            recovered = run["state"] == "interrupted" and (run["result"] or {}).get("reason") == "application_restart"
            eligible = self._recoverable(connection, run["chat_id"])[0] if recovered else None
            if (not question or question["id"] != request_id or (run["state"] != "waiting_input" and not recovered)
                    or (recovered and (not eligible or eligible["id"] != run_id))):
                raise StoreError("This planning question is no longer active.")
            if (len(answers) != len(question["questions"]) or
                    any(len(choice) != 1 or not 1 <= len(choice[0].strip()) <= 2000 for choice in answers)):
                raise StoreError("Enter one answer of up to 2000 characters for each planning question.")
            decision = {"request": question, "answers": [[choice[0].strip()] for choice in answers]}
            self._save_checkpoint(connection, run_id, {**data, "question": None,
                                  "decisions": [*data.get("decisions", []), decision]})
            if not recovered:
                connection.execute("UPDATE runs SET state='running',updated_at=? WHERE id=?", (stamp(), run_id))
            self._event(connection, run["chat_id"], "agent.answer", {"id": request_id}, run_id)
            return decision["answers"]

    def _recoverable(self, connection, chat_id):
        chat = record(connection.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone())
        latest = connection.execute("SELECT * FROM runs WHERE chat_id=? AND kind='turn' ORDER BY rowid DESC LIMIT 1", (chat_id,)).fetchone()
        if chat["archived"] or latest is None or latest["state"] != "interrupted":
            return None, None
        run = record(latest)
        data = self._checkpoint(connection, run["id"])
        if ((run["result"] or {}).get("reason") != "application_restart" or not data
                or data.get("model") != chat["model"]):
            return None, None
        return run, data

    def recovery(self, chat_id):
        with self.connect() as connection:
            run, data = self._recoverable(connection, chat_id)
        if run:
            return {"run_id": run["id"], "saved_at": data["saved_at"], "question": data.get("question"),
                    "uncertain": unsettled_effects(data), "attempts": data.get("attempts", 0)}
        return None

    def resume(self, chat_id, run_id):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run, data = self._recoverable(connection, chat_id)
            if not run or run["id"] != run_id:
                raise StoreError("This conversation has no matching saved run to resume with its current model.")
            if data.get("question"):
                raise StoreError("Submit the saved planning answer before resuming.")
            if connection.execute("SELECT 1 FROM runs WHERE chat_id=? AND state IN ('queued','running','waiting_input','waiting_approval')",
                                  (chat_id,)).fetchone():
                raise StoreError("Finish active conversation work before resuming.")
            self._save_checkpoint(connection, run_id, {**data, "attempts": data.get("attempts", 0) + 1})
            connection.execute("UPDATE runs SET state='queued',result=NULL,updated_at=? WHERE id=?", (stamp(), run_id))
            self._event(connection, chat_id, "agent.resumed", {}, run_id)
        return self.get("runs", run_id)

    def reconcile(self) -> int:
        """Startup is explicit: never repeat queued commands or uncertain approvals."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT * FROM runs WHERE state IN ('queued','running','waiting_approval','waiting_input')").fetchall()
            for row in rows:
                saved = self._checkpoint(connection, row["id"])
                if row["state"] == "running" and saved and saved.get("finished"):
                    connection.execute("UPDATE runs SET state='completed',result=?,updated_at=? WHERE id=?",
                                       (encode(saved["finished"]), stamp(), row["id"]))
                    self._event(connection, row["chat_id"], "run.completed", saved["finished"], row["id"])
                    continue
                result = {"reason": "application_restart", "previous_state": row["state"]}
                if saved and saved.get("messages"):
                    result["text_messages"] = saved["messages"]
                connection.execute("UPDATE runs SET state='interrupted',result=?,updated_at=? WHERE id=?", (encode(result), stamp(), row["id"]))
                self._event(connection, row["chat_id"], "run.interrupted", result, row["id"])
            return len(rows)
