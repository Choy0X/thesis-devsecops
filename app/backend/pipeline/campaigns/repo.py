"""Storage of the campaigns: the campaign itself, its steps, its events, its samples and the claim on its ecosystems.

The functions use embedded SQLite through the compatibility module ``repo_mongo``; the first argument is the handle ``derived.connect()`` returns. A campaign of this system has a
``plan`` (what was chosen); a row without one is an older campaign of the first system (the ``legacy`` flag), which is read but never changed here.

* A campaign has a state: ``running``, ``pausing``, ``paused``, ``interrupted`` (the process is gone), ``done``, ``partial`` or ``failed``. ``transition`` changes it only from the states
  the caller expects, so two requests that race (two pauses, a pause and a resume) cannot both win.
* ``campaign_owners`` says which campaign works on which ecosystem. ``claim`` takes all the ecosystems it is given or none, in one atomic step; a campaign that is running, pausing,
  paused or interrupted holds its ecosystems, one that is done, partial or failed (or deleted) does not, and its claim is taken over.
* ``campaign_events`` is the append-only story of a campaign, ``campaign_samples`` its counters over time (for the charts).
"""
from __future__ import annotations

import json
import secrets
import time

from app.backend.pipeline import derived, util

OWNING = ("running", "pausing", "paused", "interrupted")         # states that hold their ecosystems
RELEASING = ("done", "partial", "failed")                        # states that let go of them
STATES = OWNING + RELEASING
STEP_STATUSES = ("pending", "running", "paused", "done", "quota", "error", "skipped")
FINISHED_STEP = ("done", "quota", "error", "paused", "skipped")

FIELDS = ("name", "plan", "control", "control_at", "job_id", "pid", "pid_started", "progress", "summary", "declared", "baseline", "started_at", "finished_at", "error", "heartbeat_ts", "state",
          "paused_reason")       # why a paused campaign stopped itself: {"code": "insufficient_memory", "stage", "target", "needs_gb", "at"} for one the runner paused on its own, else None
INCREMENTS = ("resumes",)


class Taken(RuntimeError):
    """An ecosystem is held by another campaign."""

    def __init__(self, ecosystem: str, owner: str):
        self.ecosystem, self.owner = ecosystem, owner
        super().__init__(f"{ecosystem} is being collected by campaign {owner}")


def _mongo(conn):
    from app.backend.pipeline.campaigns import repo_mongo
    return repo_mongo if derived.mongo_of(conn) else None


def _check(fields: dict) -> None:
    bad = [k for k in fields if k not in FIELDS]
    if bad:
        raise KeyError(f"not a campaign field: {', '.join(bad)}")


def new_id(conn) -> str:
    """``cmp-YYYYMMDD-xxxx`` (four random hex digits), one that no campaign has."""
    day = time.strftime("%Y%m%d", time.gmtime())
    for _ in range(200):
        cid = f"cmp-{day}-{secrets.token_hex(2)}"
        if get(conn, cid) is None:
            return cid
    raise RuntimeError("could not find a free campaign id")


# -- the campaign ------------------------------------------------------------------------------------------------------------------

_COLUMNS = ("campaign_id, created_at, state, params_json, targets_json, heartbeat_ts, name, plan_json, control, control_at, job_id, pid, pid_started, progress_json, summary_json, "
            "declared_json, started_at, finished_at, resumes, error, baseline_json")


def _load(text, default):
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


def create(conn, campaign_id: str, *, name: str | None, plan: dict, ecosystems: list[str], declared: dict | None = None, state: str = "running") -> dict:
    """Store a new campaign (running, control "run"). ``plan`` is what the owner chose; ``declared`` the resources it was admitted with."""
    now = util.now_z()
    _mongo(conn).create(conn, campaign_id, name=name, plan=plan, ecosystems=ecosystems, declared=declared or {}, state=state, now=now)
    return get(conn, campaign_id)


def get(conn, campaign_id: str) -> dict | None:
    mg = _mongo(conn)
    return mg.get(conn, campaign_id)


def control_of(conn, campaign_id: str) -> str | None:
    """The campaign's control flag alone (``run`` or ``pause``): what a running campaign reads about once a second."""
    mg = _mongo(conn)
    return mg.control_of(conn, campaign_id)


def all_campaigns(conn, *, current_only: bool = False, states=None) -> list[dict]:
    """Every campaign, newest first. ``current_only`` leaves out the older system's."""
    mg = _mongo(conn)
    return mg.all_campaigns(conn, current_only=current_only, states=states)


def campaign_page(conn, **filters):
    return _mongo(conn).campaign_page(conn, **filters)


