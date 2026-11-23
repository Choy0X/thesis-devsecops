"""Settings, connection tests, model discovery, compute and storage information, health and the config summary."""
from __future__ import annotations

import os
import pathlib
import re

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.backend import errors, jobs
from app.backend.config import ENV_MAP, RETIRED_RETRIEVAL_FIELDS, SECRET_FIELDS, Settings, env_status, normalize_base_url
from app.backend.pipeline import llm_client

router = APIRouter()


class SettingsUpdate(BaseModel):
    # Engine / credentials. Omit a field to keep it; an empty string clears it.
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    nvd_api_key: str | None = None
    github_token: str | None = None
    hf_token: str | None = None
    smtp_host: str | None = None
    smtp_port: str | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None
    # Where the databases live; saved now, used after a restart (paths.py)
    interim_dir: str | None = None
    # Compute (compute.py): "auto" or a number of CPU worker processes; the Python with CUDA torch
    cpu_workers: str | None = None
    gpu_python: str | None = None
    # Embedded records folder; saved now and used after a deliberate restart.
    database_dir: str | None = None
    window_frame: str | None = None
    models_dir: str | None = None
    compute_gpu: str | None = None
    compute_remember: str | None = None
    ui_gpu: str | None = None
    max_jobs: str | None = None
    notify_email: str | None = None
    desktop_notify: str | None = None
    confirm_quit: str | None = None
    # Local Ollama and external inference engines
    llm_engine: str | None = None
    llm_local_model: str | None = None
    llm_local_adapter: str | None = None
    llm_local_context: str | None = None
    derived_cache_gb: str | None = None
    web_search_provider: str | None = None
    brave_api_key: str | None = None
    google_api_key: str | None = None
    google_cse_id: str | None = None
    agent_sandbox_idle_minutes: str | None = None
    agent_sandbox_network: str | None = None


@router.get("/api/health")
def health():
    return {"ok": True, "env": env_status(), "stages": list(jobs.STAGES.keys())}


@router.get("/api/config/summary")
def get_config_summary():
    """The model, data scope and training settings from the project config (read-only)."""
    from app.backend import project_config
    from app.backend.pipeline.ingest import osv
    try:
        s = project_config.summary()
        s["data"]["ecosystem_status"] = [{"name": e, "local_data": osv.has_local_data(e)} for e in s["data"]["ecosystems"]]
        return s
    except Exception as e:                      # an invalid config is reported, never replaced by a default
        return errors.error(f"configuration is invalid: {e}", 500)


@router.get("/api/settings")
def get_settings():
    return Settings.load().public()


# Only active key and password fields may be revealed. Legacy migration settings stay private.
REVEALABLE = SECRET_FIELDS - RETIRED_RETRIEVAL_FIELDS


class SecretIn(BaseModel):
    name: str


@router.post("/api/settings/secret")
def reveal_secret(body: SecretIn):
    """The saved value of one key, token or password, for the Show button of a field. The page never receives a secret with the settings (``Settings.public``), so a field that has a
    saved value is empty on the page and Show has nothing to show; this answers only when the person presses it. A POST, so that a link, a cache or the browser history never holds it,
    and answered with ``no-store``. It is behind the same Host and session-cookie check as every other route (security.LocalOnly). The value is not logged."""
    if body.name not in REVEALABLE:
        return errors.error("That setting is not a key, token or password that can be shown", 400, hint="Only a saved key, token or password can be shown; the database address stays hidden.")
    value = getattr(Settings.load(), body.name, "") or ""
    return JSONResponse({"value": value}, headers={"Cache-Control": "no-store"})


