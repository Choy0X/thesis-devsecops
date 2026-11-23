"""Availability of execution-based vulnerability checks."""
from __future__ import annotations


def status():
    return {"schema": "verification-runtime/1", "state": "unavailable", "code": "execution_unavailable",
            "reason": "Execution-based vulnerability checks are unavailable in this distribution.",
            "image_id": "", "runtime_id": "", "reference": "", "capability": None, "dependencies": {}}


def ensure_ready(*, progress=None, cancelled=None, prepare=True, budget=None):
    return status()


def configured_image_id():
    return None
