"""The stages of a campaign, one runner for each. A runner takes the ``StageContext`` of one step and returns a dict about what it did.

A runner works on the one ecosystem of its context (``ctx.ecosystems``), asks ``ctx.should_stop`` at its safe points (see ``stopping``) and reports through ``ctx.progress``. It returns
``{"paused": True, ...}`` when it stopped because the campaign was asked to pause, ``{"quota_reached": True, ...}`` when mining stopped at the quota, ``{"skipped": "why"}`` when there was
nothing it could do, and otherwise numbers about its work. An exception is an error of the step (the other ecosystems go on).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

from app.backend import compute
from app.backend.pipeline import stopping
from app.backend.pipeline.campaigns import plan as planmod

MINE_PAUSE_SECONDS = 10


class StageFailed(RuntimeError):
    """A step that failed after it had counted something: the runner keeps ``detail`` with the error, so the analytics still show what was tried."""

    plain = True                                  # the runner shows the sentence as it is, without the name of this class

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = dict(detail or {})


@dataclass
class StageContext:
    campaign_id: str
    target: str
    stage: str
    plan: dict
    knobs: dict
    should_stop: Callable[[], bool]
    progress: Callable[..., None]
    extra: dict = field(default_factory=dict)

    @property
    def ecosystems(self) -> list[str]:
        return [self.target]


def ingest(ctx: StageContext) -> dict:
    """Download the ecosystem's dump (when it is not here, or a newer one is asked for) and read it into its table; for an ecosystem with an adapter, put its advisories in the store."""
    from app.backend.pipeline import store
    from app.backend.pipeline.ingest import osv
    t = ctx.target
    try:
        got = osv.download_result(t, refresh=bool(ctx.knobs.get("refresh")), should_stop=ctx.should_stop,
                                  progress=lambda done, total: ctx.progress(state="downloading", label=f"{t}: downloading the dump", unit="bytes", processed=done, total=total, phase="download"))

        def report_read(done, total, rows, phase="read"):
            # once every file is read, "reading" would otherwise go quiet while the table is actually put together (a real wait on a large ecosystem): the count keeps climbing
            # past the file total instead, on a label that says what is really happening, so a long run is never silently indistinguishable from a stuck one.
            label, state = (f"{t}: putting the dump together", "assembling") if phase == "assembling" else (f"{t}: the dump is ready", "done") if phase == "done" else (f"{t}: reading the dump", "reading")
            ctx.progress(state=state, label=label, unit="files", processed=done, total=total, phase=phase, rows=rows)
        ref = osv.parse(got["path"], t, frame=False, should_stop=ctx.should_stop, progress=report_read)
    except stopping.Paused:
        return {"paused": True}
    if int(ref.rows) == 0:                        # OSV writes the name exactly (Hex, crates.io): a dump read under another spelling holds nothing, and the later stages would have nothing to do
        raise RuntimeError(f"The dump has no advisory for '{t}'. OSV writes ecosystem names exactly (for example Hex, crates.io, npm), so check the spelling of the name, or download the dump again.")
    if osv.layout_of(t) == "advisory":            # no adapter: the table is for searching; the store is not written
        return {"rows": int(ref.rows), "advisories": 0, "skipped": "no adapter: the table is for searching only"}
    written = 0
    groups = max(1, int(getattr(ref, "groups", 0) or 1))          # how many chunks this write has, so the person sees it move and not freeze once the file is fully read
    for index, df in enumerate(osv.frames(ref), start=1):
        if ctx.should_stop():
            return {"paused": True, "rows": int(ref.rows), "advisories": written}
        written += int(store.upsert_many(df, source="osv"))
        ctx.progress(state="writing", label=f"{t}: saving to the database", unit="rows", processed=index, total=groups, phase="write")
    return {"rows": int(ref.rows), "advisories": written}


def queue(ctx: StageContext) -> dict:
    from app.backend.pipeline.ingest import fixrefs
    return dict(fixrefs.queue_from_records(progress=ctx.progress, ecosystems=ctx.ecosystems, should_stop=ctx.should_stop) or {})


