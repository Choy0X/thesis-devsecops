"""Deleting an installed model: from the Hugging Face cache, from a model folder, from the app's own converted models, or from Ollama.

The page never names a path to delete. It names a model (source, id and path as the inventory listed them), ``find`` looks it up in the inventory of the moment, and the
delete happens only inside the folder that source belongs to. A plan says what would go (and why a model cannot be deleted) and deletes nothing; ``remove`` refuses in a sentence
while something uses the model, and what it deletes it deletes completely or reports.

An Ollama model is removed the way Ollama removes one (its own ``RemoveNamed``): the digests its manifest names are noted, the manifest goes, and every one of those layers that no
other manifest names goes too; the folders that are empty afterwards are removed. The weights, the only large file, are deleted first, so a file that another program holds
leaves the model whole instead of listed in Ollama with its weights gone. Ollama keeps the files of a loaded model open, so a running Ollama is stopped first, and only when asked.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import stat
import time
from dataclasses import dataclass

from app.backend.pipeline import hub_download, models_local

DIGEST = re.compile(r"^sha256[:-]([0-9a-f]{64})$")          # the only shape of a layer name that is ever turned into a file name
PROCESS_PREFIX = "ollama"                                   # ollama.exe, "ollama app.exe" (the tray program), ollama_llama_server.exe (a model runner)
STOP_WAIT = 5.0                                             # seconds Ollama's programs get to end after the first request, and again after the second
LOCK_TRIES, LOCK_DELAY = 10, 0.3                            # a file that was just released by a program that ended may still be locked for a moment

_remove = os.remove                                         # indirections for the tests
_sleep = time.sleep


class Refused(ValueError):
    """The request cannot be done as asked (not a model the app may delete). The message is a sentence."""


class NotInstalled(FileNotFoundError):
    """The model is not on this computer (any more)."""


class Busy(RuntimeError):
    """Something uses the model, Ollama is running, or a file is held by another program. The message is a sentence; nothing, or nothing more, is deleted."""


@dataclass(frozen=True)
class Roots:
    """The folders a model may be deleted from: the models folder (without what the app writes into it itself), the converted models, the Hugging Face cache and Ollama's."""
    models: pathlib.Path | None
    converted: pathlib.Path | None
    hub: pathlib.Path
    ollama: pathlib.Path | None


