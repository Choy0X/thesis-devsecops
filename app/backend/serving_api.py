"""Local Ollama status, model inventory and graphics-card controls."""
from __future__ import annotations

import pathlib
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.backend import errors

router = APIRouter()


def _models_dir() -> str:
    from app.backend.pipeline import model_files
    return str(model_files.models_dir())


def gguf_files() -> list[dict]:
    """Every GGUF file of the installed models (the Hugging Face cache and the models folder): its id, name, path and size."""
    from app.backend.pipeline import gguf_convert, models_local as ml
    out = []
    for m in ml.inventory(_models_dir(), converted=gguf_convert.CONVERTED, ollama=ml.ollama_dir())["models"]:
        if not m.get("gguf"):
            continue
        if m["source"] == "ollama":                                  # the model is one file, kept under the name of its hash
            f = pathlib.Path(m["path"])
            if f.is_file():
                name = f"Ollama's file {f.name.split('-')[-1][:12]}"
                out.append({"id": m["id"], "name": name, "label": name, "path": str(f), "bytes": f.stat().st_size, "source": "ollama"})
            continue
        root = pathlib.Path(m["path"])
        if m["source"] == "hub_cache":
            rev = m.get("main_revision") or (m["revisions"][0] if m["revisions"] else None)
            root = root / "snapshots" / rev if rev else root
        label = gguf_convert.converted_label(root) if m["source"] == "converted" else None          # a converted model is named by what it was made from, not by the name of a file
        role = gguf_convert.converted_role(root) if m["source"] == "converted" else None             # and it says what it was installed as; any other is told by its name
        for name in m["gguf"]:
            f = next(iter(root.rglob(name)), None) if root.is_dir() else None
            if f is not None and f.is_file():
                out.append({"id": m["id"], "name": name, "label": label or name, "path": str(f), "bytes": f.stat().st_size, "source": m["source"], "role": role})
    return sorted(out, key=lambda f: (f["id"], f["name"]))


def not_ready() -> list[dict]:
    """The installed models that Ollama cannot run as they are: stored as safetensors, with no GGUF file and no conversion made here (a model that was converted is ready, and
    so is one that came as GGUF, converted here or from Ollama). Settings says so instead of showing an empty list of models."""
    import re
    from app.backend.pipeline import gguf_convert, models_local as ml
    converted = gguf_convert.converted_source_ids()
    out = []
    for m in ml.inventory(_models_dir(), converted=gguf_convert.CONVERTED, ollama=ml.ollama_dir())["models"]:
        if m["source"] in ("converted", "ollama") or not m.get("safetensors") or m.get("gguf") or m["id"] in converted:
            continue
        out.append({"id": m["id"], "name": m["id"].split("/")[-1], "source": m["source"], "bytes": m["bytes"], "role": "embedding" if re.search("embed", m["id"], re.I) else "chat"})
    return sorted(out, key=lambda x: x["id"])


@router.get("/api/serving/status")
def serving_status():
    """Report Ollama's native running models and the app's configured role states."""
    from app.backend.pipeline.gateway import client as gateway
    from app.backend.pipeline.ollama import client as ollama, exclusive, server
    version = ollama.version(timeout=2.0)
    live = gateway.status(timeout=3.0, roles=("llm",)) if version else None
    snapshot = live or {"slots": {}, "exclusive": exclusive.current(), "running_models": []}
    return {**snapshot,
            "ollama": {"running": version is not None, "version": version,
                       "error": None if version else server.startup_error()},
            "choices": _choices(),
            "slots": snapshot.get("configured_slots", snapshot["slots"])}


def _choices() -> dict:
    """The configured language model and engine."""
    from app.backend.pipeline import llm_client
    llm = llm_client.resolved()
    return {"llm": {"engine": llm.engine, "model": llm.model_label}}


@router.get("/api/serving/models")
def serving_models():
    """The GGUF files that can be served, each with what its header says (architecture, quantisation, context length, layers, parameters) or why it cannot be read, and
    ``not_ready``: the installed models that Ollama cannot run as they are (safetensors with no GGUF)."""
    from app.backend.pipeline import gguf
    files = []
    for f in gguf_files():
        try:
            i = gguf.describe(f["path"])
            f["info"] = {k: i[k] for k in ("architecture", "name", "quantisation", "context_length", "layers", "parameters")}
        except (gguf.GgufError, OSError) as e:
            f["info"], f["error"] = None, str(e)
        files.append(f)
    return {"files": files, "not_ready": not_ready()}