def recover(ctx: StageContext) -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return {"skipped": "no GitHub token: link recovery needs one"}
    from app.backend.pipeline.ingest import linkrecovery
    return dict(linkrecovery.run(api=linkrecovery.GitHubClient(token), limit=ctx.knobs.get("recover_limit"), ecosystems=ctx.ecosystems, progress=ctx.progress,
                                 should_stop=ctx.should_stop, workers=compute.io_workers(8, ctx.knobs.get("recover_workers"), kind="recover_threads")) or {})


def mine(ctx: StageContext) -> dict:
    from app.backend.pipeline import codestore, mining_run
    from app.backend.pipeline.ingest import codemine
    adaptive=(ctx.plan.get("mining") or {}).get("version")==4
    if adaptive:
        repaired=codestore.reconcile_missing_fix_commits(ctx.ecosystems,should_stop=ctx.should_stop,progress=ctx.progress)
        if repaired:ctx.progress(label="Recovered saved fix links",repaired_queue_items=repaired)
    quota = int(ctx.knobs["quota"]) if ctx.knobs.get("quota") else None
    try:
        res = codemine.mine(limit=quota, workers=compute.io_workers(4, ctx.knobs.get("mine_workers"), kind="mine_threads"), until_done=True,
                            pause_seconds=float(ctx.knobs.get("pause_seconds") if ctx.knobs.get("pause_seconds") is not None else MINE_PAUSE_SECONDS),
                            progress=ctx.progress, ecosystems=ctx.ecosystems, should_stop=ctx.should_stop,
                            retry_failed=True, **({"retryable_only":True} if adaptive else {})) or {}
    except Exception:
        rec = mining_run.load()                                  # a failed miner must not block the next attempt
        if rec and rec.get("pid") == os.getpid() and rec.get("state") == "running":
            mining_run.finish("error")
        raise
    pending = codestore.count_mineable(ecosystems=ctx.ecosystems)
    out = {k: int(res.get(k, 0)) for k in ("mined", "pairs", "no_code", "failed", "functions")}
    out["skipped_commits"] = int(res.get("skipped", 0))          # not "skipped": the runner reads that key as a reason for skipping the whole step
    out["pending_after"] = pending
    if (ctx.plan.get("mining") or {}).get("version") == 4:
        from .source_resolution import counts
        dispositions = counts(ctx.campaign_id, ecosystem=ctx.target)
        out['unresolved_items'] = dispositions['unresolved']
        out['skipped_sources'] = dispositions['skipped']
    if res.get("paused"):
        out["paused"] = True
        if res.get('pause_reason'):
            out['pause_reason'] = res['pause_reason']
    elif quota and pending:
        out["quota_reached"] = True
    elif out['failed'] and (ctx.plan.get('mining') or {}).get('version') != 4:
        raise StageFailed(f"{out['failed']} commits could not be mined; completed pairs are retained for review.", out)
    return out


def verify(ctx: StageContext) -> dict:
    """Check the mined fix commits of this ecosystem against release dates. Mining checks each commit as it mines it, so after a run of the Mine stage there is usually nothing left: the step then
    says so (and why) instead of showing a bare zero. The first check of an ecosystem, and the first one after the check itself changed, covers every mined commit and is remembered."""
    from app.backend.pipeline import codestore, store
    from app.backend.pipeline.ingest import fixcheck
    total = codestore.count_commits_by_status("mined", ecosystems=ctx.ecosystems)
    if total == 0:
        return {"skipped": f"no mined commits of {ctx.target} to check"}
    key = f"fixcheck_version:{ctx.target}"
    current = store.get_meta(key) == str(fixcheck.VERSION)
    left = codestore.count_commits_by_status("mined", only_unchecked=True, ecosystems=ctx.ecosystems)
    if current and left == 0:
        return {"skipped": f"all {total} mined commits of {ctx.target} were already checked (mining checks each commit as it mines it)", "already_checked": total}
    out = dict(fixcheck.verify_all(only_unchecked=current, progress=ctx.progress, ecosystems=ctx.ecosystems, should_stop=ctx.should_stop) or {})
    if not out.get("paused"):
        store.set_meta(key, str(fixcheck.VERSION))
    return out


