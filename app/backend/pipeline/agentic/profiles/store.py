"""Profiles kept between runs, one file per fingerprint under ``data/agent/profiles``. A profile is made once for a model (the probe takes about half a minute on a small card) and read again
for as long as the file, its template, the server's build and the version of the live test are what they were."""
from __future__ import annotations

import json
import pathlib

from app.backend.pipeline import fsio
from app.backend.pipeline.agentic.profiles.model_profile import ModelProfile

ROOT = pathlib.Path("data/agent/profiles")


def _path(fp: str) -> pathlib.Path:
    return ROOT / f"{''.join(c for c in fp if c.isalnum())}.json"


def load(fp: str) -> ModelProfile | None:
    if not fp:
        return None
    try:
        doc = json.loads(_path(fp).read_text(encoding="utf-8"))
        return ModelProfile.from_dict(doc) if isinstance(doc, dict) and doc.get("fingerprint") == fp else None
    except (OSError, ValueError, TypeError):
        return None


def save(profile: ModelProfile) -> None:
    if profile.fingerprint:
        fsio.write_json(_path(profile.fingerprint), profile.to_dict(), indent=2)


def forget(fp: str) -> bool:
    try:
        _path(fp).unlink()
        return True
    except OSError:
        return False