@router.put("/api/settings")
def put_settings(update: SettingsUpdate):
    s = Settings.load()
    # Engine / credentials: a field that is sent is set; an empty string clears it
    # (secrets are never sent back to the UI, so the UI only sends a secret when
    # the user types a new one or clears it). Base URLs are normalised.
    for field_name in ENV_MAP:
        val = getattr(update, field_name, None)
        if val is None:
            continue
        if field_name == "llm_base_url":
            val = normalize_base_url(val)
        if field_name == "llm_engine":
            val = val.strip().lower()
            if val not in ("", "local", "external"):
                return errors.error("An engine is local or external", 400, hint="Choose Local to use a model on this computer, or External for an endpoint.")
        if field_name in ("llm_local_model", "llm_local_adapter") and val.strip():
            path = pathlib.Path(val.strip())
            if not path.is_file() or path.suffix.lower() != ".gguf":
                return errors.error("that model file does not exist", 400, hint="Choose a downloaded .gguf model; the Model Hub downloads and converts them.")
            val = str(path)
        if field_name == "llm_local_context":
            val = val.strip()
            if val and not (val.isdigit() and 512 <= int(val) <= 1048576):
                return errors.error("The context size is a number of tokens from 512 to 1048576", 400, hint="Leave it empty for 4096.")
        if field_name == "derived_cache_gb":
            val = val.strip()
            if val:
                try:
                    ok = 0 <= float(val) <= 100000
                except ValueError:
                    ok = False
                if not ok:
                    return errors.error("The most the converted models may take: a number of gigabytes from 0 to 100000", 400, hint="0 or empty means no limit; with a limit the converted model used least recently is deleted to make room (it can be made again).")
        if field_name == "agent_sandbox_network":
            val = val.strip().lower()
            if val not in ("", "off", "proxy"):
                return errors.error("The agent sandbox's network is off or proxy", 400, hint="Off: no network at all. Proxy: a way out through a guarded proxy that reaches public web addresses only.")
        if field_name == "agent_sandbox_idle_minutes":
            val = val.strip()
            if val:
                try:
                    ok = 0 <= float(val) <= 1440
                except ValueError:
                    ok = False
                if not ok:
                    return errors.error("Minutes before an unused agent sandbox is stopped: a number from 0 to 1440", 400, hint="0 keeps it running; leave it empty for 30.")
        if field_name == "cpu_workers":
            val = val.strip().lower() or "auto"
            if val != "auto" and not (val.isdigit() and 1 <= int(val) <= 64):
                return errors.error("CPU workers must be auto or a number from 1 to 64", 400,
                                    hint="Use auto, or a whole number between 1 and 64.")
        if field_name == "gpu_python" and val.strip() and not pathlib.Path(val.strip()).exists():
            return errors.error("that Python does not exist", 400, hint="Check the path of the interpreter in Settings.")
        if field_name in ("compute_gpu", "ui_gpu"):
            val = val.strip().lower()
            if val not in ("", "off"):
                return errors.error("Hardware acceleration is on (empty) or off", 400, hint="Choose on or off.")
        if field_name == "compute_remember":
            from app.backend.pipeline import compute_choice
            try:
                val = compute_choice.normalise_remembered(val)
            except ValueError as e:
                return errors.error(str(e), 400, hint="Choose again in the Compute dialog, or forget the choice in Settings.")
        if field_name == "max_jobs":
            val = val.strip()
            if val and not (val.isdigit() and 1 <= int(val) <= 16):
                return errors.error("Jobs at once must be a number from 1 to 16", 400, hint="Leave it empty for the default of 4.")
        if field_name == "notify_email":
            val = val.strip().lower()
            if val not in ("", "failed", "all"):
                return errors.error("A letter about a finished job is never, only when it fails, or for every job", 400, hint="Choose one of the three.")
        if field_name == "confirm_quit":
            val = val.strip().lower()
            if val not in ("", "off"):
                return errors.error("Asking before quitting is on (empty) or off", 400, hint="Choose on or off.")
        if field_name == "desktop_notify":
            val = val.strip().lower()
            if val not in ("", "off"):
                return errors.error("Desktop notifications are on (empty) or off", 400, hint="Choose on or off.")
        if field_name == "interim_dir" and val.strip():
            from app.backend import paths
            problems = paths.describe(val)["problems"]
            if problems:
                return errors.error("; ".join(problems), 400)
            val = val.strip()
        if field_name == "database_dir" and val.strip():
            from app.backend import paths
            problems = paths.describe_database(val)["problems"]
            if problems:
                return errors.error("; ".join(problems), 400)
            val = val.strip()
        s.set_field(field_name, val)
    if s.llm_local_adapter:
        from app.backend.pipeline import gguf_convert
        found = next((a for a in gguf_convert.adapters() if _same_file(a["path"], s.llm_local_adapter)), None)
        if found is None:
            return errors.error("That file is not one of the adapters converted here.", 404, hint="Convert a training run's adapter to GGUF on the Model Hub page first.")
        if s.llm_local_model:
            ok, note = gguf_convert.compatible(s.llm_local_model, found)
            if not ok:
                return errors.error(note, 400, hint="Choose the model this adapter was trained on, or another adapter.")
    if update.database_dir is not None or update.interim_dir is not None:
        from app.backend import paths
        os.environ.setdefault("ZTSA_DATABASE_DIR", str(paths.database_dir()))
    s.save()
    s.apply_env()
    if update.agent_sandbox_network is not None and s.agent_sandbox_network.strip().lower() != "proxy":
        from app.backend.pipeline.scan import agent_sandbox
        agent_sandbox.revoke_network()                       # switching the network off takes effect now, not at the next session
    return s.public()


