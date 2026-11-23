"""Routes of agentic mode (Part C): sessions, running a turn (SSE), answering its one pending call, reading the sandbox's diff against the real project, staging/applying/
reverting it there (``agent_apply.py``, the only path that ever writes to the real project; nothing here is reachable from a model's own tools in any mode), and checkpointing or
rolling back the sandbox itself.

The diff, the applies, and the checkpoints are all read from the project's one agent sandbox, not from a particular session: Manual and Auto sessions of the same project share it
(``agent_sandbox.py``), so these are a property of the project's sandbox, not of whichever session happened to write to it. Checkpointing is symmetric with the model's own
``checkpoint`` tool (either may add one); rolling back is human-only -- there is no model tool for it, since an agent silently discarding its own prior work would be un-auditable."""
from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.backend import errors

router = APIRouter()


class SessionCreateIn(BaseModel):
    mode: str = Field(max_length=10)
    instructions: str = Field(default="", max_length=4000)
    params: dict | None = None
    model_ref: str | None = Field(default=None, max_length=240)


class SessionUpdateIn(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(default=None, max_length=4000)
    params: dict | None = None
    mode: str | None = Field(default=None, max_length=10)
    model_ref: str | None = Field(default=None, max_length=240)


class RunIn(BaseModel):
    instruction: str | None = Field(default=None, max_length=25000)
    edit_of: str | None = Field(default=None, max_length=40)
    allow_remote: bool = False


class ApproveIn(BaseModel):
    call_id: str = Field(max_length=40)
    decision: str = Field(max_length=10)
    edited_arguments: dict | None = None
    feedback: str | None = Field(default=None, max_length=4000)
    remember: str | None = Field(default=None, max_length=10)


class ApplyConfirmIn(BaseModel):
    files: list[str] = Field(default_factory=list, max_length=200)
    delete: list[str] = Field(default_factory=list, max_length=200)           # removals the person ticked; a removal is never applied unless it is named here


class ReviewedFile(BaseModel):
    file: str = Field(max_length=500)
    sha256_after: str | None = Field(default=None, max_length=64)


class ApproveAllIn(BaseModel):
    delete: list[str] = Field(default_factory=list, max_length=200)
    reviewed: list[ReviewedFile] | None = Field(default=None, max_length=2000)       # the files the person looked at; when the sandbox has changed since, nothing is written
    skip: list[str] = Field(default_factory=list, max_length=2000)                   # files the person unticked: left out of the apply, and left as they are in the sandbox


class CheckpointIn(BaseModel):
    message: str = Field(default="Checkpoint", max_length=300)


class RollbackIn(BaseModel):
    root: str = Field(max_length=200)
    hash: str = Field(max_length=64)


class ProviderIn(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    kind: str | None = Field(default=None, max_length=20)
    base_url: str = Field(max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    headers: dict | None = None
    models: dict | None = None


class ProbeIn(BaseModel):
    ref: str = Field(default="", max_length=240)


def _fail(e) -> object:
    from app.backend.pipeline import agent_sessions, projects
    from app.backend.pipeline.agentic.providers import registry
    from app.backend.pipeline.scan import agent_image, agent_sandbox
    if isinstance(e, (agent_sessions.AgentSessionError, agent_sandbox.AgentSandboxError, agent_image.AgentImageMissing, registry.ProviderError)):
        return errors.error(str(e), e.status, code=e.code)
    return errors.error(str(e), 404 if isinstance(e, projects.NotFound) else 400)


@router.get("/api/projects/{project_id}/agent/sessions")
def agent_sessions_list(project_id: str):
    """The project's agent sessions, newest first (id, mode, status, rounds, a pending call if there is one), and how many files could not be read."""
    from app.backend.pipeline import agent_sessions, projects
    try:
        return agent_sessions.list_sessions(project_id)
    except (agent_sessions.AgentSessionError, projects.ProjectError) as e:
        return _fail(e)


@router.post("/api/projects/{project_id}/agent/sessions")
def agent_sessions_create(project_id: str, body: SessionCreateIn):
    """Start a session in one of the three modes (plan, manual, auto), fixed for its lifetime, with optional extra instructions added after the project's own system prompt and
    optional model settings (the same ones a project chat has, plus Manual's own auto-approve)."""
    from app.backend.pipeline import agent_loop, agent_sessions, projects
    from app.backend.pipeline.agentic import prefs
    try:
        params = dict(body.params or {})
        if not params.get("engine"):
            params["engine"] = agent_loop.NEW_SESSION_ENGINE                     # a new session starts on the newer engine unless its owner chose the other one
        if "effort" not in params and prefs.effort_for(body.model_ref or ""):
            params["effort"] = prefs.effort_for(body.model_ref or "")            # a new session starts at the level its owner last chose for this model
        return agent_sessions.create(project_id, body.mode, body.instructions, params, body.model_ref)
    except (agent_sessions.AgentSessionError, projects.ProjectError) as e:
        return _fail(e)


@router.get("/api/projects/{project_id}/agent/sessions/{session_id}")
def agent_sessions_get(project_id: str, session_id: str):
    from app.backend.pipeline import agent_sessions, projects
    try:
        return agent_sessions.get(project_id, session_id)
    except (agent_sessions.AgentSessionError, projects.ProjectError) as e:
        return _fail(e)


@router.patch("/api/projects/{project_id}/agent/sessions/{session_id}")
def agent_sessions_update(project_id: str, session_id: str, body: SessionUpdateIn):
    """Rename a session, change its instructions or settings, and/or switch its mode (only the keys given) -- mode is live, refused only while a call is pending."""
    from app.backend.pipeline import agent_sessions, projects
    from app.backend.pipeline.agentic import prefs
    try:
        params = dict(body.params) if body.params is not None else None
        if body.model_ref is not None and not (params and "effort" in params) and prefs.effort_for(body.model_ref):
            params = {**(params or {}), "effort": prefs.effort_for(body.model_ref)}      # another model: the level last chosen for it
        session = agent_sessions.update(project_id, session_id, title=body.title, instructions=body.instructions, params=params, mode=body.mode, model_ref=body.model_ref)
        if params and "effort" in params:
            prefs.remember_effort(session.get("model_ref") or "", session["params"]["effort"])
        return session
    except (agent_sessions.AgentSessionError, projects.ProjectError) as e:
        return _fail(e)


class ForkIn(BaseModel):
    message_id: str = Field(max_length=40)


@router.post("/api/projects/{project_id}/agent/sessions/{session_id}/fork")
def agent_sessions_fork(project_id: str, session_id: str, body: ForkIn):
    """A new session with this one's conversation up to the end of the turn the message belongs to. The original and the sandbox are not touched."""
    from app.backend.pipeline import agent_sessions, projects
    try:
        projects.get(project_id)
        return agent_sessions.fork(project_id, session_id, body.message_id)
    except (agent_sessions.AgentSessionError, projects.ProjectError) as e:
        return _fail(e)


@router.get("/api/projects/{project_id}/agent/sessions/{session_id}/export")
def agent_sessions_export(project_id: str, session_id: str, format: str = "md"):
    """The session as a file: ``format=md`` (to read) or ``json``. Masked for credentials once more on the way out."""
    from fastapi.responses import PlainTextResponse
    from app.backend.pipeline import agent_sessions, projects
    from app.backend.pipeline.agentic import export
    try:
        if format not in export.FORMATS:
            return errors.error(f"The format must be one of {', '.join(export.FORMATS)}.", 400, code="bad_format")
        projects.get(project_id)
        text, media, ext = export.render(agent_sessions.get(project_id, session_id), format)
    except (agent_sessions.AgentSessionError, projects.ProjectError) as e:
        return _fail(e)
    return PlainTextResponse(text, media_type=media, headers={"Content-Disposition": f'attachment; filename="agent-session-{session_id}.{ext}"'})


@router.get("/api/projects/{project_id}/agent/capabilities")
def agent_capabilities(project_id: str):
    """The real round/time caps and tool names of each mode, read from agent_loop's own constants -- one source of truth, so a description shown in the UI never drifts from
    what actually runs."""
    from app.backend.pipeline import agent_loop, agent_tools, projects
    try:
        projects.get(project_id)
    except projects.ProjectError as e:
        return _fail(e)
    names = lambda tools: [t["function"]["name"] for t in tools]
    return {"plan": {"max_rounds": agent_loop.PLAN_MAX_ROUNDS, "tools": names(agent_tools.PLAN_TOOLS)},
            "manual": {"max_rounds": agent_loop.MANUAL_MAX_ROUNDS, "tools": names(agent_tools.TOOLS), "confirm_tools": sorted(agent_loop.CONFIRM_NAMES)},
            "auto": {"max_rounds": agent_loop.AUTO_MAX_ROUNDS, "max_seconds": agent_loop.AUTO_MAX_SECONDS, "tools": names(agent_tools.TOOLS)}}


@router.get("/api/projects/{project_id}/agent/sessions/{session_id}/runs")
def agent_sessions_runs(project_id: str, session_id: str):
    """The runs of a session, newest first: each with its state, why it stopped, the budget it was given and what it has spent. Empty for a session of the legacy engine."""
    from app.backend.pipeline import agent_sessions, projects
    try:
        return {"runs": agent_sessions.runs(project_id, session_id)}
    except (agent_sessions.AgentSessionError, projects.ProjectError) as e:
        return _fail(e)


@router.get("/api/projects/{project_id}/agent/sessions/{session_id}/events")
def agent_sessions_events(project_id: str, session_id: str, after: int = 0, run: str | None = None, limit: int = 500):
    """The journal of a run after place ``after`` (the newest run when none is named), for a page that lost its stream: ``{"run", "records", "next"}``."""
    from app.backend.pipeline import agent_sessions, projects
    try:
        return agent_sessions.run_events(project_id, session_id, run, after, limit)
    except (agent_sessions.AgentSessionError, projects.ProjectError) as e:
        return _fail(e)


@router.delete("/api/projects/{project_id}/agent/sessions/{session_id}")
def agent_sessions_delete(project_id: str, session_id: str):
    from app.backend.pipeline import agent_sessions, projects
    try:
        agent_sessions.delete(project_id, session_id)
        return {"deleted": True}
    except (agent_sessions.AgentSessionError, projects.ProjectError) as e:
        return _fail(e)


@router.post("/api/projects/{project_id}/agent/sessions/{session_id}/run")
def agent_sessions_run(project_id: str, session_id: str, body: RunIn):
    """One run of a session, streamed as lines of JSON (``application/x-ndjson``): a fresh instruction, or nothing to continue after an approved or denied call. A session that is
    busy, or waiting for a decision on a pending call, answers with an ``error`` event rather than refusing the request outright -- the same way a project chat's turn does."""
    from app.backend.pipeline import agent_background, agent_loop, agent_sessions
    stream = agent_background.lines(
        lambda: agent_loop.run(project_id, session_id, instruction=body.instruction, edit_of=body.edit_of, allow_remote=body.allow_remote),
        cancel=lambda: agent_sessions.stop(project_id, session_id))
    return StreamingResponse(stream, media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/api/projects/{project_id}/agent/sessions/{session_id}/approve")
def agent_sessions_approve(project_id: str, session_id: str, body: ApproveIn):
    """Answer the session's one pending call: approve (optionally with edited arguments in place of the model's own) or deny (optionally with the owner's own feedback on what
    should change, in place of the generic refusal sentence). Returns the session to idle; call ``/run`` again to let the model see the result and continue."""
    from app.backend.pipeline import agent_sessions, projects
    try:
        from app.backend.pipeline import agent_loop
        return agent_loop.approve(project_id, session_id, body.call_id, body.decision, edited_arguments=body.edited_arguments, feedback=body.feedback, remember=body.remember)
    except (agent_sessions.AgentSessionError, projects.ProjectError) as e:
        return _fail(e)


@router.post("/api/projects/{project_id}/agent/sessions/{session_id}/cancel")
def agent_sessions_cancel(project_id: str, session_id: str):
    """Ask the run in progress to end at the next round boundary. ``{"stopped": false}`` when nothing is running."""
    from app.backend.pipeline import agent_sessions
    return {"stopped": agent_sessions.stop(project_id, session_id)}


# -- diff and apply: the only routes that can ever write to the real project -------------------------------------------------------------------------------------

@router.get("/api/projects/{project_id}/agent/diff")
def agent_diff(project_id: str):
    """What the project's agent sandbox has changed, against the real project right now (``agent_apply.diff_files``): read-only, nothing is staged or written. No sandbox yet (nothing has run
    in Manual or Auto mode) is an empty list with ``sandbox: false``, not an error: the page asks on every load."""
    from app.backend.pipeline import agent_apply, projects
    from app.backend.pipeline.scan import agent_sandbox
    try:
        return {"files": agent_apply.diff_files(projects.get(project_id))}
    except agent_sandbox.AgentSandboxError as e:
        if e.code == "no_sandbox":
            return {"files": [], "sandbox": False}
        return _fail(e)
    except projects.ProjectError as e:
        return _fail(e)


@router.get("/api/projects/{project_id}/agent/applies")
def agent_applies_list(project_id: str):
    from app.backend.pipeline import agent_apply, projects
    try:
        return {"applies": agent_apply.listing(project_id)}
    except projects.ProjectError as e:
        return _fail(e)


@router.post("/api/projects/{project_id}/agent/applies")
def agent_applies_stage(project_id: str):
    """Stage a candidate from the sandbox's diff right now: the new content of each changed or added file is read once and kept, so what is applied later is exactly what was
    reviewed. Nothing in the real project changes yet."""
    from app.backend.pipeline import agent_apply, projects
    from app.backend.pipeline.scan import agent_sandbox
    try:
        return agent_apply.stage(projects.get(project_id))
    except (projects.ProjectError, agent_sandbox.AgentSandboxError) as e:
        return _fail(e)


@router.get("/api/projects/{project_id}/agent/applies/{apply_id}")
def agent_applies_get(project_id: str, apply_id: str):
    from app.backend.pipeline import agent_apply, projects
    try:
        projects.get(project_id)
        return agent_apply.get(project_id, apply_id)
    except projects.ProjectError as e:
        return _fail(e)


@router.delete("/api/projects/{project_id}/agent/applies/{apply_id}")
def agent_applies_discard(project_id: str, apply_id: str):
    from app.backend.pipeline import agent_apply, projects
    try:
        agent_apply.discard(project_id, apply_id)
        return {"discarded": True}
    except projects.ProjectError as e:
        return _fail(e)


@router.post("/api/projects/{project_id}/agent/applies/{apply_id}/apply")
def agent_applies_apply(project_id: str, apply_id: str, body: ApplyConfirmIn):
    """Write the candidate's files into the project's real folders. ``files`` must be exactly the list the candidate names, which is what the person confirmed; every file must
    still be the file the candidate was staged from, or nothing is written."""
    from app.backend.pipeline import agent_apply, projects
    try:
        return agent_apply.apply(projects.get(project_id), apply_id, body.files, body.delete)
    except projects.ProjectError as e:
        return _fail(e)


@router.post("/api/projects/{project_id}/agent/approve-all")
def agent_approve_all(project_id: str, body: ApproveAllIn):
    """The page's "Approve and apply all": stage what the sandbox copy has changed right now and write all of it into the real folders in one step, all or nothing. A removal is
    applied only when it is named in ``delete``. Refused while a session of the project is running, and when the sandbox no longer matches what was reviewed (``reviewed``).
    Returns the applied candidate (its id is what Undo uses)."""
    from app.backend.pipeline import agent_apply, agent_sessions, projects
    from app.backend.pipeline.scan import agent_sandbox
    try:
        project = projects.get(project_id)
        if agent_sessions.project_busy(project_id):
            raise agent_sessions.AgentSessionError("A session of this project is still running, so what would be applied is still changing. Stop it or wait for it to finish.", 409, "busy")
        return agent_apply.approve_all(project, body.delete, [r.model_dump() for r in body.reviewed] if body.reviewed is not None else None, body.skip)
    except (projects.ProjectError, agent_sandbox.AgentSandboxError, agent_sessions.AgentSessionError) as e:
        return _fail(e)


@router.post("/api/projects/{project_id}/agent/discard-changes")
def agent_discard_changes(project_id: str):
    """Put the sandbox copy back to the project as it was when it was copied. The state before is kept as a checkpoint. Refused while a session of the project is running."""
    from app.backend.pipeline import agent_sessions, projects
    from app.backend.pipeline.scan import agent_sandbox
    try:
        projects.get(project_id)
        if agent_sessions.project_busy(project_id):
            raise agent_sessions.AgentSessionError("A session of this project is still running. Stop it first.", 409, "busy")
        return agent_sandbox.discard_changes(project_id)
    except (projects.ProjectError, agent_sandbox.AgentSandboxError, agent_sessions.AgentSessionError) as e:
        return _fail(e)


@router.post("/api/projects/{project_id}/agent/refresh")
def agent_refresh(project_id: str):
    """Bring into the sandbox copy what you changed in the real project since the copy was made, and make that the new baseline. Files changed on both sides are left as the session
    has them and listed. Refused while a session of the project is running."""
    from app.backend.pipeline import agent_apply, agent_sessions, projects
    from app.backend.pipeline.scan import agent_sandbox
    try:
        project = projects.get(project_id)
        if agent_sessions.project_busy(project_id):
            raise agent_sessions.AgentSessionError("A session of this project is still running. Stop it first.", 409, "busy")
        return agent_apply.refresh(project)
    except (projects.ProjectError, agent_sandbox.AgentSandboxError, agent_sessions.AgentSessionError) as e:
        return _fail(e)


@router.post("/api/projects/{project_id}/agent/applies/{apply_id}/revert")
def agent_applies_revert(project_id: str, apply_id: str):
    """Put the originals back, if the files are still exactly as this apply wrote them."""
    from app.backend.pipeline import agent_apply, projects
    try:
        return agent_apply.revert(projects.get(project_id), apply_id)
    except projects.ProjectError as e:
        return _fail(e)


# -- checkpoint and rollback: inside the sandbox only, never the real project ------------------------------------------------------------------------------

@router.post("/api/projects/{project_id}/agent/checkpoint")
def agent_checkpoint(project_id: str, body: CheckpointIn):
    """Commit the current state of every root of the project's agent sandbox as a checkpoint. Symmetric with the model's own ``checkpoint`` tool."""
    from app.backend.pipeline.scan import agent_sandbox
    try:
        return agent_sandbox.checkpoint(project_id, body.message)
    except agent_sandbox.AgentSandboxError as e:
        return _fail(e)


@router.get("/api/projects/{project_id}/agent/checkpoints")
def agent_checkpoints(project_id: str):
    from app.backend.pipeline.scan import agent_sandbox
    try:
        return {"checkpoints": agent_sandbox.checkpoints(project_id)}
    except agent_sandbox.AgentSandboxError as e:
        return _fail(e)


@router.post("/api/projects/{project_id}/agent/rollback")
def agent_rollback(project_id: str, body: RollbackIn):
    """Reset one root of the sandbox to an earlier checkpoint, discarding anything written since. Human-only: there is no model tool for this."""
    from app.backend.pipeline.scan import agent_sandbox
    try:
        return agent_sandbox.rollback(project_id, body.root, body.hash)
    except agent_sandbox.AgentSandboxError as e:
        return _fail(e)


@router.get("/api/projects/{project_id}/agent/sandbox")
def agent_sandbox_status(project_id: str):
    """Whether this project has an agent sandbox at all, and its container id only while that container is actually running (``DeviceStrip``'s own stats chip asks this; never an
    error for "no sandbox yet", since that is the ordinary case for most projects)."""
    from app.backend import sandbox
    from app.backend.pipeline.scan import agent_sandbox
    try:
        doc = agent_sandbox.record(project_id)
    except agent_sandbox.AgentSandboxError:
        return {"running": False, "container": None}
    try:
        ct = sandbox._ours(doc["container"])
        ct.reload()
        running = ct.status == "running"
    except Exception:  # ignored: a container that is gone or a daemon that is not answering is reported as not running
        running = False
    return {"running": running, "container": doc["container"] if running else None}


# -- the models an agent session can use: the one the settings name, and those of the providers that were added ----------------------------------------------------------------

def _profile_summary(profile) -> dict | None:
    from app.backend.pipeline.agentic import thinking
    return None if profile is None else {"protocol": profile.protocol, "source": profile.source, "family": profile.family, "reasoning": profile.reasoning.kind, "tool_format": profile.tool_format,
                                         "native_tools": profile.native_tools, "max_output": profile.max_output, "context": profile.context, "notes": profile.notes,
                                         "efforts": thinking.levels(profile.reasoning.kind), "thinks_itself": thinking.native(profile.reasoning.kind, profile.protocol)}


def _default_model() -> dict:
    """The model the settings name (a session that names none uses it), with what is known of it."""
    from app.backend.pipeline import agent_loop, llm_client, project_chat
    from app.backend.pipeline.agentic.profiles import local
    r = llm_client.resolved()
    where = project_chat.endpoint()
    profile = local.known(r.local_model_file, agent_loop._llama_build()) if r.engine == "local" and r.local_model_file else None
    return {"ref": "", "label": r.model_label or "(none chosen)", "provider": "default", "provider_name": "This computer (Ollama)" if r.engine == "local" else "The external endpoint of Settings",
            "trust": "local" if where["local"] else "remote", "configured": r.configured, "profile": _profile_summary(profile)}


@router.get("/api/agent/providers")
def agent_providers_list():
    """The providers that were added (no key or header value is ever returned) and the model the settings name."""
    from app.backend.pipeline.agentic.providers import registry
    try:
        return {"providers": registry.listing(), "default": _default_model()}
    except registry.ProviderError as e:
        return _fail(e)


@router.put("/api/agent/providers/{provider_id}")
def agent_providers_put(provider_id: str, body: ProviderIn):
    """Add a provider or change one. A key that is not sent stays as it is."""
    from app.backend.pipeline.agentic.providers import registry
    try:
        return registry.upsert({"id": provider_id, **body.model_dump(exclude_unset=True)})
    except registry.ProviderError as e:
        return _fail(e)


@router.delete("/api/agent/providers/{provider_id}")
def agent_providers_delete(provider_id: str):
    from app.backend.pipeline.agentic.providers import registry
    try:
        registry.remove(provider_id)
        return {"deleted": True}
    except registry.ProviderError as e:
        return _fail(e)


@router.post("/api/agent/providers/{provider_id}/test")
def agent_providers_test(provider_id: str):
    """Asks the provider for the models it serves: whether the address and the key work."""
    from app.backend.pipeline.agentic.providers import registry
    try:
        return registry.check_connection(provider_id)
    except registry.ProviderError as e:
        return _fail(e)


@router.get("/api/agent/models")
def agent_models():
    """Every model a session can be given, for the choice beside the mode: the settings' own first (an empty ref), then the models each provider lists, with what is known of each."""
    from app.backend.pipeline.agentic.providers import registry
    try:
        models = [_default_model()]
        for p in registry.listing():
            for m in p["models"]:
                handle = registry.resolve(f"{p['id']}/{m['id']}")
                models.append({"ref": handle.ref, "label": handle.label, "provider": p["id"], "provider_name": p["name"], "trust": handle.trust, "configured": True,
                               "profile": _profile_summary(registry.profile_for(handle))})
        return {"models": models}
    except registry.ProviderError as e:
        return _fail(e)


@router.post("/api/agent/models/probe")
def agent_models_probe(body: ProbeIn):
    """Runs the live test of how a model takes tools now, keeps what it found and returns it (a model on this computer needs its server to be up; a provider's model costs a few small
    requests)."""
    import urllib.parse
    from app.backend.pipeline import agent_loop, llm_client
    from app.backend.pipeline.agentic.profiles import local
    from app.backend.pipeline.agentic.providers import registry
    try:
        if body.ref:
            return {"profile": _profile_summary(registry.probe_model(registry.resolve(body.ref)))}
        r = llm_client.resolved()
        if not r.configured:
            return errors.error(llm_client.MISSING, 409, code="no_model")
        client, model = llm_client.configured(timeout=600.0)
        if r.engine == "local" and r.local_model_file:
            profile = local.for_model(r.local_model_file, client, model, llama_build=agent_loop._llama_build(), run_probe=True, force=True)
            return {"profile": _profile_summary(profile)} if profile else errors.error("The model file could not be read.", 422, code="unreadable")
        host = urllib.parse.urlparse(r.base_url).hostname or ""
        handle = registry.Handle(ref=f"default/{model}", provider_id="default", provider_name="The external endpoint of Settings", model=model, client=client, base_url=r.base_url, host=host,
                                 trust=registry.trust_of(host), label=r.model_label, entry={})
        return {"profile": _profile_summary(registry.probe_model(handle))}
    except registry.ProviderError as e:
        return _fail(e)


# -- what the agent may do without asking: the owner's rules, a project's rules and a session's grants (agentic/permissions.py) --------------------------------------------------

class RulesIn(BaseModel):
    rules: list[dict] = Field(default_factory=list, max_length=200)


def _rules_out(rules) -> dict:
    return {"rules": [r.to_dict() for r in rules]}


@router.get("/api/agent/permissions")
def agent_permissions_get():
    """The owner's own rules for every project (each ``{tool, pattern, action}``; the last matching one wins, over the mode's defaults)."""
    from app.backend.pipeline.agentic import permissions
    return _rules_out(permissions.global_rules())


@router.put("/api/agent/permissions")
def agent_permissions_put(body: RulesIn):
    from app.backend.pipeline.agentic import permissions
    try:
        return _rules_out(permissions.save_global(body.rules))
    except ValueError as e:
        return errors.error(str(e), 422)


@router.get("/api/projects/{project_id}/agent/permissions")
def agent_project_permissions_get(project_id: str):
    from app.backend.pipeline import projects
    from app.backend.pipeline.agentic import permissions
    try:
        return _rules_out(permissions.project_rules(project_id))
    except projects.ProjectError as e:
        return _fail(e)


@router.put("/api/projects/{project_id}/agent/permissions")
def agent_project_permissions_put(project_id: str, body: RulesIn):
    from app.backend.pipeline import projects
    from app.backend.pipeline.agentic import permissions
    try:
        return _rules_out(permissions.save_project(project_id, body.rules))
    except projects.ProjectError as e:                  # first: a missing project is a ValueError too
        return _fail(e)
    except ValueError as e:
        return errors.error(str(e), 422)


@router.delete("/api/projects/{project_id}/agent/sessions/{session_id}/grants")
def agent_session_grants_clear(project_id: str, session_id: str):
    """Forget what "Allow always" added for this session."""
    from app.backend.pipeline import agent_sessions, projects
    try:
        return agent_sessions.clear_grants(project_id, session_id)
    except (agent_sessions.AgentSessionError, projects.ProjectError) as e:
        return _fail(e)


# -- the owner's MCP servers: programs that give the agent more tools, run in the project's sandbox (agentic/mcp) ---------------------------------------------------------------------------

class McpIn(BaseModel):
    servers: dict = Field(default_factory=dict)             # name -> {command, env, enabled, timeout}; a variable sent as null keeps its saved value


class McpCheckIn(BaseModel):
    project_id: str


@router.get("/api/agent/mcp")
def agent_mcp_get():
    """The servers that are set up. A variable's value is never sent, only its name."""
    from app.backend.pipeline.agentic.mcp import config
    return {"servers": config.public(), "limits": {"servers": config.MAX_SERVERS, "timeout": config.DEFAULT_TIMEOUT}}


@router.put("/api/agent/mcp")
def agent_mcp_put(body: McpIn):
    from app.backend.pipeline.agentic.mcp import config, manager
    try:
        kept = config.save(body.servers)
    except config.ConfigError as e:
        return errors.error(str(e), 422)
    manager.forget()                                         # every container asks its bridge again with the new definitions
    return {"servers": config.public(kept), "limits": {"servers": config.MAX_SERVERS, "timeout": config.DEFAULT_TIMEOUT}}


@router.post("/api/agent/mcp/check")
def agent_mcp_check(body: McpCheckIn):
    """Check servers only inside a sandbox already owned by executing chat work."""
    from app.backend.pipeline import projects
    from app.backend.pipeline.agentic.mcp import manager
    from app.backend.pipeline.scan import agent_image, agent_sandbox
    try:
        projects.get(body.project_id)
        doc = agent_sandbox.record(body.project_id)
        from app.backend import sandbox_tasks
        sandbox_tasks.require_container(doc['container'])
    except PermissionError as error:
        return errors.error(str(error), 409)
    except (projects.ProjectError, agent_sandbox.AgentSandboxError, agent_image.AgentImageMissing) as e:
        return _fail(e)
    found = manager.listing(doc["container"], refresh=True)
    return {"servers": found.servers, "tools": [{"name": n, "server": t["server"], "tool": t["tool"]} for n, t in found.tools.items()]}


# -- the command tools of the agent's terminal (rtk and ripgrep): what is installed, installing and testing them (agentic/exec/toolchain.py) ----------------------------------------------

@router.get("/api/agent/toolchain")
def agent_toolchain_status():
    """For each command tool: what is pinned, whether this computer has a build for it, whether a tested copy is installed, and its licence. Nothing is downloaded by asking."""
    from app.backend.pipeline.agentic.exec import toolchain
    return toolchain.status()


@router.post("/api/agent/toolchain/{tool}/install")
def agent_toolchain_install(tool: str):
    """Download the pinned release of a tool (about 2 to 6 MB), check it against the pinned size and SHA-256, unpack the program, test it and keep it. The owner's own action: the page asks first."""
    from app.backend.pipeline.agentic.exec import toolchain
    try:
        if tool not in toolchain.load_lock()["tools"]:
            return errors.error(f"There is no command tool called {tool}.", 404)
        toolchain.install(tool)
        return toolchain.status()
    except toolchain.ToolchainError as e:
        return errors.error(str(e), 422)
    except OSError as e:
        return errors.error(f"{tool} could not be downloaded or kept: {e}", 502)


@router.post("/api/agent/toolchain/{tool}/test")
def agent_toolchain_test(tool: str):
    """Run the self-test of an installed tool again (a copy that no longer passes is not used)."""
    from app.backend.pipeline.agentic.exec import toolchain
    try:
        if tool not in toolchain.load_lock()["tools"]:
            return errors.error(f"There is no command tool called {tool}.", 404)
        toolchain.retest(tool)
        return toolchain.status()
    except toolchain.ToolchainError as e:
        return errors.error(str(e), 422)


@router.get("/api/projects/{project_id}/agent/sessions/{session_id}/outputs/{handle}")
def agent_session_output(project_id: str, session_id: str, handle: str, start: int = 1, lines: int | None = None, grep: str | None = None):
    """A part of the whole output of a command that was cut (the page's "Load all lines" and the ``recall`` tool read the same file, masked again as it is read)."""
    from app.backend.pipeline import agent_sessions, projects
    from app.backend.pipeline.agentic import outputs
    try:
        agent_sessions.get(project_id, session_id)
        return outputs.load(project_id, session_id, handle, start=start, lines=lines, grep=grep)
    except outputs.OutputError as e:
        return errors.error(str(e), 404)
    except (agent_sessions.AgentSessionError, projects.ProjectError) as e:
        return _fail(e)
