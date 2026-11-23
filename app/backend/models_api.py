"""Routes of the Model Hub: the models that are on this computer, and whether one fits in the memory that is free.

The fit routes are read-only: nothing is downloaded or loaded, and the estimate is built by ``pipeline/fit.py`` from the model's own configuration and weight headers. The last
routes (stopped downloads, removing a model) change the cache, and only when the person asks.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.backend import errors

router = APIRouter()


def _models_dir() -> str:
    from app.backend.pipeline import model_files
    return str(model_files.models_dir())


@router.get("/api/models/local")
def models_local(limit: int | None = None, offset: int = 0, search: str = ''):
    """Every model in the Hugging Face cache and in the models folder of Settings, largest first."""
    from app.backend.pipeline import gguf_convert, models_local as ml
    from training.finetune import model_eligibility
    inventory = ml.inventory(_models_dir(), converted=gguf_convert.CONVERTED, ollama=ml.ollama_dir())
    if limit is not None:
        limit, offset = max(1, min(500, limit)), max(0, offset)
        term = search.strip().casefold()
        rows = [row for row in inventory['models'] if not term or term in ' '.join(
            str(row.get(key) or '') for key in ('id', 'name', 'path', 'source')).casefold()]
        rows.sort(key=lambda row: (str(row.get('id') or row.get('name') or '').casefold(), str(row.get('path') or '')))
        inventory = {**inventory, 'total': len(rows), 'limit': limit, 'offset': offset,
                     'models': rows[offset:offset + limit]}
    inventory['models'] = [{**row, **model_eligibility.local(row)} for row in inventory['models']]
    return inventory


class FitIn(BaseModel):
    model_id: str
    source: Literal["hub_cache", "folder", "hub", "converted", "ollama"] = "hub_cache"
    mode: Literal["inference", "qlora"] = "inference"
    gguf_file: str | None = Field(None, max_length=1024)
    context: int = Field(2048, ge=128, le=1_000_000)
    micro_batch: int = Field(1, ge=1, le=512)
    rank: int = Field(16, ge=1, le=1024)


@router.get("/api/models/search")
def models_search(q: str = "", task: str = "", limit: int = Query(20, ge=1, le=50)):
    """Models on the Hugging Face Hub, the most downloaded first (``task`` is "generation", "embedding" or empty). Needs the internet; when the Hub cannot be
    reached the answer is an error that says so, never an empty list that looks like "no model"."""
    from app.backend.pipeline import hub_search
    try:
        return hub_search.search(q, task, limit)
    except ValueError as e:
        return errors.error(str(e), 400)
    except Exception as e:  # ignored: any failure to reach the Hub is turned into one plain answer below
        return errors.error("Hugging Face could not be reached, so the search has no answer.", 502, detail=f"{type(e).__name__}: {str(e)[:200]}",
                            hint="Check the internet connection. Models already on this computer are listed under Installed.")


@router.get("/api/models/download-plan")
def models_download_plan(repo_id: str, quant: str = ""):
    """What downloading a model would fetch: the files, their size, the free space on the disk that holds the cache, whether it may go ahead and, for GGUF files, the
    quantisation to offer first on this computer. Reads the Hub's metadata only; nothing is downloaded."""
    from app.backend import device
    from app.backend.pipeline import hub_download
    if "/" not in repo_id:
        return errors.error("A model name looks like org/model.", 400)
    try:
        return hub_download.plan(repo_id, quant or None, device_profile=device.snapshot())
    except ValueError as e:
        return errors.error(str(e), 400)
    except Exception as e:  # ignored: any failure to reach the Hub is turned into one plain answer
        return errors.error("Hugging Face could not be reached, so the download cannot be planned.", 502, detail=f"{type(e).__name__}: {str(e)[:200]}")


@router.get("/api/models/install-plan")
def models_install_plan(repo_id: str, role: str = "", quant: str = ""):
    """What installing a Hub model so that it is ready to run would do and cost: download it, fetch the converter scripts if needed, convert it to GGUF in the format that suits its role and this
    computer (the sizes, the disk, an estimate of the time) and every reason it cannot be done. Reads the Hub's metadata only; nothing is downloaded or written."""
    from app.backend import device
    from app.backend.pipeline import model_install
    if "/" not in repo_id:
        return errors.error("A model name looks like org/model.", 400)
    try:
        return model_install.plan(repo_id, role or None, quant or None, device_profile=device.snapshot())
    except (ValueError, model_install.InstallError) as e:
        return errors.error(str(e), 400)
    except Exception as e:  # ignored: any failure to reach the Hub is turned into one plain answer
        return errors.error("Hugging Face could not be reached, so the install cannot be planned.", 502, detail=f"{type(e).__name__}: {str(e)[:200]}")


