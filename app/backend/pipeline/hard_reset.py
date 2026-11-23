"""Hard reset: delete what the app has stored, from Settings, with the exact sizes shown first.

This module deletes things, so it is written to be unable to delete anything it was not given by name.

* **A static allowlist.** What can be deleted is a fixed table of categories (``categories``); each is a folder or a few files whose place is computed from the project root and, for the
  folders that Settings can move (parsed data, database records and models) and the settings file, from where the app really uses them, and only when that place is inside the project
  root. A moved folder outside it is shown as "not offered" with the reason, never deleted. Nothing is ever offered from the code, the tests, the notes, ``.git`` or the virtual
  environments (``.venv*``), and the Docker container and its volume are not this module's to touch.
* **Links are removed, not followed.** A symbolic link or a junction inside a category is unlinked; the folder it points to and everything in it stay. The same goes for the category's
  own folder if it is a link.
* **Local database files only.** The legacy category id ``mongo`` now means embedded SQLite files. Only ``aivf`` and ``aivf_*`` names are offered. MongoDB is never contacted.
  Other file categories preserve the database folder; only the explicit database category may remove its records. Legacy MongoDB backup archives stay for rollback.
* **Two keys.** The list is taken by a background measurement (every file's size is read with ``lstat``, with progress) and given a token; a run needs that token (an older dialog, or a
  list older than ten minutes, is refused), the word ``RESET`` typed by the person, and no job running. Both are checked here, not by the page.
* **A backup first, if asked.** The databases to be dropped are copied with the verified local backup operation; the new backup is kept even when the folder of backups is
  among what is deleted, and a backup that fails stops the whole run before anything is deleted.
* **Windows locks.** A file in use is tried three times; what still cannot be deleted goes on a list (``data/.hard-reset-pending.json``) that is tried again at the next start, against the
  allowlist again.

The run order is: backup, stop what holds files (the model gateway), the databases, the folders, then the settings last.
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import secrets
import sqlite3
import stat
import threading
import time
from typing import Callable, Protocol

ROOT = pathlib.Path(__file__).resolve().parents[3]
CONFIRM_WORD = "RESET"
TOKEN_LIFETIME = 600.0                      # seconds a measured list may be used
RETRIES = 3
RETRY_WAIT = 0.4
PENDING_NAME = ".hard-reset-pending.json"   # in <root>/data, never part of a category
REPARSE_POINT = 0x400                       # FILE_ATTRIBUTE_REPARSE_POINT: a junction (or another reparse point) on Windows
DOCKER_NOTE = ("Records are local SQLite files. Only the Local databases category removes them. Preserved MongoDB data and its archived backups are never deleted by this reset.")


class ResetError(Exception):
    """A request that is refused: ``status`` is the HTTP code, ``code`` a short word the page can act on."""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request", hint: str | None = None):
        super().__init__(message)
        self.status, self.code, self.hint = status, code, hint


# -- where things are ------------------------------------------------------------------------------------------------------------------------------

@dataclasses.dataclass
class Locations:
    root: pathlib.Path
    interim: pathlib.Path                      # parsed data and backups in use
    models: pathlib.Path                       # the models folder in use
    settings: tuple[pathlib.Path, ...]         # settings.json and its migrations record
    database: pathlib.Path | None = None      # default keeps older fixture/location callers compatible


def locations() -> Locations:
    """Where the running app keeps things (a relative path is relative to the project root, as everywhere in the app)."""
    from app.backend import config, paths
    from app.backend.pipeline import model_files
    root = ROOT

    def absolute(p: pathlib.Path) -> pathlib.Path:
        return p if p.is_absolute() else root / p
    return Locations(root=root, interim=absolute(pathlib.Path(paths.INTERIM)), models=absolute(model_files.models_dir()),
                     settings=(config.SETTINGS_PATH, config.SETTINGS_PATH.with_name("settings_migrations.json")), database=absolute(paths.database_dir()))


def database_folder(loc: Locations) -> pathlib.Path:
    return loc.database if loc.database is not None else loc.interim / "database"


def is_link(path) -> bool:
    """A symbolic link or a junction (a reparse point). ``lstat`` never follows it."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & REPARSE_POINT)