def _same_file(a: str, b: str) -> bool:
    return str(pathlib.Path(a).resolve()) == str(pathlib.Path(b).resolve())


@router.get("/api/compute/choices")
def compute_choices(stage: str, run: str = "", model: str = ""):
    """Where a heavy job could run: the processor, the graphics card, and both, each with whether it is available now (and the one-line reason when it is not), what it means for
    this job, and its estimates (memory from the model's own size; time only as measured on earlier runs, never predicted), and the choice remembered for this job, if any."""
    from app.backend.pipeline import compute_choice
    if not compute_choice.is_heavy(stage):
        return errors.error(f"{stage} has no choice of device.", 404)
    s = Settings.load()
    out = compute_choice.choices(stage, {"run": run or None, "model": model or None})
    return {**out, "remembered": compute_choice.remembered(s.compute_remember).get(stage)}


@router.get("/api/compute")
def compute_info():
    """The cores, the memory, the CPU worker count that would be used and what the GPU interpreter reports (a probe of a few seconds,
    cached 30 s, so this is a slow route)."""
    from app.backend import compute
    return {**compute.machine_info(), "gpu": compute.gpu_info()}


@router.get("/api/compute/pythons")
def compute_pythons(refresh: bool = False):
    """The Python interpreters that could drive the local GPU jobs (the virtual environments next to the code, the one the app runs in and the one chosen in Settings), each probed for
    torch and CUDA, the ones that can drive the card first; and the one "Automatic" means. A slow route (importing torch takes seconds), kept for a minute."""
    from app.backend import compute
    return {"pythons": compute.python_choices(refresh=refresh), "automatic": compute.default_gpu_python(), "chosen": os.environ.get(compute.ENV_GPU_PYTHON, "").strip()}


@router.get("/api/settings/storage")
def settings_storage(path: str = ""):
    """Facts about a database folder before it is saved: exists, writable, free space, already holds a store."""
    from app.backend import paths
    return paths.describe(path or None)


@router.get("/api/settings/database-storage")
def settings_database_storage(path: str = ""):
    from app.backend import paths
    return paths.describe_database(path or None)


class TestResult(BaseModel):
    ok: bool
    detail: str


class DiscoverReq(BaseModel):
    base_url: str | None = None
    api_key: str | None = None


class TestReq(SettingsUpdate):
    # Optional probe input: an LLM prompt, a CVE id, or an SMTP
    # recipient. When present the test returns the actual response.
    input: str | None = None


def _merge_settings(s: Settings, b: "SettingsUpdate | None") -> Settings:
    """Overlay the values the UI currently has in the form (possibly unsaved) onto
    the loaded settings, so test/discover act on what the user sees."""
    if b is None:
        return s
    from app.backend.config import SECRET_FIELDS
    for field_name in ENV_MAP:
        val = getattr(b, field_name, None)
        if val is None or (field_name in SECRET_FIELDS and not val):
            continue                     # a blank secret in the form = the saved one
        if field_name == "llm_base_url":
            val = normalize_base_url(val)
        setattr(s, field_name, val)
    return s


@router.get("/api/settings/migrations")
def settings_migrations():
    """What a newer version of the app changed in the saved settings and the person has not yet acknowledged: each with the sentences that say what was found and what was done."""
    from app.backend import settings_migrations as sm
    return {"migrations": sm.pending()}


class AcknowledgeIn(BaseModel):
    ids: list[str] | None = None


@router.post("/api/settings/migrations/acknowledge")
def acknowledge_settings_migrations(body: AcknowledgeIn):
    """Mark the changes as read (the ones named, or all of them)."""
    from app.backend import settings_migrations as sm
    return {"acknowledged": sm.acknowledge(body.ids)}


@router.post("/api/settings/test/{target}")
def test_connection(target: str, b: TestReq | None = None):
    """Check an endpoint/credential using the submitted (possibly unsaved) form
    values, falling back to saved values. When ``input`` is supplied the probe
    returns the actual response (LLM reply, NVD record, sent
    email). Never raises; reports {ok, detail, response?}."""
    s = _merge_settings(Settings.load(), b)
    probe = (b.input.strip() if (b and b.input) else None)
    try:
        if target == "llm":
            if _llm_engine(s) == "local":
                return _test_local_llm(s, probe)
            return _test_openai(s.llm_base_url, s.llm_api_key, s.llm_model, probe)
        if target == "nvd":
            return _test_nvd(s.nvd_api_key, probe)
        if target == "github":
            return _test_github(s.github_token, probe)
        if target == "huggingface":
            return _test_huggingface(s.hf_token, probe)
        if target == "smtp":
            return _test_smtp(s, probe)
    except Exception as e:  # pragma: no cover - defensive
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    return errors.error(f"unknown target: {target}", 400)


