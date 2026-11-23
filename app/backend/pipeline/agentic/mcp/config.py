"""The MCP servers the owner has set up, kept in ``data/agent/mcp.json`` (gitignored, readable by this user only, like the other files that hold a secret).

A server is a name, the program that starts it (a list: the program and its arguments, run with no shell), the environment variables it needs (an API key, usually: the values are secrets and are never
sent back to the page, only their names), whether it is on, and how long a call may take. They are the owner's alone: a project's own files cannot define a server (that would make opening a project start a
program of its author's choosing), so the file is global.

What a server may be called is limited so that the name of a tool, ``mcp__<server>__<tool>``, can always be taken apart again: lower case letters, digits and hyphens, so no underscore.
"""
from __future__ import annotations

import json
import pathlib
import re

from app.backend.pipeline import fsio

ROOT = pathlib.Path("data/agent")
FILE = "mcp.json"
MAX_SERVERS = 8
MAX_ARGS = 32
MAX_ARG_CHARS = 500
MAX_ENV = 24
MAX_ENV_CHARS = 4_000
DEFAULT_TIMEOUT = 60
NAME = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
RESERVED_ENV = ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "BASH_ENV", "ENV", "PYTHONSTARTUP", "NODE_OPTIONS", "HOME", "TMPDIR")


class ConfigError(ValueError):
    """A definition that cannot be kept, as a sentence for the owner."""


def _read() -> dict:
    try:
        value = json.loads((ROOT / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def clean(name: str, raw: dict, saved: dict | None = None) -> dict:
    """One definition, checked. ``saved`` is what is kept now for this name: a variable sent with no value (``None``) keeps its saved value, so the page never needs to hold a secret."""
    name = str(name or "").strip()
    if not NAME.match(name):
        raise ConfigError(f"The server name {name!r} must be lower case letters, digits and hyphens, starting with a letter, 24 characters at most.")
    if not isinstance(raw, dict):
        raise ConfigError(f"The definition of {name} must be an object.")
    command = raw.get("command")
    if isinstance(command, str):
        command = command.split()
    if not isinstance(command, list) or not 1 <= len(command) <= MAX_ARGS or not all(isinstance(c, str) and c and "\x00" not in c and len(c) <= MAX_ARG_CHARS for c in command):
        raise ConfigError(f"The command of {name} is the program and its arguments, 1 to {MAX_ARGS} words of at most {MAX_ARG_CHARS} characters.")
    env_in = raw.get("env") or {}
    if not isinstance(env_in, dict) or len(env_in) > MAX_ENV:
        raise ConfigError(f"The variables of {name} must be an object of at most {MAX_ENV} names.")
    kept = (saved or {}).get("env") or {}
    env = {}
    for key, value in env_in.items():
        key = str(key)
        if not ENV_NAME.match(key) or key.startswith("AIVDF_") or key in RESERVED_ENV:
            raise ConfigError(f"{key!r} cannot be used as a variable of {name}.")
        if value is None:
            if key not in kept:
                raise ConfigError(f"{key} of {name} has no value.")
            env[key] = kept[key]
        else:
            text = str(value)
            if len(text) > MAX_ENV_CHARS or "\x00" in text:
                raise ConfigError(f"The value of {key} of {name} is too long.")
            env[key] = text
    try:
        timeout = int(raw.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        raise ConfigError(f"The time limit of {name} must be a number of seconds.") from None
    if not 1 <= timeout <= 600:
        raise ConfigError(f"The time limit of {name} is 1 to 600 seconds.")
    return {"command": [str(c) for c in command], "env": env, "enabled": bool(raw.get("enabled", True)), "timeout": timeout}


def load() -> dict[str, dict]:
    """Every server that is kept, by name. A damaged file, or a definition in it that is no longer valid, is read as nothing for that part: it never stops a session."""
    out = {}
    servers = _read().get("servers")
    for name, raw in (servers.items() if isinstance(servers, dict) else []):
        try:
            out[name] = clean(name, raw, raw)
        except ConfigError:
            continue
    return dict(list(out.items())[:MAX_SERVERS])


def save(servers: dict) -> dict[str, dict]:
    """Keeps the servers (a name to a definition, with a secret sent as ``None`` kept as it is) and returns what is kept."""
    if not isinstance(servers, dict) or len(servers) > MAX_SERVERS:
        raise ConfigError(f"There can be at most {MAX_SERVERS} servers.")
    saved = load()
    cleaned = {str(name).strip(): clean(name, raw, saved.get(str(name).strip())) for name, raw in servers.items()}
    fsio.write_json(ROOT / FILE, {"servers": cleaned}, indent=2, mode=0o600)
    return cleaned


def public(servers: dict | None = None) -> list[dict]:
    """What the page may see: no value of a variable, only its name."""
    servers = load() if servers is None else servers
    return [{"name": n, "command": d["command"], "env_names": sorted(d["env"]), "enabled": d["enabled"], "timeout": d["timeout"]} for n, d in servers.items()]


def enabled() -> dict[str, dict]:
    return {n: d for n, d in load().items() if d["enabled"]}
