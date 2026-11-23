"""Routes of Projects: add, list, change and remove a project, look at a folder before it is added, and read or export a project's audit log. Folders are validated here, on the
server; nothing the page says about a folder is trusted. A project's folders are only read; the one thing that writes into them is applying a dependency update, which names the
files and needs the person's confirmation of that list."""
from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.backend import errors

router = APIRouter()


class CreateIn(BaseModel):
    name: str = Field(default="", max_length=400)
    folders: list[str] = Field(default_factory=list, max_length=50)
    authorized: bool = False


class UpdateIn(BaseModel):
    name: str | None = Field(default=None, max_length=400)
    folders: list[str] | None = Field(default=None, max_length=50)
    instructions: str | None = Field(default=None, max_length=4000)


def _fail(e) -> object:
    from app.backend.pipeline import projects
    return errors.error(str(e), 404 if isinstance(e, projects.NotFound) else 400)


@router.get("/api/projects")
def projects_list():
    from app.backend.pipeline import projects
    return {"projects": projects.list_projects(), "statement": projects.STATEMENT}


@router.get("/api/projects/capabilities")
def projects_capabilities():
    """What the app can do for each of the nine package ecosystems, as a table: implemented, partial, needs the advisories downloaded, or not implemented. The page reads this and
    says "not supported yet" for a cell that is not built, never an empty result that looks like no findings."""
    from app.backend.pipeline.scan import capabilities
    return capabilities.matrix()


@router.get("/api/projects/look")
def projects_look(path: str):
    """What a folder would be before it is added: whether it can be a source folder, the languages and ecosystems found in it, and a name suggested for the project. Nothing is stored."""
    from app.backend.pipeline import projects
    try:
        real = projects.validate_folder(path)
    except projects.ProjectError as e:
        return {"ok": False, "reason": str(e)}
    suggestion = projects.suggest_name(str(real))
    return {"ok": True, "path": str(real), **projects.detect(real), "suggested_name": suggestion["name"], "name_source": suggestion["source"]}


@router.post("/api/projects")
def projects_create(body: CreateIn):
    from app.backend.pipeline import projects
    try:
        return projects.create(body.name, body.folders, authorized=body.authorized)
    except projects.ProjectError as e:
        return _fail(e)


@router.get("/api/projects/{project_id}")
def projects_get(project_id: str):
    from app.backend.pipeline import projects
    try:
        return projects.get(project_id)
    except projects.ProjectError as e:
        return _fail(e)


@router.put("/api/projects/{project_id}")
def projects_update(project_id: str, body: UpdateIn):
    from app.backend.pipeline import projects
    try:
        return projects.update(project_id, name=body.name, folders=body.folders, instructions=body.instructions)
    except projects.ProjectError as e:
        return _fail(e)


@router.delete("/api/projects/{project_id}")
def projects_delete(project_id: str):
    """Remove the project's record, audit log, results and disposable search cache."""
    from app.backend.pipeline import projects
    try:
        project = projects.get(project_id)
        try:
            from app.backend.pipeline import legacy_project_search
            legacy_project_search.clear(project)
        except projects.ProjectError:  # An unauthorized project cannot have built a local search cache.
            index_note = None
        except (OSError, sqlite3.Error, ValueError):
            index_note = "The project's local search cache could not be removed."
        else:
            index_note = None
        try:                                                   # its agent sessions are kept in the database, not in its folder: they go with it, and the project stays until they have
            from app.backend.pipeline import agent_sessions
            agent_sessions.delete_project(project_id)
        except Exception as e:  # noqa: BLE001 - a project whose sessions could not be removed is NOT removed: the sessions would be left in the database with nothing to reach them
            return errors.error(f"The project's agent sessions could not be removed now ({type(e).__name__}), so the project was kept. Try again when the database answers.", 503, code="sessions_not_removed")
        projects.delete(project_id)
    except projects.ProjectError as e:
        return _fail(e)
    return {"ok": True, **({"note": index_note} if index_note else {})}


class ScanDepsIn(BaseModel):
    outdated: bool = False