@router.post("/api/settings/models")
def discover_models(b: DiscoverReq):
    """List models from an OpenAI-compatible endpoint (for the Discover button).

    Uses the submitted base URL/key, falling back to the saved ones. Returns
    {ok, models, detail}; never raises.
    """
    s = Settings.load()
    base = b.base_url or s.llm_base_url
    key = (b.api_key or None) or s.llm_api_key
    base = normalize_base_url(base)
    if not base:
        return {"ok": False, "models": [], "detail": "No base URL configured."}
    try:
        client = llm_client.make(base, key, timeout=20)
        ids = sorted({m.id for m in client.models.list()})
        return {"ok": True, "models": ids,
                "detail": f"{len(ids)} model(s) found." if ids else "No models listed."}
    except ImportError:
        return {"ok": False, "models": [], "detail": "openai package not installed."}
    except Exception as e:
        return {"ok": False, "models": [], "detail": f"{type(e).__name__}: {e}"}


def _llm_engine(s: Settings) -> str:
    """The language-model engine the form describes: the one chosen, else external when an endpoint is saved and local when nothing is (the rule of ``llm_client.engine``)."""
    chosen = (s.llm_engine or "").strip().lower()
    if chosen in ("local", "external"):
        return chosen
    return "external" if s.llm_base_url else "local"


def _test_local_llm(s: Settings, prompt: str | None = None) -> dict:
    """Check the local selection or ask Ollama to answer a test prompt."""
    from app.backend.pipeline.gateway import client as gateway
    from app.backend.pipeline.ollama.models import saved_chat_name
    path = (s.llm_local_model or "").strip()
    saved = saved_chat_name(s)
    if not path and not saved:
        return {"ok": False, "detail": "No local model is chosen. Choose one of the models on this computer first."}
    if path and not pathlib.Path(path).is_file():
        return {"ok": False, "detail": "The chosen model file is not on this computer any more. Choose another one."}
    name = pathlib.Path(path).name if path else saved
    if not prompt:
        if not gateway.healthy():
            from app.backend.pipeline.ollama.server import startup_error
            return {"ok": False, "detail": startup_error() or "Ollama is not running. Check the Serving page and try again."}
        return {"ok": True, "detail": f"Ollama is running and loads {name} when it is first asked."}
    model = gateway.inference_model("llm", settings=s)
    with llm_client.local_client() as client:
        r = client.chat.completions.create(model=model, temperature=0, max_tokens=300, messages=[{"role": "user", "content": prompt}])
    reply = (r.choices[0].message.content or "").strip()
    return {"ok": True, "detail": f"{name} replied.", "response": reply}


def _test_openai(base_url: str, api_key: str, model: str, prompt: str | None = None) -> dict:
    base_url = normalize_base_url(base_url)
    if not base_url:
        return {"ok": False, "detail": "No base URL configured."}
    try:
        client = llm_client.make(base_url, api_key, timeout=60)
    except ImportError:
        return {"ok": False, "detail": "openai package not installed."}
    if prompt:
        if not model:
            return {"ok": False, "detail": "Select or enter a model first."}
        r = client.chat.completions.create(
            model=model, temperature=0, max_tokens=300,
            messages=[{"role": "user", "content": prompt}])
        reply = (r.choices[0].message.content or "").strip()
        return {"ok": True, "detail": f"Model '{model}' replied.", "response": reply}
    models = list(client.models.list())
    names = [m.id for m in models]
    if model and names and model not in names:
        return {"ok": True, "detail": f"Reachable ({len(names)} models), but '{model}' not listed."}
    return {"ok": True, "detail": f"Reachable. {len(names)} model(s) available."}


