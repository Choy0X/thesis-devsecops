"""App configuration and persisted settings.

Endpoints, keys and SMTP credentials are configured from the Settings tab and
persisted to ``settings.json`` beside this file (which is gitignored, so secrets
are never committed). On startup these values are overlaid onto ``os.environ``
under their canonical names, so every pipeline module keeps reading them through
``os.getenv`` unchanged. A local ``.env`` still works as a fallback; values set
in the UI take precedence over it.

UI choices (scan scope, response mode, dataset path) persist the same way so the
desktop app remembers them between runs.
"""
from __future__ import annotations
import json
import os
import pathlib
from dataclasses import dataclass, asdict, fields

from dotenv import load_dotenv

load_dotenv()

SETTINGS_PATH = pathlib.Path(os.environ.get("ZTSA_SETTINGS_PATH") or pathlib.Path(__file__).with_name("settings.json"))     # the variable lets a process of its own (a test) use another file

# Persisted field name -> environment variable the pipeline reads it from.
RETIRED_SERVING_FIELDS = frozenset(('embed_device', 'model_idle_minutes', 'quality_policy', 'inference_public_hostname', 'cloudflared_path', 'cloudflare_tunnel_token'))
RETIRED_RETRIEVAL_FIELDS = frozenset({
    "rag_enabled", "rag_dimension", "embed_engine", "embed_local_model",
    "embed_ollama_model", "embed_device", "embed_base_url", "embed_api_key", "embed_model",
})

