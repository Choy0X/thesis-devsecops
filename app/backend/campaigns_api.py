"""Routes of the campaigns (``/api/campaigns``): list, start, check a plan, pause, resume, delete, and read one campaign and its story.

A campaign is one job process (stage ``campaign_run``) working through the stages of each of its ecosystems. Starting one is refused, with the reasons and the numbers, when the plan makes no
sense for the data (422) or the machine cannot take it beside the campaigns already running (409); nothing is stored for a refused start. Pausing asks the process to stop at a safe point;
resuming runs it again from where it was; deleting removes the campaign's records and keeps everything it produced.

The literal paths (``options``, ``resources``, ``validate``) come before ``/{campaign_id}``.
"""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from app.backend import errors

router = APIRouter(prefix="/api/campaigns")
_ADMIT = threading.Lock()                       # one start or resume is judged at a time, so two cannot both fit the same room


def context_filter_query(request: Request, language: str = '', granularity: str = '', status: str = '', search: str = '',
                         cwe: str = '', exclude_tests: bool = False, usable_only: bool = False,
                         context_quality: str = '', draft_quality: str = ''):
    from app.backend.pipeline.acceleration.context_filters import normalize
    allowed = {'language', 'granularity', 'status', 'search', 'cwe', 'exclude_tests', 'usable_only', 'context_quality', 'draft_quality'}
    allowed |= {'offset', 'limit'} if request.url.path.endswith('/context-items') else {'selection_limit'}
    unknown = set(request.query_params) - allowed
    duplicate = [key for key in request.query_params if len(request.query_params.getlist(key)) != 1]
    if unknown or duplicate:
        raise HTTPException(422, 'Unsupported or repeated context query fields: ' + ', '.join(sorted(unknown | set(duplicate))))
    try:
        return normalize(dict(language=language, granularity=granularity, status=status, search=search,
                              cwe=cwe, exclude_tests=exclude_tests, usable_only=usable_only,
                              context_quality=context_quality, draft_quality=draft_quality))
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get('/{campaign_id}/context-items')
def context_items(campaign_id: str, offset: int = 0, limit: int = 100, filters: dict = Depends(context_filter_query)):
    from app.backend.pipeline import context_exchange, derived
    from app.backend.pipeline.campaigns import repo
    with derived.connect() as conn:
        if not repo.get(conn, campaign_id):
            return errors.error('Unknown campaign', 404)
    from app.backend.pipeline.acceleration.context_gate import list_items
    return list_items(campaign_id, offset=offset, limit=limit, filters=filters)


@router.get('/{campaign_id}/sources')
def campaign_sources(campaign_id: str, offset: int = 0, limit: int = 100):
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import source_resolution
    with derived.connect() as conn:
        try:
            return source_resolution.listing(conn, campaign_id, offset, limit)
        except LookupError as error:
            return errors.error(str(error), 404)


class SourceResolutionIn(BaseModel):
    model_config = {'extra': 'forbid'}
    action: str
    commit_keys: list[str]


@router.get('/{campaign_id}/context-item-ids')
def campaign_context_item_ids(campaign_id: str, filters: dict = Depends(context_filter_query),
                              selection_limit: int | None = Query(None, ge=1, le=100000)):
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import repo
    from app.backend.pipeline.acceleration.context_gate import item_ids
    with derived.connect() as conn:
        if not repo.get(conn, campaign_id):
            return errors.error('Unknown campaign', 404)
        return item_ids(campaign_id, filters=filters, selection_limit=selection_limit)


@router.post('/{campaign_id}/sources/resolve')
def campaign_resolve_sources(campaign_id: str, body: SourceResolutionIn):
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import source_resolution
    with _ADMIT, derived.connect() as conn:
        try:
            result = source_resolution.resolve(conn, campaign_id, body.action, body.commit_keys)
            result['campaign'] = _public(result['campaign'])
            return result
        except LookupError as error:
            return errors.error(str(error), 404)
        except ValueError as error:
            return errors.error(str(error), 422)
        except RuntimeError as error:
            return errors.error(str(error), 409)


class PlanIn(BaseModel):
    name: str | None = None
    ecosystems: list[str] = []
    preset: str | None = None
    stages: dict[str, bool] | None = None
    knobs: dict | None = None
    build_profile: str | None = None
    context: dict | None = None
    compute: dict | None = None
    mining: dict | None = None

    @field_validator('mining')
    @classmethod
    def valid_mining(cls, value):
        from app.backend.pipeline.acceleration.mining_policy import normalize
        return normalize(value)

    @field_validator('compute')
    @classmethod
    def valid_compute(cls, value):
        if value is not None and value.get('mode', 'cpu') not in ('cpu', 'cuda'):
            raise ValueError('Choose CPU or CUDA for the compute mode')
        return value


