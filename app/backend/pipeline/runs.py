"""Experiment run folders: never overwrite a result, always record provenance.

A result already cited must never be overwritten, so a run whose tag
already has a folder with content gets a timestamped tag instead. Every run
writes ``run_config.json``; ``write_config`` adds the timestamp and
the code version (git commit) to whatever the stage records.
"""
from __future__ import annotations
import datetime
import json
import pathlib
import re
import subprocess

EXP = pathlib.Path("experiments")


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "")).strip("-")[:60] or "run"


def new_run(tag: str, outdir: str | pathlib.Path = EXP) -> tuple[str, pathlib.Path]:
    """(tag, folder) for a new run; an existing non-empty folder is never reused."""
    tag = slug(tag)
    d = pathlib.Path(outdir) / tag
    if d.exists() and any(d.iterdir()):
        tag = f"{tag}_{datetime.datetime.now():%Y%m%d-%H%M%S}"
        d = pathlib.Path(outdir) / tag
    d.mkdir(parents=True, exist_ok=True)
    return tag, d


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:  # ignored: git may be missing; the commit is recorded as unknown
        return None


def retrieval_fields(cfg: dict) -> dict:
    """What a run records about retrieval, from the retrieval record of the stage that did (or did not) use it: whether it was on, and when it was, the policy, the embedder and the
    knowledge-base version that answered. A bare ``rag_enabled`` in a stage's own settings is not a record and is ignored; a retrieval that was on without its embedder or its
    knowledge-base version says so (``retrieval_unverified``) instead of passing as a complete record."""
    r = cfg.get("retrieval")
    if not isinstance(r, dict) or "enabled" not in r:
        return {}
    if not r.get("enabled"):
        return {"rag_enabled": False}
    out = {"rag_enabled": True, "retrieval_policy": {"k": r.get("k"), "search": r.get("search"), "source": "the training split of the frozen split manifest"},
           "embedder": r.get("embedder"), "kb_version": r.get("kb_version")}
    missing = [name for name in ("embedder", "kb_version") if not r.get(name)]
    if missing:
        out["retrieval_unverified"] = f"retrieval was on but the run did not record: {', '.join(missing)}"
    return out


def write_config(folder: pathlib.Path, cfg: dict) -> None:
    """run_config.json with the stage's settings plus timestamp and code version.

    The config fields (input-policy hash, context policy, supplied-context policy) come from
    the project config; a stage cannot override them. Whether retrieval was on, and with
    which policy, embedder and knowledge base, comes from the stage's own retrieval record
    (``retrieval_fields``), never from a bare claim. If the config cannot be read, that is
    recorded instead of hidden."""
    try:
        from app.backend import project_config
        manifest = project_config.run_fields()
    except Exception as e:
        manifest = {"config_error": f"{type(e).__name__}: {e}"}
    from app.backend import compute
    full = {"timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "code_version": _git_commit(), "hardware_acceleration": {"compute": compute.gpu_enabled()}, **cfg, **manifest, **retrieval_fields(cfg)}   # whether the graphics card was allowed for this run
    from app.backend.pipeline import fsio
    fsio.write_json(pathlib.Path(folder) / "run_config.json", full, indent=2, default=str)