def _pathkey(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _same(a, b) -> bool:
    return _pathkey(a) == _pathkey(b)


def find(inventory: dict, source: str, model_id: str, path: str) -> dict:
    """The model the inventory lists with this source, id and path, else ``NotInstalled``. Nothing the page sends is used except to pick an entry of the inventory."""
    for m in inventory.get("models", []):
        if m["source"] == source and m["id"] == model_id and _same(m["path"], path):
            return m
    raise NotInstalled("That model is not installed on this computer any more.")


# -- reading a folder -------------------------------------------------------------------------------------------------------------------------------------------

def _tree(path: pathlib.Path) -> tuple[int, int]:
    """``(bytes, files)`` of everything under a folder; a link is counted as a file of its own size and not followed."""
    total = files = 0
    for dirpath, _dirs, names in os.walk(path):
        for name in names:
            files += 1
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue                                    # ignored: a file that vanished while counting is not a reason to stop the plan
    return total, files


def _folder_target(model: dict, root: pathlib.Path | None, skip: set[str]) -> pathlib.Path:
    """The model's folder, checked to lie inside ``root`` (not be ``root``), not to be a link or reached through one, and not to be a folder the app writes into itself."""
    if root is None:
        raise Refused("The app does not manage the folder this model is in.")
    p, r = pathlib.Path(os.path.abspath(model["path"])), pathlib.Path(os.path.abspath(root))
    if _pathkey(p) == _pathkey(r):
        raise Refused("This row stands for loose model files that lie directly in the folder, not for a folder of its own. Deleting it would delete the whole folder, so the app does not offer it. "
                      "Remove those files yourself, or put each model in its own folder.")
    try:
        parts = p.relative_to(r).parts
    except ValueError:
        raise Refused("That folder is not inside the models folder the app scans.") from None
    if any(x.startswith(".") for x in parts) or (parts and parts[0] in skip):
        raise Refused("That is not a model folder; the app does not delete it.")
    expected = pathlib.Path(os.path.realpath(r)).joinpath(*parts)
    if os.path.normcase(os.path.realpath(p)) != os.path.normcase(str(expected)):
        raise Refused("This entry is a link to another place. Deleting through it could delete something that is not a model, so the app does not; remove the link yourself.")
    if not p.is_dir():
        raise NotInstalled("That folder is not on this computer any more.")
    return p


# -- Ollama ------------------------------------------------------------------------------------------------------------------------------------------------------

def _ollama_model(model: dict, roots: Roots):
    if roots.ollama is None:
        raise Refused("The app does not know where Ollama keeps its models.")
    for manifest, entry in models_local.ollama_manifests(roots.ollama):
        if entry["id"] == model["id"] and _same(entry["path"], model["path"]):
            return manifest
    raise NotInstalled("That model is not in Ollama's folder any more.")


def _layers(manifest: pathlib.Path) -> tuple[list[str], str | None]:
    """``(every layer digest the manifest names, the digest of the weights)`` as the hex part of a valid ``sha256`` name; a name of any other shape is not used."""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    if not isinstance(data, dict):
        return [], None
    layers = [l for l in (data.get("layers") or []) if isinstance(l, dict)]
    named = [(data.get("config") or {}).get("digest") if isinstance(data.get("config"), dict) else None] + [l.get("digest") for l in layers]
    hexes: list[str] = []
    for d in named:
        m = DIGEST.match(d) if isinstance(d, str) else None
        if m and m.group(1) not in hexes:
            hexes.append(m.group(1))
    weights = next((DIGEST.match(l["digest"]).group(1) for l in layers if str(l.get("mediaType", "")).endswith("image.model") and isinstance(l.get("digest"), str) and DIGEST.match(l["digest"])), None)
    return hexes, weights


def _named_elsewhere(root: pathlib.Path, manifest: pathlib.Path, hexes: list[str]) -> set[str]:
    """The layers among ``hexes`` that another manifest file mentions. The files are searched as text, so a manifest that cannot be parsed still protects what it names."""
    found: set[str] = set()
    for f in (root / "manifests").rglob("*"):
        if not f.is_file() or _same(f, manifest):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue                                        # ignored: a file that cannot be read cannot be searched; Ollama itself skips such a manifest too
        found.update(h for h in hexes if h in text)
    return found


def _ollama_files(model: dict, roots: Roots) -> dict:
    """What deleting an Ollama model would delete: its manifest and its layers that no other manifest names (the weights apart), and what stays because another model uses it."""
    manifest = _ollama_model(model, roots)
    hexes, weights_hex = _layers(manifest)
    shared = _named_elsewhere(roots.ollama, manifest, hexes)
    blobs = {h: roots.ollama / "blobs" / f"sha256-{h}" for h in hexes}
    gone = [blobs[h] for h in hexes if h not in shared and blobs[h].is_file()]
    weights = blobs[weights_hex] if weights_hex and weights_hex not in shared and blobs[weights_hex].is_file() else None
    kept = [blobs[h] for h in hexes if h in shared and blobs[h].is_file()]
    return {"manifest": manifest, "weights": weights, "others": [b for b in gone if b != weights], "kept": kept}


def ollama_processes() -> list:
    """Every running program that is Ollama's (its name starts with "ollama"): the tray program, the server and the model runners. A program that cannot be read is skipped."""
    try:
        import psutil
    except ImportError:
        return []
    found = []
    for p in psutil.process_iter(["name"]):
        try:
            name = p.info.get("name") or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name.lower().startswith(PROCESS_PREFIX):
            found.append(p)
    return found


def _name(p) -> str:
    try:
        return p.name()
    except Exception:  # ignored: a program that ended or cannot be read is still named by what the listing saw
        return (getattr(p, "info", None) or {}).get("name") or PROCESS_PREFIX


def _names(procs) -> list[str]:
    out: list[str] = []
    for p in _stop_order(procs):
        if _name(p) not in out:
            out.append(_name(p))
    return out


def _stop_order(procs) -> list:
    """The tray program first (it starts the server again when the server ends), then the server, then the model runners."""
    def rank(p) -> int:
        n = _name(p).lower()
        return 0 if "app" in n else 1 if n.rsplit(".", 1)[0] == PROCESS_PREFIX else 2
    return sorted(procs, key=rank)


def _running(p) -> bool:
    try:
        return bool(p.is_running())
    except Exception:  # ignored: a program that cannot be asked is taken as ended, and the files tell if it was not
        return False


def _try(call) -> None:
    try:
        call()
    except Exception:  # ignored: a program that ended meanwhile, or that may not be ended, is found out by the check that follows
        pass


def _wait_gone(procs, seconds: float, sleep, now) -> list:
    start = now()
    alive = [p for p in procs if _running(p)]
    while alive and now() - start < seconds:
        sleep(0.1)
        alive = [p for p in alive if _running(p)]
    return alive


def stop_processes(procs, wait_seconds: float = STOP_WAIT, sleep=time.sleep, now=time.monotonic) -> list[str]:
    """End Ollama's programs: ask each to end, give them ``wait_seconds``, force the ones still running, give them the same time again. The names of those that still run."""
    ordered = _stop_order(procs)
    for p in ordered:
        _try(p.terminate)
    alive = _wait_gone(ordered, wait_seconds, sleep, now)
    for p in alive:
        _try(p.kill)
    return [_name(p) for p in _wait_gone(alive, wait_seconds, sleep, now)]


# -- the plan ----------------------------------------------------------------------------------------------------------------------------------------------------

def _facts(model: dict, roots: Roots) -> dict:
    source = model["source"]
    if source == "hub_cache":
        try:
            folder = hub_download.repo_folder(model["id"], roots.hub)
        except ValueError as e:
            raise Refused(str(e)) from None
        if not _same(folder, model["path"]):
            raise Refused("That is not a model of the cache.")
        try:
            plan_ = hub_download.removal_plan(model["id"], roots.hub)
        except FileNotFoundError as e:
            raise NotInstalled(str(e)) from None
        return {"kind": "cache", "bytes": plan_["bytes"], "files": _tree(folder)[1], "revisions": plan_["revisions"], "kept_shared_files": 0}
    if source in ("folder", "converted"):
        root = roots.models if source == "folder" else roots.converted
        target = _folder_target(model, root, {"converted", "adapters", "hub", "__pycache__"} if source == "folder" else set())
        total, files = _tree(target)
        return {"kind": "folder", "bytes": total, "files": files, "revisions": 0, "kept_shared_files": 0}
    if source == "ollama":
        parts = _ollama_files(model, roots)
        doomed = [parts["manifest"]] + ([parts["weights"]] if parts["weights"] else []) + parts["others"]
        return {"kind": "ollama", "bytes": sum(os.lstat(f).st_size for f in doomed), "files": len(doomed), "revisions": 0, "kept_shared_files": len(parts["kept"])}
    raise Refused("The app cannot delete a model of that kind.")


def plan(model: dict, roots: Roots, *, in_use=None, settings_use=None, processes=None) -> dict:
    """What deleting this model would do, and whether it may be done now. Deletes nothing. ``in_use(model)`` answers a sentence or None, ``settings_use(model)`` a list of the
    roles Settings gives this model, ``processes()`` the running programs of Ollama."""
    out = {"source": model["source"], "id": model["id"], "path": model["path"], "kind": None, "removable": True, "refusal": None, "bytes": 0, "files": 0, "revisions": 0,
           "kept_shared_files": 0, "in_use": None, "settings_use": [], "ollama_running": False, "ollama_processes": []}
    try:
        out.update(_facts(model, roots))
    except Refused as e:
        out["removable"], out["refusal"] = False, str(e)
        return out
    out["in_use"] = in_use(model) if in_use else None
    out["settings_use"] = list(settings_use(model)) if settings_use else []
    if model["source"] == "ollama":
        procs = (processes or ollama_processes)()
        out["ollama_running"], out["ollama_processes"] = bool(procs), _names(procs)
    return out


# -- deleting -----------------------------------------------------------------------------------------------------------------------------------------------------

def _delete_file(path: pathlib.Path) -> bool:
    """Delete one file; a file a program still holds is tried again for a moment. False when it stayed."""
    for attempt in range(LOCK_TRIES):
        try:
            _remove(path)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if attempt < LOCK_TRIES - 1:
                _sleep(LOCK_DELAY)
    return False


def _held(name: str) -> str:
    return f"A file of this model ({name}) is open in another program, so it was not deleted. Close the program that uses it and try again."


def _rmtree(path: pathlib.Path) -> None:
    def onerror(func, p, exc_info):
        err = exc_info[1]
        if isinstance(err, PermissionError) and os.path.exists(p):
            try:
                os.chmod(p, stat.S_IWRITE)                  # a read-only file: made writable, then deleted once more
                func(p)
                return
            except OSError:
                pass
        raise err
    try:
        shutil.rmtree(path, onerror=onerror)
    except PermissionError as e:
        raise Busy(_held(pathlib.Path(e.filename).name if e.filename else path.name) + " Part of the model may already be deleted.") from None


def _prune_empty_parents(start: pathlib.Path, root: pathlib.Path) -> None:
    """Remove the folders above a deleted model that are empty now, up to (not including) the folder it was scanned in."""
    cur = start
    while cur.is_dir() and _pathkey(cur) != _pathkey(root) and _pathkey(cur).startswith(_pathkey(root)):
        try:
            cur.rmdir()                                     # only an empty folder can be removed this way
        except OSError:
            return
        cur = cur.parent


def remove(model: dict, roots: Roots, *, in_use=None, stop_ollama: bool = False, processes=None, stop=None) -> dict:
    """Delete one model. Refuses in a sentence (``Refused`` for a model the app may not delete, ``Busy`` for one in use, for a running Ollama that was not told to stop, for Ollama that
    does not stop and for a weights file another program holds) and then deletes nothing. ``stop_ollama`` ends Ollama's programs first. Returns what was freed and what was stopped."""
    procs = (processes or ollama_processes)() if model["source"] == "ollama" else []
    p = plan(model, roots, in_use=in_use, processes=lambda: procs)
    if not p["removable"]:
        raise Refused(p["refusal"])
    if p["in_use"]:
        raise Busy(p["in_use"])
    stopped: list[str] = []
    if procs:
        if not stop_ollama:
            raise Busy("Ollama is running, and it keeps the files of its models open. Stop it first.")
        survivors = (stop or stop_processes)(procs)
        if survivors:
            raise Busy(f"Ollama could not be stopped ({', '.join(dict.fromkeys(survivors))} still runs), so nothing was deleted.")
        stopped = _names(procs)
    left_behind: list[str] = []
    if p["kind"] == "cache":
        try:
            hub_download.remove(model["id"], cache=roots.hub)
        except PermissionError as e:
            raise Busy(_held(pathlib.Path(e.filename).name if e.filename else model["id"])) from None
    elif p["kind"] == "folder":
        target = pathlib.Path(os.path.abspath(model["path"]))
        _rmtree(target)
        _prune_empty_parents(target.parent, roots.models if model["source"] == "folder" else roots.converted)
    else:
        left_behind = _remove_ollama(model, roots)
    return {"source": model["source"], "id": model["id"], "freed_bytes": p["bytes"], "files": p["files"], "stopped": stopped, "left_behind": left_behind}


def _remove_ollama(model: dict, roots: Roots) -> list[str]:
    parts = _ollama_files(model, roots)
    if parts["weights"] is not None and not _delete_file(parts["weights"]):
        raise Busy(_held(parts["weights"].name) + " The model is still whole.")
    if not _delete_file(parts["manifest"]):
        raise Busy("The manifest of this model could not be deleted, so it is half removed: its weights are gone. Close the program that holds " + parts["manifest"].name + " and delete it again.")
    left = [b.name for b in parts["others"] if not _delete_file(b)]
    manifests = roots.ollama / "manifests"
    cur = parts["manifest"].parent
    while _pathkey(cur) != _pathkey(manifests) and cur.is_dir():
        try:
            cur.rmdir()
        except OSError:
            break
        cur = cur.parent
    return left