def _refused(message: str, status: int, reasons: list[dict], *, hint: str | None = None, code: str | None = None) -> JSONResponse:
    return JSONResponse({**errors.body(message, status, hint=hint, code=code), "reasons": reasons}, status_code=status)


def _public(c: dict, *, alive: bool | None = None) -> dict:
    """A campaign as the API shows it."""
    out = {k: c[k] for k in ("campaign_id", "name", "state", "created_at", "started_at", "finished_at", "heartbeat_ts", "control", "job_id", "pid", "resumes", "error", "ecosystems",
                             "progress", "summary", "declared", "legacy", "paused_reason")}
    out["plan"] = c.get("plan")
    if alive is not None:
        out["alive"] = alive
    return out


def _judge(conn, plan: dict, *, self_id: str | None = None) -> dict:
    """The plan against the data (preflight) and against the machine (admission): ``{"plan_errors", "machine_errors", "warnings", "per_ecosystem", "needs", "free"}``."""
    from app.backend.pipeline import codestore, store
    from app.backend.pipeline.campaigns import admission, preflight, targets
    local = targets.local_targets(conn)
    pre = preflight.check(plan, local=local, records=lambda e: store.count_mineable_records(ecosystems=[e]), pending=lambda e: codestore.count_mineable(ecosystems=[e]),
                          llm_check=_llm_check())
    adm = admission.check(plan, admission.gather_facts(conn, plan["ecosystems"]), self_id=self_id)
    if (plan.get('compute') or {}).get('mode') == 'cuda':
        from app.backend import compute
        capability = compute.gpu_info()
        if not capability.get('available'):
            pre['errors'].append({'code': 'cuda_configuration', 'text': capability.get('reason') or 'CUDA is unavailable', 'numbers': {}})
    return {"plan_errors": pre["errors"], "machine_errors": adm["errors"], "warnings": pre["warnings"] + adm["warnings"], "per_ecosystem": pre["per_ecosystem"], "needs": adm["needs"],
            "free": adm["free"]}


def _llm_check():
    """The check of the model that writes the context, if the Enrich stage has one (until it does, a plan that uses it is refused as not available)."""
    from app.backend.pipeline.campaigns import stages
    return getattr(stages, "model_check", None)


def _normalize(body: PlanIn) -> dict:
    from app.backend.pipeline.campaigns import plan as planmod
    raw=body.model_dump()
    mining=raw.get('mining') or {}
    if mining.get('version')==4:
        raw['mining']={**mining,'acquisition_version':mining.get('acquisition_version',1)}
    return planmod.normalize(raw)


@router.get('/compute/resources')
def compute_resources():
    from app.backend import compute, device
    from app.backend.pipeline.gateway import client
    status = client.status() or {}
    owner = status.get('exclusive')
    if not owner:
        owner = next((row for row in status.get('consumers', []) if (status.get('slots', {}).get(row['role']) or {}).get('gpu_bytes')), None)
    if not owner:
        for role, slot in status.get('slots', {}).items():
            if slot.get('gpu_bytes') or slot.get('in_flight'):
                owner = {'owner': role, 'model': slot.get('model')}
                break
    from app.backend.pipeline.acceleration.runtime import Runtime
    return {'modes': ['cpu', 'cuda'], 'policy_version': 2, 'gpu': device.gpus(), 'owner': owner, 'calibration': Runtime({'mode': 'auto'}).benchmarks,
            'scheduler_available': bool(status), 'cuda_runtime': bool(compute.gpu_python()), 'context_cpu_fallback': False}


