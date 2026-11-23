"""Durable bounded-wave npm qualification orchestration. Never starts training.

The SQLite journal stores identities and compact outcomes, not corpus bodies.
External operations remain independently resumable. Build creation is reconciled
by a stable catalog marker, including interruption before its ID is journalled.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import hmac
import inspect
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from types import SimpleNamespace
import uuid

from . import oslock

POLICY = "automated-qualified-npm-workflow/1"
_MARKER = "npm-qualification-work:"
_BODY_FIELDS = {"vulnerable_code", "fixed_code", "context_before", "context_after", "families", "records", "items", "payload", "snapshots", "files", "source", "body"}
_INITIALIZED = set()
_PHASE_LABELS = {"repair": "Recovering exact source evidence", "verify": "Verifying mechanism evidence", "build": "Building the next immutable dataset",
                 "data_preflight": "Checking operational data qualification", "plan_sources": "Planning targeted source evidence",
                 "acquire_sources": "Acquiring exact npm source evidence", "prepare_run": "Preparing the next transactional run"}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _root(root=None):
    if root is None:
        from . import versions
        root = versions.PROC
    return Path(root).resolve()


def database_path(root=None):
    return _root(root) / ".npm_qualification_workflow.sqlite3"


def lock_path(root=None):
    return _root(root) / ".npm_qualification" / ".execute.lock"


@contextmanager
def _connect(root=None):
    path = database_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    initialize = path not in _INITIALIZED or not path.exists()
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    if initialize:
        db.executescript('''
      PRAGMA busy_timeout=30000;
      CREATE TABLE IF NOT EXISTS workflows (
        id TEXT PRIMARY KEY, request_id TEXT UNIQUE, dedup_key TEXT NOT NULL,
        parent_tag TEXT NOT NULL, parent_hash TEXT NOT NULL, state TEXT NOT NULL,
        fence INTEGER NOT NULL DEFAULT 0, owner_pid INTEGER, owner_ctime REAL,
        owner_token TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
        body TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS workflow_dedup ON workflows(dedup_key);
      CREATE INDEX IF NOT EXISTS workflow_parent ON workflows(parent_tag,created);
      CREATE TABLE IF NOT EXISTS workflow_requests (request_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, parent_hash TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS phase_outcomes (
        id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, wave INTEGER NOT NULL,
        phase TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
        result TEXT NOT NULL, started REAL NOT NULL, finished REAL,
        owner_pid INTEGER, owner_ctime REAL, owner_fence INTEGER);
      CREATE INDEX IF NOT EXISTS phase_lookup ON phase_outcomes(workflow_id,wave,phase,fingerprint,state);
      CREATE INDEX IF NOT EXISTS phase_semantic ON phase_outcomes(workflow_id,phase,fingerprint,state,finished);
      CREATE TABLE IF NOT EXISTS workflow_builds (
        work_key TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,
        build_id TEXT, state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}');
      CREATE TABLE IF NOT EXISTS workflow_runs (
        work_key TEXT PRIMARY KEY, state TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}');
        ''')
        existing = {row[1] for row in db.execute("PRAGMA table_info(phase_outcomes)")}
        for name, sql_type in (("owner_pid", "INTEGER"), ("owner_ctime", "REAL"), ("owner_fence", "INTEGER")):
            if name not in existing:
                db.execute("ALTER TABLE phase_outcomes ADD COLUMN " + name + " " + sql_type)
        _INITIALIZED.add(path)
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _compact(value, depth=0):
    if depth > 6:
        return None
    if isinstance(value, dict):
        result = {str(key)[:100]: _compact(item, depth + 1) for key, item in islice(value.items(), 128) if key not in _BODY_FIELDS}
    elif isinstance(value, (list, tuple)):
        result = [_compact(item, depth + 1) for item in value[:100]]
    elif isinstance(value, str):
        result = value[:1000]
    elif isinstance(value, float) and not math.isfinite(value):
        result = None
    elif value is None or isinstance(value, (int, float, bool)):
        result = value
    else:
        result = str(value)[:1000]
    if depth == 0 and len(_json(result).encode()) > 65536:
        return {"state": "summary_exceeded_metadata_budget", "reason": "Detailed result retained by its owning service."}
    return result


def _parent(tag, root):
    from . import versions
    if not isinstance(tag, str) or not re.fullmatch(r"v[1-9][0-9]*", tag):
        raise ValueError("invalid_immutable_parent_tag")
    name = "dataset_" + tag
    entry = versions.load_registry(_root(root))["frozen"].get(name)
    if not entry or entry.get("base") != name or not isinstance(entry.get("files"), dict):
        raise ValueError("registered_immutable_parent_required")
    raw = (_root(root) / name / "manifest.json").read_bytes()
    if len(raw) > 16 << 20 or hashlib.sha256(raw).hexdigest() != entry["files"].get("manifest.json"):
        raise ValueError("immutable_parent_manifest_hash_mismatch")
    manifest = json.loads(raw)
    if manifest.get("dataset_status") != "frozen" or manifest.get("tag") != tag:
        raise ValueError("registered_immutable_parent_required")
    return _digest({"tag": tag, "files": entry["files"]})


def _row(row):
    if row is None:
        return None
    return {**json.loads(row["body"]), **{key: row[key] for key in row.keys() if key != "body"},
            "cancel_requested": bool(row["cancel_requested"])}


def create(parent_tag, root=None, request_id=None):
    root = _root(root)
    parent_hash = _parent(parent_tag, root)
    if request_id is not None and (not isinstance(request_id, str) or not request_id or len(request_id) > 160):
        raise ValueError("invalid_workflow_request_id")
    fingerprint = _fingerprint(parent_tag, root)
    dedup = _digest({"parent": parent_hash, "source": fingerprint["source"], "policy": fingerprint["policy"]})
    with _connect(root) as db:
        db.execute("BEGIN IMMEDIATE")
        if request_id is not None:
            alias = db.execute("SELECT workflow_id,parent_hash FROM workflow_requests WHERE request_id=?", (request_id,)).fetchone()
            if alias:
                if alias["parent_hash"] != parent_hash:
                    raise ValueError("workflow_request_identity_conflict")
                return _row(db.execute("SELECT * FROM workflows WHERE id=?", (alias["workflow_id"],)).fetchone())
            found = db.execute("SELECT * FROM workflows WHERE request_id=?", (request_id,)).fetchone()
            if found:
                if found["parent_hash"] != parent_hash:
                    raise ValueError("workflow_request_identity_conflict")
                return _row(found)
        found = db.execute("SELECT * FROM workflows WHERE dedup_key=? AND state!='cancelled' ORDER BY created LIMIT 1", (dedup,)).fetchone()
        if found:
            if request_id is not None:
                db.execute("INSERT INTO workflow_requests VALUES (?,?,?)", (request_id, found["id"], parent_hash))
            return _row(found)
        identity = uuid.uuid4().hex
        body = {"schema": POLICY, "wave": 0, "initial_source": fingerprint["source"], "initial_policy": fingerprint["policy"],
                "dataset_tag": parent_tag, "shortages": [], "run_preparation": {"state": "pending_data_qualification"},
                "code": "queued", "reason": "Waiting for the qualification worker."}
        now = time.time()
        db.execute("INSERT INTO workflows(id,request_id,dedup_key,parent_tag,parent_hash,state,body,created,updated) VALUES (?,?,?,?,?,'queued',?,?,?)",
                   (identity, request_id, dedup, parent_tag, parent_hash, _json(body), now, now))
        if request_id is not None:
            db.execute("INSERT INTO workflow_requests VALUES (?,?,?)", (request_id, identity, parent_hash))
        return _row(db.execute("SELECT * FROM workflows WHERE id=?", (identity,)).fetchone())


def get(workflow_id, root=None):
    if not database_path(root).exists():
        return None
    db = sqlite3.connect(database_path(root).as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        db.row_factory = sqlite3.Row
        return _row(db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone())
    finally:
        db.close()


def _bounds(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
        raise ValueError("invalid_workflow_page_bounds")


def page(*, parent_tag=None, root=None, limit=50, offset=0):
    _bounds(limit, offset)
    if not database_path(root).exists():
        return {"items": [], "total": 0, "limit": limit, "offset": offset}
    with _connect(root) as db:
        db.execute("BEGIN")
        where, args = (" WHERE parent_tag=?", [parent_tag]) if parent_tag is not None else ("", [])
        total = db.execute("SELECT COUNT(*) FROM workflows" + where, args).fetchone()[0]
        items = [_row(row) for row in db.execute("SELECT * FROM workflows" + where + " ORDER BY created DESC,id LIMIT ? OFFSET ?", [*args, limit, offset])]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def phase_page(workflow_id, *, root=None, limit=50, offset=0):
    _bounds(limit, offset)
    if not database_path(root).exists():
        return {"items": [], "total": 0, "limit": limit, "offset": offset}
    with _connect(root) as db:
        db.execute("BEGIN")
        total = db.execute("SELECT COUNT(*) FROM phase_outcomes WHERE workflow_id=?", (workflow_id,)).fetchone()[0]
        rows = db.execute("SELECT * FROM phase_outcomes WHERE workflow_id=? ORDER BY started,id LIMIT ? OFFSET ?", (workflow_id, limit, offset))
        items = [{**dict(row), "result": json.loads(row["result"])} for row in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def cancel(workflow_id, *, root=None):
    with _connect(root) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if not row:
            raise KeyError(workflow_id)
        body = {**json.loads(row["body"]), "code": "cancelled", "reason": "Qualification work was cancelled; completed immutable artifacts are retained."}
        db.execute("UPDATE workflows SET cancel_requested=1,fence=fence+1,state=?,body=?,updated=? WHERE id=?",
                   ("cancelling" if row["owner_token"] else "cancelled", _json(body), time.time(), workflow_id))
    from . import evidence_recovery_history
    evidence_recovery_history.invalidate_workflow(workflow_id, root=root, state='cancelled', reason='Qualification work was cancelled.')
    return get(workflow_id, root)


class Waiting(RuntimeError):
    def __init__(self, code, reason, **metadata):
        super().__init__(reason)
        self.code, self.metadata = code, metadata


class _Cancelled(InterruptedError):
    pass


class _Fenced(InterruptedError):
    pass


def _resource_code(error):
    from .verify.resource_budget import ResourceWait
    if isinstance(error, ResourceWait):
        return error.code
    if isinstance(error, MemoryError) or isinstance(error, OSError) and error.errno == errno.ENOMEM:
        return "workflow_memory_unavailable"
    if isinstance(error, OSError) and error.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
        return "workflow_disk_unavailable"
    if isinstance(error, PermissionError):
        return "workflow_storage_unavailable"
    if isinstance(error, OSError) and error.errno in (errno.EMFILE, errno.ENFILE, errno.EAGAIN, errno.EBUSY):
        return "workflow_resource_unavailable"
    return None


def _process_identity():
    import psutil
    return os.getpid(), psutil.Process().create_time()


def _owner_alive(pid, created):
    import psutil
    try:
        return pid is not None and abs(psutil.Process(pid).create_time() - created) < .01
    except (psutil.Error, TypeError):
        return False


def recover_pending(*, root=None, limit=100, offset=0):
    """Startup reconciliation only. Return resumable metadata; never start jobs."""
    _bounds(limit, offset)
    if not database_path(root).exists():
        return {"items": [], "total": 0, "limit": limit, "offset": offset}
    lock = oslock.FileLock(lock_path(root), note=False)
    try:
        lock.acquire(wait=0)
    except oslock.Busy:
        return {"items": [], "total": 0, "limit": limit, "offset": offset, "owner_busy": True}
    try:
        with _connect(root) as db:
            db.execute("BEGIN IMMEDIATE")
            # A free OS lock proves no executing owner, even if a worker thread
            # ended inside a still-live server process. PID+ctime stay auditable.
            for row in db.execute("SELECT * FROM workflows WHERE owner_token IS NOT NULL OR state IN ('running','cancelling')"):
                body = {**json.loads(row["body"]), "code": "owner_interrupted", "reason": "The previous execution owner no longer holds the workflow lock.",
                        "previous_owner": {"pid": row["owner_pid"], "ctime": row["owner_ctime"], "alive": _owner_alive(row["owner_pid"], row["owner_ctime"])}}
                db.execute("UPDATE workflows SET state=?,fence=fence+1,owner_pid=NULL,owner_ctime=NULL,owner_token=NULL,body=?,updated=? WHERE id=?",
                           ("cancelled" if row["cancel_requested"] else "interrupted", _json(body), time.time(), row["id"]))
                db.execute("UPDATE phase_outcomes SET state='interrupted',finished=? WHERE workflow_id=? AND state='running'", (time.time(), row["id"]))
            where = "state IN ('queued','interrupted','waiting') AND cancel_requested=0"
            total = db.execute("SELECT COUNT(*) FROM workflows WHERE " + where).fetchone()[0]
            rows = db.execute("SELECT * FROM workflows WHERE " + where + " ORDER BY created,id LIMIT ? OFFSET ?", (limit, offset))
            return {"items": [_row(row) for row in rows], "total": total, "limit": limit, "offset": offset}
    finally:
        lock.release()


def execute(workflow_id, root=None, progress=None, cancelled=None, services=None):
    """Run qualification under the shared verification resource ledger."""
    if services is not None and getattr(services, 'resource_scope', False) is not True:
        return _execute(workflow_id, root=root, progress=progress, cancelled=cancelled, services=services)
    from .verify import resource_budget
    # Scope telemetry never emits workflow updates: publication guards hold the
    # workflow journal transaction while checking cancellation and ownership.
    with resource_budget.scope('qualification:' + workflow_id):
        return _execute(workflow_id, root=root, progress=progress, cancelled=cancelled, services=services)


def _execute(workflow_id, root=None, progress=None, cancelled=None, services=None):
    """Execute available bounded waves synchronously; never dispatch child jobs.

    services may replace the default service object for controlled integration.
    progress receives keyword metadata; cancelled is a callable or Event. Run
    preparation is optional, keyed, and must itself be transactionally idempotent.
    """
    root = _root(root)
    original = get(workflow_id, root)
    if original is None:
        raise KeyError(workflow_id)
    if original["cancel_requested"]:
        return original
    services = services or Services()
    if original.get("next_retry_at", 0) > time.time():
        pending = original.get('pending_verification') or {}
        if (not pending or original.get('code') != pending.get('code')
                or services.fingerprint(original['parent_tag'], root)['capability'] == pending.get('capability')):
            return original
    lock = oslock.FileLock(lock_path(root), note=False)
    try:
        lock.acquire(wait=0)
    except oslock.Busy:
        with _connect(root) as db:
            row = db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if not row["owner_token"]:
                body = {**json.loads(row["body"]), "code": "workflow_owner_busy", "reason": "Another qualification worker owns this source root."}
                db.execute("UPDATE workflows SET state='waiting',body=?,updated=? WHERE id=?", (_json(body), time.time(), workflow_id))
        return get(workflow_id, root)
    token = uuid.uuid4().hex
    try:
        with _connect(root) as db:
            db.execute("BEGIN IMMEDIATE")
            current = _row(db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone())
            if current["cancel_requested"]:
                return current
            pid, ctime = _process_identity()
            fence = current["fence"] + 1
            db.execute("UPDATE workflows SET state='running',fence=?,owner_pid=?,owner_ctime=?,owner_token=?,updated=? WHERE id=?",
                       (fence, pid, ctime, token, time.time(), workflow_id))

        next_resource_check = [0.0]
        def check():
            row = get(workflow_id, root)
            stopped = cancelled is not None and (cancelled() if callable(cancelled) else cancelled.is_set())
            if row["cancel_requested"] or stopped:
                raise _Cancelled("Qualification work was cancelled.")
            if row["fence"] != fence or row["owner_token"] != token:
                raise _Fenced("Qualification execution owner was replaced.")
            from .verify.resource_budget import current
            scoped = current()
            now = time.monotonic()
            if scoped is not None and now >= next_resource_check[0]:
                scoped.check(require_runtime=False)
                next_resource_check[0] = now + .5

        def check_parent():
            check()
            if _parent(current["parent_tag"], root) != current["parent_hash"]:
                raise ValueError("immutable_parent_identity_changed")

        def stopped():
            try:
                check()
                return False
            except (_Cancelled, _Fenced):
                return True

        def update(*, state="running", validate=True, **fields):
            if validate:
                check()
            with _connect(root) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT body FROM workflows WHERE id=? AND fence=? AND owner_token=?", (workflow_id, fence, token)).fetchone()
                if not row:
                    raise _Fenced("Qualification execution fence changed.")
                body = {**json.loads(row["body"]), **_compact(fields)}
                db.execute("UPDATE workflows SET state=?,body=?,updated=? WHERE id=? AND fence=? AND owner_token=?",
                           (state, _json(body), time.time(), workflow_id, fence, token))
            if progress is not None and validate:
                progress(**_compact({"state": state, **fields, "workflow_id": workflow_id}))

        def emit(**fields):
            update(phase_progress=_compact(fields))

        def fingerprint():
            value = services.fingerprint(current["parent_tag"], root)
            if not isinstance(value, dict) or not all(isinstance(value.get(key), str) and value[key] for key in ("source", "policy", "frontier", "capability")):
                raise ValueError("invalid_workflow_fingerprint")
            return value

        def phase_key(name, value, semantic):
            if name in ("build", "prepare_run"):
                return _digest({"work_key": semantic})
            if name == "repair":
                return _digest({"source": value.get("repair_input", value["source"]), "policy": value["policy"], "artifacts": value.get("artifacts")})
            if name == "verify":
                return _digest({"source": value.get("verification_input", value["source"]), "policy": value["policy"], "artifacts": value.get("artifacts"), "capability": value["capability"]})
            if name == "plan_sources":
                return _digest({"parent": current["parent_hash"], "policy": value["policy"], "qualification": semantic,
                                "candidates": value.get("candidates"), "discovery": value.get("discovery_input")})
            if name == "data_preflight":
                return _digest({"source": value["source"], "policy": value["policy"], "dataset": semantic})
            return _digest({"source": value["source"], "policy": value["policy"], "frontier": value["frontier"]})

        def phase(name, wave, action, *, cache=True, semantic=None):
            check_parent()
            before = fingerprint()
            key = phase_key(name, before, semantic)
            with _connect(root) as db:
                where_wave = " AND wave=?" if name == "acquire_sources" else ""
                args = [workflow_id, name, key, *([wave] if where_wave else [])]
                saved = db.execute("SELECT result FROM phase_outcomes WHERE workflow_id=? AND phase=? AND fingerprint=?" + where_wave + " AND state='done' ORDER BY finished DESC LIMIT 1", args).fetchone()
            if cache and saved and json.loads(saved[0]).get('cacheable', True):
                update(phase=name, wave=wave, code=name, phase_progress={"label": _PHASE_LABELS[name], "resumed": True})
                return json.loads(saved[0])
            attempt = uuid.uuid4().hex
            update(phase=name, wave=wave, code=name, next_retry_at=0, phase_progress={"label": _PHASE_LABELS[name]})
            with _connect(root) as db:
                db.execute("INSERT INTO phase_outcomes(id,workflow_id,wave,phase,fingerprint,state,result,started,owner_pid,owner_ctime,owner_fence) VALUES (?,?,?,?,?,'running','{}',?,?,?,?)",
                           (attempt, workflow_id, wave, name, key, time.time(), pid, ctime, fence))
            result = {}
            try:
                result = action(attempt)
                check()
                result = _compact(result or {})
                after = fingerprint()
                after_key = phase_key(name, after, semantic)
                waiting = result.get("state") == "waiting" or result.get("status") == "waiting"
                if result.get("cancelled") or result.get("state") == "cancelled":
                    raise _Cancelled("Service operation was cancelled.")
                if waiting:
                    if name == 'acquire_sources' and result.get('transfer_complete') is True:
                        # Only discovery is unavailable. A later configured
                        # snapshot must resume planning, not an empty transfer.
                        update(acquisition_pending=False)
                    raise Waiting(result.get("code") or result.get("waiting_code") or name + "_waiting", result.get("reason") or "Waiting for an execution capability.",
                                  **{key: result[key] for key in ("next_retry_at", "retry_after_seconds", "resources") if key in result})
                if result.get("state") in ("failed", "error"):
                    raise RuntimeError(result.get("reason") or result.get("error") or name + "_failed")
                with _connect(root) as db:
                    db.execute("UPDATE phase_outcomes SET fingerprint=?,state='done',result=?,finished=? WHERE id=?", (after_key, _json(result), time.time(), attempt))
                return result
            except BaseException as error:
                resource = _resource_code(error)
                state = "cancelled" if isinstance(error, _Cancelled) else "waiting" if isinstance(error, Waiting) or resource else "interrupted" if isinstance(error, (InterruptedError, KeyboardInterrupt, SystemExit)) else "failed"
                outcome = {**_compact(result if isinstance(result, dict) else {}), "code": error.code if isinstance(error, Waiting) else resource or state,
                           "reason": str(error)[:1000]}
                if getattr(error, 'resources', None) is not None:
                    outcome['resources'] = _compact(error.resources)
                with _connect(root) as db:
                    db.execute("UPDATE phase_outcomes SET state=?,result=?,finished=? WHERE id=?", (state, _json(outcome), time.time(), attempt))
                raise

        def bound_check(expected):
            check_parent()
            actual = fingerprint()
            if any(actual[key] != expected[key] for key in ("source", "policy")):
                raise Waiting("derived_inputs_changed", "Evidence or policy changed during this phase; resume against its current fingerprint.")

        @contextmanager
        def publication_context(expected=None):
            # The publisher enters this under its filesystem/version lock. The
            # same SQLite write lock serializes cancel/fence commits until the
            # final rename and registry commit have either completed or failed.
            # No emit/update may run inside this context.
            with _connect(root) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
                external_stop = cancelled is not None and (cancelled() if callable(cancelled) else cancelled.is_set())
                if row["cancel_requested"] or external_stop:
                    raise _Cancelled("Cancellation committed before immutable publication.")
                if (row["fence"] != fence or row["owner_token"] != token or row["owner_pid"] != pid or row["owner_ctime"] != ctime):
                    raise _Fenced("Publication owner fence changed.")
                bound_check(expected) if expected is not None else check_parent()
                if cancelled is not None and (cancelled() if callable(cancelled) else cancelled.is_set()):
                    raise _Cancelled("Cancellation requested before immutable publication.")
                yield

        check.publication_guard = lambda: publication_context()
        stopped.publication_guard = check.publication_guard
        stopped.workflow_id = workflow_id

        def guarded(expected):
            def guard():
                bound_check(expected)
            guard.publication_guard = lambda: publication_context(expected)
            return guard

        def build(work_key, expected):
            guard = guarded(expected)
            guard()
            with _connect(root) as db:
                row = db.execute("SELECT * FROM workflow_builds WHERE work_key=?", (work_key,)).fetchone()
                if not row:
                    db.execute("INSERT INTO workflow_builds(work_key,workflow_id,state) VALUES (?,?,'creating')", (work_key, workflow_id))
            known = services.get_build(row["build_id"], root=root) if row and row["build_id"] else services.find_build(work_key, root=root)
            if known is None:
                known = services.create_build(current["parent_tag"], root=root, work_key=work_key, check=guard)
            guard()
            if not isinstance(known, dict) or not known.get("id"):
                raise RuntimeError("build_creation_did_not_return_identity")
            with _connect(root) as db:
                db.execute("UPDATE workflow_builds SET build_id=?,state=?,result=? WHERE work_key=?", (known["id"], known.get("state", "queued"), _json(_compact(known)), work_key))
            if known.get("status") != "frozen":
                if known.get("state") in ("failed", "interrupted"):
                    resume = getattr(services, "resume_build", None)
                    if resume is not None:
                        known = resume(known["id"], root=root, check=guard)
                        guard()
                if known.get("state") not in ("queued", "building"):
                    raise Waiting("build_resume_required", "The recorded build requires recovery before execution; its request was retained.", build_id=known["id"])
                known = services.execute_build(known["id"], root=root, progress=emit, check=guard)
            guard()
            if known.get("status") != "frozen" or not known.get("tag"):
                raise RuntimeError("build_did_not_publish_frozen_dataset")
            with _connect(root) as db:
                db.execute("UPDATE workflow_builds SET state='published',result=? WHERE work_key=?", (_json(_compact(known)), work_key))
            update(dataset_tag=known["tag"], last_build_key=work_key)
            return known["tag"]

        def acquire_wave(number):
            update(acquisition_pending=True)
            def acquire(attempt):
                kwargs = {"root": root, "cancelled": stopped}
                parameters = inspect.signature(services.acquire).parameters
                if "progress" in parameters or any(value.kind == inspect.Parameter.VAR_KEYWORD for value in parameters.values()):
                    kwargs["progress"] = emit
                return services.acquire(**kwargs)
            result = phase("acquire_sources", number, acquire)
            if not result.get("attempted") and not result.get("acquired") and result.get("exhausted") is not True:
                raise Waiting("source_frontier_no_progress", "Source work remains but no task was eligible; waiting for its retry capability.")
            update(acquisition_pending=False)
            return result

        check_parent()
        wave = current.get("wave", 0)
        gate_work_key = None
        pending_verification = current.get('pending_verification')

        def handoff_memory_wait():
            # Managed roots already have a single-owner retry scheduler.
            # Exit this analysis worker before retaining its heap in build.
            if (pending_verification and pending_verification.get('code') == 'verification_memory_waiting'
                    and root == _root()):
                raise Waiting(pending_verification['code'], pending_verification['reason'],
                    next_retry_at=pending_verification['next_retry_at'],
                    pending_verification=pending_verification,
                    **{key: pending_verification[key] for key in ('resources', 'retry_after_seconds') if key in pending_verification})

        def verify_available():
            nonlocal pending_verification
            capability = fingerprint()['capability']
            if (pending_verification and pending_verification.get('capability') == capability
                    and pending_verification.get('next_retry_at', 0) > time.time()):
                handoff_memory_wait()
                return
            try:
                phase("verify", wave, lambda attempt: services.verify(current["parent_tag"], root=root, progress=emit, cancelled=stopped, attempt_id=attempt))
            except Exception as error:
                resource = _resource_code(error)
                if not isinstance(error, Waiting) and resource is None:
                    raise
                metadata = error.metadata if isinstance(error, Waiting) else {}
                pending_verification = {'code': error.code if isinstance(error, Waiting) else resource,
                    'reason': str(error), 'capability': fingerprint()['capability'],
                    'next_retry_at': metadata.get('next_retry_at') or time.time() + 60,
                    **{key: metadata[key] for key in ('resources', 'retry_after_seconds') if key in metadata}}
                if getattr(error, 'resources', None) is not None:
                    pending_verification['resources'] = _compact(error.resources)
                update(pending_verification=pending_verification)
                handoff_memory_wait()
            else:
                if pending_verification:
                    pending_verification = None
                    update(pending_verification=None)

        def exhausted_frontier(frontier, reason):
            latest = get(workflow_id, root)
            preparation = latest.get('run_preparation') if (latest.get('token_budget_audit') or {}).get('state') == 'blocked' else {'state': 'pending_data_qualification'}
            if pending_verification:
                update(state='waiting', code=pending_verification['code'], reason=pending_verification['reason'],
                    next_retry_at=pending_verification['next_retry_at'], pending_verification=pending_verification,
                    source_frontier=frontier, run_preparation=preparation)
            else:
                update(state='blocked', code='configured_source_frontier_exhausted', reason=reason,
                    source_frontier=frontier, run_preparation=preparation)
        # A quota boundary inside an acquisition wave resumes that wave before
        # any full-corpus scans or publication of its incomplete transfer prefix.
        if current.get("acquisition_pending"):
            acquired = acquire_wave(wave)
            wave += 1
            update(wave=wave, source_frontier=acquired)
        while True:
            phase("repair", wave, lambda attempt: services.repair(current["parent_tag"], root=root, progress=emit, cancelled=stopped, attempt_id=attempt))
            verify_available()
            active = fingerprint()
            work_key = _digest({"parent": current["parent_hash"], "source": active["source"], "policy": active["policy"]})
            latest = get(workflow_id, root)
            baseline = active.get("parent_current") is True and active["source"] == current["initial_source"] and active["policy"] == current["initial_policy"]
            tag = latest.get("dataset_tag", current["parent_tag"])
            if latest.get("last_build_key") != work_key and not baseline:
                built = phase("build", wave, lambda attempt: {"tag": build(work_key, active)}, semantic=work_key)
                tag = built["tag"]
            # Readiness is rechecked, including the published file hashes, even
            # when evidence processing and an earlier gate can be resumed.
            gate = phase("data_preflight", wave, lambda attempt: services.preflight(tag, root=root), cache=gate_work_key == work_key, semantic=tag)
            gate_work_key = work_key
            qualification = gate.get("data_qualification") or {}
            shortages = gate.get("blockers") or []
            update(dataset_tag=tag, qualification=qualification, raw_qualification=qualification,
                   token_qualification=None, token_budget_audit=None, shortages=shortages,
                   input_fingerprint=active["source"], policy_fingerprint=active["policy"])
            if gate.get("allowed") is True and gate.get("qualification_policy") == "qualified-npm/1" and qualification.get("qualified") is True:
                preparer = getattr(services, "prepare_run", None)
                if preparer is None:
                    update(state="qualified", code="data_qualified", reason="Operational data qualification passed; transactional run preparation is pending.",
                           run_preparation={"state": "pending_prepare_run_callback"})
                    break
                else:
                    def prepare(attempt):
                        with _connect(root) as db:
                            prepared = db.execute("SELECT state,result FROM workflow_runs WHERE work_key=?", (work_key,)).fetchone()
                            if not prepared:
                                db.execute("INSERT INTO workflow_runs(work_key,state) VALUES (?,'preparing')", (work_key,))
                        bound_check(active)
                        update(run_preparation={"state": "preparing", "work_key": work_key})
                        result = _compact(preparer(tag, root=root, work_key=work_key, check=guarded(active)))
                        bound_check(active)
                        if result.get("state") == "waiting":
                            raise Waiting(result.get("code") or "run_preparation_waiting", result.get("reason") or "Run preparation is waiting for a capability.",
                                          **{key: result[key] for key in ("next_retry_at", "retry_after_seconds") if key in result})
                        if result.get('state') == 'data_unqualified':
                            from .qualified_token_audit import validate_shortage
                            if (result.get('code') != 'token_budget_qualification_shortage' or result.get('training_started') is not False
                                    or not isinstance(result.get('run_id'), str) or not result['run_id']):
                                raise ValueError('invalid_token_qualification_shortage')
                            retained = validate_shortage({**(result.get('token_budget_audit') or {}),
                                'retained_qualification': result.get('qualification'), 'blockers': result.get('blockers')}, maximum=qualification)
                            result['qualification'] = retained
                            with _connect(root) as db:
                                db.execute("UPDATE workflow_runs SET state='data_unqualified',result=? WHERE work_key=?", (_json(result), work_key))
                            return result
                        if result.get("state") != "prepared" or not isinstance(result.get("run_id"), str) or not result["run_id"]:
                            raise ValueError("run_preparation_requires_transactional_run_identity")
                        with _connect(root) as db:
                            db.execute("UPDATE workflow_runs SET state='prepared',result=? WHERE work_key=?", (_json(result), work_key))
                        return result
                    # The idempotent preparer verifies its existing immutable
                    # bundle, notebook and audits before returning the same run.
                    result = phase("prepare_run", wave, prepare, semantic=work_key, cache=False)
                    if result.get('state') == 'data_unqualified':
                        qualification, shortages = result['qualification'], result['blockers']
                        update(qualification=qualification, token_qualification=qualification, shortages=shortages,
                            token_budget_audit=result['token_budget_audit'], code='token_budget_qualification_shortage',
                            reason='Usable encoded evidence has qualification shortages; continuing targeted source acquisition.',
                            run_preparation={'state': 'pending_token_qualification', 'run_id': result['run_id'],
                                'request_id': result.get('request_id'), 'published': False})
                    else:
                        update(state="completed", phase="prepare_run", code="run_prepared", reason="Qualified data passed preflight and the next run was prepared. Training has not started.", run_preparation=result)
                        break
            planned = phase("plan_sources", wave, lambda attempt: services.plan(current["parent_tag"], root=root, qualification=qualification, cancelled=stopped), semantic=qualification)
            queue = getattr(services, "queue_summary", None)
            if queue is not None:
                planned = {**planned, **queue(root=root)}
            if planned.get("exhausted") is True and not planned.get("remaining"):
                exhausted_frontier(planned, 'Operational qualification shortages remain after the configured source queue was exhausted.')
                break
            acquired = acquire_wave(wave)
            if not acquired.get("attempted") and not acquired.get("acquired"):
                if acquired.get("exhausted") is True:
                    exhausted_frontier(acquired, 'Configured source work is exhausted; qualification shortages remain.')
                    break
            wave += 1
            update(wave=wave, source_frontier=acquired)
    except _Cancelled as error:
        with _connect(root) as db:
            row = db.execute("SELECT * FROM workflows WHERE id=? AND owner_token=?", (workflow_id, token)).fetchone()
            if row:
                body = {**json.loads(row["body"]), "code": "cancelled", "reason": str(error)}
                db.execute("UPDATE workflows SET state='cancelled',fence=fence+CASE WHEN cancel_requested=0 THEN 1 ELSE 0 END,cancel_requested=1,body=?,updated=? WHERE id=? AND owner_token=?", (_json(body), time.time(), workflow_id, token))
    except _Fenced as error:
        with _connect(root) as db:
            row = db.execute("SELECT body FROM workflows WHERE id=? AND owner_token=?", (workflow_id, token)).fetchone()
            if row:
                body = {**json.loads(row["body"]), "code": "workflow_fenced", "reason": str(error)}
                db.execute("UPDATE workflows SET state='interrupted',body=?,updated=? WHERE id=? AND owner_token=?", (_json(body), time.time(), workflow_id, token))
    except Waiting as error:
        update(state="waiting", code=error.code, reason=str(error), **error.metadata)
    except InterruptedError as error:
        update(state="interrupted", validate=False, code="workflow_interrupted", reason=str(error))
    except Exception as error:
        resource = _resource_code(error)
        metadata = {"resources": _compact(error.resources), "next_retry_at": time.time() + 60} if getattr(error, 'resources', None) is not None else {}
        update(state="waiting" if resource else "failed", validate=False, code=resource or "workflow_failed", reason=type(error).__name__ + ": " + str(error)[:1000], **metadata)
    except BaseException as error:
        update(state="interrupted", validate=False, code="workflow_interrupted", reason=type(error).__name__)
        raise
    finally:
        with _connect(root) as db:
            db.execute("UPDATE workflows SET owner_pid=NULL,owner_ctime=NULL,owner_token=NULL,updated=? WHERE id=? AND owner_token=?", (time.time(), workflow_id, token))
        lock.release()
    return get(workflow_id, root)


def _discovery_identity(parent_tag, root, *, parent_files=None):
    from . import versions, evidence_recovery, automatic_evidence, npm_case_collector
    if parent_files is None:
        parent_files = (versions.load_registry(_root(root))['frozen'].get('dataset_' + parent_tag) or {}).get('files')
        if not isinstance(parent_files, dict):
            raise ValueError('registered_parent_required_for_discovery_scope')
    return _digest({'parent': parent_tag, 'files': parent_files, 'index_policy': 'current-validated-repair-pairs/2',
        'admission': evidence_recovery._implementation_fingerprint(), 'source_rules': automatic_evidence._registry(),
        'collector_policy': npm_case_collector.POLICY,
        'collector_sha256': hashlib.sha256(Path(npm_case_collector.__file__).read_bytes()).hexdigest()})


def _discovery_summary(root, *, parent_identity=None):
    from . import npm_case_collector as collector
    configured = collector.configured_snapshot(root=root)
    summary = {'index_identity': parent_identity, 'position': 0, 'total': None, 'exhausted': False}
    if configured.get('state') == 'ready' and parent_identity is not None:
        summary = collector.discovery_summary(root=root, parent_identity=parent_identity, dump_sha256=configured['sha256'])
    return {key: configured[key] for key in ('state', 'code', 'reason', 'sha256', 'descriptor') if key in configured} | summary


def _complete_parent_pairs(records):
    """Only a validated admitted pair makes a historical fix fully collected."""
    from . import automatic_evidence, security_quality
    for row in records:
        if row.get('admission_outcome') != 'admitted' or row.get('label_state') != 'vulnerability_supported':
            continue
        receipt = row.get('admission_provenance')
        if isinstance(receipt, str):
            try:
                receipt = json.loads(receipt)
            except ValueError:
                continue
        if (isinstance(receipt, dict) and receipt.get('repair_valid') is True
                and not security_quality.admission_problems(row, admission_policy=row.get('admission_policy') or automatic_evidence.CURRENT_POLICY)
                and not security_quality.admission_problems(row, fixed_twin=True, admission_policy=row.get('admission_policy') or automatic_evidence.CURRENT_POLICY)):
            yield row


def _queue_summary(root, *, parent_identity=None):
    from . import npm_evidence_sources as sources, npm_case_collector as collector
    report = sources.page(root=root, limit=1)
    now = time.time()
    with sources._connect(root) as db:
        retryable = "(state='failed' AND (SELECT COUNT(*) FROM source_attempts a WHERE a.source_id=w.source_id AND a.fingerprint=w.input_fingerprint AND " + sources._GENUINE_FAILURE + ")<3)"
        remaining = db.execute("SELECT COUNT(*) FROM source_work w WHERE state IN ('pending','running','waiting') OR " + retryable,
                               sources._LEGACY_WAVE_ERRORS).fetchone()[0]
        eligible = db.execute("SELECT COUNT(*) FROM source_work w WHERE state='pending' OR (state='waiting' AND COALESCE(next_retry_at,0)<=?) OR (state='running' AND lease_until<?) OR " + retryable,
                              (now, now, *sources._LEGACY_WAVE_ERRORS)).fetchone()[0]
        future = db.execute("SELECT MIN(next_retry_at) FROM source_work WHERE state='waiting' AND next_retry_at>?", (now,)).fetchone()[0]
    source = {key: value for key, value in report.items() if key not in ('items', 'limit', 'offset')} | {
        'remaining': remaining, 'eligible': 0 if future else eligible, 'exhausted': remaining == 0}
    cases, discovery = collector.queue_summary(root=root), _discovery_summary(root, parent_identity=parent_identity)
    waits = [value for value in (future, cases.get('next_retry_at')) if value and value > now]
    return {'sources': source, 'cases': cases, 'discovery': discovery,
        'remaining': remaining + cases['remaining'], 'eligible': source['eligible'] + cases['eligible'],
        'total': source['total'] + cases['total'], 'states': {'sources': source['states'], 'cases': cases['states']},
        'exhausted': remaining == 0 and cases['exhausted'] and discovery['exhausted'],
        **({'next_retry_at': min(waits)} if waits else {})}


def _repair_digest(root):
    hasher = hashlib.sha256()
    path = _root(root) / ".evidence_recovery.sqlite3"
    if path.exists():
        with sqlite3.connect(path) as db:
            for row in db.execute("SELECT repair_id FROM repairs ORDER BY repair_id"):
                hasher.update((_json(row[0]) + "\n").encode())
    return hasher.hexdigest()


def _stream_evidence_fingerprint(root):
    """Match recovery's canonical identity without loading all proof bodies.

    Only individual authenticated receipts are resident. Their 64-byte hashes
    sort in a disposable SQLite spool, never in a full in-memory receipt list.
    """
    from . import evidence_recovery, validation_exposure
    from .verify import proof_ledger
    directory = _root(root) / ".npm_qualification"
    directory.mkdir(parents=True, exist_ok=True)
    spool_path = directory / ("fingerprint-" + uuid.uuid4().hex + ".sqlite3")
    spool = sqlite3.connect(spool_path)
    try:
        spool.execute("PRAGMA journal_mode=MEMORY")
        spool.execute("CREATE TABLE digests (value TEXT NOT NULL)")
        ledger = proof_ledger.default_path()
        key_path = ledger.with_suffix(ledger.suffix + ".key")
        if ledger.is_file() and key_path.is_file():
            key = key_path.read_bytes()
            if len(key) == 32:
                try:
                    with sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True) as db:
                        for run_id, raw, mac in db.execute("SELECT run_id,receipt,mac FROM execution_runs WHERE state='completed'"):
                            try:
                                if not hmac.compare_digest(mac or "", hmac.new(key, raw.encode(), hashlib.sha256).hexdigest()):
                                    continue
                                body = json.loads(raw)
                                if (body.get("run_id") != run_id or body.get("verdict") != "verified" or "binding" not in body
                                        or body.get("receipt_id") != proof_ledger.digest({name: value for name, value in body.items() if name != "receipt_id"})):
                                    continue
                                value = {"binding": body.get("binding"), "facts": body.get("facts"), "observations": body.get("observations"),
                                         "schema": body.get("schema"), "verdict": body.get("verdict")}
                                spool.execute("INSERT INTO digests VALUES (?)", (_digest(value),))
                            except (AttributeError, TypeError, ValueError, KeyError):
                                continue
                except sqlite3.Error:
                    pass
        fixed = {**evidence_recovery._implementation_fingerprint(),
                 "validator_development": {"version": validation_exposure.VERSION, "records": sorted(validation_exposure.RECORD_IDS)},
                 "proofs": None, "repairs": None}
        hasher = hashlib.sha256()
        hasher.update(b"{")
        for index, name in enumerate(sorted(fixed)):
            if index:
                hasher.update(b",")
            hasher.update((_json(name) + ":").encode())
            if name in ("proofs", "repairs"):
                hasher.update(b"[")
                if name == "proofs":
                    rows = spool.execute("SELECT value FROM digests ORDER BY value")
                    for ordinal, row in enumerate(rows):
                        hasher.update((("," if ordinal else "") + _json(row[0])).encode())
                else:
                    path = _root(root) / ".evidence_recovery.sqlite3"
                    if path.is_file():
                        with sqlite3.connect(path) as db:
                            for ordinal, row in enumerate(db.execute("SELECT repair_id FROM repairs ORDER BY repair_id")):
                                hasher.update((("," if ordinal else "") + _json(row[0])).encode())
                hasher.update(b"]")
            else:
                hasher.update(_json(fixed[name]).encode())
        hasher.update(b"}")
        return hasher.hexdigest()
    finally:
        spool.close()
        spool_path.unlink(missing_ok=True)


def _fingerprint(parent_tag, root, *, parent_identity=None):
    from . import automatic_evidence, build_catalog, data_qualification, evidence_recovery, npm_evidence_sources
    from . import npm_case_collector, appended_evidence, finetune_qualified_run, qualified_token_audit
    from .verify import runtime
    from training.finetune import quality_gate
    from training.finetune.quality_gate import QUALIFICATION_REQUIREMENTS
    evidence = _stream_evidence_fingerprint(root)
    policy = _digest({"workflow": POLICY, "workflow_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "implementation": evidence_recovery._implementation_fingerprint(),
                      "profile": build_catalog.definition("js_ts_security"), "sources": npm_evidence_sources.POLICY,
                      "source_catalog": npm_evidence_sources.source_catalog(), "source_rules": automatic_evidence._registry(),
                      "modules": {module.__name__: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                                  for module in (npm_evidence_sources, data_qualification, quality_gate, npm_case_collector,
                                                 appended_evidence, finetune_qualified_run, qualified_token_audit)},
                      "qualification": QUALIFICATION_REQUIREMENTS})
    hasher, acquired = hashlib.sha256(), hashlib.sha256()
    path = _root(root) / ".npm_evidence_sources.sqlite3"
    if path.exists():
        with sqlite3.connect(path) as db:
            for row in db.execute("SELECT source_id,input_fingerprint,state,attempt_count,artifact_sha256 FROM source_work ORDER BY source_id"):
                hasher.update((_json(list(row)) + "\n").encode())
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='exact_source_files'").fetchone():
                for row in db.execute("SELECT repository,revision,path,content_sha256 FROM exact_source_files ORDER BY repository,revision,path"):
                    acquired.update((_json(list(row)) + "\n").encode())
    candidates = npm_case_collector.capture(root=root)
    candidate_hash = candidates['sha256']
    parent_identity = parent_identity or _discovery_identity(parent_tag, root)
    discovery = _discovery_summary(root, parent_identity=parent_identity)
    hasher.update(_json(discovery).encode())
    with npm_case_collector._connect(root) as db:
        for row in db.execute('SELECT id,state,failure_count,next_retry_at,shard_sha,artifact_sha FROM work ORDER BY id'):
            hasher.update((_json(list(row)) + '\n').encode())
    source = _digest({'evidence': evidence, 'candidates': candidate_hash})
    parent_row = build_catalog.get(parent_tag, root=root) or {}
    cap = runtime.status()
    return {"source": source, "policy": policy, "frontier": hasher.hexdigest(), "candidates": candidate_hash,
            "discovery_input": _digest({key: discovery.get(key) for key in ('sha256', 'descriptor', 'state', 'code', 'index_identity')}),
            "artifacts": _digest({'files': acquired.hexdigest(), 'candidates': candidate_hash}),
            "repair_input": _digest({'repairs': _repair_digest(root), 'candidates': candidate_hash}), "verification_input": source,
            "capability": _digest({key: cap.get(key) for key in ("runtime_id", "image_id", "state", "code")}),
            "parent_current": not candidates.get('shards') and parent_row.get("evidence_fingerprint") == evidence and parent_row.get("admission_policy") == automatic_evidence.CURRENT_POLICY
                              and parent_row.get("profile_version") == build_catalog.PROFILE_VERSION}


class Services:
    """Default adapters. All source bodies are consumed through record streams."""
    prepare_run = None
    resource_scope = True

    def fingerprint(self, tag, root):
        self._discovery_parent_identity = _discovery_identity(tag, root)
        return _fingerprint(tag, root, parent_identity=self._discovery_parent_identity)

    def queue_summary(self, *, root):
        identity = getattr(self, '_discovery_parent_identity', None)
        return _queue_summary(root, parent_identity=identity) if identity is not None else _queue_summary(root)

    def repair(self, tag, **kwargs):
        from . import evidence_recovery, frozen_evidence_build, npm_extraction_replay, oslock
        stop = kwargs.get('cancelled') or (lambda:False)
        totals = {'cases':0, 'new_units':0, 'unsupported':0}
        while not stop():
            try:
                replay = npm_extraction_replay.execute(root=kwargs.get('root'), limit=25, cancelled=stop)
            except oslock.Busy as error:
                raise Waiting('source_extraction_owner_busy', 'The existing source acquisition owner must release its lock before extraction replay.') from error
            completed = replay['replayed'] + replay.get('unsupported',0)
            totals['cases'] += completed
            totals['new_units'] += replay['additional_records']
            totals['unsupported'] += replay.get('unsupported',0)
            if kwargs.get('progress'):
                kwargs['progress'](state='recovering_units', label='Recovering complete units from saved exact snapshots',
                    processed=totals['cases'], total=totals['cases'] + replay.get('remaining',0), counts=totals)
            if replay.get('state') == 'cancelled' or stop():
                return {'state':'cancelled', 'extraction_replay':totals}
            if not replay.get('remaining'):
                break
            if not completed:
                raise Waiting('source_extraction_no_progress', 'Saved source extraction has unresolved work and made no progress.')
        if stop():
            return {'state':'cancelled', 'extraction_replay':totals}
        source = frozen_evidence_build.capture(tag, root=kwargs.get('root'))
        result = evidence_recovery.execute(tag, action="repair", source_capture=source, **kwargs)
        return {**result, 'extraction_replay':totals}

    def verify(self, tag, **kwargs):
        from . import evidence_recovery, frozen_evidence_build
        source = frozen_evidence_build.capture(tag, root=kwargs.get('root'))
        return evidence_recovery.execute(tag, action="verify_failed", source_capture=source, **kwargs)

    def plan(self, tag, *, root, qualification, cancelled):
        from . import evidence_recovery, npm_evidence_sources, npm_case_collector, frozen_evidence_build
        captured = frozen_evidence_build.capture(tag, root=root)
        self._discovery_parent_identity = _discovery_identity(tag, root, parent_files=captured.get('parent_files', {}))
        configured = npm_case_collector.configured_snapshot(root=root)
        discovery = configured
        if configured.get('state') == 'ready':
            directory, _, _ = evidence_recovery._artifact(tag, root)
            first = True
            parent_identity = self._discovery_parent_identity
            while not cancelled():
                discovery = npm_case_collector.plan(configured['path'], root=root, limit=5000,
                    parent_records=_complete_parent_pairs(evidence_recovery._records(directory)) if first else (),
                    parent_identity=parent_identity, should_stop=cancelled)
                first = False
                if discovery.get('exhausted') or discovery.get('state') in ('waiting', 'cancelled'):
                    break
                if not discovery.get('processed', discovery.get('queued', 0)):
                    discovery = {**discovery, 'state': 'waiting', 'code': 'npm_discovery_no_progress'}
                    break
        if cancelled() or discovery.get('state') == 'cancelled':
            return {'state': 'cancelled', 'cancelled': True}
        # Queue membership binds immutable source pointers, not evolving context
        # overlays. Otherwise each repair would requeue already acquired files.
        result = npm_evidence_sources.plan(frozen_evidence_build.iter_records(captured, root=root), root=root,
                                           qualification=qualification, should_stop=cancelled)
        return {**result, **self.queue_summary(root=root), 'state': 'cancelled' if cancelled() else 'done',
                'discovery': _compact(discovery), 'cacheable': discovery.get('exhausted') is True}

    def acquire(self, *, root, cancelled, progress=None):
        from . import npm_evidence_sources, npm_case_collector
        totals = dict.fromkeys(("attempted", "acquired", "failed", "bytes_used", "requests_used"), 0)
        chunks, deferred, deferred_cases = 0, {}, set()
        while True:
            if cancelled():
                return {**totals, "state": "cancelled", "cancelled": True, "transfer_chunks": chunks}
            transferred = False
            for kind in ('cases', 'sources'):
                before = self.queue_summary(root=root)
                queue = before.get(kind, before if kind == 'sources' else {})
                recover_owner = kind == 'cases' and (queue.get('states') or {}).get('running', 0) > 0
                if kind in deferred or not queue.get('remaining') or not recover_owner and queue.get('eligible', queue.get('remaining')) == 0:
                    continue
                if cancelled():
                    return {**totals, 'state': 'cancelled', 'cancelled': True, 'transfer_chunks': chunks}
                try:
                    if kind == 'cases':
                        result = npm_case_collector.acquire(root=root, limit=5, max_bytes=16 << 20, max_seconds=120,
                            max_requests=40, should_stop=cancelled, exclude_ids=deferred_cases)
                    else:
                        result = npm_evidence_sources.acquire(root=root, limit=25, max_bytes=16 << 20, max_seconds=120, max_requests=40,
                                                              max_attempts=3, retry_failed=True, should_stop=cancelled)
                except oslock.Busy:
                    result = {'state': 'waiting', 'code': kind + '_acquisition_owner_busy', 'attempted': 0,
                              'reason': 'Another source acquisition owner still holds its lock.'}
                chunks += 1
                for key in totals:
                    totals[key] += result.get('collected' if kind == 'cases' and key == 'acquired' else key, 0)
                if result.get('cancelled') or result.get('state') == 'cancelled':
                    return {**totals, **result, 'state': 'cancelled', 'cancelled': True, 'transfer_chunks': chunks}
                after = self.queue_summary(root=root)
                newly_deferred = False
                if kind == 'cases' and result.get('wait_scope') == 'wave':
                    previous_deferred = len(deferred_cases)
                    deferred_cases.update(result.get('deferred_ids') or ())
                    newly_deferred = len(deferred_cases) > previous_deferred
                changed = before.get('remaining') != after.get('remaining')
                made_progress = changed or newly_deferred or any(result.get(key) for key in ('acquired', 'collected', 'unsupported', 'failed', 'bytes_used', 'requests_used'))
                transferred = transferred or made_progress
                fresh_wave = (result.get('wait_scope') == 'wave' and
                              after.get(kind, {}).get('eligible', 0) > 0 and made_progress)
                if result.get('state') == 'waiting' and not fresh_wave or not result.get('attempted') or not made_progress:
                    deferred[kind] = {**result, 'state': 'waiting', 'code': result.get('code') or 'source_frontier_no_progress',
                        'reason': result.get('reason') or 'Source work is waiting for a retry budget or capability.'}
            summary = self.queue_summary(root=root)
            measured = {}
            if type(summary.get("total")) is int and type(summary.get("remaining")) is int:
                measured = {"processed": summary["total"] - summary["remaining"], "total": summary["total"]}
            if progress is not None:
                progress(state="running", label="Acquiring exact npm source evidence", transfer_chunks=chunks,
                         counts=summary.get("states", {}), **measured)
            combined = {**summary, **totals, "transfer_chunks": chunks}
            if summary.get('exhausted'):
                return {**combined, 'state': 'done'}
            if not summary.get('remaining') and totals['acquired']:
                # Independent source acquisition completed even if a discovery
                # snapshot is missing. Let repair/admission consume its output.
                return {**combined, 'state': 'done'}
            if not transferred:
                waiting = next(iter(deferred.values()), None)
                if waiting is None:
                    waiting = next((queue for queue in (summary.get('sources', {}), summary.get('cases', {}), summary.get('discovery', {}))
                                    if queue.get('state') == 'waiting' or queue.get('next_retry_at')), {})
                return {**combined, **{key: waiting[key] for key in ('code', 'reason', 'next_retry_at', 'retry_after_seconds') if waiting.get(key) is not None},
                    'state': 'waiting', 'transfer_complete': summary.get('remaining') == 0,
                    'code': waiting.get('code') or 'source_frontier_no_progress',
                    'reason': waiting.get('reason') or 'Source discovery or transfer is waiting for an independent capability.'}

    def find_build(self, work_key, *, root):
        from . import build_catalog
        build_catalog.ensure_catalog(root)
        with build_catalog._connect(root) as db:
            for row in db.execute("SELECT body FROM builds"):
                body = json.loads(row[0])
                if body.get("retry_of") == _MARKER + work_key:
                    return self.get_build(body["id"], root=root)
        return None

    def get_build(self, build_id, *, root):
        from . import build_catalog
        from training.finetune.quality_gate import assess_manifest
        row = build_catalog.get(build_id, root=root)
        if not row or row.get("status") == "frozen":
            return row
        tag = row.get("tag")
        if not isinstance(tag, str) or not re.fullmatch(r"v[1-9][0-9]*", tag):
            raise ValueError("invalid_recorded_build_tag")
        folder = _root(root) / ("dataset_" + tag)
        if not folder.exists():
            return row
        manifest = build_catalog.read_manifest(folder / "manifest.json")
        try:
            build_catalog.verify_publication(row, manifest, root=root)
        except (ValueError, OSError) as error:
            raise Waiting("recorded_publication_unverified", str(error), build_id=build_id) from error
        gate = assess_manifest(manifest)
        return build_catalog.update(build_id, root=root, status="frozen", state="ready" if gate["allowed"] else "blocked",
                                    counts=manifest.get("counts", {}), readiness=manifest.get("security_readiness", {}),
                                    blockers=gate["blockers"], full_training=gate, error=None,
                                    progress={"state": "done", "label": "Reconciled registered immutable publication"})

    def resume_build(self, build_id, *, root, check):
        from . import build_catalog
        resume = getattr(build_catalog, "resume_owned", None)
        if resume is None:
            raise Waiting("build_resume_unavailable", "The recorded partial build needs the owned-staging resume capability.", build_id=build_id)
        check()
        try:
            row = resume(build_id, root=root)
        except ValueError as error:
            raise Waiting("build_resume_required", str(error), build_id=build_id) from error
        check()
        return row

    def create_build(self, tag, *, root, work_key, check):
        from . import build_catalog, frozen_evidence_build
        captured = frozen_evidence_build.capture(tag, root=root)
        check()
        return build_catalog.create("js_ts_security", root=root, captured=captured, retry_of=_MARKER + work_key,
                                    start_job=lambda *args, **kwargs: SimpleNamespace(id="inline-qualification-" + work_key[:32]))

    def execute_build(self, build_id, *, root, progress, check):
        from . import build_catalog
        if "publication_guard" not in inspect.signature(build_catalog.execute).parameters:
            raise Waiting("publication_fence_unavailable", "The builder needs a prepublication ownership and cancellation guard.")
        check()
        return build_catalog.execute(build_id, root=root, progress=progress, publication_guard=getattr(check, "publication_guard", check))

    def preflight(self, tag, *, root):
        from . import versions
        from training.finetune.quality_gate import data_preflight
        _parent(tag, root)
        name = "dataset_" + tag
        folder = _root(root) / name
        entry = versions.load_registry(_root(root))["frozen"][name]
        for relative, expected in entry["files"].items():
            file = (folder / relative).resolve()
            if not file.is_relative_to(folder.resolve()) or not file.is_file() or versions._sha256(file) != expected:
                raise ValueError("qualified_dataset_file_hash_mismatch:" + relative)
        return data_preflight(folder)


# Explicit integration name; Services remains available for existing callers.
DefaultServices = Services
