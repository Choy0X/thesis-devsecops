"""Describe models registered with Ollama. File caches are not runnable inventory."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import hashlib
import re

from . import client


def normalized_tag(tag: str) -> str:
    return tag.removesuffix(":latest")


def cloud_tag(tag: str) -> bool:
    suffix = tag.rsplit("/", 1)[-1].partition(":")[2].lower()
    return suffix == "cloud" or suffix.endswith("-cloud")


def runtime_tag(tag: str, digest: str, configured_context: int | None, context: int) -> str:
    if configured_context == context:
        return tag
    key = hashlib.sha256(f"{tag}\0{digest}\0{context}".encode()).hexdigest()[:24]
    return f"aivf/runtime-{key}"


def _name(tag: str, general_name: object) -> str:
    if normalized_tag(tag) == "aivf/reviewer-v40":
        return "AIVF Reviewer v40"
    family, _, version = tag.rsplit("/", 1)[-1].partition(":")
    if family == "qwen2.5-coder":
        version = version.removesuffix("-instruct")
        return "Qwen2.5 Coder" + (f" {version.upper()}" if version and version != "latest" else "")
    if isinstance(general_name, str) and general_name.strip():
        return general_name.strip()
    return family.replace("-", " ").replace("_", " ").title() + (
        f" {version}" if version and version != "latest" else "")


def _details(tag: str) -> dict:
    data = client.show(tag, timeout=3.0)
    info = data.get("model_info")
    info = info if isinstance(info, dict) else {}
    capabilities = data.get("capabilities")
    width = info.get(f"{info.get('general.architecture')}.embedding_length")
    context = re.search(r"(?m)^num_ctx\s+(\d+)\s*$", str(data.get("parameters") or ""))
    return {
        "name": _name(tag, info.get("general.name")),
        "capabilities": sorted({cap for cap in capabilities if isinstance(cap, str)}) if isinstance(capabilities, list) else [],
        "remote": bool(data.get("remote_host") or data.get("remote_model")),
        "configured_context": int(context[1]) if context else None,
        "embedding_length": width if type(width) is int and width > 0 else None,
    }


@lru_cache(maxsize=256)
def _cached_details(host: str, tag: str, digest: str) -> dict:
    return _details(tag)


def _describe(row: dict) -> dict:
    tag = row["name"]
    digest = row["digest"]
    remote_hint = bool(row.get("remote_host") or row.get("remote_model") or cloud_tag(tag))
    try:
        details = _cached_details(client.base_url(), tag, digest) if digest else _details(tag)
        available = bool(details["capabilities"] and digest)
    except client.OllamaError:
        details = {"name": _name(tag, None), "capabilities": [], "remote": False,
                   "configured_context": None, "embedding_length": None}
        available = False
    return {"tag": tag, "name": details["name"], "digest": digest, "size": row["size"],
            "capabilities": details["capabilities"], "available": available,
            "location": "cloud" if remote_hint or details["remote"] or not client.local_endpoint() else "local",
            "configured_context": details["configured_context"],
            "embedding_length": details.get("embedding_length")}


def local_embedding(tag: str) -> dict | None:
    """Find an exact installed embedding model without loading it or contacting a remote Ollama."""
    if not tag or not client.local_endpoint() or cloud_tag(tag):
        return None
    rows = client.tags(timeout=2.0, include_metadata=True)
    for row in rows:
        if normalized_tag(row["name"]) != normalized_tag(tag):
            continue
        model = _describe(row)
        if model["available"] and model["location"] == "local" and "embedding" in model["capabilities"]:
            return model
    return None


def installed() -> list[dict]:
    candidates = [row for row in client.tags(timeout=2.0, include_metadata=True)
                  if isinstance(row["name"], str) and row["name"] and not row["name"].startswith(("aivf/import-", "aivf/runtime-"))]
    if not candidates:
        return []
    with ThreadPoolExecutor(max_workers=min(len(candidates), 6)) as workers:
        return list(workers.map(_describe, candidates))