@router.post('/compute/stop-owner')
def stop_compute_owner(body: dict):
    from app.backend import jobs
    from app.backend.pipeline.gateway import client
    status = client.status() or {}
    owner = body.get('owner')
    exclusive = status.get('exclusive') or {}
    consumer = next((row for row in status.get('consumers', []) if row['owner'] == owner), {})
    owned = exclusive if owner == exclusive.get('owner') else consumer
    if owner and owned:
        import psutil
        for job in jobs.running():
            try:
                pids = [job.proc.pid, *[p.pid for p in psutil.Process(job.proc.pid).children(recursive=True)]] if job.proc else []
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pids = []
            if owned.get('pid') in pids:
                jobs.cancel(job.id)
                return {'requested': True, 'released': False, 'job_id': job.id}
        if str(owner).startswith('chat-view:'):
            client.consumer(owner, active=False)
            return {'requested': True, 'released': not bool((client.status() or {}).get('slots', {}).get('llm', {}).get('gpu_bytes'))}
        return errors.error('The GPU owner is outside the job registry. Stop it from its service; no unrelated process was terminated.', 409)
    if owner in ('llm', 'embed') and owner in status.get('slots', {}):
        try:
            client.unload(owner, wait=0, timeout=5)
            return {'requested': True, 'released': True}
        except Exception:
            return errors.error('The model is still serving an active request. Wait for it to finish, then retry.', 409)
    return errors.error('GPU ownership changed. Refresh before stopping a service.', 409)


@router.post('/compute/chat-view')
def chat_view_gpu(body: dict):
    import os
    import re
    from app.backend.pipeline.gateway import client
    owner = str(body.get('owner') or '')
    if not re.fullmatch(r'chat-view:[a-f0-9-]{32,36}', owner) or type(body.get('active')) is not bool:
        return errors.error('A chat view identity and active state are required', 422)
    try:
        return client.consumer(owner, active=body['active'], pid=os.getpid(), ttl=45)
    except Exception:
        return errors.error('The local model scheduler is unavailable', 503)


@router.post('/{campaign_id}/compute-policy')
def migrate_compute_policy(campaign_id: str, body: dict):
    from app.backend import jobs
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import repo, plan as planmod
    with _ADMIT, derived.connect() as conn:
        campaign = repo.get(conn, campaign_id)
        if not campaign:
            return errors.error('Unknown campaign', 404)
        job = jobs.get(campaign.get('job_id')) if campaign.get('job_id') else None
        if campaign['state'] not in ('paused', 'interrupted', 'partial', 'failed') or (job and job.status == 'running'):
            return errors.error('Pause the campaign and wait for its job to finish before changing the compute policy', 409)
        try:
            updated = planmod.normalize({**campaign['plan'], 'mining':campaign['plan'].get('mining') or {'version':1},
                                        'compute': {'mode': body.get('mode', 'cpu')}})
        except ValueError as error:
            return errors.error(str(error), 422)
        repo.update(conn, campaign_id, plan=updated)
        repo.add_event(conn, campaign_id, 'compute_policy_changed', {'mode': updated['compute']['mode'], 'policy_version': 2, 'workflow_version': 2})
        return {'plan': updated, 'resume_required': True}


@router.post('/{campaign_id}/mining-policy')
def migrate_mining_policy(campaign_id: str, body: dict):
    from app.backend import jobs
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import repo
    from app.backend.pipeline.acceleration.mining_policy import normalize
    with _ADMIT, derived.connect() as conn:
        campaign = repo.get(conn, campaign_id)
        if not campaign: return errors.error('Unknown campaign', 404)
        job = jobs.get(campaign.get('job_id')) if campaign.get('job_id') else None
        if campaign['state'] not in ('paused','interrupted','partial','failed') or (job and job.status == 'running'):
            return errors.error('Pause the campaign and wait for its job to finish before changing mining', 409)
        try:
            if set(body)-{'version','mine_workers','parser_workers','acquisition_version'}:
                raise ValueError('Unknown mining policy fields')
            workers = body.get('mine_workers', 4)
            if type(workers) is not int or not 1 <= workers <= 4:
                raise ValueError('Choose 1 to 4 concurrent commits')
            policy = normalize({'version':body.get('version',2), 'parser_workers':body.get('parser_workers',1),
                                **({'acquisition_version':body['acquisition_version']} if 'acquisition_version' in body else {})})
            if policy['version'] not in (2,3,4): raise ValueError('Migration requires mining version 2, experimental 3 or adaptive 4')
        except ValueError as error:
            return errors.error(str(error), 422)
        if policy['version'] in (3,4):
            from app.backend import paths
            import json
            profile = paths.interim_dir() / 'acceleration' / 'campaigns' / campaign_id / 'performance.json'
            try:
                execution = json.loads(profile.read_text(encoding='utf-8')).get('execution') or {}
            except FileNotFoundError:
                execution = {}
            except (OSError, ValueError):
                return errors.error('Cannot confirm previous resource cleanup; retry after reconciling the campaign',409)
            if str(execution.get('owner') or '').startswith('campaign:'+campaign_id+':') and execution.get('phase') != 'released':
                return errors.error('Wait for confirmed campaign resource cleanup before migrating mining',409)
        updated = {**campaign['plan'], 'mining':policy,
                   'knobs':{**campaign['plan']['knobs'], 'mine_workers':workers,
                            **({'pause_seconds':0} if policy['version']==4 else {})}}
        repo.update(conn, campaign_id, plan=updated)
        repo.add_event(conn, campaign_id, 'mining_policy_changed', {'policy':policy,'mine_workers':workers})
        return {'plan':updated, 'resume_required':True, 'experimental':policy['version'] in (3,4)}


