"""Where the large local stores live.

``data/interim`` holds parsed advisory files, derived tables and backups. Embedded SQLite records default to its ``database`` subfolder.
``ZTSA_DATABASE_DIR`` or the saved ``database_dir`` can place those records on another local disk.

The folder is chosen once per process, before the modules that use it are imported: the environment variable ``ZTSA_INTERIM_DIR`` wins, then the
``interim_dir`` field of ``app/backend/settings.json`` (the Settings page writes it), then ``data/interim`` next to the code. A relative
value is relative to the repository root, as every other path of the app is. Changing the setting takes effect after a restart, so the
server and the jobs it starts can never use two different folders. Moving the files is a deliberate copy by the user (see the README); the
app never moves them.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil

DEFAULT_INTERIM = "data/interim"
ENV = "ZTSA_INTERIM_DIR"


def _saved_value(field: str = "interim_dir") -> str:
    from app.backend import config                    # the file the Settings page writes (tests point it at a temporary folder)
    try:
        return str(json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8")).get(field) or "").strip()
    except (OSError, ValueError):
        return ""


def configured_interim() -> str:
    """The folder asked for: the environment, then the saved setting, else the default (as a string, not yet resolved)."""
    return os.environ.get(ENV, "").strip() or _saved_value() or DEFAULT_INTERIM


def saved_interim() -> str:
    """The folder the Settings page saved (it takes effect at the next start), else the default."""
    return _saved_value() or DEFAULT_INTERIM


def interim_dir() -> pathlib.Path:
    return pathlib.Path(configured_interim())


INTERIM = interim_dir()                    # fixed for the life of the process


def database_dir() -> pathlib.Path:
    """Embedded records folder: caller environment, saved folder, then interim/database."""
    value = os.environ.get("ZTSA_DATABASE_DIR", "").strip() or _saved_value("database_dir")
    return pathlib.Path(value).expanduser().resolve() if value else (INTERIM / "database").resolve()


def describe_database(candidate: str | None = None) -> dict:
    """Check a proposed local records folder without creating or moving a database."""
    active = database_dir()
    saved = (candidate or "").strip() or _saved_value("database_dir")
    target = pathlib.Path(saved).expanduser().resolve() if saved else (pathlib.Path(saved_interim()) / "database").resolve()
    result = describe(str(target))
    # The database subfolder is created lazily, including a not-yet-created interim folder.
    result["problems"] = [p for p in result["problems"] if p != "the parent folder does not exist"]
    ancestor = target
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if ancestor.is_file():
        result["problems"].append("a parent path is a file, not a folder")
    elif ancestor.is_dir() and not _writable(ancestor):
        result["problems"].append("the parent folder cannot be written to")
    result.update(in_effect=str(active), restart_required=target != active)
    return result


def _free_bytes(path: pathlib.Path) -> int | None:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _writable(path: pathlib.Path) -> bool:
    try:
        test = path / f".write-test-{os.getpid()}"
        test.write_bytes(b"x")
        test.unlink()
        return True
    except OSError:
        return False


def describe(candidate: str | None = None) -> dict:
    """Facts about a data folder for the Settings page: does it exist, can it be written, how much room is there, and, for the folder in use,
    whether a saved change is waiting for a restart. ``problems`` is empty when the folder can be used."""
    path = pathlib.Path((candidate or "").strip() or saved_interim())
    problems = []
    exists = path.exists()
    if exists and not path.is_dir():
        problems.append("that path is a file, not a folder")
    if not exists:
        parent = path.parent if str(path.parent) != str(path) else path
        if not parent.exists():
            problems.append("the parent folder does not exist")
    elif path.is_dir() and not _writable(path):
        problems.append("the folder cannot be written to")
    free = _free_bytes(path)
    if free is not None and free < 8 * 1024 ** 3:
        problems.append(f"only {free / 1024 ** 3:.1f} GB free; the collected files and the backups grow to several GB")
    return {"path": str(path), "in_effect": str(INTERIM), "restart_required": str(path) != str(INTERIM),
            "exists": exists, "free_gb": round(free / 1024 ** 3, 1) if free is not None else None, "problems": problems}
