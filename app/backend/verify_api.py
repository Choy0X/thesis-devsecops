"""Routes that show what verification has found: the saved receipts and the pilot's summary. Read-only; running a verification starts Docker work and is a
separate, asked-for step, so no route here starts one.
"""
from __future__ import annotations

import json
import pathlib
import re

from fastapi import APIRouter

from app.backend import errors
from app.backend.api_common import EXP

router = APIRouter()
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PILOT = "verify_pilot"


def _pilot() -> pathlib.Path:
    return EXP / "verify_pilot"


def _read(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _folders() -> list[pathlib.Path]:
    """The folders that hold receipts: the saved pilot first, then every single-case run after it (verify_case, verify_case_<time>)."""
    cases = sorted(p for p in EXP.glob("verify_case*") if p.is_dir()) if EXP.is_dir() else []
    return [_pilot(), *cases]


def _handle(folder: pathlib.Path, stem: str) -> str:
    """The pilot's receipts keep their plain names; a receipt of another folder is named ``folder~stem``."""
    return stem if folder.name == PILOT else f"{folder.name}~{stem}"


@router.get("/api/verify/receipts")
def verify_receipts():
    """The receipts of the pilot and of the cases verified again, each with its verdict, grade, runner and whether it is sound."""
    from app.backend.pipeline.verify import receipts
    out = []
    for folder in _folders():
        d = folder / "receipts"
        for f in sorted(d.glob("*.json")) if d.is_dir() else []:
            r = _read(f)
            name = _handle(folder, f.stem)
            if not isinstance(r, dict):
                out.append({"name": name, "readable": False})
                continue
            runner = r.get("runner") or {}
            out.append({"name": name, "readable": True, "run": folder.name, "case_id": r.get("case_id"), "verdict": r.get("verdict"), "grade": r.get("grade"),
                        "runner": runner.get("kind"), "fixture_only": runner.get("fixture_only") is True, "sound": not receipts.validate(r)})
    return {"receipts": out}


@router.get("/api/verify/receipts/{name}")
def verify_receipt(name: str):
    """One receipt in full, with the problems ``validate`` finds in it (an edited receipt is reported, not hidden)."""
    from app.backend.pipeline.verify import receipts
    folder, _, stem = name.rpartition("~")
    folder = folder or PILOT
    if not _NAME.match(stem) or not _NAME.match(folder) or not folder.startswith("verify_"):
        return errors.error("bad receipt name", 400)
    r = _read(EXP / folder / "receipts" / f"{stem}.json")
    if not isinstance(r, dict):
        return errors.error("no such receipt", 404)
    return {"name": name, "receipt": r, "problems": receipts.validate(r)}


@router.get("/api/verify/cases")
def verify_cases():
    """The cases the saved pilot attempted, with how each ended: what a case can be verified again from."""
    from app.backend.pipeline.verify import pilot
    return {"cases": [{"case_id": c["case_id"], "library": c["library"], "outcome": c["outcome"], "evidence_level": c["evidence_level"]} for c in pilot.known_cases(_pilot())]}


@router.get("/api/verify/pilot")
def verify_pilot():
    """The pilot's own summary (cases, outcomes, libraries, time) as it was written when it ran; nothing is added to it here."""
    summary = _read(_pilot() / "summary.json")
    if not isinstance(summary, dict):
        return {"ran": False, "summary": None}
    return {"ran": True, "summary": summary}
