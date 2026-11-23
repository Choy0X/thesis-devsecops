"""Where the application's own model files are, and which of them is the embedding model.

One folder holds them all (``data/models`` next to the code, or the folder named in Settings; see ``models_dir``). The scan looks at ``*.gguf`` files, skipping the folders the application
itself writes into (``converted``, ``adapters``, ``hub``) and any folder whose name begins with a dot (the download tool's bookkeeping).
"""
from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
SKIP_FOLDERS = {"converted", "adapters", "hub", "__pycache__"}          # what the application writes into the models folder itself (listed under their own sources, or not models at all)
PREFERRED_EMBEDDERS = ("jina-code-embeddings", "qwen3-embedding")          # the embedding models the application knows how to ask (their instructions and pooling are in ``rag/embed.py``)


def default_models_dir() -> pathlib.Path:
    """``data/models`` next to the code: the models folder when Settings names none."""
    return ROOT / "data" / "models"


def models_dir() -> pathlib.Path:
    given = (os.environ.get("ZTSA_MODELS_DIR") or "").strip()
    return pathlib.Path(given) if given else default_models_dir()


def hub_cache(env: dict | None = None) -> pathlib.Path:
    """The Hugging Face cache: inside the models folder (``<models folder>/hub``), so one folder holds every model. ``HF_HUB_CACHE``, when the caller's environment names one, wins."""
    env = os.environ if env is None else env
    given = (env.get("HF_HUB_CACHE") or "").strip()
    if given:
        return pathlib.Path(given)
    base = (env.get("ZTSA_MODELS_DIR") or "").strip()
    return (pathlib.Path(base) if base else ROOT / "data" / "models") / "hub"


def old_hub_cache(env: dict | None = None, home: pathlib.Path | None = None) -> pathlib.Path:
    """Where the Hugging Face library keeps models when nothing says otherwise (``HF_HOME/hub``, else ``~/.cache/huggingface/hub``): the place the models of this computer were downloaded
    to before the application kept them in its own folder. Nothing is written there."""
    env = os.environ if env is None else env
    if (env.get("HF_HOME") or "").strip():
        return pathlib.Path(env["HF_HOME"]) / "hub"
    return (home or pathlib.Path.home()) / ".cache" / "huggingface" / "hub"


def hub_gguf_files(cache: pathlib.Path | None = None) -> list[pathlib.Path]:
    """The GGUF files of the models in the Hugging Face cache, from the main revision of each (the newest one when none is marked)."""
    root = pathlib.Path(cache) if cache else hub_cache()
    out: list[pathlib.Path] = []
    for model in sorted(root.glob("models--*")) if root.is_dir() else []:
        snaps = model / "snapshots"
        if not snaps.is_dir():
            continue
        ref = model / "refs" / "main"
        main = ref.read_text(encoding="utf-8", errors="replace").strip() if ref.is_file() else ""
        revisions = sorted(p for p in snaps.iterdir() if p.is_dir())
        current = next((r for r in revisions if r.name == main), revisions[-1] if revisions else None)
        out += sorted(current.rglob("*.gguf")) if current else []
    return out


def converted_gguf_files() -> list[pathlib.Path]:
    """The GGUF files of the models this application converted itself (``data/models/converted``, where ``gguf_convert`` writes them), sorted by path."""
    from app.backend.pipeline import gguf_convert
    return sorted(p for p in pathlib.Path(gguf_convert.CONVERTED).rglob("*.gguf") if p.is_file())          # a folder that is not there has none


def gguf_files(folder: pathlib.Path | None = None) -> list[pathlib.Path]:
    """Every GGUF file in the models folder, sorted by path (the folders the application writes into, and dot folders, are not entered). Without a folder named, the models in the
    Hugging Face cache inside the models folder are included (that is where a model downloaded in the Model Hub is) and so are the models converted here."""
    base = pathlib.Path(folder) if folder else models_dir()
    out: list[pathlib.Path] = []
    if not base.is_dir():
        return hub_gguf_files() + converted_gguf_files() if folder is None else out
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_FOLDERS and not d.startswith("."))
        out += [pathlib.Path(dirpath) / f for f in sorted(filenames) if f.lower().endswith(".gguf")]
    return out + (hub_gguf_files() + converted_gguf_files() if folder is None else [])


def _says_embedding(path: pathlib.Path) -> bool:
    """Whether a GGUF file is an embedding model: a converted one by what it was installed as, any other by its name."""
    from app.backend.pipeline import gguf_convert
    role = gguf_convert.converted_role(path.parent)
    return role == "embedding" or (role is None and "embed" in path.name.lower())


def find_embedding_gguf(folder: pathlib.Path | None = None) -> pathlib.Path | None:
    """The embedding model in the models folder when no one has been chosen: the only GGUF file whose name says it embeds, else the first of the models the application knows. None when
    there is none, or when there are several and none of them is one it knows (then the person chooses)."""
    found = [p for p in gguf_files(folder) if _says_embedding(p)]
    if len(found) == 1:
        return found[0]
    for known in PREFERRED_EMBEDDERS:
        for p in found:
            if known in p.name.lower():
                return p
    return None
