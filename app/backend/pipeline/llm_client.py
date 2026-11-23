"""The one place an OpenAI-compatible client is built, and the one place that says where the language model is.

Every caller that talks to the configured language model (the settings tests, enrichment, classification, detection, patch generation, the API patch baseline, the project chat and the
flagging probe) goes through here, so the engine, the base URL cleanup, the placeholder key, the timeout and the retry count are decided once. Nothing else imports the ``openai`` package.

``resolved()`` answers for the engine chosen in Settings:

* **local**: Ollama, with the selected GGUF registered under its native model name before inference.
* **external**: the endpoint written in Settings (``LLM_BASE_URL``, ``LLM_API_KEY`` and ``LLM_MODEL`` in the environment). A caller that also reads a local ``.env`` passes it as ``fallback``.

With no engine chosen, an endpoint that is saved means external and nothing saved means local.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
from typing import Mapping

MISSING = "Configure the LLM endpoint and model in Settings first."
MISSING_LOCAL = "Choose a local language model in Settings first."
DEFAULT_TIMEOUT = 120.0
SDK_DEFAULT_TIMEOUT = 600.0         # what the openai package uses when a caller names no timeout


def env_value(name: str, fallback: Mapping[str, str | None] | None = None, default: str | None = None) -> str | None:
    """The environment value of ``name``, else the fallback mapping's, else ``default``."""
    return os.getenv(name) or (fallback or {}).get(name) or default


@dataclasses.dataclass(frozen=True)
class Resolved:
    engine: str                 # "local" or "external"
    base_url: str               # "" when nothing is set
    api_key: str
    model: str                  # the name sent in requests
    configured: bool
    model_label: str            # what a result says the model was: the endpoint's model name, or the GGUF file of the local model
    host: str                   # where it runs, for a person
    local_model_file: str       # the chosen GGUF file (local only)
    explicit: bool = False      # the engine was chosen in Settings (not inferred from what is saved)

def engine(fallback: Mapping[str, str | None] | None = None) -> str:
    chosen = (env_value("LLM_ENGINE", fallback) or "").strip().lower()
    if chosen in ("local", "external"):
        return chosen
    return "external" if env_value("LLM_BASE_URL", fallback) else "local"


def resolved(fallback: Mapping[str, str | None] | None = None) -> Resolved:
    """Where the language model is, for the engine chosen in Settings."""
    from app.backend.config import normalize_base_url
    explicit = (env_value("LLM_ENGINE", fallback) or "").strip().lower() in ("local", "external")
    if engine(fallback) == "local":
        from app.backend.pipeline.gateway import client as gateway
        base, key = gateway.v1_base() or "", gateway.key() or ""
        path = env_value("ZTSA_LLM_LOCAL_MODEL", fallback) or ""
        settings = _local_settings(path, fallback)
        from app.backend.pipeline.ollama.models import saved_chat_name
        model = saved_chat_name(settings)
        name = pathlib.Path(path).name if path else model
        return Resolved("local", base, key, model, bool(base and name), name, "this computer (Ollama)", path, explicit)
    base = normalize_base_url(env_value("LLM_BASE_URL", fallback, "") or "")
    model = env_value("LLM_MODEL", fallback) or ""
    host = base.split("//", 1)[-1].split("/", 1)[0] if base else ""
    return Resolved("external", base, env_value("LLM_API_KEY", fallback) or "", model, bool(base and model), model, host, "", explicit)


def endpoint_url(fallback: Mapping[str, str | None] | None = None) -> str:
    """The base URL of the language model ("" when none is set)."""
    return resolved(fallback).base_url


def _local_settings(path: str, fallback: Mapping[str, str | None] | None = None):
    from app.backend.config import Settings
    settings = Settings.load()
    return dataclasses.replace(settings, llm_engine="local", llm_local_model=path,
        llm_local_context=env_value("ZTSA_LLM_LOCAL_CONTEXT", fallback) or settings.llm_local_context,
        llm_local_adapter=env_value("ZTSA_LLM_LOCAL_ADAPTER", fallback) or settings.llm_local_adapter)


def make(base_url: str, api_key: str | None = None, *, timeout: float = DEFAULT_TIMEOUT, max_retries: int | None = None, headers: Mapping[str, str] | None = None):
    """A client for ``base_url``. A missing key becomes the placeholder ``none`` that local servers accept; ``max_retries`` None keeps
    the package default. Raises ImportError when the ``openai`` package is not installed."""
    from openai import OpenAI
    extra = {} if max_retries is None else {"max_retries": max_retries}
    if headers:
        extra["default_headers"] = dict(headers)
    return OpenAI(base_url=base_url, api_key=api_key or "none", timeout=timeout, **extra)


def local_client(*, timeout: float = DEFAULT_TIMEOUT):
    from openai import DefaultHttpxClient, OpenAI
    from app.backend.pipeline.gateway import client as gateway

    def admit(_request):
        gateway.check_inference()

    http = DefaultHttpxClient(trust_env=False, follow_redirects=False, event_hooks={"request": [admit]})
    return OpenAI(base_url=gateway.v1_base(), api_key=gateway.key(), timeout=timeout, max_retries=0, http_client=http)


def request_error_type():
    from openai import APIError

    return APIError


def configured(*, timeout: float = DEFAULT_TIMEOUT, fallback: Mapping[str, str | None] | None = None,
               required: bool = True, require_model: bool = True, priority: str = "batch", require_gpu: bool = False):
    """``(client, model)`` for the engine chosen in Settings.

    With ``required`` a missing base URL (or, with ``require_model``, a missing model) raises RuntimeError instead of falling back to
    api.openai.com with a dummy key; without it missing configuration gives ``(None, None)``."""
    r = resolved(fallback)
    missing = MISSING_LOCAL if r.engine == "local" and r.explicit else MISSING          # nothing saved at all is the generic message
    if not r.base_url:
        if required:
            raise RuntimeError(missing)
        return None, None
    if r.engine == "local":
        from app.backend.pipeline.gateway import client as gateway
        if not r.configured:
            if required and require_model:
                raise RuntimeError(missing)
            return (None, None) if not required else (local_client(timeout=timeout), None)
        model = gateway.inference_model("llm", settings=_local_settings(r.local_model_file, fallback), require_gpu=require_gpu)
        return local_client(timeout=timeout), model
    if required and require_model and not r.model:
        raise RuntimeError(missing)
    return make(r.base_url, r.api_key, timeout=timeout), (r.model or None)


def engine_fields(fallback: Mapping[str, str | None] | None = None) -> dict:
    """``{"engine": {...}}`` for the run configuration of a stage that talks to the language model when that is a local model; ``{}`` for an external endpoint, so a run with another
    endpoint is not given an engine it did not use. What is loaded comes from the gateway (the program and its release, the model file, its quantisation, the context and where the
    layers ran); before the model is loaded the chosen model file is all that is known."""
    r = resolved(fallback)
    if r.engine != "local":
        return {}
    from app.backend.pipeline.gateway import client as gateway
    slot = ((gateway.status(timeout=2.0) or {}).get("slots") or {}).get("llm") or {}
    info = slot.get("engine") or {}
    if info:
        return {"engine": info}
    adapter = env_value("ZTSA_LLM_LOCAL_ADAPTER", fallback) or ""
    return {"engine": {"name": "Ollama", "model_file": r.model_label, "adapter": pathlib.Path(adapter).name if adapter else None, "loaded": False}}