ENV_MAP = {
    "llm_base_url": "LLM_BASE_URL",
    "llm_api_key": "LLM_API_KEY",
    "llm_model": "LLM_MODEL",
    "nvd_api_key": "NVD_API_KEY",
    "github_token": "GITHUB_TOKEN",
    "hf_token": "HF_TOKEN",                     # Hugging Face access token: gated models and higher download limits (read by huggingface_hub itself)
    "smtp_host": "SMTP_HOST",
    "smtp_port": "SMTP_PORT",
    "smtp_user": "SMTP_USER",
    "smtp_password": "SMTP_PASSWORD",
    "smtp_from": "SMTP_FROM",
    "smtp_to": "SMTP_TO",
    "interim_dir": "ZTSA_INTERIM_DIR",          # where the databases live (paths.py); takes effect at the next start
    "cpu_workers": "ZTSA_CPU_WORKERS",          # worker processes for CPU-bound stages: "auto" or a number (compute.py)
    "gpu_python": "ZTSA_GPU_PYTHON",            # the Python that has CUDA torch, for the local GPU jobs ("" = .venv-gpu next to the code)
    "database_dir": "ZTSA_DATABASE_DIR",        # embedded SQLite files; applied at startup, never moves data
    "window_frame": "ZTSA_WINDOW_FRAME",        # "native" = the operating system's window frame; "" = the app's own frameless window with its title bar
    "models_dir": "ZTSA_MODELS_DIR",            # the one folder that holds every model: GGUF files, converted models, adapters and, in ``hub``, the Hugging Face cache ("" = data/models)
    "compute_remember": "ZTSA_COMPUTE_REMEMBER",  # the Compute dialog's "remember for this job type": a JSON object, job stage -> cpu or gpu (managed in Settings > Performance)
    "compute_gpu": "ZTSA_COMPUTE_GPU",          # "off" = nothing is probed, planned or run on the graphics card (compute.gpu_enabled); applies to jobs started afterwards
    "ui_gpu": "ZTSA_UI_GPU",                    # "off" = the window is drawn without the graphics card (app/webengine.py); takes effect at the next start
    "max_jobs": "ZTSA_MAX_JOBS",                # jobs running at once, 1 to 16 ("" = 4); applies to jobs started afterwards
    "notify_email": "ZTSA_NOTIFY_EMAIL",        # "" = no letter when a job ends; failed = only when one fails; all = when any job that took a while ends (pipeline/notify.py)
    "confirm_quit": "ZTSA_CONFIRM_QUIT",        # "off" = closing the window does not ask "Quit the app?" (a running job is always asked about); "" = ask (the person ticked Do not ask me again)
    "desktop_notify": "ZTSA_DESKTOP_NOTIFY",    # "off" = the window says nothing when a job ends and no system notification is sent; "" = on (the page reads it: lib/jobAlerts.ts)
    "rag_enabled": "RAG_ENABLED",               # "off" preserves the opt-out from automatic project search
    "rag_dimension": "RAG_DIMENSIONS",          # the vector size of the embedding model ("" = the model's own; an external endpoint needs it)
    # The engines (the model gateway, pipeline/gateway): "local" = llama.cpp behind the gateway, "external" = an OpenAI-compatible endpoint
    "llm_engine": "LLM_ENGINE",                 # "local", "external"; "" = external when an endpoint is saved, else local
    "llm_local_model": "ZTSA_LLM_LOCAL_MODEL",  # the GGUF file the gateway loads for the language model
    "llm_local_adapter": "ZTSA_LLM_LOCAL_ADAPTER",   # an optional LoRA adapter (GGUF) loaded with it
    "llm_local_context": "ZTSA_LLM_LOCAL_CONTEXT",   # its context size in tokens ("" = 4096)
    "embed_engine": "EMBED_ENGINE",             # "local", "external"; "" = external when an embeddings endpoint is saved, else local
    "embed_local_model": "ZTSA_EMBED_LOCAL_MODEL",   # the GGUF file the gateway loads for embeddings
    "embed_ollama_model": "ZTSA_EMBED_OLLAMA_MODEL",
    "embed_base_url": "EMBED_BASE_URL",         # an external embeddings endpoint (OpenAI-compatible)
    "embed_api_key": "EMBED_API_KEY",
    "embed_model": "EMBED_MODEL",
    "derived_cache_gb": "ZTSA_DERIVED_CACHE_GB",   # the most the converted models (data/models/converted) may take, in GB ("" or 0 = no limit: nothing is ever deleted)
    "web_search_provider": "WEB_SEARCH_PROVIDER",   # which search engine a chat's web search (when turned on for a conversation) uses: "" = off, "brave", "google", "duckduckgo"
    "brave_api_key": "BRAVE_API_KEY",
    "google_api_key": "GOOGLE_API_KEY",
    "google_cse_id": "GOOGLE_CSE_ID",           # the id of the Google Custom Search Engine the key searches (not a secret on its own)
    "agent_sandbox_network": "ZTSA_AGENT_SANDBOX_NETWORK",   # the agent sandbox's network: "" or off (none at all, the default) or proxy (a way out through a guarded proxy, public web ports only)
    "agent_sandbox_idle_minutes": "ZTSA_AGENT_SANDBOX_IDLE_MINUTES",   # a project's agent sandbox left untouched this long is stopped, never deleted ("" = 30, 0 = never)
}

# Fields that are exported to the environment only when the app starts: the server and every job it starts must use the same value, so a
# change saved while the app runs waits for a restart.
RESTART_FIELDS = {"interim_dir", "database_dir", "window_frame", "ui_gpu"}

# API-key fields (rendered with a show/hide toggle in the UI).
SECRET_FIELDS = {'cloudflare_tunnel_token', "llm_api_key", "nvd_api_key", "github_token", "hf_token",
                 "smtp_password", "embed_api_key", "brave_api_key", "google_api_key"}

# Retained only in the private settings file for an explicit migration or rollback.
# These values are never active settings, returned by public(), or exported to the environment.
LEGACY_MONGO_FIELDS = frozenset({"mongo_uri", "mongo_db", "mongo_container"})
LEGACY_RETRIEVAL_FIELDS = frozenset({"rag_model", "rag_revision", "rag_gpu_strategy"})
_PRIVATE_LEGACY_FIELDS = LEGACY_MONGO_FIELDS | LEGACY_RETRIEVAL_FIELDS

# Full OpenAI endpoint paths the user might paste (we want the base, not these).
_OPENAI_SUFFIXES = ("/chat/completions", "/completions", "/embeddings", "/responses")


