"""Looking models up on the Hugging Face Hub: a search, and what a memory-fit estimate needs from one model (its parameters, file sizes and configuration).

Only metadata is read: the search, the model's information with its file sizes, and the small ``config.json``. No weight is downloaded. Every figure comes
from the Hub's answer and is absent when the Hub does not state it; nothing is filled in from a name. The Hub client is passed in so the tests run offline.
"""
from __future__ import annotations

import json
import math
import re

from app.backend.pipeline import fit

TASKS = {"": None, "generation": "text-generation", "embedding": "feature-extraction", "gguf": None}       # "gguf": repositories of GGUF files (ready for the local server)
SEARCH_FIELDS = ["downloads", "likes", "pipeline_tag", "library_name", "gated", "safetensors", "lastModified", "config", "siblings", "sha", "tags"]


def _api(api=None):
    if api is not None:
        return api
    from huggingface_hub import HfApi
    return HfApi()                                   # the token, when one is set, is read from HF_TOKEN by the library itself


def _total(model) -> int | None:
    st = getattr(model, "safetensors", None)
    total = getattr(st, "total", None) if st is not None else None
    return int(total) if isinstance(total, (int, float)) and total > 0 and math.isfinite(total) else None


def _row(m) -> dict:
    from training.finetune import model_eligibility
    when = getattr(m, "last_modified", None)
    return {"id": m.id, "downloads": getattr(m, "downloads", None), "likes": getattr(m, "likes", None), "task": getattr(m, "pipeline_tag", None),
            "library": getattr(m, "library_name", None), "gated": bool(getattr(m, "gated", False)), "parameters": _total(m),
            "updated": when.isoformat() if hasattr(when, "isoformat") else (str(when) if when else None), **model_eligibility.hub_metadata(m)}


FULL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")        # org/name, as a model's page address and its id spell it


def search(query: str = "", task: str = "", limit: int = 20, api=None) -> dict:
    """The most downloaded models matching ``query`` (and a task: text generation or embeddings), as plain rows. The Hub's search matches words in a name, not a name with its
    organisation in front: a query that is a full model name (``org/name``) searches for the name part, and the model with exactly that id comes first (asked for by name when the search
    did not list it), so pasting a model's address finds the model."""
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}")
    limit = max(1, min(int(limit), 50))
    extra = {"filter": "gguf"} if task == "gguf" else {}
    client = _api(api)
    text = query.strip()
    exact = text if FULL_NAME.match(text) else None
    words = text.split("/", 1)[1] if exact else text
    found = client.list_models(search=words or None, pipeline_tag=TASKS[task], sort="downloads", limit=limit, expand=SEARCH_FIELDS, **extra)
    rows = [_row(m) for m in found]
    if exact:
        rows = _exact_first(rows, exact, client, limit)
    return {"query": text, "task": task, "models": rows}


def _exact_first(rows: list[dict], exact: str, client, limit: int) -> list[dict]:
    """``rows`` with the model whose id is ``exact`` (any case) first; looked up by name, and put in front, when the list does not hold it."""
    wanted = exact.lower()
    at = next((i for i, r in enumerate(rows) if str(r["id"]).lower() == wanted), None)
    if at is not None:
        return [rows[at]] + rows[:at] + rows[at + 1:]
    try:
        info = client.model_info(exact, expand=SEARCH_FIELDS)
    except Exception:  # ignored: a name the Hub does not know, or one call that fails, only means the list has no exact match; the search itself has answered
        return rows
    return ([_row(info)] + rows)[:limit]


