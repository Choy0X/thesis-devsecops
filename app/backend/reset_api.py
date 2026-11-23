"""Hard reset (Settings, App and data): the list of what can be deleted with its exact sizes, and the run. The rules that make it safe are in ``pipeline/hard_reset.py``; this file only
carries the requests to it and its refusals back as the app's usual error body."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.backend import errors
from app.backend.pipeline import hard_reset

router = APIRouter()


class StartIn(BaseModel):
    token: str = Field(default="", max_length=64)
    categories: list[str] = Field(default_factory=list, max_length=20)
    confirm: str = Field(default="", max_length=20)
    backup: bool = False


@router.get("/api/reset/inventory")
def reset_inventory(refresh: bool = False):
    """The categories with their sizes. The measuring runs in the background: while ``done`` is false the page asks again (``progress`` says where it is); when it is true the answer has the
    ``token`` that ``POST /api/reset/start`` needs. ``refresh`` measures again."""
    return hard_reset.start_scan(force=refresh)


@router.post("/api/reset/start")
def reset_start(body: StartIn):
    """Delete the chosen categories, if the token is the current one, the word RESET was typed and no job is running. Runs in the background; ``GET /api/reset/status`` follows it."""
    try:
        return hard_reset.start(body.token, body.categories, body.confirm, body.backup)
    except hard_reset.ResetError as e:
        return errors.error(str(e), e.status, hint=e.hint, code=e.code)


@router.get("/api/reset/status")
def reset_status():
    """The steps of the run so far, whether it is done, how many items could not be deleted yet (they are tried again at the next start)."""
    return hard_reset.run_status()
