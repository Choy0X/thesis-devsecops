"""The sessions an earlier version of the application left as files: ``data/projects/<project id>/agent_sessions/<session id>.json`` (schema ``agent_session/1``).

They are only ever READ here. A session that is still a file is shown and used as it is; the first time it is changed it is imported into the store (``agent_sessions._ensure``), and from then on
the store holds it. The file is left in place (it is the way back to the earlier version) and removed only when the owner deletes the session. ``tools/agent_sessions_migrate.py`` imports them all
at once, after a backup. The ``.outputs`` folders of the commands a session ran stay where they are: they are files by design.
"""
from __future__ import annotations

import json
import pathlib
import re

from app.backend.pipeline import projects
from app.backend.pipeline.agentic.engine.errors import AgentSessionError, NotFound

SCHEMA = "agent_session/1"
ID = re.compile(r"[0-9a-f]{12}")


def folder(project_id: str) -> pathlib.Path:
    return projects.ROOT / project_id / "agent_sessions"


def path(project_id: str, session_id: str) -> pathlib.Path:
    """The file of a session. Only a twelve-digit hexadecimal id names one: an id read out of a file's content is never a path."""
    if not ID.fullmatch(session_id or ""):
        raise NotFound()
    return folder(project_id) / f"{session_id}.json"


def read_path(file: pathlib.Path) -> dict:
    """The session in a file. Raises ``AgentSessionError`` (422, ``damaged``) when it cannot be read."""
    try:
        doc = json.loads(file.read_text(encoding="utf-8"))
    except OSError:
        raise AgentSessionError("This session's file cannot be read.", 422, "damaged") from None
    except ValueError:
        raise AgentSessionError("This session's file is damaged and cannot be read. It was left as it is; delete it to remove it.", 422, "damaged") from None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(doc.get("transcript"), list):
        raise AgentSessionError("This session's file is not in a format this version reads. It was left as it is; delete it to remove it.", 422, "damaged")
    return doc


def read(project_id: str, session_id: str) -> dict | None:
    """The session of that id if it is still a file, else None."""
    file = path(project_id, session_id)
    return read_path(file) if file.is_file() else None


def list_all(project_id: str) -> tuple[list[dict], int]:
    """``(the sessions that are files, how many files could not be read)``."""
    root = folder(project_id)
    docs, damaged = [], 0
    for file in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            doc = read_path(file)
        except AgentSessionError:
            damaged += 1
            continue
        if doc.get("id") != file.stem or not ID.fullmatch(file.stem):          # a file whose content names another session (or a path) is not a session this version can use
            damaged += 1
            continue
        docs.append(doc)
    return docs, damaged


def remove(project_id: str, session_id: str) -> None:
    """Remove the file of a session. No file is what a removal wants; a file that is there and cannot be removed (another program has it open) is an error, because the session would come back."""
    file = path(project_id, session_id)
    try:
        file.unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        raise AgentSessionError(f"The session's file could not be removed ({type(e).__name__}). Close whatever has it open and delete the session again.", 409, "file_in_use") from None
