"""The models that are already on this computer: the Hugging Face cache and any folders the owner points to.

Only the file system is read; nothing is downloaded and nothing is loaded. A model's size is the size of its stored files (the blobs of the cache, so a
file shared by two revisions is counted once). A model is listed with the facts a person chooses by: its name, its size, the formats it comes in (weights
in safetensors, files in GGUF), and where it is.
"""
from __future__ import annotations

import json
import os
import pathlib

WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt")
OLLAMA_ROOT: pathlib.Path | None = None          # tests give another; None means the usual place (``ollama_dir``)


def hub_cache_dir(env: dict | None = None, home: pathlib.Path | None = None) -> pathlib.Path:
    """Where the application keeps the models it downloads from the Hugging Face Hub: ``HF_HUB_CACHE`` when the environment names one, else the ``hub`` folder inside the models folder
    (``data/models/hub``, or the folder chosen in Settings). ``home`` is accepted for the callers that name the old place; it is not used."""
    from app.backend.pipeline import model_files
    return model_files.hub_cache(env)


def ollama_dir(env: dict | None = None, home: pathlib.Path | None = None) -> pathlib.Path:
    """Where Ollama keeps the models it downloaded: ``OLLAMA_MODELS``, else ``~/.ollama/models``."""
    if OLLAMA_ROOT is not None:
        return OLLAMA_ROOT
    env = os.environ if env is None else env
    return pathlib.Path(env["OLLAMA_MODELS"]) if env.get("OLLAMA_MODELS") else (home or pathlib.Path.home()) / ".ollama" / "models"


def scan_ollama(root: pathlib.Path) -> list[dict]:
    """The models Ollama downloaded, read from its manifests alone: nothing of Ollama's is run, changed or moved. A model is listed with the weights file it names (a GGUF file stored under a
    name made of its hash), which the app's own server can load without a second copy. The program does not have to be installed or running."""
    return sorted((entry for _manifest, entry in ollama_manifests(root)), key=lambda m: -m["bytes"])


def ollama_manifests(root: pathlib.Path):
    """``(manifest file, the model's entry as scan_ollama lists it)`` for every Ollama model whose manifest names a weights file that is there. Deleting a model needs its manifest."""
    manifests = root / "manifests"
    if not manifests.is_dir():
        return
    for f in sorted(p for p in manifests.rglob("*") if p.is_file()):
        parts = f.relative_to(manifests).parts                        # registry / namespace / model / tag
        if len(parts) < 3:
            continue
        try:
            layers = json.loads(f.read_text(encoding="utf-8")).get("layers") or []
            digest = next(l["digest"] for l in layers if str(l.get("mediaType", "")).endswith("image.model"))
        except (OSError, ValueError, StopIteration, KeyError, TypeError, AttributeError):
            continue
        blob = root / "blobs" / str(digest).replace(":", "-")
        if not blob.is_file() or blob.name != pathlib.PurePath(blob.name).name:
            continue
        namespace = parts[-3] if len(parts) >= 4 and parts[-3] != "library" else ""
        yield f, {"id": f"{namespace + '/' if namespace else ''}{parts[-2]}:{parts[-1]}", "source": "ollama", "path": str(blob), "bytes": _size(blob), "revisions": [], "main_revision": None,
                  "safetensors": False, "gguf": [blob.name], "weights": True, "config": False}


def _size(path: pathlib.Path) -> int:
    try:
        return path.stat().st_size                           # follows a link to the blob it points at
    except OSError:
        return 0


def _tree_bytes(root: pathlib.Path) -> int:
    total = 0
    for p in root.rglob("*"):
        if p.is_file():
            total += _size(p)
    return total


def _formats(files: list[pathlib.Path]) -> dict:
    names = [f.name.lower() for f in files]
    return {"safetensors": any(n.endswith(".safetensors") for n in names), "gguf": sorted(f.name for f in files if f.name.lower().endswith(".gguf")),
            "weights": any(n.endswith(WEIGHT_SUFFIXES) for n in names), "config": "config.json" in names}


def _repo_id(folder: str) -> str:
    kind, _, rest = folder.partition("--")
    return rest.replace("--", "/")


def scan_hub_cache(root: pathlib.Path) -> list[dict]:
    """Every model of a Hugging Face cache folder (``models--org--name``), largest first. Datasets and spaces in the same cache are not models and are skipped."""
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.glob("models--*")):
        if not d.is_dir():
            continue
        snaps = d / "snapshots"
        revisions = sorted(p for p in snaps.iterdir() if p.is_dir()) if snaps.is_dir() else []
        files = [f for r in revisions for f in r.rglob("*") if f.is_file() or f.is_symlink()]
        blobs = d / "blobs"
        main = ""
        ref = d / "refs" / "main"
        if ref.is_file():
            main = ref.read_text(encoding="utf-8", errors="replace").strip()
        # A file shared by two revisions is one blob, so the blobs give the true size. Downloads made with the Xet backend, or on a disk that cannot link, keep
        # the files in the snapshot and leave the blob folder empty: then the files of the main revision (else the newest one) are the size.
        size = _tree_bytes(blobs) if blobs.is_dir() else 0
        if not size:
            current = next((r for r in revisions if r.name == main), revisions[-1] if revisions else None)
            size = sum(_size(f) for f in (current.rglob("*") if current else []) if f.is_file())
        out.append({"id": _repo_id(d.name), "source": "hub_cache", "path": str(d), "bytes": size, "revisions": [r.name for r in revisions], "main_revision": main or None,
                    **_formats(files)})
    return sorted(out, key=lambda m: -m["bytes"])


