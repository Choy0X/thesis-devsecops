"""Backend-owned execution journal. Dataset receipts are references, never issuers.

The SQLite file and its separate authentication key are trusted application state,
outside record imports. A record writer cannot mint a completed run by constructing
JSON or by recomputing a content hash. Host administrators and backend Python code
are inside this trust boundary. This is not a remote attestation service.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def default_path():
    from app.backend import paths
    return paths.database_dir() / "verification-proof.sqlite3"


class ProofLedger:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_path()

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("CREATE TABLE IF NOT EXISTS execution_runs (run_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, binding TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT, mac TEXT, created REAL NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS execution_state ON execution_runs(state)")
        return db

    def _key(self):
        path = self.path.with_suffix(self.path.suffix + ".key")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = path.read_bytes()
        else:
            key = secrets.token_bytes(32)
            with os.fdopen(fd, "wb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
        if len(key) != 32:
            raise ValueError("invalid backend proof ledger authentication key")
        return key

    def _begin(self, binding):
        """Private issuer capability, never serialised into a receipt or record."""
        token, run_id = secrets.token_hex(32), secrets.token_hex(24)
        with self._open() as db:
            self._key()
            db.execute("INSERT INTO execution_runs VALUES (?, ?, ?, 'running', NULL, NULL, ?)",
                       (run_id, digest(token), canonical(binding), time.time()))
        return run_id, token

    def _finish(self, handle, receipt):
        run_id, token = handle
        body = {**receipt, "run_id": run_id}
        body["receipt_id"] = digest(body)
        with self._open() as db:
            row = db.execute("SELECT token_hash, binding, state FROM execution_runs WHERE run_id=?", (run_id,)).fetchone()
            if (not row or row[2] != "running" or not hmac.compare_digest(row[0], digest(token))
                    or row[1] != canonical(body.get("binding"))):
                raise ValueError("execution completion requires its unused backend capability and exact binding")
            raw = canonical(body)
            mac = hmac.new(self._key(), raw.encode("utf-8"), hashlib.sha256).hexdigest()
            db.execute("UPDATE execution_runs SET state='completed', receipt=?, mac=? WHERE run_id=?", (raw, mac, run_id))
        return body

    def completed(self, binding):
        """Authenticated completed successes matching every supplied binding field.

        Reading an absent ledger has no filesystem side effects. No receipt import
        API exists. Malformed or unauthenticated rows are ignored fail-closed.
        """
        if not self.path.exists() or not self.path.with_suffix(self.path.suffix + ".key").exists():
            return []
        try:
            with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as db:
                rows = db.execute("SELECT run_id, receipt, mac FROM execution_runs WHERE state='completed' ORDER BY created DESC").fetchall()
            key = self._key()
        except (OSError, ValueError, sqlite3.Error):
            return []
        results = []
        for run_id, raw, mac in rows:
            try:
                if not hmac.compare_digest(mac or "", hmac.new(key, raw.encode("utf-8"), hashlib.sha256).hexdigest()):
                    continue
                body = json.loads(raw)
                if (body.get("run_id") != run_id or body.get("receipt_id") != digest({k: v for k, v in body.items() if k != "receipt_id"})
                        or body.get("verdict") != "verified"):
                    continue
                actual = body["binding"]
                if all(actual.get(k) == v for k, v in binding.items()):
                    results.append(body)
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
        return results
