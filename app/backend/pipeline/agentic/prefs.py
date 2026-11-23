"""What the owner chose last for each model, kept beside the app's other agent files (``data/agent/prefs.json``): the thinking effort. A new session with a model starts at the level its owner last
set for that model, so a model that is slow to think is not asked to for every new session, and one that is worth it stays that way.
"""
from __future__ import annotations

import json
import pathlib

from app.backend.pipeline import fsio
from app.backend.pipeline.agentic import thinking

ROOT = pathlib.Path("data/agent")
FILE = "prefs.json"
MAX_MODELS = 200


def _read() -> dict:
    try:
        value = json.loads((ROOT / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def effort_for(ref: str) -> str | None:
    """The level last set for the model ``ref`` (an empty ref is the model the settings name), or None when it was never set."""
    value = (_read().get("effort") or {}).get(str(ref or ""))
    return value if value in thinking.EFFORTS else None


def remember_effort(ref: str, effort: str) -> None:
    """Keeps ``effort`` as the level to start the next session of ``ref`` at. A level that is not one is ignored; the list is kept to ``MAX_MODELS`` models, the oldest dropped."""
    if effort not in thinking.EFFORTS:
        return
    doc = _read()
    levels = {k: v for k, v in (doc.get("effort") or {}).items() if v in thinking.EFFORTS and str(k) != str(ref or "")}
    levels[str(ref or "")] = effort
    doc["effort"] = dict(list(levels.items())[-MAX_MODELS:])
    fsio.write_json(ROOT / FILE, doc, indent=2)
