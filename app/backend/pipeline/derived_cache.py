"""A size-limited cache of the files made from a model: the GGUF models in ``data/models/converted``.

Those files can always be made again from the model they came from, which stays where it was (the safetensors copy is the canonical one and is never touched here), so they are a cache. The
person may give it a budget (Settings, ``derived_cache_gb``; empty or 0 means no limit and then nothing is ever deleted). With a budget, a conversion that would take the cache above it first
makes room by deleting the model folders that were used least recently, and only those that are

* not pinned (a pin is kept in a file of its own, ``.pins.json``),
* not loaded by the model gateway, and not the model chosen in Settings for a role,
* direct subfolders of the converted folder: nothing outside it can be named, adapters (``data/models/adapters``), training runs and the canonical models are out of reach by construction, and
  the deletion itself is ``model_removal.remove``, which refuses a link, a hidden folder and the folder itself.

If enough cannot be freed that way, nothing at all is deleted: ``CacheFull`` says how much is needed, how much could be freed and why the rest cannot, and the person decides.

The last use of a model folder is written by the gateway when it loads a file from it (``.last_used.json``); the gateway and the application each write one file, so neither overwrites the other.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Callable

from app.backend.pipeline import fsio

GB = 1024 ** 3
LAST_USED, PINS = ".last_used.json", ".pins.json"


class CacheFull(RuntimeError):
    """The new files do not fit in the budget and cannot be made to fit without deleting what must stay. The message is a sentence; nothing was deleted."""


def default_root() -> pathlib.Path:
    from app.backend.pipeline import gguf_convert
    return gguf_convert.CONVERTED


def _read(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _names(root: pathlib.Path) -> list[str]:
    try:
        return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".") and not p.is_symlink())
    except OSError:
        return []


def folder_of(model_path, root: pathlib.Path | None = None) -> str | None:
    """The name of the model folder directly under ``root`` that holds ``model_path`` (a file in it or the folder), else None."""
    root = pathlib.Path(root or default_root())
    try:
        parts = pathlib.Path(os.path.abspath(str(model_path))).relative_to(pathlib.Path(os.path.abspath(root))).parts
    except ValueError:
        return None
    return parts[0] if parts and not parts[0].startswith(".") else None


# -- last use and pins ---------------------------------------------------------------------------------------------------------------------------------

def last_used(root: pathlib.Path | None = None) -> dict[str, float]:
    data = _read(pathlib.Path(root or default_root()) / LAST_USED)
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))} if isinstance(data, dict) else {}


def touch(model_path, root: pathlib.Path | None = None, now: float | None = None) -> bool:
    """Record that the model folder holding ``model_path`` was used now. False when the path is not in the cache or the record cannot be written (a load never fails for it)."""
    root = pathlib.Path(root or default_root())
    name = folder_of(model_path, root)
    if name is None or not (root / name).is_dir():
        return False
    used = {k: v for k, v in last_used(root).items() if (root / k).is_dir()}
    used[name] = time.time() if now is None else now
    try:
        fsio.write_json(root / LAST_USED, used, indent=1)
    except OSError:
        return False
    return True


def pins(root: pathlib.Path | None = None) -> set[str]:
    data = _read(pathlib.Path(root or default_root()) / PINS)
    return {x for x in data if isinstance(x, str)} if isinstance(data, list) else set()


def set_pin(name: str, pinned: bool, root: pathlib.Path | None = None) -> bool:
    """Pin or unpin a model folder by its name. False when there is no such folder in the cache."""
    root = pathlib.Path(root or default_root())
    if name not in _names(root):
        return False
    current = {p for p in pins(root) if p in _names(root)}
    current = current | {name} if pinned else current - {name}
    fsio.write_json(root / PINS, sorted(current), indent=1)
    return True


# -- what is in it -------------------------------------------------------------------------------------------------------------------------------------

def _size(folder: pathlib.Path) -> tuple[int, int]:
    total = files = 0
    for dirpath, _dirs, names in os.walk(folder):
        for n in names:
            files += 1
            try:
                total += os.lstat(os.path.join(dirpath, n)).st_size
            except OSError:
                continue                                    # ignored: a file that vanished while counting is not a reason to stop
    return total, files


def entries(root: pathlib.Path | None = None, *, in_use: Callable[[dict], str | None] | None = None, settings_use: Callable[[dict], list[str]] | None = None) -> list[dict]:
    """The model folders of the cache, the least recently used first: ``{name, path, bytes, files, last_used, pinned, label, in_use, settings_roles}``. ``last_used`` is the time the gateway
    last loaded a file from it, else the time the folder was made."""
    root = pathlib.Path(root or default_root())
    used, pinned = last_used(root), pins(root)
    out = []
    for name in _names(root):
        folder = root / name
        total, files = _size(folder)
        try:
            made = folder.stat().st_mtime
        except OSError:
            made = 0.0
        entry = {"name": name, "path": str(folder), "bytes": total, "files": files, "last_used": used.get(name, made), "used": name in used, "pinned": name in pinned,
                 "label": _label(folder)}
        entry["in_use"] = in_use(entry) if in_use else None
        entry["settings_roles"] = list(settings_use(entry)) if settings_use else []
        out.append(entry)
    return sorted(out, key=lambda e: (e["last_used"], e["name"]))


def _label(folder: pathlib.Path) -> str | None:
    from app.backend.pipeline import gguf_convert
    return gguf_convert.converted_label(folder)


def usage(root: pathlib.Path | None = None) -> int:
    return sum(e["bytes"] for e in entries(root))


# -- making room ---------------------------------------------------------------------------------------------------------------------------------------

def plan_room(needed_bytes: int, budget_bytes: int, *, root: pathlib.Path | None = None, in_use=None, settings_use=None, protect=()) -> dict:
    """What would have to be deleted for ``needed_bytes`` more to fit in ``budget_bytes``: ``{budget, used, needed, evict, freed, fits, kept, message}``. Deletes nothing. A budget of 0 or
    less is no limit. ``evict`` is empty when the room is not enough even with every model that may go, so that nothing is deleted for nothing."""
    listed = entries(root, in_use=in_use, settings_use=settings_use)
    used = sum(e["bytes"] for e in listed)
    out = {"budget": max(0, int(budget_bytes)), "used": used, "needed": int(needed_bytes), "evict": [], "freed": 0, "fits": True, "kept": [], "message": ""}
    if budget_bytes <= 0 or used + needed_bytes <= budget_bytes:
        out["message"] = "It fits." if budget_bytes > 0 else "There is no limit."
        return out
    over = used + needed_bytes - budget_bytes
    keep_reason = lambda e: ("it is pinned" if e["pinned"] else "the model gateway has it loaded" if e["in_use"] else
                             "Settings chooses it for " + " and ".join(e["settings_roles"]) if e["settings_roles"] else "it is being made" if e["name"] in protect else None)
    may_go, kept = [], []
    for e in listed:
        why = keep_reason(e)
        (kept if why else may_go).append({**e, "why": why} if why else e)
    chosen, freed = [], 0
    for e in may_go:                                       # the least recently used first
        if freed >= over:
            break
        chosen.append(e)
        freed += e["bytes"]
    out["kept"] = [{"name": e["name"], "bytes": e["bytes"], "why": e["why"]} for e in kept]
    if freed < over:
        out.update(fits=False, evict=[], freed=0, message=f"{_gb(needed_bytes)} more would take the cache from {_gb(used)} to {_gb(used + needed_bytes)} against a budget of {_gb(budget_bytes)}, and only "
                   f"{_gb(sum(e['bytes'] for e in may_go))} of it can be freed" + (": " + "; ".join(f"{e['name']} stays because {e['why']}" for e in kept) if kept else "") + ". Nothing was deleted.")
        return out
    out.update(evict=chosen, freed=freed, message=f"{len(chosen)} model(s) least recently used would be deleted to make room: " + ", ".join(e["name"] for e in chosen) + f" ({_gb(freed)}).")
    return out


def _gb(n) -> str:
    return f"{n / GB:.1f} GB"


def make_room(needed_bytes: int, budget_gb: float, *, root: pathlib.Path | None = None, in_use=None, settings_use=None, remove=None, protect=(), log=print) -> dict:
    """Delete what ``plan_room`` says, so that ``needed_bytes`` more fit in the budget. Raises ``CacheFull`` (and deletes nothing) when they cannot. Returns the plan with what was removed."""
    root = pathlib.Path(root or default_root())
    in_use = in_use if in_use is not None else loaded_by_gateway
    settings_use = settings_use if settings_use is not None else chosen_in_settings
    p = plan_room(needed_bytes, int(float(budget_gb or 0) * GB), root=root, in_use=in_use, settings_use=settings_use, protect=protect)
    if not p["fits"]:
        raise CacheFull(p["message"])
    removed = []
    for e in p["evict"]:
        (remove or _remove)(e, root)
        removed.append(e["name"])
        log(f"deleted the converted model {e['name']} ({_gb(e['bytes'])}) to stay within the cache budget; it can be made again from the model it came from")
    if removed:
        used = {k: v for k, v in last_used(root).items() if k not in removed}
        try:
            fsio.write_json(root / LAST_USED, used, indent=1)
        except OSError:
            pass  # ignored: a stale record of a folder that is gone is dropped by the next touch
    return {**p, "removed": removed}


def _remove(entry: dict, root: pathlib.Path) -> None:
    """Delete one model folder with the app's own removal, which refuses a link, a hidden folder, the converted folder itself and anything that is not inside it."""
    from app.backend.pipeline import model_removal
    roots = model_removal.Roots(models=None, converted=root, hub=pathlib.Path(os.devnull), ollama=None)
    model_removal.remove({"source": "converted", "id": entry["name"], "path": entry["path"]}, roots)