def inside(root: pathlib.Path, target: pathlib.Path) -> bool:
    """Whether ``target`` lies strictly inside ``root`` once everything above its last part is resolved (the last part may itself be a link: it is not followed)."""
    try:
        r = root.resolve()
        t = target.parent.resolve() / target.name
    except OSError:
        return False
    return t != r and r in t.parents


def looks_like_the_project(root: pathlib.Path) -> bool:
    return (root / "app" / "desktop.py").is_file() and (root / "app" / "backend").is_dir()


@dataclasses.dataclass(frozen=True)
class Category:
    id: str
    label: str
    note: str
    default: bool
    warning: str | None = None


CATEGORIES = (
    Category("mongo", "Local databases", "The app's aivf and aivf_* SQLite files (records, pairs, reviews, campaigns and indexes). The previous MongoDB source is preserved.", True),
    Category("raw", "Downloaded sources", "data/raw: the advisory dumps and the cloned repositories.", True),
    Category("interim", "Parsed files and derived tables", "The parsed data folder (data/interim by default): parquet files, derived tables and locks. The separate database files, app lock and run marker stay.", True),
    Category("processed", "Built datasets", "data/processed: the datasets built from the records.", True),
    Category("logs", "Logs", "data/logs and experiments/logs. The log of restarts stays: the running app holds it open.", True),
    Category("training_runs", "Training runs", "training/runs: adapters, checkpoints and results of fine-tuning runs.", True),
    Category("projects", "Projects", "data/projects: the added projects, their scans, audit logs and results. Their source folders are not touched.", True),
    Category("sandbox", "Sandbox and verification pilot", "data/sandbox and experiments/verify_pilot.", True),
    Category("db_backups", "Database backups", "Local backups in the parsed data folder. A backup made for this reset and historic MongoDB archives are kept.", True),
    Category("models", "Models", "The models folder (data/models by default), with the Hugging Face cache inside it. They are downloaded again when needed, and that can be many GB.", False,
             "Not ticked: every model downloaded or converted here goes, and the local language model and the embedding model stop working until they are downloaded again."),
    Category("tools", "Installed tools", "data/tools: the locally installed tools, including the GGUF conversion scripts.", False,
             "Not ticked: model conversion requires its scripts to be installed again."),
    Category("settings", "Settings and keys", "The settings file (with the API keys and tokens saved in it), and its record of changes. The app starts with its defaults.", False,
             "Not ticked: every key and token saved in Settings is deleted, and nothing can bring them back."),
)
BY_ID = {c.id: c for c in CATEGORIES}


def targets(cid: str, loc: Locations) -> tuple[list[pathlib.Path], str | None]:
    """The paths of a folder or file category (never Mongo) and, if it cannot be offered, why. Only places inside the project root are returned."""
    root = loc.root
    data = root / "data"
    if cid == "raw":
        wanted = [data / "raw"]
    elif cid == "interim":
        wanted = [loc.interim]
    elif cid == "processed":
        wanted = [data / "processed"]
    elif cid == "logs":
        wanted = [data / "logs", root / "experiments" / "logs"]
    elif cid == "training_runs":
        wanted = [root / "training" / "runs"]
    elif cid == "projects":
        wanted = [data / "projects"]
    elif cid == "sandbox":
        wanted = [data / "sandbox", root / "experiments" / "verify_pilot"]
    elif cid == "db_backups":
        wanted = [loc.interim / "backups"]
    elif cid == "models":
        wanted = [loc.models]
    elif cid == "tools":
        wanted = [data / "tools"]
    elif cid == "settings":
        wanted = list(loc.settings)
    else:
        return [], "unknown category"
    outside = [p for p in wanted if not inside(root, p)]
    if outside:
        return [], f"{outside[0]} is outside the project folder, so it is not offered here. Delete it yourself in the file manager if you want it gone."
    return wanted, None