@router.post("/api/projects/{project_id}/scan-dependencies")
def projects_scan_dependencies(project_id: str, body: ScanDepsIn):
    """Start a dependency scan of the project as a job (it reads the manifests, matches the advisories on this computer and, when asked, looks the registries up). Refused for a
    project without the confirmation that it may be tested."""
    from app.backend import jobs
    from app.backend.pipeline import projects
    try:
        projects.require_authorized(projects.get(project_id))
    except projects.ProjectError as e:
        return _fail(e)
    try:
        job = jobs.start("scan_project_deps", {"project": project_id, "outdated": body.outdated})
    except jobs.TooManyJobs as e:
        return errors.error(str(e), 409)
    return {"id": job.id, "stage": job.stage}


@router.post("/api/projects/{project_id}/scan-code")
def projects_scan_code(project_id: str):
    """Start a scan of the project's source code with the static rules, as a job. The files are only read. Refused for a project without the confirmation that it may be tested."""
    from app.backend import jobs
    from app.backend.pipeline import projects
    try:
        projects.require_authorized(projects.get(project_id))
    except projects.ProjectError as e:
        return _fail(e)
    try:
        job = jobs.start("scan_project_code", {"project": project_id})
    except jobs.TooManyJobs as e:
        return errors.error(str(e), 409)
    return {"id": job.id, "stage": job.stage}


class ScanCodebaseIn(BaseModel):
    start_command: str = Field(default="", max_length=500)       # how the project starts, for the loopback probes; empty means the probes are not run
    port: int | None = Field(default=None, ge=1024, le=65535)
    model_review: bool = False
    model_max_files: int = Field(default=32, ge=1, le=1000)


@router.post("/api/projects/{project_id}/scan-codebase")
def projects_scan_codebase(project_id: str, body: ScanCodebaseIn):
    """Start the full Scan Codebase as a job: the static rules, then the approved toolkit, the project's tests and (with a start command and a port) loopback probes, all inside a closed container on
    a copy of the project. Refused for a project without the confirmation that it may be tested, and while the tool image has not been built or Docker is not running."""
    from app.backend import jobs, sandbox
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import sandbox_run
    try:
        projects.require_authorized(projects.get(project_id))
    except projects.ProjectError as e:
        return _fail(e)
    try:
        sandbox_run.toolkit_image()
    except sandbox_run.ToolkitMissing as e:
        return errors.error(str(e), 409)
    if not sandbox.available():
        return errors.error("Docker is not running, so the sandbox cannot be started.", 503)
    try:
        params = {"project": project_id, "start_command": body.start_command.strip(), "port": body.port}
        if body.model_review: params.update(model_review=True, model_max_files=body.model_max_files)
        job = jobs.start("scan_project_codebase", params)
    except jobs.TooManyJobs as e:
        return errors.error(str(e), 409)
    return {"id": job.id, "stage": job.stage}


class RespondIn(BaseModel):
    scope: str = Field(default="both", max_length=20)
    mode: str = Field(default="log", max_length=20)
    depth: str = Field(default="quick", max_length=20)
    assist: bool = False                            # ask the configured model for a severity where a finding has no score
    allow_remote: bool = False                      # the person confirmed that the words of a finding and excerpts of its file may be sent to a model that is not on this computer
    test: bool = True
    start_command: str = Field(default="", max_length=500)
    port: int | None = Field(default=None, ge=1024, le=65535)


@router.post("/api/projects/{project_id}/respond")
def projects_respond(project_id: str, body: RespondIn):
    """Start the response orchestrator as a job: collect the findings of the scope, classify them, respond by mode. Patch candidates are never applied to the project. Refused for a project
    without the confirmation that it may be tested, for a choice the orchestrator does not offer, and, for the full depth, while the tool image has not been built or Docker is down."""
    from app.backend import jobs, sandbox
    from app.backend.pipeline import projects
    from app.backend.pipeline.agent import orchestrator
    from app.backend.pipeline.scan import sandbox_run
    try:
        projects.require_authorized(projects.get(project_id))
    except projects.ProjectError as e:
        return _fail(e)
    if body.scope not in orchestrator.SCOPES or body.mode not in orchestrator.MODES or body.depth not in orchestrator.DEPTHS:
        return errors.error(f"scope is one of {', '.join(orchestrator.SCOPES)}, mode one of {', '.join(orchestrator.MODES)}, depth one of {', '.join(orchestrator.DEPTHS)}.", 400)
    if body.depth == "full" and body.scope != "dependencies":
        try:
            sandbox_run.toolkit_image()
        except sandbox_run.ToolkitMissing as e:
            return errors.error(str(e), 409)
        if not sandbox.available():
            return errors.error("Docker is not running, so the sandbox cannot be started.", 503)
    try:
        job = jobs.start("respond_project", {"project": project_id, **body.model_dump()})
    except jobs.TooManyJobs as e:
        return errors.error(str(e), 409)
    return {"id": job.id, "stage": job.stage}


