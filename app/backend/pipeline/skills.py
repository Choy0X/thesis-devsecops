"""Skills (agentic mode, Part E): a short, named block of system-prompt text a person prepares once and applies to any conversation or agent session with ``/skill <name>``, instead
of retyping the same instructions every time. v1 is text only -- no new tool, no execution of anything a skill names -- a categorically smaller risk than the scan toolkit's own
executable-trust problem, so there is no hash-pinned build step here, only a lighter, scaled-down version of the same idea: tamper-evidence.

A skill starts unapproved (``approved: false``) when it is created. ``/skill`` applies a skill's prompt text silently (it is not previewed before being appended, only confirmed
after) -- the one piece of friction that keeps a person from ever having unreviewed instructions folded into a conversation without someone first having read them in the Skills
management page and clicked Approve. ``approve()`` re-reads the current ``prompt.md`` and records its hash as the one that was reviewed; ``get()`` re-checks that hash every time a
skill is read, so an edit to ``prompt.md`` made outside this module (by hand, or by another process) after approval is caught rather than silently taking effect as already-reviewed
text.

Stored under ``data/skills/<name>/`` (``skill.json`` the manifest, ``prompt.md`` the text), shared across every project the same way the scan toolkit's own rules are shared across
scans -- a skill is not a property of one project.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re

from app.backend.pipeline import fsio, util

ROOT = pathlib.Path("data/skills")                          # relative to the working folder, like the other run folders
SCHEMA = "skill/1"
MAX_NAME = 40
MAX_DESCRIPTION = 200
MAX_PROMPT = 4_000                                           # a skill is appended whole into ChatParams.system, which has this same limit (project_chats.MAX_SYSTEM)
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")


class SkillError(ValueError):
    """What is wrong with a skill, as a sentence for the person, with the HTTP status a route answers with."""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.status, self.code = status, code


class NotFound(SkillError):
    def __init__(self, message: str = "There is no such skill."):
        super().__init__(message, 404, "not_found")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dir(name: str) -> pathlib.Path:
    if not _NAME.match(name or ""):
        raise SkillError("A skill's name must be lowercase letters, digits, - or _, up to 40 characters, and start with a letter.")
    return ROOT / name


def _manifest(name: str) -> dict:
    d = _dir(name)
    path = d / "skill.json"
    if not path.is_file():
        raise NotFound()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SkillError("This skill's record is damaged.", 500, "damaged") from None


def list_skills() -> list[dict]:
    """Every skill's manifest (without its prompt text), the approved ones first, then alphabetically by name. A folder whose manifest is missing or damaged is left out, not raised."""
    out = []
    if ROOT.is_dir():
        for d in sorted(ROOT.iterdir()):
            if not d.is_dir():
                continue
            try:
                out.append(_manifest(d.name))
            except SkillError:
                continue
    out.sort(key=lambda s: (not s.get("approved"), s["name"]))
    return out


def get(name: str) -> dict:
    """The skill's manifest plus its prompt text, re-hashed against the manifest's own ``prompt_sha256`` -- refuses (409) when ``prompt.md`` no longer matches what was last created
    or approved, rather than serving stale or tampered text silently. Does not itself check ``approved``: this is also how the management page previews a skill before approving it;
    the one place that must refuse an unapproved skill is wherever a skill's text is actually applied."""
    manifest = _manifest(name)
    d = _dir(name)
    try:
        text = (d / "prompt.md").read_text(encoding="utf-8")
    except OSError:
        raise SkillError("This skill's prompt file is missing.", 500, "damaged") from None
    if _sha256(text) != manifest.get("prompt_sha256"):
        raise SkillError("This skill's prompt has changed since it was last approved; approve it again before it is used.", 409, "changed")
    return {**manifest, "prompt": text}


def create(name: str, description: str, prompt_text: str) -> dict:
    """Add a skill, starting unapproved. Refused when the name is already taken -- a skill is edited by removing and re-adding it, not overwritten in place, so an approval can
    never be silently carried over to different text."""
    d = _dir(name)
    if (d / "skill.json").is_file():
        raise SkillError(f'A skill named "{name}" already exists.', 409, "exists")
    description = str(description or "").strip()
    if not description:
        raise SkillError("Describe what this skill is for.")
    if len(description) > MAX_DESCRIPTION:
        raise SkillError(f"The description is {len(description):,} characters; the limit is {MAX_DESCRIPTION:,}.")
    prompt_text = str(prompt_text or "")
    if not prompt_text.strip():
        raise SkillError("Give the skill's prompt text.")
    if len(prompt_text) > MAX_PROMPT:
        raise SkillError(f"The prompt is {len(prompt_text):,} characters; the limit is {MAX_PROMPT:,}.")
    manifest = {"schema": SCHEMA, "name": name, "description": description, "approved": False, "created_at": util.now_zulu(), "prompt_sha256": _sha256(prompt_text)}
    fsio.write_text(d / "prompt.md", prompt_text)
    fsio.write_json(d / "skill.json", manifest, indent=2)
    return manifest


def approve(name: str) -> dict:
    """Mark a skill approved, pinning its ``prompt_sha256`` to the prompt text as it reads right now -- approving always means "what is there now is reviewed and fine", whether or
    not it has been edited since it was created."""
    d = _dir(name)
    manifest = _manifest(name)
    try:
        text = (d / "prompt.md").read_text(encoding="utf-8")
    except OSError:
        raise SkillError("This skill's prompt file is missing.", 500, "damaged") from None
    manifest = {**manifest, "approved": True, "prompt_sha256": _sha256(text)}
    fsio.write_json(d / "skill.json", manifest, indent=2)
    return manifest


def delete(name: str) -> None:
    d = _dir(name)
    if not (d / "skill.json").is_file():
        raise NotFound()
    import shutil
    shutil.rmtree(d, ignore_errors=True)
