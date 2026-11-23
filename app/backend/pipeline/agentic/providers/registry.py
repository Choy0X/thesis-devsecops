"""Named providers: the model servers the owner has added, each with its address, its key and the models it serves, kept in ``providers.json`` next to the settings (gitignored, owner-only: the
file holds keys, as ``settings.json`` does).

The settings knew one model server, chosen with the engine switch: the one on this computer, or one external address. A session can now name any provider's model (``provider/model``) and the
loop is handed a client for it and what is known of the model (its profile). The model the settings name stays the default: a session that names none uses it, as before.

A model need not be listed to be used: the entries of a provider's ``models`` hold only what the owner wants to say about one (its context, how much it may write, which protocol), and a model id
that is not there is a model with nothing said. A key is never returned (only that one is set and its last four characters), and an update that names no key keeps the one that is there.
"""
from __future__ import annotations

import ipaddress
import json
import pathlib
import re
import urllib.parse
from dataclasses import dataclass

from app.backend import config
from app.backend.pipeline import fsio, llm_client
from app.backend.pipeline.agentic.profiles import derive, probe, store
from app.backend.pipeline.agentic.profiles.model_profile import ModelProfile, SUITE_VERSION, fingerprint

RESERVED = ("local", "external", "default")
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MAX_PROVIDERS = 40
MAX_MODELS = 200
KINDS = ("openai_compat",)
PROTOCOL_CHOICES = ("native", "schema", "text")


class ProviderError(ValueError):
    """What is wrong with a provider, in a sentence, with the status a route answers with."""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.status, self.code = status, code


def path() -> pathlib.Path:
    return config.SETTINGS_PATH.with_name("providers.json")


def _read() -> list[dict]:
    try:
        doc = json.loads(path().read_text(encoding="utf-8"))
    except OSError:
        return []
    except ValueError:
        raise ProviderError("providers.json is damaged and was left as it is; fix or delete it.", 422, "damaged") from None
    items = doc.get("providers") if isinstance(doc, dict) else None
    return [p for p in items if isinstance(p, dict) and isinstance(p.get("id"), str)] if isinstance(items, list) else []


def _write(providers: list[dict]) -> None:
    fsio.write_json(path(), {"providers": providers}, indent=2, mode=0o600)


