"""Consistent SQLite snapshots and verified restores to separate new files.

The original MongoDB archive implementation is preserved in
legacy_migration/backup_mongo.py for migration tooling only. Runtime operations
never import it or write to a MongoDB source.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
import shutil

from app.backend import paths

ARCHIVE_SUFFIX = ".sqlite3"


class BackupError(RuntimeError):
    pass


def backup_dir():
    directory = paths.INTERIM / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _readonly(path, *, immutable=False):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=True, timeout=30)


def _sidecars(path):
    return [path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal")]


def _has_recovery_data(path):
    return any(p.exists() and p.stat().st_size > 0 for p in _sidecars(path) if not p.name.endswith("-shm"))


def _integrity(path):
    try:
        conn = _readonly(path, immutable=True)
        try:
            return conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return False


def verify(archive):
    """Verify SQLite structure and the receipt hash and size when present."""
    archive = Path(archive)
    if not archive.is_file() or _has_recovery_data(archive) or not _integrity(archive):
        return False
    receipt = archive.with_name(archive.name + ".json")
    if receipt.exists():
        try:
            expected = json.loads(receipt.read_text(encoding="utf-8"))
            return archive.stat().st_size == expected["bytes"] and _sha256(archive) == expected["sha256"]
        except (OSError, ValueError, KeyError, TypeError):
            return False
    return True


def _counts(path):
    """Count snapshot tables without importing application bootstrap code."""
    conn = _readonly(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        if "_collections" in tables:
            counts = {}
            for (name,) in conn.execute("SELECT name FROM _collections ORDER BY name"):
                table = "d_" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]
                if table not in tables:
                    raise BackupError("Backup collection catalog does not match its data tables")
                counts[name] = conn.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0]
            return counts
        return {name: conn.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                for name in sorted(tables) if not name.startswith("_")}
    finally:
        conn.close()


def _reserve(path):
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except FileExistsError as exc:
        raise BackupError("Destination already exists; it was not overwritten") from exc


def backup_file(source_path, directory, *, database_name=None):
    source = Path(source_path).resolve()
    if not source.is_file():
        raise BackupError("Database file does not exist")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = directory / f"{source.stem}-{stamp}-{uuid.uuid4().hex[:8]}{ARCHIVE_SUFFIX}"
    _reserve(archive)
    receipt_path = archive.with_name(archive.name + ".json")
    try:
        src, dst = _readonly(source), sqlite3.connect(archive)
        try:
            src.backup(dst, pages=256, sleep=0.01)
            result = dst.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result[0] != 0:
                raise BackupError("Backup WAL checkpoint did not complete")
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
            src.close()
        if not _integrity(archive):
            raise BackupError("Backup failed SQLite integrity verification")
        with archive.open("r+b") as stream:
            os.fsync(stream.fileno())
        counts = _counts(archive)
        receipt = {"format": "ztsa-sqlite-backup/1", "file": archive.name,
                   "database": database_name or source.stem,
                   "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                   "bytes": archive.stat().st_size, "sha256": _sha256(archive), "verified": True,
                   "collections": counts, "documents": sum(counts.values())}
        with receipt_path.open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        return receipt
    except BaseException:
        archive.unlink(missing_ok=True)
        receipt_path.unlink(missing_ok=True)
        raise


def restore_file(archive_path, target_path):
    """Restore into a separately reserved file, refusing all existing targets."""
    archive, target = Path(archive_path).resolve(), Path(target_path).resolve()
    if target.exists() or any(p.exists() for p in _sidecars(target)):
        raise BackupError("Destination or recovery sidecar already exists; it was not overwritten")
    if not archive.with_name(archive.name + ".json").is_file():
        raise BackupError("Backup receipt is missing; hash and size cannot be verified")
    if not verify(archive):
        raise BackupError("Backup hash, size, or integrity verification failed")
    try:
        expected = json.loads(archive.with_name(archive.name + ".json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError("Backup receipt changed during restore") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.with_name(target.name + ".restoring-" + uuid.uuid4().hex)
    _reserve(stage)
    try:
        # Copy only bytes bound to the receipt. Reading SQLite through its online
        # API here could silently incorporate an un-hashed archive WAL file.
        with archive.open("rb") as src, stage.open("wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
            dst.flush()
            os.fsync(dst.fileno())
        if _has_recovery_data(archive) or stage.stat().st_size != expected["bytes"] or _sha256(stage) != expected["sha256"]:
            raise BackupError("Backup hash or WAL changed during restore")
        if not _integrity(stage):
            raise BackupError("Restored database failed SQLite integrity verification")
        counts = _counts(stage)
        if target.exists() or any(p.exists() for p in _sidecars(target)):
            raise BackupError("Destination or recovery sidecar appeared during restore")
        try:
            os.link(stage, target)
        except OSError as exc:
            raise BackupError("Destination could not be promoted without overwriting existing files") from exc
        return {"database": target.stem, "path": str(target), "collections": counts, "verified": True}
    finally:
        stage.unlink(missing_ok=True)


def backup(db_name=None):
    from . import database
    db = database.database(db_name)
    return backup_file(db.path, backup_dir(), database_name=db.name)


def list_backups():
    rows = []
    directory = backup_dir()
    files = [*directory.glob("*" + ARCHIVE_SUFFIX), *directory.glob("*.archive.gz")]
    for archive in sorted(files, reverse=True):
        try:
            receipt = json.loads(archive.with_name(archive.name + ".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            receipt = {"file": archive.name, "bytes": archive.stat().st_size, "verified": None}
        if archive.name.endswith(".archive.gz"):
            receipt.update(verified=None, migration_required=True, format="mongodb-archive")
        elif receipt.get("verified") is not None:
            receipt["verified"] = verify(archive)
        rows.append(receipt)
    return rows


def restore(archive_name, into):
    archive = backup_dir() / Path(archive_name).name
    if not archive.is_file():
        raise BackupError(f"no backup named {archive_name!r}")
    if archive.name.endswith(".archive.gz"):
        raise BackupError("Legacy MongoDB archive requires isolated migration tooling; the original file is preserved")
    if not archive.name.endswith(ARCHIVE_SUFFIX):
        raise BackupError("Unsupported backup format")
    from . import database
    name = database.check_name(into)
    current = database.database()
    if name == current.name:
        raise BackupError("A restore never writes over the database in use; choose another name")
    target = Path(current.path).parent / (name + Path(current.path).suffix)
    if target.exists() or any(path.exists() for path in _sidecars(target)):
        raise BackupError(f"database {name!r} already holds data or has an existing file or recovery sidecar; restore into a new name")
    return restore_file(archive, target)
