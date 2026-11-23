"""Skills (agentic mode, Part E): list, create, approve and remove the named blocks of system-prompt text /skill applies. See pipeline/skills.py."""
from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter
from pydantic import BaseModel

from app.backend import errors
from app.backend.pipeline import skills

router = APIRouter()


def _fail(e: skills.SkillError):
    return errors.error(str(e), e.status, code=e.code)


class SkillIn(BaseModel):
    name: str
    description: str
    prompt: str


@router.get("/api/skills")
def skills_list():
    bundled = []
    directory = Path(__file__).parent / "pipeline" / "project_ide" / "skills"
    for path in sorted(directory.glob("*/SKILL.md")):
        try:
            document = path.read_text(encoding="utf-8")
            header, separator, _ = document.removeprefix("---\n").partition("\n---\n")
            manifest = yaml.safe_load(header) if document.startswith("---\n") and separator else None
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if isinstance(manifest, dict) and isinstance(manifest.get("description"), str):
            bundled.append({"name": path.parent.name, "description": manifest["description"]})
    return {"skills": skills.list_skills(), "bundled": bundled}


@router.post("/api/skills")
def skills_create(body: SkillIn):
    try:
        return skills.create(body.name, body.description, body.prompt)
    except skills.SkillError as e:
        return _fail(e)


@router.get("/api/skills/{name}")
def skills_get(name: str):
    """The skill's manifest and its prompt text, for the management page's preview and for ``/skill`` to apply."""
    try:
        return skills.get(name)
    except skills.SkillError as e:
        return _fail(e)


@router.post("/api/skills/{name}/approve")
def skills_approve(name: str):
    try:
        return skills.approve(name)
    except skills.SkillError as e:
        return _fail(e)


@router.delete("/api/skills/{name}")
def skills_delete(name: str):
    try:
        skills.delete(name)
        return {"ok": True}
    except skills.SkillError as e:
        return _fail(e)