def scan_folder(root: pathlib.Path, depth: int = 2) -> list[dict]:
    """Folders under ``root`` (to ``depth`` levels) that hold a model: a ``config.json`` next to weights, or at least one GGUF file. The folders the application writes into itself
    (``converted``, ``adapters``, ``hub``) and folders that begin with a dot are not entered, and a folder reached twice (a link) is listed once."""
    from app.backend.pipeline.model_files import SKIP_FOLDERS
    out: list[dict] = []
    if not root.is_dir():
        return out
    seen: set[str] = set()

    def walk(d: pathlib.Path, level: int) -> None:
        try:
            entries = list(d.iterdir())
        except OSError:
            return                                                     # a folder that cannot be read has no models we can list
        try:
            here = str(d.resolve())
        except OSError:
            return
        if here in seen:
            return
        seen.add(here)
        files = [e for e in entries if e.is_file()]
        fm = _formats(files)
        if fm["gguf"] or (fm["config"] and fm["weights"]):
            out.append({"id": d.name if level else "Files in the models folder", "source": "folder", "path": str(d), "bytes": sum(_size(f) for f in files), "revisions": [], "main_revision": None, **fm})
            return
        if level < depth:
            for e in entries:
                if e.is_dir() and not e.name.startswith(".") and not (level == 0 and e.name in SKIP_FOLDERS):
                    walk(e, level + 1)
    walk(root, 0)
    return sorted(out, key=lambda m: -m["bytes"])


def inventory(models_dir: str | None = None, env: dict | None = None, home: pathlib.Path | None = None, converted: pathlib.Path | None = None, ollama: pathlib.Path | None = None) -> dict:
    """The models in the Hugging Face cache and in the owner's models folder, with the places that were looked at. ``converted`` is the folder the app writes its own GGUF
    conversions to; what is in it is listed with the source ``converted``. ``ollama`` is the folder Ollama keeps its models in; they are listed, read-only, with the source ``ollama``."""
    cache = hub_cache_dir(env, home)
    folder = pathlib.Path(models_dir) if models_dir else None
    made = [{**m, "source": "converted"} for m in scan_folder(pathlib.Path(converted))] if converted else []
    models = scan_hub_cache(cache) + (scan_folder(folder) if folder else []) + made + (scan_ollama(pathlib.Path(ollama)) if ollama else [])
    return {"hub_cache": str(cache), "hub_cache_exists": cache.is_dir(), "models_dir": str(folder) if folder else None,
            "models_dir_exists": bool(folder and folder.is_dir()), "models": models, "total_bytes": sum(m["bytes"] for m in models)}


# -- the dimensions of a stored model, read from its own files -------------------------------------------------------------------------------------------

def _model_files(model: dict) -> list[pathlib.Path]:
    """The files of the model's main revision (a cache model) or of its folder."""
    root = pathlib.Path(model["path"])
    if model["source"] == "hub_cache":
        rev = model.get("main_revision") or (model["revisions"][0] if model["revisions"] else None)
        root = root / "snapshots" / rev if rev else root
    return [p for p in root.rglob("*") if p.is_file()] if root.is_dir() else []


def safetensors_params(path: pathlib.Path) -> int:
    """The exact number of parameters in a safetensors file, from its header (eight bytes of length, then JSON naming each tensor's shape): no weight is loaded."""
    import json
    with open(path, "rb") as f:
        raw = f.read(8)
        if len(raw) < 8:
            raise ValueError(f"{path.name} is not a safetensors file")
        n = int.from_bytes(raw, "little")
        if n <= 0 or n > 100 * 1024 * 1024:
            raise ValueError(f"{path.name} has an unreasonable header size")
        header = json.loads(f.read(n))
    total = 0
    for name, meta in header.items():
        if name == "__metadata__":
            continue
        count = 1
        for dim in meta["shape"]:
            count *= int(dim)
        total += count
    return total


def read_shape(model: dict):
    """``(Shape, notes)`` for a stored model: the dimensions from its ``config.json`` and the parameter count summed from its safetensors headers. A model that
    has only GGUF files has no readable dimensions here, and the notes say so."""
    import json
    from app.backend.pipeline import fit
    files = _model_files(model)
    notes: list[str] = []
    cfg_file = next((f for f in files if f.name == "config.json"), None)
    config: dict = {}
    if cfg_file:
        try:
            config = json.loads(cfg_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            notes.append("config.json could not be read")
    else:
        notes.append("There is no config.json, so the layers and sizes of the model are not known" + (" (GGUF files carry them in a header this app does not read yet)" if model.get("gguf") else ""))
    params = None
    shards = [f for f in files if f.suffix == ".safetensors"]
    if shards:
        try:
            params = sum(safetensors_params(f) for f in shards)
        except (OSError, ValueError, KeyError) as e:
            notes.append(f"The parameter count could not be read from the weights: {e}")
    elif model.get("gguf"):
        notes.append("The parameter count is not read from GGUF files yet")
    return fit.shape_from_config(config, params), notes