def resplit(ctx: StageContext) -> dict:
    """Split the stored file pairs of this ecosystem into function and hunk pairs. Mining splits what it mines, so when every file pair was seen by the current splitter the step says so; after
    that only file pairs mined since are split. The first split of an ecosystem, and the first after the splitter changed, covers every file pair and is remembered."""
    from app.backend.pipeline import codestore, funcsplit, store
    from app.backend.pipeline.ingest import codemine
    total = codestore.count_file_pairs_to_split(ecosystems=ctx.ecosystems)
    if total == 0:
        return {"skipped": f"no stored file pairs of {ctx.target} to split"}
    suffix = f":{ctx.target}"
    current = codemine.split_is_current(suffix)
    new = codestore.count_file_pairs_to_split(ecosystems=ctx.ecosystems, unsplit_only=True)
    if current and new == 0:
        return {"skipped": f"all {total} stored file pairs of {ctx.target} were already split by the current splitter (v{funcsplit.VERSION})", "already_split": total}
    out = dict(codemine.resplit(progress=ctx.progress, ecosystems=ctx.ecosystems, should_stop=ctx.should_stop, only_unsplit=current) or {})
    if not out.get("paused"):
        codemine.stamp_split(suffix)
    return out


def dep_versions(ctx: StageContext) -> dict:
    from app.backend.pipeline.ingest import depversions
    return dict(depversions.collect(workers=compute.io_workers(24, ctx.knobs.get("deps_workers"), kind="deps_threads"), progress=ctx.progress, only_mined=True, ecosystems=ctx.ecosystems,
                                    should_stop=ctx.should_stop) or {})


def build(ctx: StageContext) -> dict:
    """Build a draft code-pair dataset (never a frozen version) of this campaign's ecosystems with the chosen dataset profile, once the other stages are done. It is not interruptible: a
    pause asked before it starts keeps it from starting, and one asked while it runs is honoured when it ends."""
    from app.backend.pipeline import oslock
    from app.backend.pipeline.ingest import build_code_dataset as bcd
    if ctx.should_stop():
        return {"paused": True}
    chosen = ctx.plan.get("build_profile") or "training"
    profile = next((p for p in bcd.PROFILES if p["id"] == chosen), None)
    if profile is None:
        raise ValueError(f"not a dataset profile: {chosen}")
    ctx.progress(state="building", label="Building a draft dataset", processed=0, total=1)
    try:
        stats = bcd.main(freeze=False, filters={**profile["filters"], "profile": chosen, "ecosystems": list(ctx.plan.get("ecosystems") or [])})
    except oslock.Busy as e:
        raise RuntimeError(f"Another build is writing the dataset files ({e}). Wait for it to finish and run the campaign again.") from None
    ctx.progress(state="done", label="Draft dataset built", processed=1, total=1)
    if not stats:
        return {"skipped": "No rows to build yet: nothing was mined for these ecosystems, or the filters left none."}
    severity = stats["by_severity"]
    # the first four plain values are what the overview shows in the row; the rest is for the funnel table and the analytics
    return {"profile": chosen, "rows": stats["rows"], "records": stats["records"], "cves": stats["cves"], "records_mined": stats["records_mined"], "unscored": int(severity.get("unscored", 0)),
            "duplicates_removed": int(stats.get("duplicates_removed", 0)), "leak_guard_removed": int(stats.get("leak_guard_removed", 0)), "warnings_n": len(stats["warnings"]),
            "split_counts": stats["split_counts"], "by_language": stats["by_language"], "by_severity": severity, "by_granularity": stats["by_granularity"], "by_fix_check": stats["by_fix_check"],
            "by_context": stats["by_context"], "funnel": stats["funnel"], "warnings": stats["warnings"], "output": stats["outputs"]["parquet"], "draft": True}


MODEL_CHECK_SECONDS = 8.0
# How long the check keeps retrying while another local model operation (a load) holds Ollama.
MODEL_CHECK_BUSY_SECONDS = 300.0


