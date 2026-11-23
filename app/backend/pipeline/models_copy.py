"""Copy the models the application uses from the Hugging Face cache of this computer into its own models folder.

The application keeps every model in one folder (``data/models``, with the Hugging Face cache in ``hub`` inside it; see ``model_files``). Models that were downloaded earlier sit in
the shared cache of the person's account (``~/.cache/huggingface/hub``), which other programs may use too. So they are copied, never moved: the old cache is only read, and what
is in it stays. A model that is in the new cache already is left as it is. Each copy is made beside its place under a temporary name, checked file by file against the
original (size and SHA-256), and renamed into place only when it is the same, so a copy that was cut short (a power cut, a full disk) leaves nothing that looks like a model.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
from typing import Callable

from app.backend.pipeline import model_files, models_local


def _models(cache: pathlib.Path) -> dict[str, pathlib.Path]:
    return {m["id"]: pathlib.Path(m["path"]) for m in models_local.scan_hub_cache(cache)}


def used_by_the_app() -> list[str]:
    """The models the application itself names: the base of the fine-tuning configuration, the model of the local GPU check and the reference embedding model of the GGUF check, and the
    GGUF repositories it recommends. Read from the code that uses them."""
    ids: list[str] = []
    try:
        from training.finetune import config as cfgmod
        ids.append(cfgmod.BASE_MODEL_ID)
    except Exception:                                              # ignored: a missing training library only shortens the list
        pass
    from app.backend.pipeline import gpu_local
    from app.backend.pipeline.rag import embed
    ids += [gpu_local.DEFAULT_MODEL, embed.RECOMMENDED["repo"], "jinaai/jina-code-embeddings-0.5b"]
    return list(dict.fromkeys(ids))


def plan(old: pathlib.Path | None = None, new: pathlib.Path | None = None, only: list[str] | None = None) -> dict:
    """What would be copied: every model of the old cache (or those named in ``only``), its size, whether the application uses it and whether it is in the new cache already."""
    old = pathlib.Path(old) if old else model_files.old_hub_cache()
    new = pathlib.Path(new) if new else model_files.hub_cache()
    used = set(used_by_the_app())
    present = _models(new)
    rows = []
    for m in models_local.scan_hub_cache(old):
        if only is not None and m["id"] not in only:
            continue
        rows.append({"id": m["id"], "path": m["path"], "bytes": m["bytes"], "used": m["id"] in used, "already": m["id"] in present,
                     "target": str(new / pathlib.Path(m["path"]).name)})
    todo = [r for r in rows if not r["already"]]
    return {"old": str(old), "new": str(new), "models": rows, "bytes": sum(r["bytes"] for r in todo), "same_place": old.resolve() == new.resolve() if old.exists() and new.exists() else str(old) == str(new)}


def _sha(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 22):
            h.update(block)
    return h.hexdigest()


def _files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink())


def _copy_tree(src: pathlib.Path, dst: pathlib.Path, log: Callable[[str], None]) -> int:
    """Copy a model folder as it is: folders, files and, where the system allows, the links of the cache (a link that cannot be made is replaced by a copy of what it points to)."""
    total = 0
    for p in sorted(src.rglob("*")):
        target = dst / p.relative_to(src)
        if p.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(os.readlink(p), target, target_is_directory=p.is_dir())
                continue
            except OSError:
                pass                                               # no right to make links here: the content is copied below
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif p.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
            total += target.stat().st_size
    return total


def copy_model(src: pathlib.Path, dst: pathlib.Path, log: Callable[[str], None] = lambda s: None) -> dict:
    """Copy one model folder of the cache to ``dst``, checked against the original, never over an existing one."""
    src, dst = pathlib.Path(src), pathlib.Path(dst)
    if dst.exists():
        return {"copied": False, "reason": "it is in the new cache already", "bytes": 0}
    tmp = dst.with_name(".copying-" + dst.name)                 # not a name the cache scan lists as a model
    if tmp.exists():
        shutil.rmtree(tmp)                                         # the leftover of a copy that was cut short
    dst.parent.mkdir(parents=True, exist_ok=True)
    total = _copy_tree(src, tmp, log)
    a, b = _files(src), _files(tmp)
    if [p.relative_to(src) for p in a] != [p.relative_to(tmp) for p in b]:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"the copy of {src.name} does not hold the same files as the original; nothing was kept")
    for p, q in zip(a, b):
        if p.is_file() and not p.is_symlink() and (p.stat().st_size != q.stat().st_size or _sha(p) != _sha(q)):
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"{q.relative_to(tmp)} of {src.name} differs from the original after copying; nothing was kept")
    os.replace(tmp, dst)
    log(f"copied {src.name}: {len(a)} files, {total / 1024 ** 3:.2f} GB, every file checked against the original")
    return {"copied": True, "files": len(a), "bytes": total}


def free_bytes(path: pathlib.Path) -> int:
    p = pathlib.Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def copy(old: pathlib.Path | None = None, new: pathlib.Path | None = None, only: list[str] | None = None, log: Callable[[str], None] = print) -> dict:
    """Copy the models of ``plan`` that are not in the new cache yet; stops before starting when the disk would not hold them (with a tenth to spare)."""
    p = plan(old, new, only)
    if p["same_place"]:
        raise ValueError("The old and the new cache are the same folder; there is nothing to copy.")
    todo = [r for r in p["models"] if not r["already"]]
    need = int(sum(r["bytes"] for r in todo) * 1.1)
    free = free_bytes(pathlib.Path(p["new"]))
    if need > free:
        raise ValueError(f"The models need {need / 1024 ** 3:.1f} GB and {free / 1024 ** 3:.1f} GB are free where the new cache is.")
    done = []
    for r in todo:
        res = copy_model(pathlib.Path(r["path"]), pathlib.Path(r["target"]), log)
        done.append({"id": r["id"], **res})
    return {**p, "done": done}
