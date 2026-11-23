"""Campaigns of the first system as the stores still hold them: a row with no plan, and its steps.

The engine that wrote them is gone; the Campaigns page shows these rows as campaigns of the earlier kind (state and steps, nothing more). This writes one the way the first system
did, on whichever backend the test runs.
"""
from __future__ import annotations

import json
import time

from app.backend.pipeline import derived, util
from app.backend.pipeline.db import mappers as m


def add_legacy(conn, campaign_id: str = "campaign-20260101-000000", targets=("npm",), *, state: str = "done", steps: dict | None = None, params: dict | None = None,
               created: str = "2026-01-01T00:00:00+00:00", heartbeat: float | None = None) -> str:
    """Store a campaign of the first system. ``steps`` is ``{(target, stage): (status, detail)}``."""
    steps = steps or {}
    beat = time.time() if heartbeat is None else heartbeat
    now = util.now_utc()
    if (mg := derived.mongo_of(conn)):
        conn.db["campaigns"].insert_one({"_id": campaign_id, "created_at": m.parse_dt(created), "state": state, "params": params or {}, "targets": list(targets), "heartbeat_ts": beat})
        for (target, stage), (status, detail) in steps.items():
            conn.db["campaign_steps"].insert_one({"_id": f"{campaign_id}|{target}|{stage}", "campaign_id": campaign_id, "target": target, "stage": stage, "status": status,
                                                  "started_at": m.parse_dt(now), "finished_at": m.parse_dt(now), "detail_json": json.dumps(detail or {})})
        mg._touch(conn)
    else:
        conn.execute("INSERT INTO campaigns (campaign_id, created_at, state, params_json, targets_json, heartbeat_ts) VALUES (?,?,?,?,?,?)",
                     (campaign_id, created, state, json.dumps(params or {}), json.dumps(list(targets)), beat))
        for (target, stage), (status, detail) in steps.items():
            conn.execute("INSERT INTO campaign_steps (campaign_id, target, stage, status, started_at, finished_at, detail_json) VALUES (?,?,?,?,?,?,?)",
                         (campaign_id, target, stage, status, now, now, json.dumps(detail or {})))
        conn.commit()
    return campaign_id