def model_check(plan: dict | None = None, *, timeout: float = MODEL_CHECK_SECONDS) -> tuple[bool, str]:
    """Can the model that writes the context be used? The endpoint and the model are set in Settings; the server must answer, and must list the model. ``(ok, what to do)``."""
    from urllib.parse import urlsplit
    from app.backend.pipeline import llm_client
    r = llm_client.resolved()
    base, model = r.base_url, r.model
    if r.engine == "local":
        from app.backend.pipeline.gateway import client as gateway
        if not r.configured:
            return False, "No local language model is chosen. Choose one in Settings first."
        from app.backend.pipeline.gateway.errors import GatewayError
        deadline = time.monotonic() + MODEL_CHECK_BUSY_SECONDS
        while True:
            try:
                gateway.inference_model("llm", timeout=timeout)
            except GatewayError as error:
                if error.code != "busy" or time.monotonic() >= deadline:
                    return False, str(error)
                time.sleep(error.retry_after or 1.0)
                continue
            except RuntimeError as error:
                return False, str(error)
            return True, ""
    if not base:
        return False, "No model endpoint is set. Choose one in Settings first."
    elif not model:
        return False, "No model is chosen. Choose one in Settings first."
    host = urlsplit(base).netloc.split("@")[-1] or base
    try:
        client = llm_client.make(base, r.api_key, timeout=timeout, max_retries=0)
        listed = [m.id for m in client.models.list().data]
    except Exception as e:
        return False, f"The model server at {host} did not answer ({type(e).__name__}: {str(e)[:120]}). Start it, or check the endpoint in Settings."
    if model not in listed:
        return False, f"The server at {host} does not serve the model {model}" + (f" (it serves {', '.join(listed[:4])})" if listed else "") + ". Choose the model it serves in Settings."
    return True, ""


def enrich(ctx: StageContext) -> dict:
    """Write the model's root cause, secure pattern and explanation for the pairs of this ecosystem that the chosen depth offers, with the model chosen in Settings."""
    from app.backend.pipeline.ingest import enrich as writer
    from app.backend.pipeline import context_exchange
    if ctx.plan.get('workflow_version', 1) >= 2:
        return {'skipped': 'Context review is a manual step after collection finishes.'}
    chosen = ctx.plan.get("context") or {}
    profile = next((p for p in planmod.CONTEXT_PROFILES if p["id"] == chosen.get("profile")), planmod.CONTEXT_PROFILES[0])
    selected, _ = writer.select_candidates({**profile['filters'], 'ecosystems': [ctx.target]})
    if chosen.get('cap'):
        selected = selected.head(int(chosen['cap']))
    ids = selected.pair_id.astype(int).tolist() if len(selected) else []
    waiting = context_exchange.default().awaiting(ctx.campaign_id, ids) if ids else set()
    todo = [i for i in ids if i not in waiting]
    if not todo:
        if waiting:
            raise StageFailed('Context items are awaiting external JSON results; import their responses, then retry this stage.',
                              {'awaiting_external': sorted(waiting), 'failed': 0, 'enriched': 0})
        return {'enriched': 0, 'failed': 0, 'candidates': 0}
    ok, text = model_check(ctx.plan)
    if not ok:
        raise RuntimeError(text)
    result = context_exchange.generate_configured(todo, campaign_id=ctx.campaign_id, should_stop=ctx.should_stop, progress=ctx.progress, _legacy_stage=True)
    out = {**result, 'enriched': result['completed'], 'candidates': len(ids), 'awaiting_external': sorted(waiting)}
    if (out['failed'] or waiting or out.get('needs_context')) and not out.get('paused'):
        raise StageFailed('Some context items need retry, missing evidence, or external results. Completed drafts were retained.', out)
    return out


RUNNERS: dict[str, Callable[[StageContext], dict]] = {"ingest": ingest, "queue": queue, "recover": recover, "mine": mine, "verify": verify, "resplit": resplit, "dep_versions": dep_versions,
                                                     "enrich": enrich, "build": build}
