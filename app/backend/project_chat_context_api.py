"""Chat-oriented Sources, Plan and Subagents panels."""
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.backend import project_ide_api
from app.backend.pipeline.project_ide.chat_context import Sources, plan_for_chat, subagents_for_chat

router = APIRouter()


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["project_file", "local", "url"]
    path: str | None = Field(default=None, max_length=2000)
    url: str | None = Field(default=None, max_length=2000)


@router.get("/api/chats/{chat_id}/sources")
def sources(chat_id: str):
    return project_ide_api.invoke(lambda app: Sources(app.service.store).list(chat_id))


@router.post("/api/chats/{chat_id}/sources")
def add_source(chat_id: str, body: SourceIn):
    return project_ide_api.invoke(lambda app: Sources(app.service.store).add(chat_id, body.model_dump(exclude_none=True)))


@router.delete("/api/chats/{chat_id}/sources/{source_id}")
def remove_source(chat_id: str, source_id: str):
    return project_ide_api.invoke(lambda app: Sources(app.service.store).delete(chat_id, source_id))


@router.get("/api/chats/{chat_id}/plan")
def plan(chat_id: str):
    return project_ide_api.invoke(lambda app: plan_for_chat(chat_id, app))


@router.get("/api/chats/{chat_id}/subagents")
def subagents(chat_id: str):
    return project_ide_api.invoke(lambda app: subagents_for_chat(chat_id, app))