def update(conn, campaign_id: str, **fields) -> None:
    """Set fields of a campaign of this system (any state)."""
    _check(fields)
    mg = _mongo(conn)
    return mg.update(conn, campaign_id, fields)


def transition(conn, campaign_id: str, to: str, *, expect: str | tuple[str, ...] | None = None, inc: dict | None = None, **fields) -> bool:
    """Move a campaign to the state ``to`` (and set ``fields``) only if it is in one of the states ``expect``; True if it moved. A request that lost a race gets False."""
    if to not in STATES:
        raise ValueError(f"not a campaign state: {to}")
    _check(fields)
    for k in inc or {}:
        if k not in INCREMENTS:
            raise KeyError(f"not a counter: {k}")
    expect = (expect,) if isinstance(expect, str) else expect
    mg = _mongo(conn)
    return mg.transition(conn, campaign_id, to, expect, inc or {}, fields)


def beat(conn, campaign_id: str, **fields) -> None:
    """The heartbeat of the running process, with whatever it wants to record with it (progress, pid)."""
    update(conn, campaign_id, heartbeat_ts=time.time(), **fields)


def delete(conn, campaign_id: str) -> bool:
    """Remove a campaign and everything stored about it (steps, events, samples, the claim on its ecosystems). What it produced stays where it is."""
    mg = _mongo(conn)
    return mg.delete(conn, campaign_id)


# -- steps -------------------------------------------------------------------------------------------------------------------------

def put_step(conn, campaign_id: str, target: str, stage: str, status: str, *, detail: dict | None = None, error: str | None = None, progress: dict | None = None) -> dict:
    """Record where a step of a target is (``target`` "*" for a step of the whole campaign). Starting a step counts an attempt."""
    if status not in STEP_STATUSES:
        raise ValueError(f"not a step status: {status}")
    now = util.now_z()
    mg = _mongo(conn)
    return mg.put_step(conn, campaign_id, target, stage, status, detail, error, progress, now)


def steps(conn, campaign_id: str) -> dict[tuple[str, str], dict]:
    mg = _mongo(conn)
    return mg.steps(conn, campaign_id)


# -- events and samples ------------------------------------------------------------------------------------------------------------

def add_event(conn, campaign_id: str, kind: str, data: dict | None = None) -> int:
    """Append to the story of a campaign (created, started, stage_started, stage_done, stage_error, pause_requested, paused, resumed, interrupted, forced_kill, rate_limited, warning)."""
    now = util.now_z()
    mg = _mongo(conn)
    return mg.add_event(conn, campaign_id, kind, data or {}, now)


def events(conn, campaign_id: str, *, after: int = 0, limit: int = 200) -> list[dict]:
    """Events with an id above ``after``, oldest first."""
    mg = _mongo(conn)
    return mg.events(conn, campaign_id, after, limit)


def add_sample(conn, campaign_id: str, sample: dict, *, at: str | None = None) -> None:
    """One point of the campaign's counters over time (cumulative commits and pairs, the GitHub quota, the queue, memory)."""
    at = at or util.now_z()
    mg = _mongo(conn)
    return mg.add_sample(conn, campaign_id, sample, at)


def samples(conn, campaign_id: str, *, since: str | None = None, limit: int | None = None) -> list[dict]:
    """The samples (``{"at", ...}``), oldest first; ``since`` leaves out the older ones."""
    mg = _mongo(conn)
    return mg.samples(conn, campaign_id, since, limit)


# -- the claim on ecosystems -------------------------------------------------------------------------------------------------------

def owners(conn) -> dict[str, str]:
    """``{ecosystem: campaign id}`` of the claims that exist."""
    mg = _mongo(conn)
    return mg.owners(conn)


def _holds(conn, owner: str) -> bool:
    c = get(conn, owner)
    return c is not None and c["state"] in OWNING


def claim(conn, campaign_id: str, ecosystems: list[str]) -> None:
    """Take every one of ``ecosystems`` for the campaign, or none: raises ``Taken`` (naming the first one held by another running, pausing, paused or interrupted campaign) and leaves the
    claims as they were. A claim of a campaign that is done, partial, failed or gone is taken over; claiming what the campaign already holds is fine."""
    wanted = sorted(set(ecosystems))
    mg = _mongo(conn)
    return mg.claim(conn, campaign_id, wanted, _holds)


def release(conn, campaign_id: str) -> int:
    """Let go of every ecosystem the campaign holds; returns how many."""
    mg = _mongo(conn)
    return mg.release(conn, campaign_id)
