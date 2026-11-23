"""Changes to the saved settings that a newer version of the app makes: applied once, recorded, and shown to the person until they have read them.

A migration never deletes a credential or an endpoint. It moves a setting to where the app now looks for it, or drops one the app no longer reads, and it says what it found and what it
did. Each runs once (its id goes into the record), so a person who changes a setting back is not overruled at the next start. The record sits beside the settings file.
"""
from __future__ import annotations

import json
import ipaddress
import urllib.parse

from app.backend import config
from app.backend.pipeline import fsio, util

OLLAMA_PORT = 11434                                        # the port Ollama listens on by default


def record_path():
    return config.SETTINGS_PATH.with_name("settings_migrations.json")


def _read() -> dict:
    try:
        data = json.loads(record_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("applied"), list) else {"applied": []}
    except (OSError, ValueError):
        return {"applied": []}


def _is_ollama(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url if "://" in url else "//" + url)
        if (parsed.scheme not in ("", "http", "https") or parsed.port != OLLAMA_PORT
                or not parsed.hostname or parsed.username is not None):
            return False
        return parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def matches_ollama_runtime(url: str) -> bool:
    """Check that a legacy endpoint addresses the Ollama instance this app uses."""
    if not _is_ollama(url):
        return False
    from app.backend.pipeline.ollama.client import base_url

    try:
        legacy = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
        active = urllib.parse.urlsplit(base_url())
        if (legacy.path.rstrip("/") not in ("", "/v1") or legacy.query or legacy.fragment
                or legacy.scheme != active.scheme or legacy.port != active.port):
            return False
        return legacy.hostname == active.hostname or {legacy.hostname, active.hostname} == {"localhost", "127.0.0.1"}
    except ValueError:
        return False


def _ollama_cutover(raw: dict, s: config.Settings) -> list[str]:
    """Keep the historical migration ID without clearing settings on older installs."""
    return []


def _drop_sqlite_backend(raw: dict, s: config.Settings) -> list[str]:
    """Retire the historic backend selector without reopening historic data files."""
    if str(raw.get("database_backend") or "").strip().lower() != "sqlite":
        return []
    return ["Your settings chose a legacy database backend. That selector was removed. The app now uses its embedded SQLite database. "
            "Historic data files were not opened or changed; import existing data explicitly before using it."]


def _embedded_database(raw: dict, s: config.Settings) -> list[str]:
    """Explain the engine change without activating or disclosing the saved source connection."""
    if not any(raw.get(k) for k in config.LEGACY_MONGO_FIELDS):
        return []
    return ["The app now stores records in a local SQLite database. Your previous MongoDB connection settings remain private in settings.json for an explicit migration or rollback. "
            "MongoDB data was not opened or changed. Import that data explicitly to make it available here; starting the app does not migrate it."]


RETIRED_ENGINE_KEYS = ("llm_provider", "llama_server_path")


def _engines(raw: dict, s: config.Settings) -> list[str]:
    """Migrate saved language-model settings to the current engine fields."""
    changes = []
    provider = str(raw.get("llm_provider") or "").strip().lower()
    if not s.llm_engine:
        if provider == "managed":
            s.set_field("llm_engine", "local")
            changes.append("The language-model source 'local model server' is now the engine Local. Choose the model in Settings to run it in Ollama. The saved endpoint is available for the External engine.")
        elif str(raw.get("llm_base_url") or "").strip() and not matches_ollama_runtime(s.llm_base_url):
            s.set_field("llm_engine", "external")
            changes.append("Your language-model endpoint is kept as the engine External.")
    if str(raw.get("llama_server_path") or "").strip():
        changes.append("The app now runs local models in Ollama. The legacy llama.cpp program path was removed.")
    return changes


def _ollama_runtime(raw: dict, s: config.Settings) -> list[str]:
    if s.llm_engine == "local" or not matches_ollama_runtime(str(raw.get("llm_base_url") or "").strip()):
        return []
    s.set_field("llm_engine", "local")
    return ["Your loopback language-model endpoint now uses the Local engine in Ollama. Its saved endpoint and model remain available in Settings."]


def _local_project_search(raw: dict, s: config.Settings) -> list[str]:
    private_fields = config.RETIRED_RETRIEVAL_FIELDS | config.LEGACY_RETRIEVAL_FIELDS
    if not any(raw.get(key) for key in private_fields):
        return []
    return ["Projects now search local files. Your legacy retrieval settings stay private for historical experiments."]


MIGRATIONS = (("ollama-cutover-2026-10", _ollama_cutover, ()), ("drop-sqlite-backend-2026-10", _drop_sqlite_backend, ("database_backend",)),
              ("engines-2026-10", _engines, RETIRED_ENGINE_KEYS), ("embedded-database-2026-10", _embedded_database, ()),
              ("ollama-runtime-2026-11", _ollama_runtime, ()), ("local-project-search-2026-11", _local_project_search, ()))


def run() -> list[dict]:
    """Apply the migrations that have not run yet, record them, and return the records made now. Never raises: a migration that fails is left for the next start."""
    state = _read()
    done = {m["id"] for m in state["applied"]}
    made = []
    try:
        raw = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
        raw = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        raw = {}                                            # the file as the person saved it: every migration reads this, not what an earlier one wrote
    for mid, fn, drops in MIGRATIONS:
        if mid in done:
            continue
        try:
            s = config.Settings.load()
            changes = fn(raw, s)
            if changes or any(k in raw for k in drops):
                s.save()                                    # also writes the file without the settings the app no longer has
                s.apply_env()
        except Exception as e:                              # ignored: the settings are left as they were and the migration is tried again at the next start
            print(f"settings migration {mid} did not run: {type(e).__name__}: {e}")
            continue
        rec = {"id": mid, "at": util.now_zulu(), "changes": changes, "acknowledged": not changes}
        state["applied"].append(rec)
        made.append(rec)
    if made:
        fsio.write_json(record_path(), state, indent=2)
    return made


def pending() -> list[dict]:
    """The migrations that changed something and that the person has not acknowledged."""
    out = []
    for record in _read()["applied"]:
        if record.get("acknowledged") or record["id"] == "ollama-cutover-2026-10":
            continue
        changes = record.get("changes") or []
        if record["id"] == "engines-2026-10":
            changes = [message for message in changes if not message.startswith(
                ("Choose an embedding GGUF", "Ollama manages GPU placement for the embedding model"))]
        if changes:
            out.append({**record, "changes": changes})
    return out


def acknowledge(ids: list[str] | None = None) -> int:
    """Mark migrations as read (all of them without ``ids``); returns how many were marked."""
    state = _read()
    n = 0
    for m in state["applied"]:
        if m.get("changes") and not m.get("acknowledged") and (ids is None or m["id"] in ids):
            m["acknowledged"] = True
            n += 1
    if n:
        fsio.write_json(record_path(), state, indent=2)
    return n