@router.get("/api/models/finish-plan")
def models_finish_plan(model_id: str, source: str, role: str = "", quant: str = ""):
    """What finishing the install of a model that is already on this computer (safetensors, not yet converted) would do and cost, and every reason it cannot be done."""
    from app.backend import device
    from app.backend.pipeline import gguf_convert, model_install, models_local as ml
    entry = next((m for m in ml.inventory(_models_dir(), converted=gguf_convert.CONVERTED, ollama=ml.ollama_dir())["models"] if m["id"] == model_id and m["source"] == source), None)
    if entry is None:
        return errors.error("That model is not installed on this computer.", 404)
    try:
        return model_install.finish_plan(entry, role or None, quant or None, device_profile=device.snapshot())
    except (ValueError, model_install.InstallError, gguf_convert.ConvertError) as e:
        return errors.error(str(e), 400)


@router.get("/api/models/gguf-for")
def models_gguf_for(base: str, limit: int = 5):
    """GGUF versions of a base model that are already converted, with the size of each quantisation, the size of the base model's own weights and the free space of the disk
    that holds the cache: the disk cost of the training copy and of the serving copy, before anything is downloaded. Reads the Hub's metadata only."""
    from app.backend.pipeline import hub_download, hub_search
    if "/" not in base:
        return errors.error("A model name looks like org/model.", 400)
    try:
        out = hub_search.gguf_for(base, limit=limit)
    except Exception as e:  # ignored: any failure to reach the Hub is turned into one plain answer
        return errors.error("Hugging Face could not be reached, so GGUF versions cannot be looked for.", 502, detail=f"{type(e).__name__}: {str(e)[:200]}")
    return {**out, "free_bytes": hub_download._free_bytes(hub_download.cache_dir())}


@router.get("/api/models/shelf")
def models_shelf():
    """The base model for fine-tuning, checked against the Hub now. Needs the
    internet; a model the Hub does not answer for carries its reason."""
    from app.backend.pipeline import hub_search
    try:
        return {"models": hub_search.shelf()}
    except Exception as e:  # ignored: failing to set up the Hub client at all is one plain answer; per-model failures are inside the rows
        return errors.error("Hugging Face could not be reached.", 502, detail=f"{type(e).__name__}: {str(e)[:200]}")


@router.post("/api/models/fit")
def models_fit(body: FitIn):
    """The estimated memory to run or fine-tune a model, by part, and the verdict against this computer's free memory. The model is an installed one, or
    (``source`` "hub") one on the Hub, read by its metadata only."""
    from app.backend import device
    from app.backend.pipeline import fit, gguf, gguf_convert, hub_search, models_local as ml
    gguf_weights = None
    embedding = False
    if body.source == "hub":
        try:
            shape, notes, weights, facts = hub_search.fit_inputs(body.model_id)
        except Exception as e:  # ignored: any failure to reach the Hub is turned into one plain answer
            return errors.error("Hugging Face could not be reached, so this model cannot be looked at.", 502, detail=f"{type(e).__name__}: {str(e)[:200]}")
        size_note = "The weights are counted at the size of their files on the Hub."
        files = facts.get("gguf_files", {})
        if body.mode == "inference" and body.gguf_file:
            if body.gguf_file not in files:
                return errors.error("That GGUF file was not found in the model repository.", 400)
            gguf_weights = weights = files[body.gguf_file]
        elif body.mode == "inference" and weights is None and len(files) == 1:
            gguf_weights = weights = next(iter(files.values()))
        embedding = facts.get("task") in ("feature-extraction", "sentence-similarity")
    else:
        model = next((m for m in ml.inventory(_models_dir(), converted=gguf_convert.CONVERTED, ollama=ml.ollama_dir())["models"] if m["id"] == body.model_id and m["source"] == body.source), None)
        if model is None:
            return errors.error("That model is not installed on this computer.", 404)
        shape, notes = ml.read_shape(model)
        weights = model["bytes"]
        size_note = "The weights are counted at the size of the stored files."
        if body.mode == "inference" and model["gguf"] and not model["safetensors"]:
            files = [Path(model["path"])] if body.source == "ollama" else [p for p in ml._model_files(model) if p.suffix.lower() == ".gguf"]
            if len(files) == 1:
                try:
                    info = gguf.describe(files[0])
                except (OSError, gguf.GgufError):
                    notes.append("The GGUF header could not be read.")
                else:
                    gguf_weights = weights = info["file_bytes"]
                    shape, notes = gguf.shape(info), ["Dimensions were read from the GGUF header."]
                    embedding = info["pooling"] not in (None, "none")
    try:
        if body.mode == "inference":
            if embedding and gguf_weights is not None:
                return _gguf_weights_only(gguf_weights, "This embedding model's memory use depends on how Ollama runs it.")
            parts = fit.inference_estimate(shape, body.context, weights_bytes=weights)
            notes.append(size_note)
        else:
            parts = fit.qlora_estimate(shape, body.context, body.micro_batch, body.rank, mlp=shape.intermediate is not None)
    except fit.MissingDimension as e:
        if gguf_weights is not None:
            return _gguf_weights_only(gguf_weights, "The available metadata gives the file size. More dimensions are needed to estimate runtime memory.")
        return errors.error(f"{e}.", 400, hint="Choose a copy of the model that has a config.json, or one stored as safetensors.")
    return {"mode": body.mode, "parts": parts, "total": parts["total"], "notes": notes, "check": fit.check(parts["total"], device.snapshot())}