def normalize_base_url(url: str) -> str:
    """Reduce a pasted endpoint to the OpenAI-compatible *base* URL.

    The OpenAI client appends the path (``/chat/completions``, ``/embeddings``,
    ``/models``) to the base, so the base must not already include it. This accepts
    a full endpoint path and returns the base:

    * ``http://host:8080/v1/chat/completions`` -> ``http://host:8080/v1``
    * ``http://host:8080/v1``                  -> unchanged
    """
    if not url:
        return url
    u = url.strip().rstrip("/")
    low = u.lower()
    for suffix in _OPENAI_SUFFIXES:
        if low.endswith(suffix):
            return u[: -len(suffix)]
    return u


@dataclass
class Settings:
    # Engine / credentials (persisted to settings.json, overlaid onto os.environ)
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    nvd_api_key: str = ""
    github_token: str = ""
    hf_token: str = ""
    smtp_host: str = ""
    smtp_port: str = "587"
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    # Where the databases live ("" = data/interim); see paths.py
    interim_dir: str = ""
    # Compute: CPU worker processes ("auto" = physical cores - 1, limited by free memory) and the GPU interpreter; see compute.py
    cpu_workers: str = "auto"
    gpu_python: str = ""
    # Embedded records (empty = the database subfolder of interim_dir).
    database_dir: str = ""
    window_frame: str = ""
    models_dir: str = ""
    # Performance: hardware acceleration for compute and for drawing the window ("" = on, "off"), and how many jobs may run at once ("" = 4)
    compute_gpu: str = ""
    compute_remember: str = ""
    ui_gpu: str = ""
    max_jobs: str = ""
    # Retrieval: the switch for the project index and the chat's search, and the vector size of the embedding model
    notify_email: str = ""
    desktop_notify: str = ""
    confirm_quit: str = ""
    rag_enabled: str = ""
    rag_dimension: str = ""
    # The engines: the language model and the embedding model are each local (Ollama) or external (an OpenAI-compatible endpoint)
    llm_engine: str = ""
    llm_local_model: str = ""
    llm_local_adapter: str = ""
    llm_local_context: str = ""
    embed_engine: str = ""
    embed_local_model: str = ""
    embed_ollama_model: str = ""
    embed_device: str = ""
    embed_base_url: str = ""
    embed_api_key: str = ""
    embed_model: str = ""
    model_idle_minutes: str = ""
    quality_policy: str = ""
    cloudflare_tunnel_token: str = ''
    cloudflared_path: str = ''
    inference_public_hostname: str = ''
    derived_cache_gb: str = ""
    # Web search for the chat (pipeline/rag/web_search.py): off until a provider is chosen
    web_search_provider: str = ""
    brave_api_key: str = ""
    google_api_key: str = ""
    google_cse_id: str = ""
    # Agentic mode's per-project sandbox (pipeline/scan/agent_sandbox.py): minutes of being left alone before it is stopped
    agent_sandbox_idle_minutes: str = ""
    agent_sandbox_network: str = ""

    @classmethod
    def load(cls) -> "Settings":
        data = {}
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text())
            except Exception as e:
                print(f"{SETTINGS_PATH} could not be read ({type(e).__name__}: {e}); using the defaults")
                data = {}
        data = data if isinstance(data, dict) else {}
        s = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        s._private_legacy = {k: data[k] for k in _PRIVATE_LEGACY_FIELDS if k in data}
        s._cleared = set(data.get("_cleared") or [])
        # Fill any env-backed field that is unset in settings.json from the
        # current environment (.env fallback), so the UI reflects reality - but
        # never a field the user cleared on purpose.
        for field_name, env_name in ENV_MAP.items():
            if field_name in s._cleared:
                continue
            if not getattr(s, field_name):
                env_val = os.getenv(env_name)
                if env_val:
                    setattr(s, field_name, env_val)
        return s

    def set_field(self, name: str, value: str) -> None:
        """Set a value; an empty value clears it for good (no .env fallback)."""
        setattr(self, name, value)
        cleared = set(getattr(self, "_cleared", set()))
        if value:
            cleared.discard(name)
        else:
            cleared.add(name)
        self._cleared = cleared

    def save(self) -> None:
        keep = {f.name: getattr(self, f.name) for f in fields(self)}
        keep["_cleared"] = sorted(getattr(self, "_cleared", set()))
        legacy = dict(getattr(self, "_private_legacy", {}))
        try:
            previous = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if isinstance(previous, dict):
            legacy.update({k: previous[k] for k in _PRIVATE_LEGACY_FIELDS if k in previous})
        keep.update(legacy)
        from app.backend.pipeline import fsio
        fsio.write_json(SETTINGS_PATH, keep, indent=2, mode=0o600)     # the file holds keys and tokens: owner-only

    def apply_env(self, startup: bool = False) -> None:
        """Overlay env-backed values onto os.environ so the pipeline (and every job
        started from now on) sees them; a cleared value is removed. The fields in
        ``RESTART_FIELDS`` are applied only at start-up."""
        for field_name, env_name in ENV_MAP.items():
            if field_name in RESTART_FIELDS and (not startup or os.environ.get(env_name)):
                continue                             # applied only at start-up, and an environment value set by the caller wins (paths.py)
            val = getattr(self, field_name)
            if val:
                os.environ[env_name] = str(val)
            elif field_name in getattr(self, "_cleared", set()):
                os.environ.pop(env_name, None)
        pin_hub_cache()

    def public(self) -> dict:
        """Settings for the client. Secret values never leave the backend: the UI
        gets ``<name>_set`` booleans (and ``<name>_hint``, the last 4 characters)
        and sends a new value only to replace one."""
        out = {}
        for f in fields(self):
            if f.name in RETIRED_SERVING_FIELDS or f.name in RETIRED_RETRIEVAL_FIELDS:
                continue
            val = getattr(self, f.name)
            if f.name in SECRET_FIELDS:
                out[f.name] = ""
                out[f"{f.name}_set"] = bool(val)
                out[f"{f.name}_hint"] = val[-4:] if val and len(val) > 8 else ""
            else:
                out[f.name] = val
        from app.backend import paths
        out["storage"] = paths.describe(self.interim_dir or None)
        out["database_storage"] = paths.describe_database(self.database_dir or None)
        from app.backend.pipeline import model_files
        default = model_files.default_models_dir()
        in_use = pathlib.Path((self.models_dir or "").strip()) if (self.models_dir or "").strip() else default
        out["models"] = {"default": str(default), "in_effect": str(model_files.models_dir()), "saved": str(in_use), "exists": in_use.is_dir()}
        return out


