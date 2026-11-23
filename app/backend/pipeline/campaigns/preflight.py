"""Does the plan make sense for these ecosystems and this data? The questions that are about the plan and the data, not about the machine (that is ``admission``).

``check`` answers with problems, each ``{"code", "text", "stage"?, "ecosystem"?}``: an *error* means the campaign cannot do what it was asked, a *warning* that it can but probably not what the
owner expects (for example, mining with the queue off when nothing is queued for the ecosystem). The same call says, for each ecosystem, which stages will run (an ecosystem with no adapter
only has its dump read).
"""
from __future__ import annotations

from typing import Callable

from app.backend.pipeline.campaigns import plan as planmod, targets

KNOB_LIMITS = {"mine_workers": (1, 16), "deps_workers": (1, 64), "recover_workers": (1, 32), "recover_limit": (1, None), "quota": (1, None), "pause_seconds": (0, 3600)}


def _problem(code: str, text: str, **where) -> dict:
    return {"code": code, "text": text, **where}


def check(plan: dict, *, local: list[dict], records: Callable[[str], int], pending: Callable[[str], int], llm_check: Callable[[dict], tuple[bool, str]] | None = None) -> dict:
    """``{"ok", "errors", "warnings", "per_ecosystem": {name: {"stages": [...], "notes": [...]}}}``.

    ``local`` is ``targets.local_targets()``; ``records(eco)`` how many records of the ecosystem the store holds, ``pending(eco)`` how many fix commits wait for it;
    ``llm_check(plan)`` says whether the model that writes the context can be reached."""
    errors, warnings, per = [], [], {}
    by_name = {t["name"]: t for t in local}
    on = planmod.enabled_stages(plan)
    stages = plan.get("stages") or {}
    knobs = plan.get("knobs") or {}

    for name, (low, high) in KNOB_LIMITS.items():
        value = knobs.get(name)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            errors.append(_problem("knob", f"{name.replace('_', ' ')} must be a number.", knob=name))
            continue
        if number < low or (high is not None and number > high):
            errors.append(_problem("knob", f"{name.replace('_', ' ')} must be {'at least ' + str(low) if high is None else f'between {low} and {high}'}.", knob=name))

    for eco in plan.get("ecosystems") or []:
        t = by_name.get(eco)
        notes: list[str] = []
        if t is None:
            if stages.get("ingest"):
                notes.append("Its dump is not on this computer yet; the Ingest stage downloads it first.")
            else:
                errors.append(_problem("no_dump", f"{eco} has no dump on this computer and Ingest is off, so there is nothing to work on. Switch Ingest on, or download the dump on the Collections page.", ecosystem=eco))
        else:
            if t.get("stopped"):
                notes.append(f"A {t['stopped']} of its dump was cut short; Ingest continues it.")
            if not t["downloaded"] and not stages.get("ingest"):
                errors.append(_problem("no_dump", f"The dump of {eco} is not finished downloading and Ingest is off.", ecosystem=eco))
        mineable = t["mineable"] if t else targets.has_adapter(eco)
        run = targets.stages_for(on, eco)
        if not mineable:
            notes.append(targets.INGEST_ONLY)
            if len(on) > 1 or (on and on[0] != "ingest"):
                warnings.append(_problem("ingest_only", f"{eco} has no mining adapter, so only its dump is read; the other stages are skipped for it.", ecosystem=eco))
            if "ingest" not in on:
                errors.append(_problem("nothing_to_do", f"{eco} has no mining adapter and Ingest is off, so a campaign has nothing to do for it.", ecosystem=eco))
        else:
            have = records(eco)
            if "queue" in on and "ingest" not in on and have == 0:
                warnings.append(_problem("queue_without_records", f"The store holds no {eco} records and Ingest is off, so Queue will find nothing. Switch Ingest on first.", stage="queue", ecosystem=eco))
            if "mine" in on and "queue" not in on and pending(eco) == 0:
                warnings.append(_problem("mine_without_queue", f"Nothing is queued for {eco} and Queue is off, so Mine will find nothing to mine.", stage="mine", ecosystem=eco))
            if {"verify", "resplit", "dep_versions"} & set(on) and "mine" not in on and have == 0:
                warnings.append(_problem("checks_without_data", f"The store holds no {eco} records, so the checks after mining will find nothing.", ecosystem=eco))
        per[eco] = {"stages": run, "notes": notes}

    if stages.get("recover") and not stages.get("queue") and not stages.get("mine"):
        warnings.append(_problem("recover_alone", "Recover finds fix commits but nothing queues or mines them in this plan.", stage="recover"))
    if 'enrich' in on:
        if llm_check is None:
            errors.append(_problem("enrich_unavailable", "Writing the context with the model is not available yet.", stage="enrich"))
        else:
            ok, text = llm_check(plan)
            if not ok:
                errors.append(_problem("model_unreachable", text, stage="enrich"))
    if stages.get("build") and not stages.get("mine") and not stages.get("enrich") and not stages.get("verify"):
        warnings.append(_problem("build_alone", "A draft dataset is built from what is stored now; nothing in this plan changes that.", stage="build"))
    return {"ok": not errors, "errors": errors, "warnings": warnings, "per_ecosystem": per}