def _test_nvd(api_key: str, cve: str | None = None) -> dict:
    import requests
    headers = {"apiKey": api_key} if api_key else {}
    params = {"cveId": cve} if cve else {"resultsPerPage": 1}
    r = requests.get("https://services.nvd.nist.gov/rest/json/cves/2.0",
                     params=params, headers=headers, timeout=30)
    if r.status_code != 200:
        return {"ok": False, "detail": f"NVD returned HTTP {r.status_code}."}
    if cve:
        vulns = r.json().get("vulnerabilities", [])
        if not vulns:
            return {"ok": True, "detail": f"NVD reachable, but {cve} was not found."}
        c = vulns[0]["cve"]
        desc = next((d["value"] for d in c.get("descriptions", []) if d.get("lang") == "en"), "")
        return {"ok": True, "detail": f"Found {c.get('id')} ({c.get('vulnStatus', '')}).",
                "response": (desc[:400] + "…") if len(desc) > 400 else desc}
    suffix = "with key" if api_key else "without key (rate-limited)"
    return {"ok": True, "detail": f"NVD reachable {suffix}."}


def _test_github(token: str, repo: str | None = None) -> dict:
    """Rate-limit status for the token; with a probe input ("owner/repo") also checks
    that the repository's commits are readable (what the code miner needs)."""
    import requests
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    r = requests.get("https://api.github.com/rate_limit", headers=h, timeout=30)
    if r.status_code != 200:
        return {"ok": False, "detail": f"GitHub returned HTTP {r.status_code}."}
    core = r.json().get("resources", {}).get("core", {})
    detail = (f"{'Token' if token else 'Anonymous'} access: {core.get('remaining')}/"
              f"{core.get('limit')} API requests left this hour.")
    if repo:
        c = requests.get(f"https://api.github.com/repos/{repo.strip()}/commits",
                         params={"per_page": 1}, headers=h, timeout=30)
        if c.status_code != 200:
            return {"ok": False, "detail": f"{detail} Repository {repo}: HTTP {c.status_code}."}
        head = c.json()[0]
        return {"ok": True, "detail": detail,
                "response": f"{repo} latest commit {head['sha'][:10]}: "
                            f"{head['commit']['message'].splitlines()[0][:120]}"}
    if not token:
        detail += " Add a token to raise the limit to 5,000/hour for code mining."
    return {"ok": True, "detail": detail}


def _test_huggingface(token: str, model: str | None = None) -> dict:
    """Who the token belongs to; with a probe input ("org/model") also whether that model can be read with it (a gated model needs access granted on the site)."""
    import requests
    if not token:
        return {"ok": False, "detail": "No token is set. Public models can be downloaded without one; gated models need one."}
    h = {"Authorization": f"Bearer {token}"}
    r = requests.get("https://huggingface.co/api/whoami-v2", headers=h, timeout=30)
    if r.status_code == 401:
        return {"ok": False, "detail": "Hugging Face refused the token (it may be wrong, expired or revoked)."}
    if r.status_code != 200:
        return {"ok": False, "detail": f"Hugging Face returned HTTP {r.status_code}."}
    who = r.json().get("name") or "this account"
    detail = f"The token belongs to {who}."
    if model:
        m = requests.get(f"https://huggingface.co/api/models/{model.strip()}", headers=h, timeout=30)
        if m.status_code in (401, 403):
            return {"ok": False, "detail": f"{detail} It cannot read {model.strip()}: the model is gated, so access has to be granted on its page."}
        if m.status_code == 404:
            return {"ok": False, "detail": f"{detail} There is no model called {model.strip()}."}
        if m.status_code != 200:
            return {"ok": False, "detail": f"{detail} {model.strip()}: HTTP {m.status_code}."}
        return {"ok": True, "detail": detail, "response": f"{model.strip()} can be read with this token."}
    return {"ok": True, "detail": detail}


def _test_smtp(s: Settings, to: str | None = None) -> dict:
    from email.message import EmailMessage
    if not s.smtp_host:
        return {"ok": False, "detail": "No SMTP host configured."}
    from app.backend.pipeline import notify
    try:
        srv = notify.connect(s.smtp_host, int(s.smtp_port or 587), s.smtp_user, s.smtp_password)
    except notify.SmtpRefused as e:
        return {"ok": False, "detail": str(e)}
    with srv:
        recipient = to or s.smtp_to
        if recipient:
            msg = EmailMessage()
            msg["Subject"] = "AI Vulnerability Framework — SMTP test"
            msg["From"] = s.smtp_from or s.smtp_user or "noreply@localhost"
            msg["To"] = recipient
            msg.set_content("This is a test email confirming your SMTP settings work.")
            srv.send_message(msg)
            return {"ok": True, "detail": f"Test email sent to {recipient}.",
                    "response": f"Delivered to {recipient} via {s.smtp_host}."}
        authed = " and authenticated" if (s.smtp_user and s.smtp_password) else ""
        return {"ok": True, "detail": f"SMTP connected{authed} (no recipient to send to)."}