def desktop_auto_retrieval_enabled(settings: Settings | None = None) -> bool:
    """Honor the private legacy opt-out for automatic project search."""
    current = Settings.load() if settings is None else settings
    return str(getattr(current, "rag_enabled", "") or "").strip().lower() != "off"


_pinned_hub_cache: str | None = None


def pin_hub_cache() -> None:
    """Point ``HF_HUB_CACHE`` at the ``hub`` folder inside the models folder, so a download, a training job and the local GPU child all use the one folder. An ``HF_HUB_CACHE`` that the
    caller's environment set itself is left alone (only a value this function set is replaced when the models folder changes). The Hugging Face library reads the variable once, when
    it is first imported, and the application imports it only when a download starts; a job is a process of its own and reads it fresh."""
    global _pinned_hub_cache
    current = os.environ.get("HF_HUB_CACHE")
    if current and current != _pinned_hub_cache:
        return
    from app.backend.pipeline import model_files
    _pinned_hub_cache = str(model_files.hub_cache({k: v for k, v in os.environ.items() if k != "HF_HUB_CACHE"}))
    os.environ["HF_HUB_CACHE"] = _pinned_hub_cache


def env_status() -> dict:
    """Which capabilities are configured (booleans only; no secret values)."""
    return {
        "llm_endpoint": bool(os.getenv("LLM_BASE_URL")),
        "llm_key": bool(os.getenv("LLM_API_KEY")),
        "nvd_key": bool(os.getenv("NVD_API_KEY")),
        "smtp": bool(os.getenv("SMTP_HOST")),
    }


# Overlay persisted settings onto the environment at import time (UI wins over
# .env) so modules importing os.getenv later observe the configured values.
try:
    Settings.load().apply_env(startup=True)
except Exception as e:                       # the app still starts; the Settings page shows what is missing
    print(f"settings could not be applied at startup: {type(e).__name__}: {e}")