def _models(raw) -> dict[str, dict]:
    """The notes an owner made about models, kept to what is understood: a context and a longest reply in tokens, and a protocol."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for model_id, entry in list(raw.items())[:MAX_MODELS]:
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 200:
            continue
        e = entry if isinstance(entry, dict) else {}
        clean: dict = {}
        if isinstance(e.get("name"), str) and e["name"].strip():
            clean["name"] = e["name"].strip()[:100]
        for key in ("context", "max_output"):
            v = e.get(key)
            if isinstance(v, int) and not isinstance(v, bool) and 256 <= v <= 10_000_000:
                clean[key] = v
        if e.get("protocol") in PROTOCOL_CHOICES:
            clean["protocol"] = e["protocol"]
        out[model_id.strip()] = clean
    return out


def validate(body: dict, existing: dict | None = None) -> dict:
    """A provider as it is kept, from what was sent. ``api_key`` absent or None keeps the one ``existing`` has; a string sets it (an empty one clears it)."""
    pid = str(body.get("id") or "").strip().lower()
    if not ID.match(pid):
        raise ProviderError("The id is 1 to 32 characters: lowercase letters, digits and hyphens, starting with a letter or digit.")
    if pid in RESERVED:
        raise ProviderError(f"{pid!r} is the name of a built-in choice; use another id.")
    kind = body.get("kind") or "openai_compat"
    if kind not in KINDS:
        raise ProviderError(f"The kind must be one of {', '.join(KINDS)}.")
    base = config.normalize_base_url(str(body.get("base_url") or "").strip())
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ProviderError("The address must start with http:// or https:// and name a host.")
    name = str(body.get("name") or "").strip()[:80] or pid
    key = body.get("api_key")
    api_key = (existing or {}).get("api_key", "") if key is None else str(key).strip()
    headers = body.get("headers") if body.get("headers") is not None else (existing or {}).get("headers", {})
    if not isinstance(headers, dict) or len(headers) > 10 or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ProviderError("headers must be up to 10 names with text values.")
    return {"id": pid, "name": name, "kind": kind, "base_url": base, "api_key": api_key, "headers": dict(headers), "models": _models(body.get("models"))}


def trust_of(host: str) -> str:
    """Where a host is: ``local`` (this computer), ``lan`` (another computer on a private network, which still needs the owner's yes) or ``remote``."""
    h = (host or "").strip("[]").lower()
    if h in ("localhost", "::1") or h.startswith("127."):
        return "local"
    try:
        ip = ipaddress.ip_address(h)
        return "lan" if (ip.is_private or ip.is_link_local) else "remote"
    except ValueError:
        return "lan" if ("." not in h or h.endswith((".local", ".lan", ".internal"))) else "remote"


def public(p: dict) -> dict:
    """A provider as the page sees it: no key and no header value."""
    key = p.get("api_key") or ""
    return {"id": p["id"], "name": p.get("name") or p["id"], "kind": p.get("kind", "openai_compat"), "base_url": p.get("base_url", ""), "api_key_set": bool(key),
            "api_key_hint": key[-4:] if len(key) > 8 else "", "headers": sorted((p.get("headers") or {}).keys()), "trust": trust_of(urllib.parse.urlparse(p.get("base_url", "")).hostname or ""),
            "models": [{"id": k, **v} for k, v in (p.get("models") or {}).items()]}


def listing() -> list[dict]:
    return [public(p) for p in _read()]


def get(pid: str) -> dict:
    for p in _read():
        if p["id"] == pid:
            return p
    raise ProviderError(f"There is no provider {pid!r}.", 404, "not_found")


def upsert(body: dict) -> dict:
    pid = str(body.get("id") or "").strip().lower()
    items = _read()
    existing = next((p for p in items if p["id"] == pid), None)
    clean = validate(body, existing)
    if existing is None and len(items) >= MAX_PROVIDERS:
        raise ProviderError(f"At most {MAX_PROVIDERS} providers can be kept.")
    _write([clean if p["id"] == pid else p for p in items] if existing else [*items, clean])
    return public(clean)


def remove(pid: str) -> None:
    items = _read()
    if not any(p["id"] == pid for p in items):
        raise ProviderError(f"There is no provider {pid!r}.", 404, "not_found")
    _write([p for p in items if p["id"] != pid])


# -- a model of a provider, in hand -------------------------------------------------------------------------------------------------------------------------------------

@dataclass
class Handle:
    ref: str
    provider_id: str
    provider_name: str
    model: str
    client: object
    base_url: str
    host: str
    trust: str
    label: str
    entry: dict


def split_ref(ref: str) -> tuple[str, str]:
    provider_id, _, model = str(ref or "").partition("/")
    if not provider_id or not model:
        raise ProviderError("A model is named provider/model, for example myserver/qwen3-8b.")
    return provider_id, model


def resolve(ref: str, *, timeout: float = 600.0) -> Handle:
    """The client and the facts of a provider's model, from ``provider/model``."""
    provider_id, model = split_ref(ref)
    p = get(provider_id)
    host = urllib.parse.urlparse(p["base_url"]).hostname or ""
    client = llm_client.make(p["base_url"], p.get("api_key") or None, timeout=timeout, headers=p.get("headers") or None)
    entry = (p.get("models") or {}).get(model) or {}
    return Handle(ref=f"{provider_id}/{model}", provider_id=provider_id, provider_name=p.get("name") or provider_id, model=model, client=client, base_url=p["base_url"], host=host,
                  trust=trust_of(host), label=entry.get("name") or model, entry=entry)


def profile_key(h: Handle) -> str:
    return fingerprint(provider=h.provider_id, base_url=h.base_url, model=h.model, suite=SUITE_VERSION)


def profile_for(h: Handle) -> ModelProfile:
    """What is known of a provider's model: the profile that a live test made and kept, else one made from what the owner said (and nothing else: a hosted model's template is not ours to
    read). A model on this computer starts on the schema, any other on native tools, until a test says otherwise."""
    fp = profile_key(h)
    saved = store.load(fp)
    if saved is not None:
        return saved
    p = derive.derive(h.ref, None, context=h.entry.get("context"), local=h.trust == "local", fingerprint=fp)
    p.notes = [n for n in p.notes if "no chat template" not in n]                  # a remote model has no template for this app to read; saying so on every card would be noise
    if h.entry.get("max_output"):
        p.max_output = h.entry["max_output"]
    if h.entry.get("protocol") in PROTOCOL_CHOICES:
        p.protocol, p.source = h.entry["protocol"], "manual"
    return p


def probe_model(h: Handle) -> ModelProfile:
    """Runs the live test on a provider's model now (it costs a few small requests) and keeps what it found."""
    p = profile_for(h)
    result = probe.run(h.client, h.model)
    p.probe, p.protocol, p.source = result, probe.choose(result), "probed"
    p.notes = [n for n in p.notes if not n.startswith("Live test")] + [probe.describe(result, p.protocol)]
    store.save(p)
    return p


def check_connection(h_or_id) -> dict:
    """Whether the provider answers: the models it lists, or the reason it does not."""
    p = get(h_or_id) if isinstance(h_or_id, str) else h_or_id
    client = llm_client.make(p["base_url"], p.get("api_key") or None, timeout=15.0, max_retries=0, headers=p.get("headers") or None)
    try:
        names = sorted(m.id for m in client.models.list().data)
    except Exception as e:  # noqa: BLE001 - whatever the server says is the answer
        return {"ok": False, "error": llm_client_explain(e)}
    return {"ok": True, "models": names[:300], "count": len(names)}


def llm_client_explain(e: Exception) -> str:
    from app.backend.pipeline import project_chat
    return project_chat._explain(e)