def kept_names(cid: str, loc: Locations) -> set[str]:
    """Protect separately selected records and rollback archives from folder deletion."""
    keep = {"app-instance.lock", "app-instance.lock.json", "run-marker.json", "backups"} if cid == "interim" else set()
    if cid == "db_backups":
        folder = loc.interim / "backups"
        keep.update(p.name for p in folder.glob("*.archive.gz*"))
    records = database_folder(loc)
    for base in targets(cid, loc)[0]:
        if base == records:
            keep.update(p.name for p in base.glob("*.sqlite3*"))
        else:
            try:
                relative = records.relative_to(base)
            except ValueError:
                continue
            if relative.parts:
                keep.add(relative.parts[0])
    return keep


def protected_path(path: pathlib.Path, loc: Locations) -> bool:
    """A stale or modified pending-cleanup record cannot bypass current protections."""
    target = path.parent.resolve() / path.name
    records = database_folder(loc)
    records = records.parent.resolve() / records.name
    if target == records or records in target.parents or target in records.parents:
        return True
    backups = loc.interim / "backups"
    return inside(backups, path) and ".archive.gz" in path.name


# -- measuring -------------------------------------------------------------------------------------------------------------------------------------

def measure(path: pathlib.Path, keep: set[str] = frozenset(), tick: Callable[[int, int], None] | None = None) -> tuple[int, int]:
    """(bytes, files) under ``path`` by ``lstat``; a link counts as one file of no size and is not followed; ``keep`` names at the top are left out."""
    total = files = 0
    if is_link(path):
        return 0, 1
    if not path.exists():
        return 0, 0
    if path.is_file():
        return path.lstat().st_size, 1
    stack = [(str(path), True)]
    while stack:
        here, top = stack.pop()
        try:
            it = os.scandir(here)
        except OSError:
            continue
        with it:
            for e in it:
                if top and e.name in keep:
                    continue
                try:
                    if is_link(e.path):
                        files += 1
                    elif e.is_dir(follow_symlinks=False):
                        stack.append((e.path, False))
                    else:
                        total += e.stat(follow_symlinks=False).st_size
                        files += 1
                except OSError:
                    continue
        if tick:
            tick(total, files)
    return total, files


class MongoOps(Protocol):
    def names(self) -> list[str]: ...
    def stats(self, name: str) -> dict: ...
    def drop(self, name: str) -> None: ...


