"""``run_config.json`` of a campaign: what it was asked to do and on what, written beside its other records so a result can be reproduced.

It is written when the campaign is started and written again (atomically, over the old one) each time it is resumed. It names the plan, the database, the machine, the resources the campaign was
admitted with, the model and its host when the model writes context (never a key), and whether a GitHub token was present (never the token).
"""
from __future__ import annotations

import os
import pathlib
import re
from urllib.parse import urlsplit

FOLDER = pathlib.Path("experiments/campaigns")
SAFE_ID = re.compile(r"^cmp-\d{8}-[0-9a-f]{4}$")


def folder(campaign_id: str) -> pathlib.Path:
    return FOLDER / campaign_id


def _host(url: str) -> str:
    """The address of an endpoint without any user name or password that was pasted into it."""
    if not url:
        return ""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}{':' + str(parts.port) if parts.port else ''}{parts.path.rstrip('/')}" if parts.hostname else url.split("@")[-1]


def write(c: dict) -> pathlib.Path:
    """Write (or rewrite) the campaign's ``run_config.json``; returns its folder."""
    from app.backend import compute
    from app.backend.pipeline import llm_client, runs
    from app.backend.pipeline.db import mongo
    d = folder(c["campaign_id"])
    d.mkdir(parents=True, exist_ok=True)
    plan = c.get("plan") or {}
    cfg = {"task": "campaign", "campaign_id": c["campaign_id"], "name": c.get("name"), "plan": plan, "ecosystems": c.get("ecosystems"), "resumes": c.get("resumes", 0),
           "database": mongo.db_name(), "machine": compute.machine_info(), "declared_resources": c.get("declared") or {}, "github_token_present": bool(os.environ.get("GITHUB_TOKEN")),
           "created_at": c.get("created_at")}
    if (plan.get("stages") or {}).get("enrich"):
        r = llm_client.resolved()
        cfg["model"] = {"id": r.model_label, "host": _host(r.base_url) if r.engine == "external" else r.host, "engine": r.engine}
        cfg.update(llm_client.engine_fields())
    from app.backend.pipeline.acceleration.staged_mining import source_fingerprint
    cfg["source_sha256"]=source_fingerprint()
    runs.write_config(d, cfg)
    import uuid
    attempt=d/"attempts"/uuid.uuid4().hex
    attempt.mkdir(parents=True)
    (attempt/"run_config.json").write_bytes((d/"run_config.json").read_bytes())
    return d


def remove(campaign_id: str) -> bool:
    """Delete the folder of a campaign of the new system (only that: the id must have the form of one of ours)."""
    import shutil
    if not SAFE_ID.match(campaign_id):
        return False
    d = folder(campaign_id)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False
