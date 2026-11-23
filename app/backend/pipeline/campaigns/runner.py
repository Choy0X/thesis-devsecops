"""The runner of one campaign: for each ecosystem, its stages in order, with state that survives a pause, a crash and a power cut.

``run`` is what the campaign's job process does. It reads the campaign (what was chosen is its ``plan``), works through the steps (an ecosystem and a stage each), and writes what it did as
it goes: the step, the events, the progress, a heartbeat and now and then a sample of its counters. Every stage is idempotent and picks up from what is stored, so a campaign that was
paused, killed or cut off simply runs its unfinished step again when it is resumed.

* A step is ``done``, ``skipped`` (nothing to do, with the reason), ``quota`` (mining stopped at the quota; a larger quota continues it), ``paused`` or ``error``. An error ends that
  ecosystem's stages only; the other ecosystems go on, and the campaign ends ``partial`` (or ``failed`` when nothing finished).
* The pause is cooperative: when the app asks (``control``), the stage in progress ends at its next safe point, the step is saved as ``paused``, the campaign is ``paused`` (it keeps its
  ecosystems) and the function returns, which ends the process and frees its memory.
* The campaign claims its ecosystems when it is created (see ``repo.claim``) and lets them go when it ends.
"""
from __future__ import annotations

import time
from contextlib import ExitStack
from typing import Callable

from app.backend.pipeline import derived, stopping, util
from app.backend.pipeline.campaigns import admission, control as ctl, plan as planmod, repo, stages, targets

DONE = ("done", "quota", "skipped")


