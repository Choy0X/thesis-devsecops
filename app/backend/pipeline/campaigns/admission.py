"""May this campaign start now? The machine has a number of cores, an amount of memory, a disk, a GitHub quota and a limit on how many jobs run at once, and a campaign that is going to run
beside others must fit what is left. The answer is yes, or no with the reasons, in words and numbers; nothing is queued and nothing is overridden.

``check`` is a pure function of the plan and the ``Facts`` it is given (so every rule is tested with chosen numbers); ``gather_facts`` reads the real ones. The numbers a running campaign
declared when it was admitted are the ones counted against the machine, and a paused campaign holds none of them (only its ecosystems).

The estimates are starting values (``LIMITS``), meant to be refined with what runs measure: memory is dominated by the metadata index the miner builds from the tables of its ecosystems.
"""
from __future__ import annotations

import math
import os
import shutil
from dataclasses import dataclass, field

from app.backend.pipeline.campaigns import plan as planmod, repo

LIMITS = {
    "base_ram_gb": 0.7,                # a campaign process with its stage libraries loaded
    "ram_per_table_gb": 2.864,         # times the size in GB of the parquet tables of its ecosystems (measured: tools/metaindex_bench.py)
    "nvd_ram_gb": 0.414,               # the NVD table is loaded whole regardless of ecosystem; a flat add, not scaled by table size (same measurement)
    "ram_per_worker_gb": 0.1,
    "enrich_ram_gb": 0.3,
    "build_ram_gb": 3.0,
    "ram_share": 0.8,                  # of all the memory
    "ram_reserve_share": 0.05,         # of the machine's total memory, kept free for the app and the system (not a flat number: sane from a tiny machine to a large one)
    "ram_reserve_min_gb": 0.2,         # the reserve never drops below this, on a machine small enough that 5 percent would be less
    "min_disk_gb": 15.0,
    "disk_per_download": 2.5,          # times the dump bytes to fetch (the zip, and its table beside it)
    "github_per_worker": 550,          # calls an hour for each commit mined at once
    "github_recover": 600,             # calls an hour for link recovery
}


def _limit(name: str) -> float:
    try:
        return float(os.environ.get(f"ZTSA_CAMPAIGN_{name.upper()}", LIMITS[name]))
    except ValueError:
        return LIMITS[name]


@dataclass
class Facts:
    """What the machine and the app say now."""
    physical_cores: int = 4
    ram_total_gb: float = 16.0
    ram_free_gb: float = 8.0
    disk_free_gb: float = 200.0
    jobs_running: int = 0
    jobs_max: int = 4
    github_token: bool = True
    requests_per_hour: int = 4500
    running: list[dict] = field(default_factory=list)       # the campaigns that are running or pausing: {"campaign_id", "declared", "plan"}
    owners: dict[str, str] = field(default_factory=dict)     # ecosystem -> campaign id (running, pausing, paused or interrupted)
    table_gb: dict[str, float] = field(default_factory=dict)  # size of the parquet table of each ecosystem
    download_gb: dict[str, float] = field(default_factory=dict)  # dump still to fetch, for the ecosystems that have none (an estimate)


