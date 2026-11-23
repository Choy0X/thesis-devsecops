"""What happened in a sandbox container, kept on this computer: the commands it ran (with how long they took and how they ended), the changes made to it, samples of its CPU and memory, and
named sessions saved from that record.

Plain files under ``data/sandbox/history/<container name>/``: ``events.jsonl`` (one JSON object per event, appended), ``stats.jsonl`` (one sample per line, kept to the newest
``MAX_SAMPLES``) and ``sessions/<id>.json`` (a copy of the events up to the moment a session was saved). The record is keyed by the container's name, so it survives the container being
created again with another network, and it stays after the container is deleted (it can then be removed by name). Output of a command is kept to its last ``TAIL`` characters; a
secret that a command printed would otherwise be in it, so the command and its output are passed through the same masker as the code scan (``code_rules.mask``: keys, tokens, passwords,
bearer headers, credentials in URLs, private key bodies) before they are written. Masking is a best effort, not a guarantee, so the record stays local, is never exported with the audit logs,
is shown under the container's page only, and can be forgotten by name.
"""
from __future__ import annotations

import datetime
import json
import pathlib
import re
import time

from app.backend.pipeline import fsio, util
from app.backend.pipeline.scan import code_rules

ROOT = pathlib.Path("data/sandbox/history")
MAX_SAMPLES = 2000
KEEP_SAMPLES = 1000
MAX_EVENTS = 5000
TAIL = 2000
MIN_SAMPLE_GAP = 1.5                         # seconds between two samples of one container
_last_sample: dict[str, float] = {}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")


def _folder(name: str) -> pathlib.Path:
    if not _NAME.match(name or ""):
        raise ValueError("That is not the name of a sandbox container.")
    return ROOT / name


def _append(path: pathlib.Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")


def _read(path: pathlib.Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue                                                   # a half-written last line after a power cut is skipped
        if isinstance(row, dict):
            out.append(row)
    return out


def _trim(path: pathlib.Path, limit: int, keep: int) -> None:
    rows = _read(path)
    if len(rows) > limit:
        fsio.write_text(path, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows[-keep:]))


def record(name: str, kind: str, detail: dict | None = None) -> None:
    """Append an event. A failure to write the record never stops what it records."""
    try:
        path = _folder(name) / "events.jsonl"
        _append(path, {"at": util.now_zulu(), "kind": kind, **(detail or {})})
        _trim(path, MAX_EVENTS, MAX_EVENTS * 4 // 5)
    except (OSError, ValueError):
        pass                                                           # ignored: the record is a help; the container's own work goes on


def _masked(text: str, limit: int, *, head: bool) -> str:
    """``text`` with credentials masked, then cut to ``limit`` characters from its start (a command) or from its end (the tail of an output)."""
    window = text[:limit * 2] if head else text[-limit * 2:]
    done = code_rules.mask(window, config=True)
    return done[:limit] if head else done[-limit:]


def record_command(name: str, command: str, exit_code: int | None, seconds: float, output: str, timed_out: bool = False) -> None:
    # the output is masked first and cut after: a value cut in half would have no end for the patterns to find
    record(name, "command", {"command": _masked(command or "", 2000, head=True), "exit_code": exit_code, "seconds": round(seconds, 2), "timed_out": timed_out,
                             "output_tail": _masked(output or "", TAIL, head=False)})


def record_stats(name: str, sample: dict) -> None:
    """Append a sample of the container's CPU and memory, at most one every ``MIN_SAMPLE_GAP`` seconds."""
    now = time.monotonic()
    if now - _last_sample.get(name, -1e9) < MIN_SAMPLE_GAP:
        return
    _last_sample[name] = now
    try:
        path = _folder(name) / "stats.jsonl"
        _append(path, {"at": util.now_zulu(), **{k: sample.get(k) for k in ("cpu_pct", "mem_mb", "mem_limit_mb")}})
        _trim(path, MAX_SAMPLES, KEEP_SAMPLES)
    except (OSError, ValueError):
        pass                                                           # ignored: a sample that could not be written is only a gap in the graph


def events(name: str, limit: int = 200, kind: str | None = None) -> list[dict]:
    rows = _read(_folder(name) / "events.jsonl")
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    return rows[-max(1, min(int(limit), MAX_EVENTS)):]


def samples(name: str, limit: int = 600) -> list[dict]:
    return _read(_folder(name) / "stats.jsonl")[-max(1, min(int(limit), MAX_SAMPLES)):]


def known() -> list[dict]:
    """Every container that has a record, with the counts and the time of its last event."""
    out = []
    for d in sorted(ROOT.iterdir()) if ROOT.is_dir() else []:
        if not (d.is_dir() and _NAME.match(d.name)):
            continue
        rows = _read(d / "events.jsonl")
        out.append({"name": d.name, "events": len(rows), "commands": sum(1 for r in rows if r.get("kind") == "command"), "last": rows[-1]["at"] if rows else None,
                    "sessions": len(list((d / "sessions").glob("*.json"))) if (d / "sessions").is_dir() else 0})
    return out


def forget(name: str) -> dict:
    """Delete the whole record of one container (its events, samples and saved sessions)."""
    d = _folder(name)
    if not d.is_dir() or not d.resolve().is_relative_to(ROOT.resolve()):
        raise FileNotFoundError(f"There is no record of {name}.")
    import shutil
    shutil.rmtree(d)
    _last_sample.pop(name, None)
    return {"name": name, "forgotten": True}


# -- saved sessions --------------------------------------------------------------------------------------------------------------------------------------

def save_session(name: str, label: str) -> dict:
    """Keep a named copy of the container's events up to now (the commands and what came of them), to look at or replay by hand later."""
    label = (label or "").strip()
    if not label or len(label) > 80:
        raise ValueError("Give the session a name of up to 80 characters.")
    rows = events(name, MAX_EVENTS)
    if not rows:
        raise ValueError("There is nothing to save yet: no command has run in this container.")
    sid = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-").lower()[:30]
    path = _folder(name) / "sessions" / f"{sid}.json"
    fsio.write_json(path, {"id": sid, "label": label, "container": name, "saved_at": util.now_zulu(), "events": rows}, indent=1)
    return {"id": sid, "label": label, "events": len(rows)}


def sessions(name: str) -> list[dict]:
    d = _folder(name) / "sessions"
    out = []
    for f in sorted(d.glob("*.json"), reverse=True) if d.is_dir() else []:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"id": doc.get("id", f.stem), "label": doc.get("label"), "saved_at": doc.get("saved_at"), "events": len(doc.get("events", [])),
                    "commands": sum(1 for r in doc.get("events", []) if r.get("kind") == "command")})
    return out


def session(name: str, sid: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", sid or ""):
        raise ValueError("That is not the id of a session.")
    path = _folder(name) / "sessions" / f"{sid}.json"
    if not path.is_file():
        raise FileNotFoundError("There is no such session.")
    return json.loads(path.read_text(encoding="utf-8"))


def delete_session(name: str, sid: str) -> dict:
    session(name, sid)
    (_folder(name) / "sessions" / f"{sid}.json").unlink()
    return {"id": sid, "deleted": True}