@router.get('/{campaign_id}/performance')
def campaign_performance(campaign_id: str):
    from app.backend import paths
    from app.backend.pipeline.acceleration.context_gate import campaign
    import json
    if not campaign(campaign_id):
        return errors.error('Unknown campaign', 404)
    path = paths.interim_dir() / 'acceleration' / 'campaigns' / campaign_id / 'performance.json'
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {'schema': 'campaign-performance/1', 'counts': {}, 'timings': {}, 'note': 'No performance samples recorded yet.'}


# -- reading ---------------------------------------------------------------------------------------------------------------------

@router.get("")
def campaigns_list(limit: int = 100, offset: int = 0, search: str = '', state: str = ''):
    """Every campaign, newest first, with its state and progress. Campaigns whose process is gone are marked interrupted first."""
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import control, repo
    with derived.connect() as conn:
        control.reconcile(conn)
        page = repo.campaign_page(conn, limit=limit, offset=offset, search=search, state=state)
        rows = page['campaigns']
        return {**page, "campaigns": [_public(c) for c in rows], "interrupted": [c["campaign_id"] for c in rows if c["state"] == "interrupted"], "owners": repo.owners(conn)}


@router.get("/options")
def campaigns_options():
    """What the start dialog offers: the dumps on this computer, the presets, the stages, the knobs, the dataset build profiles and the context depth profiles."""
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import plan as planmod, preflight, targets
    from app.backend.pipeline.ingest import build_code_dataset
    names = {"ingest_only": "Ingest only", "collect": "Collect", "collect_verify": "Collect and verify", "full": "Full", "full_context": "Full collection and manual context review"}
    with derived.connect() as conn:
        found = targets.local_targets(conn)
    return {"targets": found, "presets": [{"id": k, "name": names[k], "stages": [s for s in v if s != 'enrich']} for k, v in planmod.PRESETS.items()],
            "stages": [{"id": s, "label": planmod.STAGE_LABELS[s], "scope": "campaign" if s in planmod.GLOBAL_STAGES else "ecosystem"} for s in planmod.STAGE_ORDER if s != 'enrich'],
            "knobs": planmod.DEFAULT_KNOBS, "knob_limits": preflight.KNOB_LIMITS, "build_profiles": [{"id": p["id"], "name": p["name"], "description": p["description"]} for p in build_code_dataset.PROFILES],
            "context_profiles": list(planmod.CONTEXT_PROFILES), "context_defaults": planmod.CONTEXT_DEFAULTS, "context_workers": list(planmod.CONTEXT_WORKERS), "default_preset": "collect"}


@router.get("/resources")
def campaigns_resources():
    """The room on this computer and what the campaigns running use of it (for the meters in the start dialog)."""
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import admission, repo
    with derived.connect() as conn:
        f = admission.gather_facts(conn)
        running = [c for c in repo.all_campaigns(conn, current_only=True) if c["state"] in ("running", "pausing")]
    used = {"cpu": sum(int((c["declared"] or {}).get("cpu", 1)) for c in running), "ram_gb": round(sum(float((c["declared"] or {}).get("ram_gb", 0)) for c in running), 2),
            "github_per_hour": sum(int((c["declared"] or {}).get("github_per_hour", 0)) for c in running)}
    return {"cores": f.physical_cores, "cpu_cap": max(1, f.physical_cores - 1), "ram_total_gb": f.ram_total_gb, "ram_free_gb": f.ram_free_gb,
            "ram_cap_gb": round(admission._limit("ram_share") * f.ram_total_gb, 1), "disk_free_gb": f.disk_free_gb, "jobs_running": f.jobs_running, "jobs_max": f.jobs_max,
            "github_token": f.github_token, "github_per_hour": f.requests_per_hour, "used": used,
            "campaigns": [{"campaign_id": c["campaign_id"], "name": c["name"], "state": c["state"], "declared": c["declared"]} for c in running]}


