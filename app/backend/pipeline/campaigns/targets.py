"""The ecosystems a campaign can be started on: every dump that is on this computer (found from the files alone, no network), with what can be done with it and who is using it.

An ecosystem with an adapter can be taken through all the stages; one without (Ubuntu, Debian, Alpine, ...) is for reading and searching, so a campaign only reads its dump.
"""
from __future__ import annotations

from app.backend.pipeline import ecosystems
from app.backend.pipeline.campaigns import repo
from app.backend.pipeline.ingest import osv_catalog

INGEST_ONLY = "This ecosystem has no mining adapter, so a campaign only reads its dump (it can then be searched under Find an advisory)."


def has_adapter(name: str) -> bool:
    return ecosystems.get(name) is not None


def local_targets(conn=None) -> list[dict]:
    """``[{"name", "downloaded", "ingested", "dump_bytes", "snapshot", "mineable", "reason", "owner", "stopped"}]`` for every dump on this computer, in name order.

    ``mineable`` is whether the later stages apply (an adapter exists); ``reason`` says why not. ``owner`` is the campaign that holds the ecosystem now, if any. ``stopped`` says a read
    or a download of the dump was cut and not finished (a campaign continues it)."""
    owners = repo.owners(conn) if conn is not None else {}
    out = []
    for d in osv_catalog.local_downloads():
        name = d["name"]
        mineable = has_adapter(name)
        out.append({"name": name, "downloaded": bool(d.get("downloaded")), "ingested": bool(d.get("ingested")), "dump_bytes": d.get("dump_bytes"), "snapshot": d.get("snapshot"),
                    "mineable": mineable, "reason": None if mineable else INGEST_ONLY, "owner": owners.get(name),
                    "stopped": ("read" if d.get("read_partial") else "download" if d.get("download_partial") else None)})
    return sorted(out, key=lambda t: t["name"].lower())


def stages_for(plan_stages: list[str], name: str) -> list[str]:
    """The stages that apply to one ecosystem: all that are on for one with an adapter, only ``ingest`` for one without."""
    return list(plan_stages) if has_adapter(name) else [s for s in plan_stages if s == "ingest"]