@router.get("/api/projects/{project_id}/candidates")
def projects_candidates(project_id: str):
    """The patch candidates made for the project, newest first. Each is a candidate for review: not applied, and never called verified without a test that reproduces the weakness."""
    from app.backend.pipeline import projects
    from app.backend.pipeline.agent import candidates
    try:
        return {"candidates": candidates.listing(project_id)}
    except projects.ProjectError as e:
        return _fail(e)


@router.get("/api/projects/{project_id}/candidates/{candidate_id}")
def projects_candidate(project_id: str, candidate_id: str):
    from app.backend.pipeline import projects
    from app.backend.pipeline.agent import candidates
    try:
        return {**candidates.get(project_id, candidate_id), "diff": candidates.diff_of(project_id, candidate_id)}
    except projects.ProjectError as e:
        return _fail(e)


@router.get("/api/projects/{project_id}/chat/endpoint")
def projects_chat_endpoint(project_id: str):
    """Where the chat's model is: whether one is configured, its name, its host and whether that host is this computer (the key is never returned), and the fixed system prompt every
    conversation of this project is sent, filled in with its own name and folders."""
    from app.backend.pipeline import project_chat, projects
    try:
        project = projects.get(project_id)
    except projects.ProjectError as e:
        return _fail(e)
    return project_chat.endpoint(project)


# -- stored conversations ----------------------------------------------------------------------------------------------------------------------------------------------

class ChatCreateIn(BaseModel):
    title: str = Field(default="", max_length=500)
    params: dict | None = None                       # {system, temperature, top_p, max_tokens}; the store checks every value and says what is wrong