def _gguf_weights_only(weights: int, note: str) -> dict:
    from app.backend import device
    from app.backend.pipeline import fit

    check = fit.check(weights, device.snapshot())
    check.update(gpu="unknown", ram="unknown", runs_on="nowhere")
    return {"mode": "inference", "parts": {"weights": weights}, "total": weights, "estimate_complete": False, "notes": [note], "check": check}


# -- the models in the cache: downloads that stopped part-way, and removing a model ----------------------------------------------------------------------
# These change the cache, and only when the person asks (an AlertDialog names what is deleted before the request is sent). Only a folder of the Hugging Face cache can be touched, and
# a model that a download, a training, an index or the model server is using is refused in a sentence.

class RepoIn(BaseModel):
    repo_id: str


def _in_use(repo_id: str):
    """A function that says, for a model's cache folder, what is using it now (a sentence), or None."""
    def check(folder) -> str | None:
        from app.backend import jobs
        from app.backend.pipeline.gateway import client as gateway
        if any((j.params or {}).get("repo_id") == repo_id for j in jobs.running("hf_download")):
            return "A download of this model is running; stop it first."
        if any((j.params or {}).get("model") == repo_id for j in jobs.running("train_local") + jobs.running("gpu_smoke") + jobs.running("gpu_eval")):
            return "A training or check on this computer is using this model; wait for it to finish."
        if any(_under(path, folder) for path in gateway.running_source_paths()):
            return "The model gateway has this model loaded. Unload it on the Serving tab first."
        return None
    return check


@router.get("/api/models/partial")
def models_partial():
    """The models whose download stopped part-way, with the bytes already fetched. Downloading again continues from there; discarding deletes only the unfinished files."""
    from app.backend.pipeline import hub_download
    return {"partial": hub_download.partial_downloads()}


@router.post("/api/models/partial/discard")
def models_partial_discard(body: RepoIn):
    from app.backend.pipeline import hub_download
    try:
        return hub_download.discard_partial(body.repo_id)
    except ValueError as e:
        return errors.error(str(e), 400)


@router.get("/api/models/remove-plan")
def models_remove_plan(repo_id: str):
    """What removing a model from the cache would delete (its size, its revisions, the unfinished part) and whether something is using it now. Deletes nothing."""
    from app.backend.pipeline import hub_download
    try:
        out = hub_download.removal_plan(repo_id)
    except ValueError as e:
        return errors.error(str(e), 400)
    except FileNotFoundError as e:
        return errors.error(str(e), 404)
    return {**out, "in_use": _in_use(repo_id)(hub_download.repo_folder(repo_id))}


def _under(child, parent) -> bool:
    """Whether ``child`` is ``parent`` or lies inside it (compared as paths, so ``models/qwen2`` is not inside ``models/qwen``)."""
    c, p = os.path.normcase(os.path.abspath(str(child))), os.path.normcase(os.path.abspath(str(parent)))
    return c == p or c.startswith(p + os.sep)