@router.get("/api/serving/ollama/models")
def ollama_models():
    """Show registered tags by capability, including separately identified remote models."""
    from app.backend.pipeline.ollama import catalog, client
    try:
        return {"models": catalog.installed(), "status": "ready"}
    except client.OllamaError:
        return {"models": [], "status": "unreachable"}


class OllamaInstallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["pull", "huggingface", "gguf", "adapter"]
    tag: str | None = Field(default=None, max_length=160)
    repo_id: str | None = Field(default=None, max_length=200)
    revision: str | None = Field(default=None, max_length=200)
    filename: str | None = Field(default=None, max_length=1000)
    path: str | None = Field(default=None, max_length=4096)
    run_name: str | None = Field(default=None, max_length=250)
    base_path: str | None = Field(default=None, max_length=4096)


@router.post("/api/serving/ollama/install")
def ollama_install(body: OllamaInstallIn):
    """Queue a local install and expose its progress through the existing Jobs interface."""
    from app.backend import jobs
    from app.backend.pipeline.ollama import client, install, server
    if not client.local_endpoint():
        return errors.error("Choose Ollama running on this computer before installing a model.", 409)
    try:
        params = install.validate(body.model_dump(exclude_none=True))
        if client.version(timeout=0.5) is None:
            server.start_in_background()
        job = jobs.start("install_ollama_model", params)
    except (jobs.Blocked, jobs.TooManyJobs) as error:
        return errors.error(str(error), 409)
    except ValueError as error:
        return errors.error(str(error), 400)
    return {"id": job.id, "stage": job.stage}


class LoadIn(BaseModel):
    role: str


@router.post("/api/serving/load")
def serving_load(body: LoadIn):
    """Ask Ollama to load the configured local role and return its running state."""
    from app.backend.pipeline.gateway import client as gateway
    from app.backend.pipeline.gateway.errors import GatewayError
    if body.role == "embed":
        return errors.error("The desktop embedding role has been retired.", 410, code="retired")
    if body.role != "llm":
        return errors.error("Choose the language model role.", 400)
    choice = _choices()[body.role]
    if choice["engine"] != "local":
        return errors.error("This uses a remote endpoint; there is nothing on this computer to load.", 409, code="not_local")
    if not choice["model"]:
        return errors.error("No model is chosen in Settings, so there is nothing to load.", 409, code="not_configured")
    try:
        result = gateway.load(body.role)
        snapshot = result["status"]
        return {**result, "status": {**snapshot, "slots": snapshot.get("configured_slots", snapshot.get("slots", {}))}}
    except GatewayError as e:
        return errors.error(e.message, e.status if e.status < 600 else 502, code=e.code)


class UnloadIn(BaseModel):
    role: str | None = None
    model: str | None = None


@router.post("/api/serving/unload")
def serving_unload(body: UnloadIn | None = None):
    """Unload an exact Ollama model or the configured local language model."""
    from app.backend.pipeline.gateway import client as gateway
    from app.backend.pipeline.gateway.errors import GatewayError
    role = body.role if body else None
    model = body.model if body else None
    if role == "embed":
        return errors.error("The desktop embedding role has been retired.", 410, code="retired")
    if role not in (None, "llm") or (model is not None and (role or not model.strip())):
        return errors.error("Choose a role or a running Ollama model name.", 400)
    try:
        return gateway.unload_model(model) if model else gateway.unload_configured("llm")
    except GatewayError as e:
        return errors.error(e.message, e.status if e.status < 600 else 502, code=e.code)


# -- the adapters converted here (the conversions themselves are done by the install and by the fine-tune import, as jobs) ------------------------------------------

@router.get("/api/serving/adapters")
def serving_adapters():
    """The adapters converted here, each with the model it was trained on and its hashes. One can be loaded on top of a model by the gateway (Settings, advanced)."""
    from app.backend.pipeline import gguf_convert
    return {"adapters": gguf_convert.adapters()}


