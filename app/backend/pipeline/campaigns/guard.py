"""Manual jobs and campaigns must not work on the same ecosystem at once.

A campaign holds its ecosystems (``campaign_owners``) from its start until it is done, partial, failed or deleted, even while it is paused. A job started by hand that would download or
read one of them, or that works on the whole mining queue (extract, mine, recover), would collide with the campaign, so it is refused with the reason.
"""
from __future__ import annotations

# stage -> how to find the ecosystems the job touches in its parameters: ("one", key), ("many", key) or ("all",): it works on every ecosystem at once
TOUCHES = {
    "osv_download": ("one", "ecosystem"),
    "osv_install": ("one", "ecosystem"),
    "ingest": ("many", "ecosystems"),
    "recover_fixes": ("many", "ecosystems"),
    "extract_fixes": ("all",),
    "mine_code": ("all",),
}


def blocked(stage: str, params: dict | None, owners: dict[str, str]) -> str | None:
    """Why a job of this stage may not start now, or None. ``owners`` is ``{ecosystem: campaign id}``."""
    how = TOUCHES.get(stage)
    if how is None or not owners:
        return None
    params = params or {}
    if how[0] == "all":
        eco, cid = next(iter(sorted(owners.items())))
        return f"Not started: campaign {cid} holds {eco}, and this job works on every ecosystem at once. Pause the campaign and let it finish, or delete it, first."
    wanted = [params.get(how[1])] if how[0] == "one" else list(params.get(how[1]) or [])
    if how[0] == "many" and not wanted:                        # no ecosystem named means all of them
        eco, cid = next(iter(sorted(owners.items())))
        return f"Not started: campaign {cid} holds {eco}, and this job would work on every ecosystem. Name the ecosystems it should use, or pause the campaign first."
    for eco in wanted:
        if eco in owners:
            return f"Not started: campaign {owners[eco]} is collecting {eco}. Pause it and let it finish, or delete it, before working on {eco} by hand."
    return None


def check(stage: str, params: dict | None) -> str | None:
    """``blocked`` with the claims as they are now. A failure to look is no reason to refuse: the job then fails by itself if the store is not there."""
    if stage not in TOUCHES:
        return None
    try:
        from app.backend.pipeline import derived
        from app.backend.pipeline.campaigns import repo
        with derived.connect() as conn:
            return blocked(stage, params, repo.owners(conn))
    except Exception:  # ignored: see above
        return None