def fit_inputs(repo_id: str, api=None, config_reader=None) -> tuple[fit.Shape, list[str], int | None, dict]:
    """``(shape, notes, weights_bytes, facts)`` for one model: its dimensions from ``config.json``, its parameter count from the Hub, and the size of its
    weight files. ``config_reader(repo_id, revision)`` returns the parsed configuration (the default downloads that one small file)."""
    info = _api(api).model_info(repo_id, files_metadata=True)          # the Hub refuses `expand` together with `files_metadata`
    notes: list[str] = []
    siblings = [{"rfilename": s.rfilename, "size": getattr(s, "size", None)} for s in (getattr(info, "siblings", None) or [])]
    weights = sum(int(s["size"]) for s in siblings if s["rfilename"].endswith(".safetensors") and isinstance(s["size"], (int, float))) or None
    if weights is None:
        sizes = fit.gguf_sizes(siblings)
        notes.append("The model has no safetensors weights" + (f"; its GGUF files come as {', '.join(sorted(sizes))}" if sizes else "") + ".")
    reader = config_reader or _read_config
    try:
        config = reader(repo_id, getattr(info, "sha", None))
    except Exception as e:  # ignored: a missing or unreadable configuration is reported as a missing dimension, not as a failure of the search
        config = {}
        notes.append(f"The model's config.json could not be read ({type(e).__name__}).")
    params = _total(info)
    if params is None:
        notes.append("The Hub does not state the parameter count of this model.")
    facts = {"id": repo_id, "sha": getattr(info, "sha", None), "gated": bool(getattr(info, "gated", False)), "parameters": params, "weight_bytes": weights,
             "gguf": fit.gguf_sizes(siblings), "gguf_files": fit.single_gguf_sizes(siblings), "task": getattr(info, "pipeline_tag", None)}
    return fit.shape_from_config(config, params), notes, weights, facts


def _file_sizes(info) -> dict[str, int]:
    return {s.rfilename: int(s.size) for s in (getattr(info, "siblings", None) or []) if isinstance(getattr(s, "size", None), (int, float))}


def gguf_for(base_id: str, api=None, limit: int = 5) -> dict:
    """GGUF versions of a base model that someone has already converted: the repositories the Hub lists as quantised from it (else a search for GGUF repositories of its name),
    the most downloaded first, each with the size of every quantisation, and the size of the base model's own weights, so the disk cost of the training copy and of the serving
    copy can be shown before anything is downloaded. Only metadata is read. A repository the Hub does not answer for is left out."""
    client = _api(api)
    limit = max(1, min(int(limit), 10))
    found = list(client.list_models(filter=f"base_model:quantized:{base_id}", sort="downloads", limit=limit, expand=["downloads", "likes", "gated"]))
    how = "converted from this model"
    if not found:
        name = base_id.split("/")[-1]
        found = list(client.list_models(search=name, filter="gguf", sort="downloads", limit=limit, expand=["downloads", "likes", "gated"]))
        how = "found by name"
    base = client.model_info(base_id, files_metadata=True)                          # the Hub refuses `expand` together with `files_metadata`
    training = sum(n for f, n in _file_sizes(base).items() if f.endswith(".safetensors")) or None
    out = []
    for m in found:
        try:
            quants = fit.gguf_sizes([{"rfilename": f, "size": n} for f, n in _file_sizes(client.model_info(m.id, files_metadata=True)).items()])
        except Exception:  # ignored: a repository that does not answer is left out of the list; the others are still shown
            continue
        if quants:
            out.append({"id": m.id, "downloads": getattr(m, "downloads", None), "likes": getattr(m, "likes", None), "gated": bool(getattr(m, "gated", False)), "quants": quants})
    return {"base": base_id, "how": how if out else "none found", "training_bytes": training, "candidates": out}


def _read_config(repo_id: str, revision: str | None) -> dict:
    """The model's ``config.json`` (a few kilobytes), fetched into a folder that is thrown away so that merely looking at a model never adds an entry to the
    Hugging Face cache that the Installed list reads."""
    import tempfile
    from huggingface_hub import hf_hub_download
    with tempfile.TemporaryDirectory(prefix="aivdf-config-") as tmp:
        with open(hf_hub_download(repo_id, "config.json", revision=revision, cache_dir=tmp), encoding="utf-8") as f:
            return json.loads(f.read())


def project_models() -> list[tuple[str, str]]:
    """The base model from the fine-tuning configuration."""
    from training.finetune import config as cfgmod
    return [(cfgmod.BASE_MODEL_ID, "Base model for fine-tuning")]


def shelf(api=None, models=None) -> list[dict]:
    """The project's own models, each checked against the Hub when this is asked: whether it is there, its parameters, whether it is gated. A model the Hub
    does not answer for carries the reason instead of a figure."""
    client = _api(api)
    out = []
    for model_id, role in (models if models is not None else project_models()):
        try:
            info = client.model_info(model_id, expand=SEARCH_FIELDS + ["sha"])
            out.append({**_row(info), "role": role, "sha": getattr(info, "sha", None)})
        except Exception as e:  # ignored: one model the Hub does not answer for is shown with its reason; the others are still checked
            out.append({"id": model_id, "role": role, "error": f"{type(e).__name__}: {str(e)[:160]}"})
    return out