def loaded_by_gateway(entry: dict) -> str | None:
    """A sentence when the model gateway has a file of this folder loaded (or is loading it), else None."""
    from app.backend.pipeline.gateway import client as gateway
    here = os.path.normcase(os.path.abspath(entry["path"]))
    for model in gateway.running_source_paths():
        there = os.path.normcase(os.path.abspath(model))
        if there == here or there.startswith(here + os.sep):
            return "the model gateway has it loaded"
    return None


def chosen_in_settings(entry: dict) -> list[str]:
    """The roles Settings gives a file of this folder."""
    from app.backend.config import Settings
    s = Settings.load()
    here = os.path.normcase(os.path.abspath(entry["path"]))
    roles = []
    for role, value in (("the language model", s.llm_local_model), ("the embedding model", s.embed_local_model)):
        v = (value or "").strip()
        if v:
            there = os.path.normcase(os.path.abspath(v))
            if there == here or there.startswith(here + os.sep):
                roles.append(role)
    return roles


def for_job(budget_gb) -> Callable[[int], dict] | None:
    """The function a conversion calls with the size of what it is about to write, or None when the person set no budget (nothing is then ever deleted)."""
    try:
        budget = float(budget_gb or 0)
    except (TypeError, ValueError):
        return None
    if budget <= 0:
        return None
    return lambda needed: make_room(needed, budget)
