"""Application API for coding chats, saved workspaces, scans, and terminals."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ConfigDict

from app.backend.pipeline.project_ide import staging, workbench, workspace
from app.backend.pipeline.project_ide.application import Application
from app.backend.pipeline.project_ide.resources import Cancelled
from app.backend.pipeline.project_ide.store import StoreError, encode

router = APIRouter()
_application = None
_lock = threading.RLock()


def application():
    global _application
    with _lock:
        if _application is None:
            from app.backend import paths, lifecycle
            _application = Application(paths.INTERIM / "projects_agent_ide")
            lifecycle.register_step("Stopping project agents and terminals", close)
        return _application


def close():
    global _application
    with _lock:
        current, _application = _application, None
    if current:
        current.close()


def invoke(operation):
    try:
        return operation(application())
    except (workspace.Conflict, Cancelled) as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectIn(Input):
    name: str = Field(min_length=1, max_length=120)
    source: str = Field(min_length=1, max_length=2000)
    instructions: str = Field(default="", max_length=8000)


class ProjectUpdate(Input):
    name: str | None = Field(default=None, max_length=120)
    instructions: str | None = Field(default=None, max_length=8000)
    memory: list[str] | None = Field(default=None, max_length=100)
    legacy_project_id: str | None = Field(default=None, min_length=3, max_length=61)


class ChatIn(Input):
    title: str = Field(default="New chat", min_length=1, max_length=160)
    model: dict | None = None
    preparation_id: str | None = None


class ModelPrepareIn(Input):
    model: dict
    context: int | None = Field(default=None, ge=256, le=8192)


class ChatUpdate(Input):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    archived: bool | None = None
    pinned: bool | None = None


class ChatModelIn(Input):
    model: dict
    preparation_id: str
    expected_model_id: str | None = None


class TurnIn(Input):
    request_key: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=100000)
    mode: str = "build"


class ScanIn(Input):
    request_key: str = Field(min_length=1, max_length=128)
    scope: str
    dependency_mode: str | None = None
    selected_package_ids: list[str] = Field(default_factory=list, max_length=500)


class SaveIn(Input):
    path: str
    text: str
    version: str | None = None


class IntegrationIn(Input):
    paths: list[str] = Field(min_length=1, max_length=500)
    review: dict[str, str | None]


class PatchStageIn(Input):
    expected_diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PatchLinkIn(Input):
    authorized: Literal[True]


class PatchProposalIn(Input):
    finding_id: str = Field(min_length=1, max_length=180)
    run_checks: bool = False


@router.get("/api/ide/projects")
def projects():
    return invoke(lambda a: {"projects": a.service.store.projects()})


@router.post("/api/ide/projects")
def create_project(body: ProjectIn):
    return invoke(lambda a: a.service.create_project(body.name, body.source, instructions=body.instructions))


@router.patch("/api/ide/projects/{project_id}")
def update_project(project_id: str, body: ProjectUpdate):
    def operation(a):
        name = body.name.strip() if body.name is not None else None
        if name == "":
            raise StoreError("A project name is required.")
        store = a.service.store
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute("SELECT 1 FROM runs r JOIN chats c ON c.id=r.chat_id WHERE c.project_id=? AND r.state IN ('running','waiting_input','waiting_approval')", (project_id,)).fetchone()
            if active:
                raise StoreError("Project configuration can change only between turns.")
            project = store.get("projects", project_id)
            settings = {**project["settings"], **body.model_dump(exclude_none=True, exclude={"name", "legacy_project_id"})}
            if "legacy_project_id" in body.model_fields_set:
                if body.legacy_project_id is None:
                    settings.pop("legacy_project_id", None)
                else:
                    staging.checked_link(project["source"], body.legacy_project_id)
                    settings["legacy_project_id"] = body.legacy_project_id
            if len(settings.get("instructions", "")) + sum(map(len, settings.get("memory", []))) > 8000:
                raise StoreError("Instructions and curated memory exceed the selected local model's budget.")
            connection.execute("UPDATE projects SET name=COALESCE(?,name),settings=? WHERE id=?", (name, encode(settings), project_id))
        return store.get("projects", project_id)
    return invoke(operation)


@router.post("/api/ide/projects/{project_id}/patch-link")
def enable_patch_proposals(project_id: str, body: PatchLinkIn):
    return invoke(lambda a: staging.enable_proposals(a.service.store, project_id))


@router.get("/api/ide/models")
def models():
    return invoke(lambda a: {"models": a.models()})


@router.post("/api/ide/models/prepare")
def prepare_model(body: ModelPrepareIn):
    return invoke(lambda a: a.prepare(body.model, context=body.context))


@router.get("/api/ide/models/prepare/{preparation_id}")
def model_preparation(preparation_id: str):
    return invoke(lambda a: a.preparation(preparation_id))


@router.get("/api/ide/projects/{project_id}/chats")
def chats(project_id: str):
    return invoke(lambda a: {"chats": a.service.store.chats(project_id)})


@router.get("/api/ide/projects/{project_id}/checkout")
def project_checkout(project_id: str, path: str | None = Query(None, min_length=1, max_length=4096)):
    def operation(a):
        source = Path(a.service.store.get("projects", project_id)["source"])
        return workspace.checkout_diff(source, path) if path is not None else workspace.checkout_changes(source)
    return invoke(operation)


@router.post("/api/ide/projects/{project_id}/chats")
def create_chat(project_id: str, body: ChatIn):
    return invoke(lambda a: a.service.create_chat(project_id, body.title, a.prepared_model(body.preparation_id, body.model),
                                                  defer_workspace=True))


@router.post("/api/ide/projects/{project_id}/scan-chat")
def create_scan_chat(project_id: str):
    return invoke(lambda a: a.service.create_scan_chat(project_id))


@router.get("/api/chats/{chat_id}")
def chat(chat_id: str):
    return invoke(lambda a: a.service.detail(chat_id))


@router.get("/api/chats/{chat_id}/stream")
def chat_stream(chat_id: str, request: Request):
    def get_service(app):
        app.service.store.get("chats", chat_id)
        return app.service

    service = invoke(get_service)

    async def snapshots():
        previous = None
        heartbeat = time.monotonic()
        while not service.stopping and not await request.is_disconnected():
            revision = await asyncio.to_thread(service.stream_revision, chat_id)
            if revision != previous:
                try:
                    detail = await asyncio.to_thread(service.detail, chat_id)
                except StoreError:
                    return  # A deleted conversation closes its open stream.
                yield "event: snapshot\ndata: " + encode(detail) + "\n\n"
                previous, heartbeat = revision, time.monotonic()
            elif time.monotonic() - heartbeat >= 15:
                yield ": keep-alive\n\n"
                heartbeat = time.monotonic()
            await asyncio.sleep(0.15)

    return StreamingResponse(snapshots(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.patch("/api/chats/{chat_id}")
def update_chat(chat_id: str, body: ChatUpdate):
    def operation(a):
        with a._lock:
            if body.archived:
                active = [r for r in a.service.store.runs(chat_id) if r["state"] in ("queued", "running", "waiting_input", "waiting_approval")]
                if active:
                    raise StoreError("Cancel pending work before archiving this chat.")
                if any(t.get("status") in ("starting", "running") for t in a.terminals.list(chat_id)):
                    raise StoreError("Close this chat's running terminals before archiving it.")
            return a.service.store.update_chat(chat_id, **body.model_dump(exclude_none=True))
    return invoke(operation)


@router.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str, discard: bool = False):
    def operation(a):
        with a._lock:
            if any(t.get("status") in ("starting", "running") for t in a.terminals.list(chat_id)):
                raise StoreError("Close this conversation's running terminals before deleting it.")
            a.service.delete_chat(chat_id, discard=discard)
        return {"deleted": True}
    return invoke(operation)


@router.post("/api/chats/{chat_id}/model")
def change_chat_model(chat_id: str, body: ChatModelIn):
    return invoke(lambda a: a.switch_model(chat_id, body.preparation_id, body.model, expected_model_id=body.expected_model_id))


@router.get("/api/chats/{chat_id}/model-status")
def chat_model_status(chat_id: str):
    return invoke(lambda a: a.model_status(chat_id))


@router.post("/api/chats/{chat_id}/runtime/prepare")
def prepare_chat_runtime(chat_id: str):
    return invoke(lambda a: a.prepare_runtime(chat_id))


@router.get("/api/chats/{chat_id}/runtime-status")
def chat_runtime_status(chat_id: str):
    return invoke(lambda a: a.runtime_status(chat_id))


@router.post("/api/chats/{chat_id}/turns")
def turn(chat_id: str, body: TurnIn):
    return invoke(lambda a: a.service.submit(chat_id, body.request_key, body.text, mode=body.mode))


@router.get("/api/chats/{chat_id}/events")
def events(chat_id: str, after: int = Query(default=0, ge=0)):
    return invoke(lambda a: {"events": a.service.store.events(chat_id, after)})


@router.post("/api/chats/{chat_id}/runs/{run_id}/cancel")
@router.post("/api/chats/{chat_id}/scans/{run_id}/cancel")
def cancel(chat_id: str, run_id: str):
    return invoke(lambda a: a.service.cancel(chat_id, run_id))


@router.post("/api/chats/{chat_id}/runs/{run_id}/resume")
def resume(chat_id: str, run_id: str):
    def operation(a):
        if not a.model_status(chat_id)["ready"]:
            raise StoreError("Prepare the conversation's saved model before resuming.")
        return a.service.resume(chat_id, run_id)
    return invoke(operation)


def _view(a, chat_id: str) -> tuple[dict, bool]:
    """The conversation's workspace when it exists; otherwise a read-only view of the registered source.
    Reads never provision a workspace (a copy of up to 2 GiB); the worker and explicit actions do."""
    chat = a.service.store.get("chats", chat_id)
    if chat["workspace"]:
        return workspace.load(Path(chat["workspace"])), False
    source = Path(a.service.store.get("projects", chat["project_id"])["source"])
    return {"root": source, "source": source, "directory": None, "baseline": {}}, True


@router.get("/api/chats/{chat_id}/files")
def files(chat_id: str):
    def operation(a):
        item, read_only = _view(a, chat_id)
        return {"files": [{"path": name, "bytes": path.stat().st_size} for name, path in workspace.files(item["root"]).items()],
                "workspace": str(item["root"]), "exclusions": sorted(workspace.EXCLUDED_DIRS), "read_only": read_only}
    return invoke(operation)


@router.get("/api/chats/{chat_id}/file")
def read_file(chat_id: str, path: str):
    return invoke(lambda a: workspace.read(_view(a, chat_id)[0], path))


@router.put("/api/chats/{chat_id}/file")
def save_file(chat_id: str, body: SaveIn):
    def operation(a):
        result = workspace.save(a.item(chat_id), body.path, body.text, body.version)
        a.service.store.event(chat_id, "file.saved", {"path": body.path, "sha256": result["version"]})
        return result
    return invoke(operation)


@router.get("/api/chats/{chat_id}/search")
def search(chat_id: str, q: str = Query(min_length=1, max_length=300), mode: Literal["exact", "ranked"] = "exact"):
    def operation(a):
        from app.backend.pipeline.project_ide.project_grounding import conversation_search_scope, query_terms
        from app.backend.pipeline.project_ide.repo_search import search_exact

        chat = a.service.store.get("chats", chat_id)
        scope = conversation_search_scope(a.service, chat)
        if mode == "ranked":
            from app.backend.pipeline.project_ide.repo_index import search_project
            response = search_project(scope, query_terms(q) or [q[:120]])
        else:
            response = search_exact(scope, q, case_sensitive=False)
        return {**response.to_dict(), "truncated": not response.complete,
                "matches": [{"path": hit.path, "line": hit.start_line, "text": hit.excerpt} for hit in response.hits]}
    return invoke(operation)


@router.get("/api/chats/{chat_id}/changes")
def changes(chat_id: str):
    def operation(a):
        chat = a.service.store.get("chats", chat_id)
        return {"changes": workspace.changes(a.item(chat_id)) if chat["workspace"] else []}
    return invoke(operation)


@router.get("/api/chats/{chat_id}/patches")
def patches(chat_id: str):
    def operation(a):
        chat = a.service.store.get("chats", chat_id)
        return staging.listing(a.service.store.get("projects", chat["project_id"]))
    return invoke(operation)


@router.post("/api/chats/{chat_id}/patches")
def propose_patch(chat_id: str, body: PatchProposalIn):
    from app.backend import jobs

    def operation(a):
        staging.proposal_inputs(a.service.store, chat_id, body.finding_id)
        params = {"chat_id": chat_id, **body.model_dump()}
        try:
            job = jobs.start("propose_project_patch", params)
        except (jobs.TooManyJobs, jobs.Blocked) as error:
            raise workspace.Conflict(str(error)) from error
        return {"id": job.id, "stage": job.stage}
    return invoke(operation)


@router.get("/api/chats/{chat_id}/patches/{candidate_id}")
def patch_detail(chat_id: str, candidate_id: str):
    def operation(a):
        chat = a.service.store.get("chats", chat_id)
        project = a.service.store.get("projects", chat["project_id"])
        return staging.detail(chat, project, _view(a, chat_id)[0], candidate_id)
    return invoke(operation)


@router.post("/api/chats/{chat_id}/patches/{candidate_id}/stage")
def stage_patch(chat_id: str, candidate_id: str, body: PatchStageIn):
    return invoke(lambda a: staging.stage_candidate(a, chat_id, candidate_id, body.expected_diff_sha256))


@router.post("/api/chats/{chat_id}/integrations")
def integrate(chat_id: str, body: IntegrationIn):
    def operation(a):
        if any(r["state"] in ("queued", "running", "waiting_approval", "waiting_input") for r in a.service.store.runs(chat_id)):
            raise StoreError("Wait for or cancel queued work before integrating reviewed changes.")
        item = a.item(chat_id)
        current = {r["path"]: r["after"] for r in workspace.changes(item)}
        if any(name not in current or name not in body.review or current[name] != body.review[name] for name in body.paths):
            raise workspace.Conflict("The reviewed changes have changed. Review the current diff again.")
        result = workspace.integrate(item, body.paths, body.review)
        a.service.store.event(chat_id, "integration.completed", result)
        return result
    return invoke(operation)


@router.get("/api/chats/{chat_id}/dependencies")
def dependencies(chat_id: str):
    return invoke(lambda a: a.dependency_inventory(chat_id))


@router.get("/api/chats/{chat_id}/verification")
def verification(chat_id: str):
    def operation(a):
        a.service.store.get("chats", chat_id)
        with a.service.store.connect() as connection:
            rows = connection.execute("SELECT sequence,run_id,timestamp,payload FROM events WHERE chat_id=? AND type='tool.execution' ORDER BY sequence DESC LIMIT 200", (chat_id,))
            return {"receipts": [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]}
    return invoke(operation)


@router.post("/api/chats/{chat_id}/scans")
def scan(chat_id: str, body: ScanIn):
    def operation(a):
        if body.scope not in ("project_code", "model_review", "dependencies"):
            raise StoreError("Choose project_code, model_review, or dependencies.")
        if body.scope in ("project_code", "model_review") and (body.dependency_mode or body.selected_package_ids):
            raise StoreError("Project source scans do not accept dependency options.")
        if body.scope == "dependencies" and body.dependency_mode not in ("versions", "source"):
            raise StoreError("Choose versions or source for dependency scanning.")
        if body.dependency_mode == "source":
            available = {p["id"] for p in a.dependency_inventory(chat_id)["packages"]}
            if not body.selected_package_ids or len(set(body.selected_package_ids)) != len(body.selected_package_ids) or set(body.selected_package_ids) - available:
                raise StoreError("Select distinct packages from this chat's dependency inventory before reviewing sources.")
        elif body.selected_package_ids:
            raise StoreError("Package selection applies to dependency source review only.")
        return a.service.submit_scan(chat_id, body.request_key, body.model_dump(exclude={"request_key"}))
    return invoke(operation)


@router.get("/api/chats/{chat_id}/scans/{scan_id}")
def scan_result(chat_id: str, scan_id: str):
    def operation(a):
        run = a.service.store.get("runs", scan_id)
        if run["chat_id"] != chat_id or run["kind"] != "scan":
            raise StoreError("That scan does not belong to this chat.")
        return run
    return invoke(operation)


@router.get("/api/chats/{chat_id}/findings")
def findings(chat_id: str):
    def operation(a):
        item, read_only = _view(a, chat_id)
        recorded = []
        for path in ([] if read_only else (item["directory"] / "receipts").glob("*.json")):
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
                if "assessment" in receipt and "task" in receipt:
                    recorded.append(receipt)
            except (ValueError, OSError):
                continue
        cache = getattr(getattr(a, "scans", None), "cache", None)
        return workbench.findings(item, a.service.store.runs(chat_id), recorded,
                                  source_vault=Path(cache) / "sources" if cache else None)
    return invoke(operation)


class PermissionIn(Input):
    reply: str


class AnswerIn(Input):
    answers: list[list[str]]


@router.post("/api/chats/{chat_id}/permissions/{request_id}")
def permission(chat_id: str, request_id: str, body: PermissionIn):
    return invoke(lambda a: a.service.reply(chat_id, request_id, approval=body.reply))


@router.post("/api/chats/{chat_id}/questions/{request_id}")
def answer(chat_id: str, request_id: str, body: AnswerIn):
    return invoke(lambda a: a.service.reply(chat_id, request_id, answers=body.answers))


class TerminalIn(Input):
    cols: int = Field(default=100, ge=10, le=500)
    rows: int = Field(default=30, ge=3, le=200)


class TerminalInput(Input):
    data: str = Field(max_length=65536)


@router.get("/api/chats/{chat_id}/terminals")
def terminals(chat_id: str):
    return invoke(lambda a: {"terminals": a.terminals.list(chat_id)})


@router.post("/api/chats/{chat_id}/terminals")
def terminal_open(chat_id: str, body: TerminalIn):
    def operation(a):
        root = a.item(chat_id)["root"]
        with a._lock:
            if a.service.store.get("chats", chat_id)["archived"]:
                raise StoreError("Archived chats cannot open new terminals.")
            return a.terminals.open(chat_id, root, cols=body.cols, rows=body.rows)
    return invoke(operation)


@router.get("/api/chats/{chat_id}/terminals/{terminal_id}")
def terminal_read(chat_id: str, terminal_id: str, after_sequence: int = Query(default=0, ge=0)):
    return invoke(lambda a: a.terminals.read(terminal_id, after_sequence, chat_id=chat_id))


@router.post("/api/chats/{chat_id}/terminals/{terminal_id}/input")
def terminal_write(chat_id: str, terminal_id: str, body: TerminalInput):
    return invoke(lambda a: a.terminals.write(terminal_id, body.data, chat_id=chat_id))


@router.post("/api/chats/{chat_id}/terminals/{terminal_id}/resize")
def terminal_resize(chat_id: str, terminal_id: str, body: TerminalIn):
    return invoke(lambda a: a.terminals.resize(terminal_id, body.cols, body.rows, chat_id=chat_id))


@router.delete("/api/chats/{chat_id}/terminals/{terminal_id}")
def terminal_close(chat_id: str, terminal_id: str):
    return invoke(lambda a: a.terminals.close(terminal_id, chat_id=chat_id))


class ExtensionIn(Input):
    kind: str
    name: str = ""
    source: str = ""
    config: dict = Field(default_factory=dict)


class TrustIn(Input):
    version: int
    sha256: str


class EnableIn(Input):
    enabled: bool


@router.get("/api/ide/extensions")
def extensions():
    return invoke(lambda a: {"extensions": a.extensions.list()})


@router.post("/api/ide/extensions")
def register_extension(body: ExtensionIn):
    def operation(a):
        if body.kind == "skill":
            return a.extensions.import_skill(body.source)
        if body.kind == "mcp":
            return a.extensions.register_mcp(body.name, body.config)
        if body.kind == "plugin":
            return a.extensions.register_plugin(body.name, body.source)
        raise StoreError("Choose skill, mcp, or plugin.")
    return invoke(operation)


@router.post("/api/ide/extensions/{extension_id}/trust")
def trust_extension(extension_id: str, body: TrustIn):
    return invoke(lambda a: a.extensions.trust(extension_id, expected_version=body.version, expected_hash=body.sha256))


@router.post("/api/ide/projects/{project_id}/extensions/{extension_id}")
def enable_extension(project_id: str, extension_id: str, body: EnableIn):
    def operation(a):
        a.service.store.get("projects", project_id)
        if any(r["state"] in ("running", "waiting_approval", "waiting_input") for c in a.service.store.chats(project_id) for r in a.service.store.runs(c["id"])):
            raise StoreError("Extensions can change only between turns.")
        return a.extensions.enable(project_id, extension_id, body.enabled)
    return invoke(operation)