def needs(plan: dict, facts: Facts) -> dict:
    """What this plan asks of the machine: ``{"cpu", "ram_gb", "github_per_hour", "disk_gb", "context", "build"}``."""
    stages, knobs = plan.get("stages") or {}, plan.get("knobs") or {}
    workers = max(1, int(knobs.get("mine_workers") or 4))
    mining = bool(stages.get("mine"))
    cpu = 1 + math.ceil(workers / 4) if mining else 1
    biggest_table = max((facts.table_gb.get(e, 0.0) for e in plan.get("ecosystems") or []), default=0.0)       # not the sum: the runner works through one ecosystem
    ram = _limit("base_ram_gb") + _limit("ram_per_table_gb") * biggest_table + ((_limit("nvd_ram_gb") + _limit("ram_per_worker_gb") * workers) if mining else 0)       # at a time (StageContext.ecosystems), so the miner's index never holds more than one ecosystem's table in memory at once; the NVD table is loaded whole every time mining runs, regardless of which ecosystem
    if mining and (plan.get('mining') or {}).get('version') == 2:
        parsers = plan['mining'].get('parser_workers', 1)
        # Streamed disk index, one parser by default, bounded readers and I/O.
        # CUDA imports/IPC consume host RAM independently of device buffers.
        cpu = 1+parsers
        ram = _limit('base_ram_gb')+.2+.5*parsers+.1+.25*min(workers,4)
        if (plan.get('compute') or {}).get('mode') == 'cuda': ram += .7
    if mining and (plan.get('mining') or {}).get('version') == 3:
        cpu = 2
        ram = 2.0 if (plan.get('compute') or {}).get('mode') == 'cuda' else 1.3
    manual_context = plan.get('workflow_version', 1) >= 2
    ram += max(_limit("enrich_ram_gb") if stages.get("enrich") and not manual_context else 0.0, _limit("build_ram_gb") if stages.get("build") else 0.0)
    github = (_limit("github_per_worker") * workers if mining else 0) + (_limit("github_recover") if stages.get("recover") else 0)
    fetch = sum(facts.download_gb.get(e, 0.0) for e in plan.get("ecosystems") or []) if stages.get("ingest") else 0.0
    disk = max(_limit("min_disk_gb"), _limit("disk_per_download") * fetch)
    if mining and (plan.get("mining") or {}).get("version") == 4:
        cpu, ram = 1, 0.0  # Actual resource admission is continuous and stage-specific.
        github = _limit("github_recover") if stages.get("recover") else 0
    return {"cpu": cpu, "ram_gb": round(ram, 2), "github_per_hour": int(github), "disk_gb": round(disk, 1), "context": bool(stages.get("enrich")) and not manual_context, "build": bool(stages.get("build"))}


def _reason(code: str, text: str, **numbers) -> dict:
    return {"code": code, "text": text, "numbers": numbers}


def _reserve_of(ram_total_gb: float) -> float:
    """What is kept free for the app and the system: a share of the machine's own total memory (not a flat number), with a small floor."""
    return round(max(_limit("ram_reserve_min_gb"), _limit("ram_reserve_share") * ram_total_gb), 2)


def _reserve_gb(facts: Facts) -> float:
    return _reserve_of(facts.ram_total_gb)


# A campaign's stages run one ecosystem at a time (see StageContext.ecosystems), but admission is only asked once, when the campaign starts or resumes. Free memory moves while a long
# campaign runs, so the stages whose cost is dominated by the table of the ecosystem they are on (today only "mine": codemine.mine() builds its MetaIndex fresh for each ecosystem) are
# checked again, against memory as it stands right then, before each one of their steps.
MEMORY_SENSITIVE_STAGES = frozenset({"mine"})


def mine_fits(target: str, plan: dict, facts: Facts, *, allocated_gb=0.0) -> dict:
    """Does the next memory-sensitive step on ``target`` fit in what is free right now? A narrower, cheaper question than ``check()``: only the memory this one step would use,
    against the machine as ``facts`` says it is at this moment, since free memory itself changes while a campaign runs and the fuller checks (jobs, disk, GitHub budget, other
    campaigns) do not need re-asking between steps. ``{"ok", "needs_gb", "free_gb"}``."""
    want = needs({**plan, "ecosystems": [target]}, facts)
    free = round(facts.ram_free_gb - _reserve_gb(facts), 2)
    remaining = max(0, want['ram_gb']-min(max(0, allocated_gb), want['ram_gb']))
    return {"ok": remaining <= free, "needs_gb": round(remaining,2), "free_gb": free,
            **({'resume_needs_gb':want['ram_gb']} if allocated_gb else {})}