def _model_in_use(model: dict) -> str | None:
    """What is using an installed model now, as a sentence, or None: a download or a training or check on this computer, or the model gateway with it loaded."""
    from app.backend import jobs
    from app.backend.pipeline import hub_download
    from app.backend.pipeline.gateway import client as gateway
    if model["source"] == "hub_cache":
        return _in_use(model["id"])(hub_download.repo_folder(model["id"]))
    where = model["path"]
    if any(_under((j.params or {}).get("model") or "", where) for j in jobs.running("train_local") + jobs.running("gpu_smoke") + jobs.running("gpu_eval") if (j.params or {}).get("model")):
        return "A training or check on this computer is using this model; wait for it to finish."
    if any(_under(path, where) for path in gateway.running_source_paths()):
        return "The model gateway has this model loaded. Unload it on the Serving tab first."
    return None


def _settings_roles(model: dict) -> list[str]:
    """The roles Settings gives this model (the file chosen as the language model, its adapter or the embedding model lies in it), for the confirmation to name."""
    from app.backend.config import Settings
    s = Settings.load()
    return [role for role, value in (("the language model", s.llm_local_model), ("the language model's adapter", getattr(s, "llm_local_adapter", "")), ("the embedding model", s.embed_local_model))
            if (value or "").strip() and _under(value, model["path"])]


def _removal_roots():
    from app.backend.pipeline import gguf_convert, hub_download, model_files, model_removal, models_local as ml
    return model_removal.Roots(models=model_files.models_dir(), converted=gguf_convert.CONVERTED, hub=hub_download.cache_dir(), ollama=ml.ollama_dir())


def _installed_model(source: str, model_id: str, path: str):
    from app.backend.pipeline import gguf_convert, model_removal, models_local as ml
    return model_removal.find(ml.inventory(_models_dir(), converted=gguf_convert.CONVERTED, ollama=ml.ollama_dir()), source, model_id, path)


Source = Literal["hub_cache", "folder", "converted", "ollama"]


@router.get("/api/models/installed/remove-plan")
def installed_remove_plan(source: Source, path: str, model_id: str = Query(..., alias="id")):
    """What deleting one installed model would delete (bytes, files, what stays because another Ollama model uses it), whether something uses it, the roles Settings gives it, whether
    Ollama is running and why a model cannot be deleted. The model is named as the list of installed models names it. Deletes nothing."""
    from app.backend.pipeline import model_removal
    try:
        return model_removal.plan(_installed_model(source, model_id, path), _removal_roots(), in_use=_model_in_use, settings_use=_settings_roles)
    except model_removal.NotInstalled as e:
        return errors.error(str(e), 404)


@router.delete("/api/models/installed")
def installed_remove(source: Source, path: str, model_id: str = Query(..., alias="id"), stop_ollama: bool = False):
    """Delete one installed model from this computer. A model in use, a running Ollama that was not told to stop (``stop_ollama``) and a file another program holds are refused in a
    sentence (409); a model the app may not delete is a 400; one that is gone is a 404."""
    from app.backend.pipeline import model_removal
    try:
        return model_removal.remove(_installed_model(source, model_id, path), _removal_roots(), in_use=_model_in_use, stop_ollama=stop_ollama)
    except model_removal.NotInstalled as e:
        return errors.error(str(e), 404)
    except model_removal.Refused as e:
        return errors.error(str(e), 400)
    except model_removal.Busy as e:
        return errors.error(str(e), 409)
    except OSError as e:
        return errors.error("The model could not be deleted completely.", 500, detail=f"{type(e).__name__}: {str(e)[:200]}",
                            hint="Close any program that uses its files and try again; a model that is partly deleted is listed with what is left.")


@router.delete("/api/models/cache")
def models_remove(repo_id: str):
    """Remove one model from the Hugging Face cache. It can be downloaded again; a model in use is refused in a sentence and nothing is deleted."""
    from app.backend.pipeline import hub_download
    try:
        return hub_download.remove(repo_id, in_use=_in_use(repo_id))
    except ValueError as e:
        return errors.error(str(e), 400)
    except FileNotFoundError as e:
        return errors.error(str(e), 404)
    except RuntimeError as e:
        return errors.error(str(e), 409)