@router.post("/validate")
def campaigns_validate(body: PlanIn):
    """Judge a plan without starting it: what is wrong with it, what looks odd, which stages run for each ecosystem, and whether the machine can take it now."""
    from app.backend.pipeline import derived
    plan = _normalize(body)
    with derived.connect() as conn:
        v = _judge(conn, plan)
    errs = [{**e, "source": "plan"} for e in v["plan_errors"]] + [{**e, "source": "machine"} for e in v["machine_errors"]]
    return {"ok": not errs, "plan": plan, "errors": errs, "warnings": v["warnings"], "per_ecosystem": v["per_ecosystem"], "needs": v["needs"], "free": v["free"]}


@router.get("/{campaign_id}")
def campaign_get(campaign_id: str):
    from app.backend import jobs
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import control, plan as planmod, repo, targets
    with derived.connect() as conn:
        control.reconcile(conn)
        c = repo.get(conn, campaign_id)
        if c is None:
            return errors.error(f"No campaign {campaign_id}", 404)
        stored = repo.steps(conn, campaign_id)
    plan = c["plan"]
    if plan:
        wanted = [(t, s) for t in c["ecosystems"] for s in targets.stages_for(planmod.target_stages(plan), t)] + [(planmod.TARGET_ALL, s) for s in planmod.global_stages(plan)]
    else:
        wanted = sorted(stored)
    steps = [stored.get(k) or {"target": k[0], "stage": k[1], "status": "pending", "started_at": None, "finished_at": None, "detail": {}, "error": None, "attempts": 0, "progress": {}}
             for k in wanted]
    job = jobs.get(c["job_id"]) if c.get("job_id") else None
    from app.backend.pipeline.acceleration.context_gate import review_status
    return {"campaign": _public(c), "steps": steps, "job": job.public() if job else None,
            "context_review": review_status(c, stored)}


@router.get("/{campaign_id}/events")
def campaign_events(campaign_id: str, after: int = 0, limit: int = 200):
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import repo
    with derived.connect() as conn:
        if repo.get(conn, campaign_id) is None:
            return errors.error(f"No campaign {campaign_id}", 404)
        rows = repo.events(conn, campaign_id, after=after, limit=max(1, min(int(limit), 1000)))
    return {"events": rows, "last": rows[-1]["id"] if rows else after}


@router.get("/{campaign_id}/analytics")
def campaign_analytics(campaign_id: str):
    """The campaign's progress and stage timeline, its errors, the yield from advisory to code pair, its speed, the mix of what it produced and, if it writes context, how far that got."""
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import analytics
    with derived.connect() as conn:
        out = analytics.cached(conn, campaign_id)
    return out if out is not None else errors.error(f"No campaign {campaign_id}", 404)


# -- starting, pausing, resuming, deleting ---------------------------------------------------------------------------------------

@router.post("")
def campaign_create(body: PlanIn):
    """Start a campaign now. 422 when the plan cannot work on this data, 409 when the machine cannot take it beside the campaigns that run; nothing is stored then."""
    from app.backend import jobs
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import repo, runconfig
    plan = _normalize(body)
    with _ADMIT, derived.connect() as conn:
        v = _judge(conn, plan)
        if v["plan_errors"]:
            return _refused("The plan cannot run", 422, v["plan_errors"], hint="Fix what is listed and try again.", code="plan_invalid")
        if v["machine_errors"]:
            return _refused("Not enough room to start this campaign", 409, v["machine_errors"], hint="Pause or wait for a running campaign, or choose less.", code="no_room")
        cid = repo.new_id(conn)
        try:
            repo.claim(conn, cid, plan["ecosystems"])
        except repo.Taken as e:
            return _refused(str(e), 409, [{"code": "ecosystem_busy", "text": str(e), "numbers": {"ecosystem": e.ecosystem, "owner": e.owner}}], code="no_room")
        repo.create(conn, cid, name=plan["name"], plan=plan, ecosystems=plan["ecosystems"], declared=v["needs"])
        repo.add_event(conn, cid, "created", {"ecosystems": plan["ecosystems"], "preset": plan["preset"], "needs": v["needs"]})
        try:
            runconfig.write(repo.get(conn, cid))
            job = jobs.start("campaign_run", {"campaign_id": cid, "campaign_name": plan["name"]})
        except Exception:
            repo.delete(conn, cid)
            runconfig.remove(cid)
            raise
        repo.update(conn, cid, job_id=job.id)
        return JSONResponse({"ok": True, "campaign": _public(repo.get(conn, cid)), "job": job.public(), "warnings": v["warnings"]}, status_code=201)