def memory_now_covers(needs_gb: float) -> bool:
    """Is at least ``needs_gb`` free on this machine right now, after the usual reserve? Reads only the machine's memory (cheap enough to ask often, for example from the watchdog
    deciding whether a campaign paused for memory can be tried again), not the fuller picture ``gather_facts`` builds. Memory that cannot be read answers no, never a guess."""
    from app.backend import compute
    total, free = compute.memory_gb()
    if free is None or total is None:
        return False
    return needs_gb <= round(free - _reserve_of(total), 2)


def memory_free_at_least(free_gb: float) -> bool:
    """Is at least ``free_gb`` of RAM free right now, reserve included? The question a pause raised by the mining pipeline asks (its number comes from the memory profile, which
    already includes its own reserve). Memory that cannot be read answers no."""
    from app.backend import compute
    total, free = compute.memory_gb()
    return free is not None and free >= free_gb


def check(plan: dict, facts: Facts, *, self_id: str | None = None) -> dict:
    """``{"ok", "errors": [{code, text, numbers}], "warnings": [...], "needs", "free"}``. ``self_id`` is the campaign being resumed (its own claims and nothing it declared count against it)."""
    errors, warnings = [], []
    ecosystems = list(plan.get("ecosystems") or [])
    stages = plan.get("stages") or {}
    want = needs(plan, facts)
    others = [c for c in facts.running if c["campaign_id"] != self_id]

    if not ecosystems:
        errors.append(_reason("no_ecosystems", "Choose at least one ecosystem."))
    if not planmod.enabled_stages(plan):
        errors.append(_reason("no_stages", "Switch at least one stage on."))
    for eco in ecosystems:
        owner = facts.owners.get(eco)
        if owner and owner != self_id:
            errors.append(_reason("ecosystem_busy", f"{eco} is being collected by campaign {owner}. Pause it and let it finish, or delete it, before starting another on {eco}.", ecosystem=eco, owner=owner))
    if facts.jobs_running >= facts.jobs_max:
        errors.append(_reason("too_many_jobs", f"{facts.jobs_running} jobs are already running (the most at once is {facts.jobs_max}). Wait for one to finish or stop one.", running=facts.jobs_running, most=facts.jobs_max))

    cpu_cap = max(1, facts.physical_cores - 1)
    cpu_used = sum(int((c.get("declared") or {}).get("cpu", 1)) for c in others)
    if cpu_used + want["cpu"] > cpu_cap:
        errors.append(_reason("cpu", f"This campaign needs {want['cpu']} processor core(s) and the campaigns running use {cpu_used} of the {cpu_cap} that can be spared "
                                     f"({facts.physical_cores} cores, one is left for the app). Lower the commits mined at once, or wait.", needs=want["cpu"], used=cpu_used, cap=cpu_cap))
    ram_cap = round(_limit("ram_share") * facts.ram_total_gb, 1)
    ram_used = round(sum(float((c.get("declared") or {}).get("ram_gb", 0)) for c in others), 2)
    if ram_used + want["ram_gb"] > ram_cap:
        errors.append(_reason("ram_total", f"This campaign needs about {want['ram_gb']} GB of memory and the campaigns running have {ram_used} GB; at most {ram_cap} GB "
                                          f"({round(100 * _limit('ram_share'))} percent of {facts.ram_total_gb} GB) may be used by campaigns.", needs=want["ram_gb"], used=ram_used, cap=ram_cap))
    elif (plan.get("mining") or {}).get("version") != 4 and want["ram_gb"] > facts.ram_free_gb - _reserve_gb(facts):
        errors.append(_reason("ram_free", f"This campaign needs about {want['ram_gb']} GB of memory, but only {facts.ram_free_gb} GB is free now and {_reserve_gb(facts):g} GB is kept for the app. "
                                          f"Close something, or choose fewer ecosystems.", needs=want["ram_gb"], free=facts.ram_free_gb))
    if facts.disk_free_gb < want["disk_gb"]:
        errors.append(_reason("disk", f"The disk has {facts.disk_free_gb} GB free and this campaign wants {want['disk_gb']} GB free before it starts (room for the dumps it downloads and the tables it makes).",
                              needs=want["disk_gb"], free=facts.disk_free_gb))
    if stages.get("recover") and not facts.github_token:
        errors.append(_reason("github_token_required", "Recovering fix commits needs a GitHub token. Add one in Settings, or switch the Recover stage off."))
    if stages.get("mine") and not facts.github_token:
        warnings.append(_reason("github_no_token", "There is no GitHub token, so commits are fetched with git only. That works, but it is slow, and the GitHub quota cannot be watched."))
    github_used = sum(int((c.get("declared") or {}).get("github_per_hour", 0)) for c in others)
    if github_used + want["github_per_hour"] > facts.requests_per_hour:
        errors.append(_reason("github_budget", f"This campaign would make about {want['github_per_hour']} GitHub calls an hour and the campaigns running make {github_used}; the budget is {facts.requests_per_hour} an hour "
                                               f"for all of them. Lower the commits mined at once, or wait.", needs=want["github_per_hour"], used=github_used, cap=facts.requests_per_hour))
    if want["context"]:
        other = next((c["campaign_id"] for c in others if (c.get("declared") or {}).get("context")), None)
        if other:
            errors.append(_reason("one_context", f"Campaign {other} is writing context with the model; only one campaign can do that at a time.", owner=other))
    if want["build"]:
        other = next((c["campaign_id"] for c in others if (c.get("declared") or {}).get("build")), None)
        if other:
            errors.append(_reason("one_build", f"Campaign {other} is building a dataset; only one campaign can do that at a time.", owner=other))
    return {"ok": not errors, "errors": errors, "warnings": warnings, "needs": want,
            "free": {"cpu": cpu_cap - cpu_used, "ram_gb": round(min(ram_cap - ram_used, facts.ram_free_gb - _reserve_gb(facts)), 2), "github_per_hour": facts.requests_per_hour - github_used,
                     "disk_gb": facts.disk_free_gb, "jobs": facts.jobs_max - facts.jobs_running}}


