"""Backups of the application database with the ``mongodump`` and ``mongorestore`` that ship inside the Atlas Local container.

A backup is one gzip archive in ``data/interim/backups/`` (``<database>-<stamp>.archive.gz``) with a small JSON receipt beside it: the document
count of every collection at the time, the size and the SHA-256 of the archive, and whether the archive was proven readable (a ``mongorestore
--dryRun`` over the file). A backup that cannot be proven is deleted and reported, never kept as if it were good. A restore always goes to a database
of another name (never over the live one) and refuses a target that already holds data; to use it, point the application at that database.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import shutil
import subprocess

from app.backend import paths
from app.backend.pipeline.db import mongo

ARCHIVE_SUFFIX = ".archive.gz"
_TIMEOUT = 3600


class BackupError(RuntimeError):
    """The dump or the restore did not complete; the message says why."""


def backup_dir() -> pathlib.Path:
    d = paths.INTERIM / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _docker() -> str:
    exe = shutil.which("docker")
    if not exe:
        raise BackupError("the docker command is not available; backups run mongodump inside the database container")
    return exe


def _tool(args: list[str], *, stdin=None, stdout=None) -> subprocess.CompletedProcess:
    cmd = [_docker(), "exec", *(["-i"] if stdin is not None else []), mongo.container_name(), *args]
    try:
        return subprocess.run(cmd, stdin=stdin, stdout=stdout if stdout is not None else subprocess.PIPE, stderr=subprocess.PIPE, timeout=_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise BackupError(f"{args[0]} did not finish within {_TIMEOUT} seconds") from e


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _counts(db_name: str) -> dict[str, int]:
    db = mongo.client()[mongo.check_name(db_name)]
    return {n: db[n].estimated_document_count() for n in sorted(db.list_collection_names()) if not n.startswith("system.")}


def _uri_args() -> list[str]:
    return ["--uri=mongodb://localhost:27017/?directConnection=true"]


def verify(archive: pathlib.Path) -> bool:
    """Whether ``mongorestore`` can read the whole archive (a dry run: nothing is written)."""
    with archive.open("rb") as f:
        done = _tool(["mongorestore", *_uri_args(), "--archive", "--gzip", "--dryRun"], stdin=f)
    return done.returncode == 0


def backup(db_name: str | None = None) -> dict:
    """Dump the database to a new archive and prove it readable. Returns the receipt (also written beside the archive)."""
    name = mongo.check_name(db_name or mongo.db_name())
    mongo.require()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = backup_dir() / f"{name}-{stamp}{ARCHIVE_SUFFIX}"
    before = _counts(name)
    try:
        with archive.open("wb") as out:
            done = _tool(["mongodump", *_uri_args(), f"--db={name}", "--archive", "--gzip"], stdout=out)
        if done.returncode != 0:
            raise BackupError("mongodump failed: " + done.stderr.decode("utf-8", "replace").strip().splitlines()[-1][:300])
        if archive.stat().st_size == 0 or not verify(archive):
            raise BackupError("the archive written by mongodump could not be read back, so it was discarded")
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    receipt = {"file": archive.name, "database": name, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "bytes": archive.stat().st_size, "sha256": _sha256(archive), "verified": True, "collections": before,
               "documents": sum(before.values())}
    archive.with_name(archive.name + ".json").write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    return receipt


def list_backups() -> list[dict]:
    """The receipts of the backups on disk, newest first (an archive without a receipt is listed with what the file system knows)."""
    out = []
    for archive in sorted(backup_dir().glob(f"*{ARCHIVE_SUFFIX}"), reverse=True):
        side = archive.with_name(archive.name + ".json")
        try:
            out.append(json.loads(side.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            out.append({"file": archive.name, "bytes": archive.stat().st_size, "verified": None})
    return out


def restore(archive_name: str, into: str) -> dict:
    """Restore an archive into the database ``into``, which must be empty or not exist. The live database is never the target."""
    archive = backup_dir() / pathlib.Path(archive_name).name
    if not archive.exists() or not archive.name.endswith(ARCHIVE_SUFFIX):
        raise BackupError(f"no backup named {archive_name!r}")
    target = mongo.check_name(into)
    if target == mongo.db_name():
        raise BackupError("a restore never writes over the database in use; restore into another name and switch to it in Settings")
    try:
        source = json.loads(archive.with_name(archive.name + ".json").read_text(encoding="utf-8"))["database"]
    except (OSError, ValueError, KeyError) as e:
        raise BackupError("the receipt of this backup is missing, so the database it came from is unknown") from e
    mongo.require()
    if any(_counts(target).values()):
        raise BackupError(f"database {target!r} already holds data; restore into an empty name")
    with archive.open("rb") as f:
        done = _tool(["mongorestore", *_uri_args(), "--archive", "--gzip", f"--nsFrom={source}.*", f"--nsTo={target}.*"], stdin=f)
    if done.returncode != 0:
        raise BackupError("mongorestore failed: " + done.stderr.decode("utf-8", "replace").strip().splitlines()[-1][:300])
    return {"database": target, "collections": _counts(target)}