@router.post("/{campaign_id}/pause")
def campaign_pause(campaign_id: str):
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import control, repo
    with derived.connect() as conn:
        control.reconcile(conn)
        c = repo.get(conn, campaign_id)
        if c is None:
            return errors.error(f"No campaign {campaign_id}", 404)
        if c["legacy"]:
            return errors.error("This campaign was made by the earlier kind and cannot be paused here", 409)
        if not control.request_pause(conn, campaign_id):
            return errors.error(f"Campaign {campaign_id} is {c['state']}, so it cannot be paused", 409,
                                hint="Resume it first." if c["state"] in ("paused", "interrupted") else None)
        return {"ok": True, "campaign": _public(repo.get(conn, campaign_id))}


@router.post("/{campaign_id}/resume")
def campaign_resume(campaign_id: str):
    """Run a paused or interrupted campaign again from where it stopped (and, for a partial or failed one, retry its steps that failed). The room is judged again."""
    from app.backend import jobs
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import control, repo, runconfig
    with _ADMIT, derived.connect() as conn:
        control.reconcile(conn)
        c = repo.get(conn, campaign_id)
        if c is None:
            return errors.error(f"No campaign {campaign_id}", 404)
        if c["legacy"]:
            return errors.error("This campaign was made by the earlier kind and cannot be resumed here", 409)
        if c["state"] not in ("paused", "interrupted", "partial", "failed"):
            return errors.error(f"Campaign {campaign_id} is {c['state']}, so it cannot be resumed", 409)
        if (c['plan'].get('compute') or {}).get('version') != 2:
            return errors.error('Choose CPU or CUDA with Apply compute policy before resuming this historical execution policy', 409, code='compute_migration_required')
        v = _judge(conn, c["plan"], self_id=campaign_id)
        if v["plan_errors"]:
            return _refused("The plan cannot run", 422, v["plan_errors"], hint="Fix what is listed and try again.", code="plan_invalid")
        if v["machine_errors"]:
            return _refused("Not enough room to resume this campaign", 409, v["machine_errors"], hint="Pause or wait for a running campaign, or choose less.", code="no_room")
        before = c["state"]
        try:
            repo.claim(conn, campaign_id, c["ecosystems"])
        except repo.Taken as e:
            return _refused(str(e), 409, [{"code": "ecosystem_busy", "text": str(e), "numbers": {"ecosystem": e.ecosystem, "owner": e.owner}}], code="no_room")
        if not control.mark_resumed(conn, campaign_id, expect=(before,)):
            return errors.error(f"Campaign {campaign_id} changed while it was being resumed", 409)
        repo.update(conn, campaign_id, declared=v["needs"])
        try:
            runconfig.write(repo.get(conn, campaign_id))
            job = jobs.start("campaign_run", {"campaign_id": campaign_id, "campaign_name": c["name"]})
        except Exception:
            repo.transition(conn, campaign_id, before, expect="running")
            if before in repo.RELEASING:
                repo.release(conn, campaign_id)
            raise
        repo.update(conn, campaign_id, job_id=job.id)
        return {"ok": True, "campaign": _public(repo.get(conn, campaign_id)), "job": job.public()}


@router.delete("/{campaign_id}")
def campaign_delete(campaign_id: str):
    """Remove a campaign: its steps, its story, its samples, its claim on the ecosystems, its run folder and the record of the job that ran it (so a deleted campaign's job entry does not
    keep showing up, unlabelled, in the general list of jobs long after the campaign itself is gone). Everything it produced (pairs, commits, drafts) stays."""
    from app.backend import jobs
    from app.backend.pipeline import derived
    from app.backend.pipeline.campaigns import control, repo, runconfig
    with derived.connect() as conn:
        control.reconcile(conn)
        c = repo.get(conn, campaign_id)
        if c is None:
            return errors.error(f"No campaign {campaign_id}", 404)
        if c["state"] in ("running", "pausing"):
            return errors.error(f"Campaign {campaign_id} is {c['state']}", 409, hint="Pause it and wait until it has paused before deleting it.")
        repo.delete(conn, campaign_id)
        runconfig.remove(campaign_id)
    if c.get("job_id"):
        jobs.forget(c["job_id"])
    return {"ok": True, "deleted": campaign_id, "kept": "everything the campaign produced: commits, pairs and drafts stay where they are"}