DUMP_GB_ESTIMATE = 1.0           # a dump not yet on this computer: what it is is not known without asking OSV, so this is a rough size for the disk check


def gather_facts(conn, ecosystems=()) -> Facts:
    """The real numbers: the machine, the jobs, the campaigns running, who holds which ecosystem, the tables' sizes, and the dumps the plan's ``ecosystems`` still have to download."""
    from app.backend import compute, jobs, paths
    from app.backend.pipeline.db import mongo
    from app.backend.pipeline.ingest import osv, osv_catalog
    info = compute.machine_info()
    running = [{"campaign_id": c["campaign_id"], "declared": c["declared"], "plan": c["plan"]} for c in repo.all_campaigns(conn, current_only=True) if c["state"] in ("running", "pausing")]
    table_gb = {}
    for eco in {e for c in repo.all_campaigns(conn, current_only=True) for e in c["ecosystems"]} | {d["name"] for d in osv_catalog.local_downloads()}:
        path = osv.table_path(eco)
        if path.is_file():
            table_gb[eco] = round(path.stat().st_size / 1e9, 6)
    have = {d["name"] for d in osv_catalog.local_downloads() if d.get("downloaded")}
    return Facts(download_gb={e: DUMP_GB_ESTIMATE for e in ecosystems if e not in have}, physical_cores=int(info["physical_cores"] or 1), ram_total_gb=float(info["ram_gb"] or 0), ram_free_gb=float(info["ram_free_gb"] or 0),
                 disk_free_gb=round(shutil.disk_usage(paths.INTERIM if paths.INTERIM.exists() else ".").free / 1e9, 1), jobs_running=len(jobs.running()), jobs_max=jobs.max_running(),
                 github_token=bool(os.environ.get("GITHUB_TOKEN")), requests_per_hour=_requests_per_hour(), running=running, owners=repo.owners(conn), table_gb=table_gb)


def _requests_per_hour() -> int:
    try:
        from app.backend import project_config
        return int(project_config.config().mining.campaign.max_requests_per_hour)
    except Exception:  # ignored: a configuration that cannot be read leaves the default budget
        return 4500