@router.get("/api/gpu/mode")
def gpu_mode():
    """Who holds the graphics card: ``idle``, ``train`` or ``serve``, with what holds it."""
    from app.backend.pipeline import gpu_modes
    return gpu_modes.current()


@router.post("/api/gpu/release")
def gpu_release():
    """Free the card: the gateway's models are unloaded once they have finished their answers; a job that holds it is named and left to finish or to be cancelled from the Jobs list."""
    from app.backend.pipeline import gpu_modes
    return gpu_modes.release()


@router.get("/api/serving/convert-adapter/runs")
def convert_adapter_runs():
    """The fine-tuning runs that have an adapter, with the model each was trained on, whether that model is installed here, and whether the adapter was converted already."""
    from app.backend.pipeline import gguf_convert
    done = {a["name"] for a in gguf_convert.adapters()}
    out = []
    for a in gguf_convert.run_adapters():
        out.append({**a, "base_installed": gguf_convert.base_entry(a["base"], _models_dir()) is not None, "converted": f"{a['name']}-F16" in done})
    return {"adapters": out}


# -- the cache of converted models ----------------------------------------------------------------------------------------------------------------------

def _derived_budget_gb() -> float:
    from app.backend.config import Settings
    try:
        return max(0.0, float(Settings.load().derived_cache_gb or 0))
    except ValueError:
        return 0.0


def _adapters_made_for(entry: dict) -> int:
    """How many converted adapters were trained on the model this folder was converted from."""
    import json
    from app.backend.pipeline import gguf_convert
    try:
        source = json.loads((pathlib.Path(entry["path"]) / "conversion.json").read_text(encoding="utf-8"))["source"]["id"]
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    return sum(1 for a in gguf_convert.adapters() if a.get("base") == source)


@router.get("/api/serving/derived")
def serving_derived():
    """The cache of converted models (``data/models/converted``): its budget (Settings, empty is no limit), what it holds now and, for each model folder, its size, when it was last loaded,
    whether it is pinned, why it could not be deleted now (loaded, or chosen in Settings) and how many adapters were made for the model it came from. The canonical models are not in it."""
    from app.backend.pipeline import derived_cache
    budget = _derived_budget_gb()
    rows = derived_cache.entries(in_use=derived_cache.loaded_by_gateway, settings_use=derived_cache.chosen_in_settings)
    return {"path": str(derived_cache.default_root()), "budget_gb": budget, "used_bytes": sum(r["bytes"] for r in rows),
            "entries": [{**r, "adapters": _adapters_made_for(r)} for r in rows]}


class PinIn(BaseModel):
    name: str
    pinned: bool = True


@router.put("/api/serving/derived/pin")
def serving_derived_pin(body: PinIn):
    """Pin a converted model so that making room never deletes it, or unpin it. The model is named by its folder as the list gave it."""
    from app.backend.pipeline import derived_cache
    if not derived_cache.set_pin(body.name, body.pinned):
        return errors.error("That converted model is not in the cache any more.", 404)
    return {"name": body.name, "pinned": body.pinned}


@router.post("/api/serving/derived/trim")
def serving_derived_trim():
    """Delete the converted models used least recently until the cache is within its budget (not the pinned ones, the loaded ones or the ones Settings chooses). With no budget set, or
    nothing to delete, nothing is deleted. When the cache cannot be brought within the budget nothing at all is deleted and the sentence says why."""
    from app.backend.pipeline import derived_cache
    budget = _derived_budget_gb()
    if budget <= 0:
        return errors.error("No limit is set for the converted models, so nothing is deleted.", 400, hint="Set the most they may take in Settings.")
    try:
        done = derived_cache.make_room(0, budget)
    except derived_cache.CacheFull as e:
        return errors.error(str(e), 409)
    except (OSError, RuntimeError, ValueError) as e:        # a model another program holds (Busy), one that is not ours to delete (Refused), or a file system error
        return errors.error("The cache could not be trimmed completely.", 500, detail=f"{type(e).__name__}: {str(e)[:200]}")
    return {"removed": done["removed"], "freed_bytes": done["freed"], "message": done["message"]}