class RealMongo:
    """Compatibility name for local SQLite file operations. Never contacts MongoDB."""

    def names(self) -> list[str]:
        from app.backend import paths
        return sorted(p.stem for p in paths.database_dir().glob("*.sqlite3") if not is_link(p))

    def stats(self, name: str) -> dict:
        from app.backend.pipeline.db import database
        from app.backend.pipeline.db.embedded import table_name
        path = database.file_path(name)
        if is_link(path):
            raise ValueError("A linked database is not offered for deletion")
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            conn.execute("BEGIN")
            names = [row[0] for row in conn.execute("SELECT name FROM _collections")]
            documents = sum(conn.execute("SELECT count(*) FROM " + table_name(collection)).fetchone()[0] for collection in names)
            total = sum(p.stat().st_size for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")) if p.is_file() and not is_link(p))
            return {"bytes": total, "documents": documents, "collections": len(names)}
        finally:
            conn.close()

    def drop(self, name: str) -> None:
        from app.backend.pipeline.db import database
        name = database.check_name(name)
        database.database(name).client.drop_database(name)


def is_application_database(name: str) -> bool:
    from app.backend.pipeline.db import database
    return (name == database.DEFAULT_DB or name.startswith("aivf_")) and name not in database.PROTECTED


def application_databases(ops: MongoOps) -> list[str]:
    return sorted(n for n in ops.names() if is_application_database(n))


# -- the list ---------------------------------------------------------------------------------------------------------------------------------------

def scan(loc: Locations, ops: MongoOps | None, progress: Callable[[str, int, int], None] | None = None) -> list[dict]:
    """Every category with its exact size. A category that cannot be offered says why."""
    out: list[dict] = []
    for c in CATEGORIES:
        row = {"id": c.id, "label": c.label, "note": c.note, "default": c.default, "warning": c.warning, "present": False, "bytes": 0, "files": 0, "paths": [], "blocked": None, "databases": []}
        if progress:
            progress(c.id, 0, 0)
        if c.id == "mongo":
            records = database_folder(loc)
            if not inside(loc.root, records):
                row["blocked"] = "The database folder is outside the project folder, so it is not offered here."
            elif is_link(records):
                row["blocked"] = "The database folder is a link, so its files are not offered here."
            elif ops is None:
                row["blocked"] = "Local storage is not reachable, so its databases cannot be listed."
            else:
                try:
                    for name in application_databases(ops):
                        s = ops.stats(name)
                        row["databases"].append({"name": name, "bytes": s["bytes"], "documents": s.get("documents", 0), "collections": s.get("collections", 0)})
                    row["bytes"] = sum(d["bytes"] for d in row["databases"])
                    row["files"] = len(row["databases"])
                    row["present"] = bool(row["databases"])
                except Exception as e:                               # a database that cannot be read: the category says so and cannot be run
                    row["blocked"] = f"Local database files could not be read ({type(e).__name__}: {str(e)[:120]})."
            out.append(row)
            continue
        paths, why = targets(c.id, loc)
        if why:
            row["blocked"] = why
            out.append(row)
            continue
        keep = kept_names(c.id, loc)
        for p in paths:
            b, f = measure(p, keep, (lambda tb, tf, cid=c.id: progress(cid, tb, tf)) if progress else None)
            row["bytes"] += b
            row["files"] += f
            row["paths"].append(p.relative_to(loc.root).as_posix() if p.is_relative_to(loc.root) else str(p))
        row["present"] = row["files"] > 0
        out.append(row)
    return out


_lock = threading.RLock()
_scan: dict = {"running": False, "done": False, "token": None, "taken": 0.0, "categories": [], "progress": {"category": "", "files": 0, "bytes": 0}, "error": None, "root": ""}
_run: dict = {"running": False, "done": False, "started": None, "steps": [], "ok": None, "error": None, "pending": 0, "categories": []}


def scan_status() -> dict:
    with _lock:
        out = {k: _scan[k] for k in ("running", "done", "token", "taken", "progress", "error", "root")}
        out["categories"] = list(_scan["categories"])
        out["docker_note"] = DOCKER_NOTE
        out["confirm_word"] = CONFIRM_WORD
        return out


def start_scan(loc: Locations | None = None, ops: "MongoOps | None | bool" = None, *, force: bool = False, inline: bool = False) -> dict:
    """Begin measuring in the background (``inline`` runs it here, for tests). A measurement that is running, or finished less than ten minutes ago, is kept unless ``force``. ``ops`` is
    the local storage to ask: None means the app's own, ``False`` means "not reachable" (the databases category then says so)."""
    with _lock:
        fresh = _scan["done"] and time.time() - _scan["taken"] < TOKEN_LIFETIME
        if _scan["running"] or (fresh and not force):
            return scan_status()
        _scan.update(running=True, done=False, token=None, categories=[], error=None, progress={"category": "", "files": 0, "bytes": 0})

    def work():
        try:
            where = loc or locations()
            _scan["root"] = str(where.root)
            mongo_ops = None if ops is False else ops
            if ops is None:
                mongo_ops = RealMongo()
                try:
                    from app.backend.pipeline.db import database
                    database.require()
                except Exception:  # ignored: a local file cannot be read; the category explains why it is blocked
                    mongo_ops = None
            rows = scan(where, mongo_ops, lambda cid, b, f: _scan.update(progress={"category": cid, "files": f, "bytes": b}))
            with _lock:
                _scan.update(categories=rows, token=secrets.token_hex(8), taken=time.time(), done=True, running=False)
        except Exception as e:
            with _lock:
                _scan.update(error=f"{type(e).__name__}: {e}", running=False, done=False)
    if inline:
        work()
    else:
        threading.Thread(target=work, daemon=True, name="hard-reset-scan").start()
    return scan_status()


# -- deleting -------------------------------------------------------------------------------------------------------------------------------------------

def _retry(fn: Callable[[], None], wait: float = RETRY_WAIT) -> None:
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            fn()
            return
        except FileNotFoundError:
            return
        except OSError as e:
            last = e
            if attempt < RETRIES - 1:
                time.sleep(wait)
    raise last or OSError("could not be deleted")


def _writable(path: str) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass                                                      # ignored: the delete that follows says whether it mattered


def _unlink_link(path: str) -> None:
    try:
        os.unlink(path)
    except (IsADirectoryError, PermissionError, OSError):
        os.rmdir(path)                                            # a directory link (a junction): removing it does not touch what it points to


def remove_item(path: str) -> int:
    """Delete one file or link. Returns 1."""
    def go():
        if is_link(path):
            _unlink_link(path)
        else:
            _writable(path)
            os.unlink(path)
    _retry(go)
    return 1


def remove_tree(path: str, keep: set[str], failed: list[tuple[str, str]], top: bool = True) -> int:
    """Delete everything under ``path`` (the folder itself too, unless something stays in it). Links are removed and not followed. What cannot be deleted after the retries is added to
    ``failed`` as (path, reason). Returns the number of items removed."""
    removed = 0
    try:
        entries = list(os.scandir(path))
    except OSError as e:
        failed.append((path, f"{type(e).__name__}: {e}"))
        return 0
    stayed = False
    for e in entries:
        if top and e.name in keep:
            stayed = True
            continue
        try:
            if is_link(e.path) or not e.is_dir(follow_symlinks=False):
                removed += remove_item(e.path)
            else:
                removed += remove_tree(e.path, set(), failed, top=False)
        except OSError as err:
            failed.append((e.path, f"{type(err).__name__}: {err}"))
    if stayed or any(p == path or p.startswith(path + os.sep) for p, _ in failed):
        return removed
    try:
        _retry(lambda: os.rmdir(path))
        removed += 1
    except OSError as err:
        failed.append((path, f"{type(err).__name__}: {err}"))
    return removed


def remove_target(path: pathlib.Path, keep: set[str], keep_files: set[str], failed: list[tuple[str, str]]) -> int:
    """One allowlisted path of a category: a link is unlinked, a file deleted, a folder emptied. ``keep_files`` are names directly inside it that stay (the backup made for this run)."""
    if not (path.exists() or is_link(path)):
        return 0
    try:
        if is_link(path) or path.is_file():
            return remove_item(str(path))
        return remove_tree(str(path), keep | keep_files, failed)
    except OSError as err:
        failed.append((str(path), f"{type(err).__name__}: {err}"))
        return 0


# -- the run --------------------------------------------------------------------------------------------------------------------------------------------

def run_status() -> dict:
    with _lock:
        return {**_run, "steps": list(_run["steps"]), "categories": list(_run["categories"])}


def _busy() -> list[str]:
    from app.backend import jobs
    return [j.stage for j in jobs.running()]


def start(token: str, ids: list[str], confirm: str, backup: bool = False, *, loc: Locations | None = None, ops: MongoOps | None = None, busy: Callable[[], list[str]] | None = None,
          backup_fn: Callable[[str], dict] | None = None, before_files: Callable[[], str] | None = None, inline: bool = False) -> dict:
    """Check the request and, if it holds, start the run in the background. Raises ResetError with the HTTP code to answer."""
    where = loc or locations()
    if (confirm or "").strip() != CONFIRM_WORD:
        raise ResetError(f"Type {CONFIRM_WORD} to confirm.", 400, "not_confirmed", f"Nothing is deleted until the word {CONFIRM_WORD} has been typed.")
    if not looks_like_the_project(where.root):
        raise ResetError("The project folder could not be recognised, so nothing is deleted.", 500, "unknown_root")
    with _lock:
        if _run["running"]:
            raise ResetError("A reset is already running.", 409, "running")
        snapshot = list(_scan["categories"])
        current, taken, done = _scan["token"], _scan["taken"], _scan["done"]
    if not done or not token or token != current:
        raise ResetError("The list on the page is not the current one.", 409, "stale", "Look at the list again and choose again.")
    if time.time() - taken > TOKEN_LIFETIME:
        raise ResetError("The list on the page is more than ten minutes old.", 409, "stale", "Look at the list again and choose again.")
    ids = list(dict.fromkeys(ids or []))
    if not ids:
        raise ResetError("Nothing is chosen.", 400, "nothing_chosen")
    offered = {r["id"]: r for r in snapshot}
    for cid in ids:
        row = offered.get(cid)
        if row is None:
            raise ResetError(f"{cid!r} is not something this reset can delete.", 400, "unknown_category")
        if row["blocked"]:
            raise ResetError(f"{row['label']} cannot be deleted from here: {row['blocked']}", 400, "blocked")
    running = (busy or _busy)()
    if running:
        raise ResetError("Jobs are running (" + ", ".join(sorted(set(running))) + "). A reset waits until they are done or stopped.", 409, "jobs_running", "Stop them from the Jobs list and try again.")
    with _lock:
        _run.update(running=True, done=False, started=time.time(), steps=[], ok=None, error=None, pending=0, categories=ids)
    args = (ids, offered, bool(backup), where, ops, backup_fn, before_files)
    if inline:
        _execute(*args)
    else:
        threading.Thread(target=_execute, args=args, daemon=True, name="hard-reset").start()
    return run_status()


def _step(id: str, label: str, ok: bool, detail: str, **extra) -> None:
    with _lock:
        _run["steps"].append({"id": id, "label": label, "ok": ok, "detail": detail, **extra})


def _execute(ids: list[str], offered: dict, backup: bool, loc: Locations, ops: MongoOps | None, backup_fn, before_files) -> None:
    keep_files: set[str] = set()
    failed_all: list[tuple[str, str, str]] = []
    try:
        names = [d["name"] for d in offered["mongo"]["databases"]] if "mongo" in ids else []
        if backup and names:
            make = backup_fn or _real_backup
            for name in names:
                try:
                    receipt = make(name)
                except Exception as e:
                    _step("backup", f"Backing up {name}", False, f"{type(e).__name__}: {str(e)[:200]}. Nothing was deleted.")
                    raise _Stop() from e
                keep_files.update({receipt["file"], receipt["file"] + ".json"})
                _step("backup", f"Backing up {name}", True, f"{receipt.get('documents', 0)} documents in {receipt['file']}")
        folders = [i for i in ids if i != "mongo"]
        if any(i in folders for i in ("interim", "models", "tools", "logs", "db_backups")):
            note = (before_files or _stop_gateway)()
            _step("gateway", "Stopping what holds files", True, note or "")
        if "mongo" in ids and names:
            ops = ops or RealMongo()
            from app.backend.pipeline.db import mongo
            dropped, errors = 0, []
            for name in names:
                try:
                    ops.drop(mongo.check_name(name))
                    dropped += 1
                except Exception as e:
                    errors.append(f"{name}: {type(e).__name__}: {str(e)[:100]}")
            _step("mongo", BY_ID["mongo"].label, not errors, f"{dropped} of {len(names)} databases dropped" + ("; " + "; ".join(errors) if errors else ""))
        order = [i for i in folders if i != "settings"] + (["settings"] if "settings" in folders else [])
        for cid in order:
            paths_, why = targets(cid, loc)
            if why:
                _step(cid, BY_ID[cid].label, False, why)
                continue
            failed: list[tuple[str, str]] = []
            removed = 0
            for p in paths_:
                removed += remove_target(p, kept_names(cid, loc), keep_files if cid == "db_backups" else set(), failed)
            failed_all += [(path, why_, cid) for path, why_ in failed]
            _step(cid, BY_ID[cid].label, not failed, f"{removed} items removed" + (f"; {len(failed)} could not be removed and will be tried again at the next start" if failed else ""))
        if failed_all:
            _save_pending(loc, failed_all)
        with _lock:
            _run.update(ok=all(s["ok"] for s in _run["steps"]), pending=len(failed_all))
    except _Stop:
        with _lock:
            _run.update(ok=False)
    except Exception as e:                                         # the run never dies silently: the page shows what happened
        _step("error", "The reset stopped", False, f"{type(e).__name__}: {str(e)[:200]}")
        with _lock:
            _run.update(ok=False, error=f"{type(e).__name__}: {e}")
    finally:
        with _lock:
            _run.update(running=False, done=True)
            _scan.update(done=False, token=None)                   # what was measured is not true any more: the next list is measured again


class _Stop(Exception):
    pass


def _real_backup(name: str) -> dict:
    from app.backend.pipeline.db import backup
    return backup.backup(name)


def _stop_gateway() -> str:
    from app.backend import lifecycle
    return lifecycle._stop_gateway()


# -- what could not be deleted: tried again at the next start -------------------------------------------------------------------------------------------------

def pending_path(loc: Locations) -> pathlib.Path:
    return loc.root / "data" / PENDING_NAME


def _save_pending(loc: Locations, items: list[tuple[str, str, str]]) -> None:
    p = pending_path(loc)
    try:
        old = json.loads(p.read_text(encoding="utf-8")).get("items", [])
    except (OSError, ValueError):
        old = []
    at = time.strftime("%Y-%m-%dT%H:%M:%S")
    known = {i["path"] for i in old}
    old += [{"path": path, "category": cid, "reason": why[:200], "at": at} for path, why, cid in items if path not in known]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"items": old}, indent=2), encoding="utf-8")


