"""The run profiles: named sets of overrides with the reason for each. No torch."""
from __future__ import annotations

import pathlib

import yaml

from training.finetune import config as cfgmod, overrides as ov

PATH = pathlib.Path(__file__).resolve().parents[1] / "configs" / "profiles.yaml"


def load(path=PATH, contract_path=cfgmod.DEFAULT_PATH) -> dict[str, dict]:
    """Every profile with its label, purpose, rationale and overrides. Each profile's overrides are checked against the contract (they must name settable
    keys and pass the config models), so a profile that no longer fits the contract fails here and not in the middle of a run."""
    doc = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
    base = yaml.safe_load(pathlib.Path(contract_path).read_text(encoding="utf-8"))
    out = {}
    for name, p in (doc.get("profiles") or {}).items():
        for field in ("label", "purpose", "rationale"):
            if not str(p.get(field) or "").strip():
                raise ValueError(f"profile {name} has no {field}")
        ov.apply(base, p.get("overrides") or {})
        out[name] = {"label": p["label"], "purpose": p["purpose"], "rationale": " ".join(str(p["rationale"]).split()), "overrides": dict(p.get("overrides") or {})}
    return out