class ChatUpdateIn(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    params: dict | None = None


class TurnIn(BaseModel):
    text: str | None = Field(default=None, max_length=25000)         # the store says what its limit is (20,000 characters); this one only keeps a body from being huge
    regenerate: bool = False                                          # the last answer again
    edit_of: str | None = Field(default=None, max_length=40)         # the id of a question that is edited and asked again from there (the new words are ``text``)
    allow_remote: bool = False                                        # the person confirmed that excerpts of the files may be sent to a model that is not on this computer
    deep_research: bool = False                                       # /deep-research: more tool rounds and more retrieved excerpts, for this turn only
    plan_mode: bool = False                                           # /plan: a plan instead of a direct answer, for this turn only
    force_web_search: bool = False                                    # /deep-research also forces a web search for this turn only, on top of the conversation's own web_search toggle


class CompactIn(BaseModel):
    allow_remote: bool = False


def _chat_fail(e) -> object:
    """An error of a conversation, a project or the chat as an answer: the status and the code the page acts on, and a sentence."""
    from app.backend.pipeline import project_chat, project_chats, projects
    if isinstance(e, (project_chats.ChatStoreError, project_chat.ChatError)):
        return errors.error(str(e), e.status, code=e.code)
    if isinstance(e, projects.ProjectError):
        return _fail(e)
    raise e


@router.get("/api/projects/{project_id}/chats")
def projects_chats_list(project_id: str):
    """The project's conversations, the newest first (id, title, times, number of messages, a preview of the last words), and how many files of conversations could not be read."""
    from app.backend.pipeline import project_chats, projects
    try:
        return project_chats.list_chats(project_id)
    except (project_chats.ChatStoreError, projects.ProjectError) as e:
        return _chat_fail(e)


@router.post("/api/projects/{project_id}/chats")
def projects_chats_create(project_id: str, body: ChatCreateIn):
    """Start an empty conversation, with a name or without one (the first question names it) and with settings or the model's own."""
    from app.backend.pipeline import project_chats, projects
    try:
        return project_chats.create(project_id, body.title, body.params)
    except (project_chats.ChatStoreError, projects.ProjectError) as e:
        return _chat_fail(e)


@router.get("/api/projects/{project_id}/chats/{chat_id}")
def projects_chats_get(project_id: str, chat_id: str):
    """One conversation as it is saved: its settings and all its messages. An answer that was cut short is shown as stopped."""
    from app.backend.pipeline import project_chats, projects
    try:
        return project_chats.get(project_id, chat_id)
    except (project_chats.ChatStoreError, projects.ProjectError) as e:
        return _chat_fail(e)


@router.patch("/api/projects/{project_id}/chats/{chat_id}")
def projects_chats_update(project_id: str, chat_id: str, body: ChatUpdateIn):
    """Rename a conversation and/or change its settings (only the keys given; an empty number is the model's own)."""
    from app.backend.pipeline import project_chats, projects
    try:
        return project_chats.update(project_id, chat_id, title=body.title, params=body.params)
    except (project_chats.ChatStoreError, projects.ProjectError) as e:
        return _chat_fail(e)


@router.delete("/api/projects/{project_id}/chats/{chat_id}")
def projects_chats_delete(project_id: str, chat_id: str):
    """Delete a conversation (its file). A file that cannot be read can be deleted too."""
    from app.backend.pipeline import project_chats, projects
    try:
        project_chats.delete(project_id, chat_id)
    except (project_chats.ChatStoreError, projects.ProjectError) as e:
        return _chat_fail(e)
    return {"ok": True}


@router.post("/api/projects/{project_id}/chats/{chat_id}/stop")
def projects_chats_stop(project_id: str, chat_id: str):
    """Stop the answer that is being written in this conversation: it ends at the next word, keeps what was written (marked stopped) and its stream ends with ``done``. ``stopped`` is false
    when no answer was being written."""
    from app.backend.pipeline import project_chats, projects
    try:
        projects.get(project_id)
        project_chats.get(project_id, chat_id)
    except (project_chats.ChatStoreError, projects.ProjectError) as e:
        return _chat_fail(e)
    return {"stopped": project_chats.stop(project_id, chat_id)}


@router.post("/api/projects/{project_id}/chats/{chat_id}/messages")
def projects_chats_turn(project_id: str, chat_id: str, body: TurnIn):
    """One turn of a conversation, streamed as lines of JSON (``application/x-ndjson``): a new question (``text``), the last answer again (``regenerate``) or an edited question asked again
    from there (``edit_of`` and ``text``). The events are those of the project chat plus ``saved`` first (the ids of the question and the answer, and the title) and ``thinking``; ``done``
    carries the files and results it read, the tokens and the speed, and ``stopped`` after a stop. The question is saved before the model is asked, the answer as it is written and for good
    at the end. Refused before anything is saved: 404 (no such project, conversation or message), 409 ``busy`` (an answer is being written), ``no_model``, ``remote_not_confirmed``, ``too_long``,
    400 ``no_question`` and 413 for a question that is too long."""
    from app.backend.pipeline import project_chat, project_chats, projects
    try:
        turn = project_chat.StoredTurn(project_id, chat_id, text=body.text, regenerate=body.regenerate, edit_of=body.edit_of, allow_remote=body.allow_remote,
                                        deep_research=body.deep_research, plan_mode=body.plan_mode, force_web_search=body.force_web_search)
    except (project_chats.ChatStoreError, project_chat.ChatError, projects.ProjectError) as e:
        return _chat_fail(e)

    def lines():
        for event in turn.events():
            yield json.dumps(event, ensure_ascii=False) + "\n"
    return StreamingResponse(lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/api/projects/{project_id}/chats/{chat_id}/compact")
def projects_chats_compact(project_id: str, chat_id: str, body: CompactIn):
    """``/compact``: fold the conversation's oldest turns into a summary now, regardless of its size, so the next turn reuses it instead of making one on demand. Never changes the
    stored conversation itself. Refused the same way a turn is: 409 ``busy``, ``no_model``, ``remote_not_confirmed``."""
    from app.backend.pipeline import project_chat, project_chats, projects
    try:
        return project_chat.force_compact(project_id, chat_id, allow_remote=body.allow_remote)
    except (project_chats.ChatStoreError, project_chat.ChatError, projects.ProjectError) as e:
        return _chat_fail(e)


@router.post("/api/projects/{project_id}/chats/{chat_id}/clear")
def projects_chats_clear(project_id: str, chat_id: str):
    """``/clear``: empty a conversation's messages, keeping its id, title and settings."""
    from app.backend.pipeline import project_chats, projects
    try:
        return project_chats.clear(project_id, chat_id)
    except (project_chats.ChatStoreError, projects.ProjectError) as e:
        return _chat_fail(e)


@router.get("/api/projects/{project_id}/index")
def projects_index(project_id: str):
    """Status of the disposable lexical cache for an authorized project."""
    from app.backend.config import desktop_auto_retrieval_enabled
    from app.backend.pipeline import legacy_project_search, projects
    try:
        state = legacy_project_search.status(projects.get(project_id))
        return {"enabled": desktop_auto_retrieval_enabled(), **state}
    except projects.ProjectError as e:
        return _fail(e)
    except (OSError, sqlite3.Error, ValueError):
        return errors.error("The project's local search cache is unavailable.", 503)


@router.get("/api/projects/{project_id}/index/changes")
def projects_index_changes(project_id: str):
    """Compatibility status. Local search checks freshness when it uses the cache."""
    from app.backend.pipeline import legacy_project_search, projects
    try:
        state = legacy_project_search.status(projects.get(project_id))
        return {key: state[key] for key in ("built", "tracked", "backend")}
    except projects.ProjectError as e:
        return _fail(e)
    except (OSError, sqlite3.Error, ValueError):
        return errors.error("The project's local search cache is unavailable.", 503)


@router.post("/api/projects/{project_id}/index")
def projects_index_build(project_id: str):
    """Refresh the authorized project's local search cache through the legacy job interface."""
    from app.backend import jobs
    from app.backend.pipeline import legacy_project_search, projects
    try:
        projects.require_authorized(projects.get(project_id))
    except projects.ProjectError as e:
        return _fail(e)
    try:
        job = jobs.start("rag_index_project", {"project": project_id})
    except jobs.TooManyJobs as e:
        return errors.error(str(e), 409)
    return {"id": job.id, "stage": job.stage, "backend": legacy_project_search.BACKEND}


@router.delete("/api/projects/{project_id}/index")
def projects_index_delete(project_id: str):
    """Remove only the project's disposable lexical cache."""
    from app.backend.pipeline import legacy_project_search, projects
    try:
        removed = legacy_project_search.clear(projects.get(project_id))
        return {"ok": True, "removed": removed, "backend": legacy_project_search.BACKEND}
    except projects.ProjectError as e:
        return _fail(e)
    except (OSError, sqlite3.Error, ValueError):
        return errors.error("The project's local search cache could not be removed.", 503)


class PlanIn(BaseModel):
    mode: str = Field(default="security", max_length=20)


class ApplyIn(BaseModel):
    files: list[str] = Field(default_factory=list, max_length=200)


@router.post("/api/projects/{project_id}/updates")
def projects_update_plan(project_id: str, body: PlanIn):
    """Start planning a dependency update from the project's newest dependency scan, as a job. ``mode`` is ``security`` (fixes for what an advisory affects) or ``all`` (everything the
    scan found outdated). The result is a candidate on a copy of the manifests; nothing in the project changes."""
    from app.backend import jobs
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import update_plan
    try:
        projects.require_authorized(projects.get(project_id))
    except projects.ProjectError as e:
        return _fail(e)
    if body.mode not in update_plan.MODES:
        return errors.error("The mode is security fixes only, or everything that is outdated.", 400)
    try:
        job = jobs.start("plan_project_update", {"project": project_id, "mode": body.mode})
    except jobs.TooManyJobs as e:
        return errors.error(str(e), 409)
    return {"id": job.id, "stage": job.stage}


@router.get("/api/projects/{project_id}/updates")
def projects_updates(project_id: str):
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import update_plan
    try:
        return {"updates": update_plan.listing(project_id)}
    except projects.ProjectError as e:
        return _fail(e)


@router.get("/api/projects/{project_id}/updates/{update_id}")
def projects_update(project_id: str, update_id: str):
    """One update: the plan, the files and their edits, the diff, what the scan would say afterwards, and its status (candidate, applied, reverted)."""
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import update_plan
    try:
        projects.get(project_id)
        return update_plan.get(project_id, update_id)
    except projects.ProjectError as e:
        return _fail(e)


@router.post("/api/projects/{project_id}/updates/{update_id}/apply")
def projects_update_apply(project_id: str, update_id: str, body: ApplyIn):
    """Write the candidate's manifests into the project's folders. ``files`` must be exactly the list of files the plan names, which is what the person confirmed; every file must still be
    the file the plan was made from, or nothing is written."""
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import update_plan
    try:
        return update_plan.apply(projects.get(project_id), update_id, body.files)
    except projects.ProjectError as e:
        return _fail(e)


@router.post("/api/projects/{project_id}/updates/{update_id}/revert")
def projects_update_revert(project_id: str, update_id: str):
    """Put the originals back, if the files are still exactly as the update wrote them."""
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import update_plan
    try:
        return update_plan.revert(projects.get(project_id), update_id)
    except projects.ProjectError as e:
        return _fail(e)


@router.post("/api/projects/{project_id}/updates/{update_id}/trial")
def projects_update_trial(project_id: str, update_id: str):
    """Try a candidate update in the sandbox as a job: the packages are fetched as wheels on this computer (the only use of the network, and the page asks first), installed offline in closed
    containers next to a copy of the project, and the tests and tools are compared before and after. Refused while the tool image is not built or Docker is down, for an update that is not a
    candidate and for one that changes no requirements file."""
    from app.backend import jobs, sandbox
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import sandbox_run, update_plan, update_trial
    try:
        project = projects.get(project_id)
        projects.require_authorized(project)
        update = update_plan.get(project_id, update_id)
    except projects.ProjectError as e:
        return _fail(e)
    if update["status"] != "candidate":
        return errors.error("Only a candidate update that has not been applied can be tried.", 400)
    if not update_trial.requirement_files(update):
        return errors.error("This update changes no requirements file, and only requirements files can be installed in the sandbox so far.", 400)
    try:
        sandbox_run.toolkit_image()
    except sandbox_run.ToolkitMissing as e:
        return errors.error(str(e), 409)
    if not sandbox.available():
        return errors.error("Docker is not running, so the sandbox cannot be started.", 503)
    try:
        job = jobs.start("trial_update", {"project": project_id, "update": update_id})
    except jobs.TooManyJobs as e:
        return errors.error(str(e), 409)
    return {"id": job.id, "stage": job.stage}


@router.get("/api/projects/{project_id}/updates/{update_id}/trial")
def projects_update_trial_get(project_id: str, update_id: str):
    """The stored result of trying the update in the sandbox: what was fetched, the install, and the tests and findings before and after."""
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import update_trial
    try:
        projects.get(project_id)
        return update_trial.get(project_id, update_id)
    except projects.ProjectError as e:
        return _fail(e)


@router.delete("/api/projects/{project_id}/updates/{update_id}")
def projects_update_discard(project_id: str, update_id: str):
    """Discard a candidate (or an undone update). Nothing in the project is touched."""
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import update_plan
    try:
        projects.get(project_id)
        update_plan.discard(project_id, update_id)
    except projects.ProjectError as e:
        return _fail(e)
    return {"ok": True}


@router.get("/api/projects/{project_id}/scans")
def projects_scans(project_id: str):
    from app.backend.pipeline import projects
    try:
        return {"scans": projects.scans(project_id)}
    except projects.ProjectError as e:
        return _fail(e)


@router.get("/api/projects/{project_id}/scans/{scan_id}")
def projects_scan(project_id: str, scan_id: str):
    """One scan's report: every dependency with its state, the evidence of a finding, what could not be checked and why."""
    from app.backend.pipeline import projects
    try:
        return projects.scan_report(project_id, scan_id)
    except projects.ProjectError as e:
        return _fail(e)


@router.get("/api/projects/{project_id}/audit")
def projects_audit(project_id: str, limit: int = 500):
    from app.backend.pipeline import projects
    try:
        return {"entries": projects.audit_log(project_id, limit)}
    except projects.ProjectError as e:
        return _fail(e)


@router.get("/api/projects/{project_id}/audit/export")
def projects_audit_export(project_id: str):
    """The audit log as lines of JSON, oldest first, as a file."""
    from app.backend.pipeline import projects
    try:
        text = projects.audit_export(project_id)
    except projects.ProjectError as e:
        return _fail(e)
    return PlainTextResponse(text, media_type="application/x-ndjson", headers={"Content-Disposition": f'attachment; filename="{project_id}-audit.jsonl"'})