def run_stage(runner, ctx, on_retry=lambda **event: None):
    """Retry idempotent collection steps, keeping the context generator's own budget."""
    budget = 1 if ctx.stage == 'enrich' else 3
    attempt = 0
    while attempt < budget:
        attempt += 1
        if ctx.should_stop():
            raise stopping.Paused()
        ctx.extra['attempt'] = attempt
        try:
            return dict(runner(ctx) or {})
        except (stopping.Paused, KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            from app.backend.pipeline.acceleration.failures import retryable
            adaptive=(getattr(ctx,'plan',{}).get('mining') or {}).get('version') == 4
            if adaptive and isinstance(error, MemoryError):
                from app.backend.pipeline.acceleration.resource_control import active
                resources=active()
                if resources is None:raise
                error.__traceback__=None
                resources.pressure()
                resources.idle(should_stop=ctx.should_stop,progress=ctx.progress)
                attempt -= 1
                continue
            if attempt == budget or (adaptive and not retryable(error)):
                raise
            on_retry(attempt=attempt, next_attempt=attempt + 1, message=str(error)[:500])


def cause_of(exc: BaseException) -> str:
    """A short name for why a step failed, for the events and the charts: rate_limit, timeout, network, http_4xx, http_5xx, git_failed, or the type of the exception."""
    import requests
    from app.backend.pipeline.ingest import codemine
    if isinstance(exc, codemine.RateLimited):
        return "rate_limit"
    if isinstance(exc, (requests.Timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, requests.HTTPError):
        code = getattr(getattr(exc, "response", None), "status_code", None)
        return f"http_{code // 100}xx" if code else "http"
    if isinstance(exc, requests.RequestException):
        return "network"
    if isinstance(exc, codemine.FetchError):
        return "git_failed"
    return type(exc).__name__


def skippable(step: dict | None, quota) -> bool:
    """Does a stored step need no running now? Done and skipped steps do not; a step that stopped at the quota is done unless the quota is now larger (or gone)."""
    if not step:
        return False
    if (step.get("detail") or {}).get("unresolved_items"):
        return False
    if step["status"] in ("done", "skipped"):
        return True
    if step["status"] == "quota":
        was = (step["detail"] or {}).get("quota")
        return quota is not None and was is not None and int(quota) <= int(was)
    return False


def default_sampler(plan: dict, control: "ctl.Control") -> Callable[[], dict]:
    """What a sample holds: where the campaign is, the commits still queued for its ecosystems, the counters its stage last reported, and its memory."""
    def sample() -> dict:
        out: dict = {"stage": control.progress.get("stage"), "target": control.progress.get("target")}
        from app.backend.pipeline.acceleration.metrics import current as performance
        from app.backend.pipeline import campaign_ctx
        cid = campaign_ctx.current()
        if cid:
            from app.backend import paths
            root = paths.interim_dir() / 'acceleration' / 'campaigns' / cid
            performance.save(root / 'performance.json')
            performance.sample(root / 'resource_history.jsonl', stage=out['stage'])
        out['performance'] = performance.snapshot()
        try:
            import psutil
            out["rss_mb"] = round(psutil.Process().memory_info().rss / 1048576)
        except Exception:  # ignored: memory is a nice-to-have
            pass
        try:
            from app.backend.pipeline import codestore
            out["pending"] = codestore.count_mineable(ecosystems=plan.get("ecosystems") or [])
        except Exception:  # ignored: the queue size is a nice-to-have
            pass
        counts = control.progress.get("counts")
        if isinstance(counts, dict):
            out["counts"] = {k: v for k, v in counts.items() if isinstance(v, (int, float))}
        for k in ("processed", "total"):
            if isinstance(control.progress.get(k), (int, float)):
                out[k] = control.progress[k]
        quota = control.progress.get("quota")
        if isinstance(quota, dict):
            out["quota"] = {k: v for k, v in quota.items() if isinstance(v, (int, float))}
        if control.progress.get("state") in ("rate_limited", "pause"):
            out["waiting"] = True
        return out
    return sample


def _summary(steps: dict, order: list[tuple[str, str]], started: float) -> dict:
    counts = {"total": len(order), "done": 0, "quota": 0, "skipped": 0, "error": 0, "paused": 0, "pending": 0}
    per_target: dict[str, list[str]] = {}
    errors = []
    unresolved = 0
    skipped_sources = 0
    for target, stage in order:
        s = steps.get((target, stage))
        status = s["status"] if s else "pending"
        counts["pending" if status in ("pending", "running") else status] += 1
        per_target.setdefault(target, []).append(status)
        if status == "error":
            errors.append({"target": target, "stage": stage, "error": s["error"]})
        remaining = int(((s or {}).get("detail") or {}).get("unresolved_items", 0))
        unresolved += remaining
        skipped_sources += int(((s or {}).get('detail') or {}).get('skipped_sources', 0))
        if remaining and status != "error":
            errors.append({"target": target, "stage": stage,
                           "error": f"{remaining} source commits remain unresolved; completed evidence and checks are retained."})
    targets = {t: ("error" if "error" in sts else "done" if all(x in DONE for x in sts) else "paused" if "paused" in sts else "pending") for t, sts in per_target.items()}
    return {"steps": counts, "targets": targets, "errors": errors, "seconds": round(time.time() - started),
            **({"unresolved_items": unresolved} if unresolved else {}),
            **({'skipped_sources': skipped_sources} if skipped_sources else {})}


def run(campaign_id: str, *, runners: dict | None = None, progress: Callable[..., None] | None = None, conn=None, control: "ctl.Control | None" = None) -> dict:
    """Work through a campaign. Returns ``{"campaign_id", "state"}`` where the state is ``paused``, ``done``, ``partial`` or ``failed``."""
    conn = conn if conn is not None else derived.connect()
    c = repo.get(conn, campaign_id)
    if c is None or c["legacy"]:
        raise LookupError(f"no campaign {campaign_id}")
    if c['state']=='pausing' and c.get('control')=='pause':
        repo.transition(conn,campaign_id,'paused',expect='pausing',paused_reason=None)
        repo.add_event(conn,campaign_id,'paused',{'reason':'pause_before_start'})
        return {'campaign_id':campaign_id,'state':'paused'}
    if c["state"] != "running":
        raise RuntimeError(f"campaign {campaign_id} is {c['state']}, not running")
    runners = stages.RUNNERS if runners is None else runners
    forward = progress or (lambda **_: None)
    plan = c["plan"]
    if (plan.get("mining") or {}).get("version") == 4:
        import os
        if os.environ.get("ZTSA_JOB_STAGE") == "campaign_run":
            from app.backend.pipeline.acceleration.windows_job import install_campaign_limit
            install_campaign_limit(int(os.environ.get('ZTSA_CAMPAIGN_RAM_CAP_BYTES') or 3_000_000_000))      # the cap the job worker already installed (3 GB, or the large tier's)
    from app.backend.pipeline.acceleration.metrics import current as performance
    from app.backend import paths
    performance.resume(paths.interim_dir() / 'acceleration' / 'campaigns' / campaign_id / 'performance.json')
    started = time.time()
    order = [(t, s) for t in c["ecosystems"] for s in targets.stages_for(planmod.target_stages(plan), t)] + [(planmod.TARGET_ALL, s) for s in planmod.global_stages(plan)]
    total = len(order)
    pid, born = ctl.identity()
    repo.beat(conn, campaign_id, pid=pid, pid_started=born, error=None)
    repo.add_event(conn, campaign_id, "started", {"pid": pid, "resumes": c["resumes"], "steps": total})
    if not c.get("baseline"):                              # what the ecosystems looked like before this campaign touched them, to show what it added
        try:
            from app.backend.pipeline import store
            funnel = store.funnel_cached(30)
            baseline = {"at": util.now_z(), "funnel": {e: funnel.get(e, {}) for e in c["ecosystems"]}}
        except Exception:  # ignored: without a baseline the analytics show the state now and say so
            baseline = {"at": util.now_z(), "funnel": {}}
        repo.update(conn, campaign_id, baseline=baseline)
    if control is None:
        control = ctl.Control(campaign_id, conn)
        control.sampler = default_sampler(plan, control)
    quota = (plan.get("knobs") or {}).get("quota")
    sessions, shared = ExitStack(), None

    def pause(target: str, stage: str, reason: dict | None = None) -> dict:
        if shared is not None:
            shared.close()
        control.flush()
        repo.update(conn, campaign_id, paused_reason=reason)                  # cleared (None) for every pause except one the runner chose for itself over memory
        repo.transition(conn, campaign_id, "paused", expect=("running", "pausing"), progress=control.progress, summary=_summary(repo.steps(conn, campaign_id), order, started))
        repo.add_event(conn, campaign_id, "paused", {"target": target, "stage": stage, **({"reason": reason["code"]} if reason else {})})
        label = (f"Paused: {target} needs about {reason['needs_gb']:.1f} GB free; {reason['free_gb']:.1f} GB is free right now"
                 if reason and reason.get('code') == 'insufficient_memory' else 'Paused: ' + reason['detail'] if reason else "Campaign paused")
        forward(state="paused", label=label, campaign=campaign_id)
        return {"campaign_id": campaign_id, "state": "paused"}

    def memory_gate(target: str, stage: str) -> dict | None:
        """For a stage whose cost depends on the ecosystem's own table (today only mining), is there room for it right now? Free memory moves while a campaign runs, so this is asked
        again before every one of these steps, not only once when the campaign started. Returns a pause reason when there is not enough room, else None."""
        if (plan.get('mining') or {}).get('version') == 4:return None
        if stage not in admission.MEMORY_SENSITIVE_STAGES:
            return None
        facts = admission.gather_facts(conn, ecosystems=[target])
        allocated = 0.0
        if (plan.get('mining') or {}).get('version') in (2,3,4):
            # Credit only this attempt's already allocated private memory. Shared
            # pages and unrelated processes never reduce the system reserve.
            import psutil
            process = psutil.Process()
            try:
                allocated = sum(p.memory_full_info().uss for p in [process,*process.children(recursive=True)]) / 1024**3
            except psutil.Error:
                allocated = 0.0
        fit = admission.mine_fits(target, plan, facts, allocated_gb=allocated)
        if fit["ok"]:
            return None
        return {"code": "insufficient_memory", "stage": stage, "target": target, "needs_gb": fit.get('resume_needs_gb',fit["needs_gb"]), "free_gb": fit["free_gb"], "at": util.now_z()}

    import os
    from app.backend.pipeline.acceleration import memory_profile
    in_job = os.environ.get("ZTSA_JOB_STAGE") == "campaign_run"        # only the campaign's own process sizes itself; a test or a library call keeps the normal values

    def adapt_memory() -> dict | None:
        """Size caches, workers and tickets to the memory this machine has free right now (at the start and before every stage, so a campaign can grow back when memory frees up
        and shrink when it tightens). Returns a pause reason when even the smallest tier has no room; the watchdog resumes that pause by itself once memory is back."""
        if not in_job:
            return None
        profile = memory_profile.measure()
        memory_profile.activate(profile)
        performance.set_memory_profile(profile.snapshot())
        if profile.viable:
            return None
        return {"code": "mining_memory_pressure", "detail": f"only {profile.budget_bytes // 2**20} MB of RAM can be used; waiting for about {profile.resume_needs_gb:.2f} GB to be free",
                "retryable": True, "resume_needs_gb": profile.resume_needs_gb, "at": util.now_z()}

    failed_targets: set[str] = set()
    try:
        low_memory = adapt_memory()
        if low_memory is not None:
            return pause(None, None, low_memory)
        from app.backend.pipeline.acceleration import runtime
        if (plan.get('compute') or {}).get('version') != 2:
            return pause(None, None, {'code': 'compute_migration_required', 'detail': 'Choose CPU or CUDA at a paused boundary before resuming this historical policy'})
        def preparing(**state):
            forward(campaign=campaign_id, **state)
            control.tick(**state)
        shared = sessions.enter_context(runtime.session({**plan['compute'], 'stream_metadata': plan.get('workflow_version', 1) >= 2 or (plan.get('mining') or {}).get('version') in (2,3,4),
            'mining': plan.get('mining') or {'version': 1}},
            campaign_id=campaign_id, should_stop=control.should_stop, progress=preparing,
            on_update=lambda state: performance.save(paths.interim_dir() / 'acceleration' / 'campaigns' / campaign_id / 'performance.json')))
        shared.prepare()
        steps = repo.steps(conn, campaign_id)
        for index, (target, stage) in enumerate(order):
            if target in failed_targets or skippable(steps.get((target, stage)), quota):
                continue
            if control.should_stop():
                return pause(target, stage)
            reason = adapt_memory() or memory_gate(target, stage)
            if reason is not None:
                return pause(target, stage, reason)
            t0 = time.time()
            repo.put_step(conn, campaign_id, target, stage, "running", progress={})
            repo.add_event(conn, campaign_id, "stage_started", {"target": target, "stage": stage, "index": index + 1, "of": total})
            control.progress = {"target": target, "stage": stage, "step": index + 1, "steps": total, "percent": round(100 * index / max(total, 1), 1), "label": planmod.STAGE_LABELS.get(stage)}
            control.flush()
            control.sample()

            progress_cache = {'at':0, 'outcomes':None, 'pairs':0}
            def report(_target=target, _stage=stage, _index=index, **state) -> None:
                if _stage == 'mine' and (plan.get('mining') or {}).get('version') in (2,3,4):
                    from app.backend.pipeline import codestore
                    from app.backend.pipeline.acceleration.mining_policy import cumulative_progress
                    if time.monotonic()-progress_cache['at'] >= 5:
                        progress_cache.update(at=time.monotonic(), outcomes=codestore.campaign_outcomes(campaign_id),
                            pairs=sum(n for _,n in codestore.campaign_mined(campaign_id)))
                    if progress_cache['outcomes']:
                        state = cumulative_progress(state, progress_cache['outcomes'], pairs=progress_cache['pairs'],minimum_total=progress_cache.get('scope_total',0))
                        progress_cache['scope_total'] = state['total']
                fraction = state["processed"] / state["total"] if isinstance(state.get("processed"), (int, float)) and isinstance(state.get("total"), (int, float)) and state["total"] else 0
                forward(**{**state, "campaign": campaign_id, "target": _target, "stage": _stage, "step": _index + 1, "steps": total})
                control.tick(target=_target, stage=_stage, step=_index + 1, steps=total, percent=round(100 * (_index + min(fraction, 1)) / max(total, 1), 1),
                             **{k: v for k, v in state.items() if k in ("label", "state", "processed", "total", "counts", "quota", "unit", "phase", "gpu_owner") and v is not None})
                shared.publish(activity=state.get('phase') or state.get('state'))
            ctx = stages.StageContext(campaign_id, target, stage, plan, plan.get("knobs") or {}, control.should_stop, report)
            error = None
            try:
                if stage not in runners:
                    raise RuntimeError(f"the {stage} stage is not available")
                shared.bind_progress(ctx.progress, stage=stage, target=target)
                shared.publish('processing', stage=stage, target=target)
                with performance.timer(stage):
                    detail = run_stage(runners[stage], ctx, lambda **event: repo.add_event(conn, campaign_id, 'stage_retry',
                                       {'target': target, 'stage': stage, **event}))
                detail['performance'] = performance.snapshot()
                from app.backend import paths
                performance.save(paths.interim_dir() / 'acceleration' / 'campaigns' / campaign_id / 'performance.json')
            except stopping.Paused as paused_error:
                detail = {"paused": True, 'pause_reason': getattr(paused_error, 'reason', None)}
            except KeyboardInterrupt:
                raise
            except (Exception, SystemExit) as e:
                detail, error = {}, e
            if error is not None:
                message = (str(error) if getattr(error, "plain", False) else f"{type(error).__name__}: {error}") if isinstance(error, Exception) else f"stopped with exit code {error.code}"
                repo.put_step(conn, campaign_id, target, stage, "error", error=message[:2000], detail=getattr(error, "detail", None) or None, progress=control.progress)
                repo.add_event(conn, campaign_id, "stage_error", {"target": target, "stage": stage, "cause": cause_of(error) if isinstance(error, Exception) else "exit", "message": message[:500]})
                failed_targets.add(target)
                control.flush()
                continue
            if detail.pop("paused", False):
                repo.put_step(conn, campaign_id, target, stage, "paused", detail=detail, progress=control.progress)
                return pause(target, stage, detail.get('pause_reason'))
            status = "skipped" if isinstance(detail.get("skipped"), str) and detail["skipped"] else "quota" if detail.get("quota_reached") else "done"      # a count called skipped is a number, not a reason
            if status == "quota":
                detail["quota"] = quota
            repo.put_step(conn, campaign_id, target, stage, status, detail=detail, progress=control.progress)
            repo.add_event(conn, campaign_id, "stage_done", {"target": target, "stage": stage, "status": status, "seconds": round(time.time() - t0, 1)})
            control.sample()
            control.flush()
        shared.close()
        steps = repo.steps(conn, campaign_id)
        errors = [s for key, s in steps.items() if key in order and s["status"] == "error"]
        if (plan.get("mining") or {}).get("version")==4:
            errors += [s for s in steps.values() if (s.get("detail") or {}).get("unresolved_items")]
        final = "done" if not errors else "partial" if any(s["status"] in DONE for s in steps.values()) else "failed"
        control.progress = {**control.progress, "percent": round(100*sum(s.get("status") in DONE for s in steps.values())/max(total,1),1), "label": "Finished" if final == "done" else "Finished with errors"}
        from app.backend.pipeline import codestore
        from app.backend.pipeline.acceleration.mining_policy import cumulative_progress
        outcomes=codestore.campaign_outcomes(campaign_id)
        if outcomes.get("commits"):
            control.progress={**control.progress,**cumulative_progress({"processed":0,"total":0},outcomes,pairs=sum(n for _,n in codestore.campaign_mined(campaign_id)))}
        summary=_summary(steps,order,started)
        summary["attempt_seconds"]=summary["seconds"]
        summary["seconds"]=round(performance.snapshot()["elapsed_seconds"])
        summary["active_seconds"]=summary["seconds"]
        repo.transition(conn, campaign_id, final, expect=("running", "pausing"), progress=control.progress, summary=summary, finished_at=util.now_z(), control="run")
        repo.release(conn, campaign_id)
        repo.add_event(conn, campaign_id, "finished", {"state": final})
        forward(state="done", label=f"Campaign {final}", campaign=campaign_id)
        return {"campaign_id": campaign_id, "state": final}
    except stopping.Paused as error:
        return pause(None, None, getattr(error, 'reason', None))
    except KeyboardInterrupt:
        ctl.mark_interrupted(conn, campaign_id, "stopped from the keyboard")
        raise
    except Exception as e:
        repo.transition(conn, campaign_id, "failed", expect=("running", "pausing"), error=f"{type(e).__name__}: {e}"[:2000], finished_at=util.now_z())
        repo.release(conn, campaign_id)
        repo.add_event(conn, campaign_id, "stage_error", {"cause": type(e).__name__, "message": str(e)[:500]})
        raise
    finally:
        try:
            sessions.close()
        finally:
            if in_job:
                memory_profile.deactivate()
            performance.save(paths.interim_dir() / 'acceleration' / 'campaigns' / campaign_id / 'performance.json')