def retry_pending(loc: Locations | None = None) -> dict:
    """At the start of the app: try again what the last reset could not delete. Every path is checked against the allowlist again (it must lie inside one of the places a category
    covers), so a changed list file cannot make this delete anything else. Returns what was removed and what is left."""
    where = loc or locations()
    p = pending_path(where)
    try:
        items = json.loads(p.read_text(encoding="utf-8")).get("items", [])
    except (OSError, ValueError):
        return {"removed": 0, "left": 0, "refused": 0}
    allowed: list[pathlib.Path] = []
    for cid in BY_ID:
        if cid != "mongo":
            allowed += targets(cid, where)[0]
    removed = refused = 0
    left: list[dict] = []
    for item in items:
        path = pathlib.Path(str(item.get("path", "")))
        covered = inside(where.root, path) and (path in allowed or any(a.is_dir() and inside(a, path) for a in allowed))
        if not covered or protected_path(path, where):
            refused += 1                                           # not a place the reset covers: dropped from the list, never touched
            continue
        failed: list[tuple[str, str]] = []
        keep = set()
        for cid in BY_ID:
            if path in targets(cid, where)[0]:
                keep.update(kept_names(cid, where))
        remove_target(path, keep, set(), failed)
        if failed:
            left.append(item)
        else:
            removed += 1
    try:
        if left:
            p.write_text(json.dumps({"items": left}, indent=2), encoding="utf-8")
        else:
            p.unlink()
    except OSError:
        pass                                                       # ignored: the list is tried again at the next start
    return {"removed": removed, "left": len(left), "refused": refused}


def reset_state() -> None:
    """Forget the measurement and the last run (tests)."""
    with _lock:
        _scan.update(running=False, done=False, token=None, taken=0.0, categories=[], progress={"category": "", "files": 0, "bytes": 0}, error=None, root="")
        _run.update(running=False, done=False, started=None, steps=[], ok=None, error=None, pending=0, categories=[])
