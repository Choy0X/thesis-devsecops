"""Embedded document implementation of ``campaigns.repo``, retaining its compatibility module name (collections ``campaigns``, ``campaign_steps``, ``campaign_events``, ``campaign_samples``, ``campaign_owners``)."""
from __future__ import annotations

import json
import time

from app.backend.pipeline import util
from app.backend.pipeline.db import derived_mongo, legacy, mappers as m

DATE_FIELDS = ("created_at", "started_at", "finished_at", "control_at")


def _utc_text(text: str | None):
    return m.parse_dt(text)


def _clean(value):
    return None if value is None else json.loads(json.dumps(value, sort_keys=True, default=str))


def _from_doc(d: dict) -> dict:
    plan = d.get("plan")
    return {"campaign_id": d["_id"], "name": d.get("name"), "state": d.get("state"), "created_at": m.fmt_dt(d.get("created_at")), "started_at": m.fmt_dt(d.get("started_at")),
            "finished_at": m.fmt_dt(d.get("finished_at")), "heartbeat_ts": d.get("heartbeat_ts"), "control": d.get("control"), "control_at": m.fmt_dt(d.get("control_at")),
            "job_id": d.get("job_id"), "pid": d.get("pid"), "pid_started": d.get("pid_started"), "resumes": d.get("resumes") or 0, "error": d.get("error"), "plan": plan,
            "ecosystems": (plan or {}).get("ecosystems") or d.get("targets") or [], "progress": d.get("progress") or {}, "summary": d.get("summary") or {},
            "declared": d.get("declared") or {}, "baseline": d.get("baseline") or {}, "params": d.get("params") or {}, "paused_reason": d.get("paused_reason"), "legacy": plan is None}


def _doc_fields(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        out[k] = _utc_text(v) if k in DATE_FIELDS else _clean(v) if k in ("plan", "progress", "summary", "declared", "baseline") else v
    return out


def create(conn, campaign_id, *, name, plan, ecosystems, declared, state, now) -> None:
    doc = {"_id": campaign_id, "created_at": _utc_text(now), "state": state, "params": {}, "targets": list(ecosystems), "heartbeat_ts": time.time(), "name": name,
           "plan": _clean(plan), "control": "run", "control_at": _utc_text(now), "declared": _clean(declared), "started_at": _utc_text(now), "resumes": 0, "progress": {}, "summary": {}}
    conn.db["campaigns"].insert_one(doc)
    derived_mongo._touch(conn)


def get(conn, campaign_id):
    d = conn.db["campaigns"].find_one({"_id": campaign_id})
    return _from_doc(d) if d else None


def control_of(conn, campaign_id):
    d = conn.db["campaigns"].find_one({"_id": campaign_id}, {"control": 1})
    return d.get("control") if d else None


def all_campaigns(conn, *, current_only: bool, states=None):
    flt = {"plan": {"$exists": True, "$ne": None}} if current_only else {}
    if states:
        flt['state'] = {'$in': list(states)}
    return [_from_doc(d) for d in conn.db["campaigns"].find(flt).sort([("created_at", -1), ("_id", -1)])]


def campaign_page(conn, *, limit=100, offset=0, search='', state=''):
    import re
    flt = {}
    if state:
        flt['state'] = state
    if search.strip():
        term = {'$regex': re.escape(search.strip()), '$options': 'i'}
        flt['$or'] = [{'name': term}, {'_id': term}]
    limit, offset = max(1, min(500, int(limit))), max(0, int(offset))
    coll = conn.db['campaigns']
    fields = {'name': 1, 'created_at': 1, 'state': 1, 'control': 1, 'control_at': 1,
              'plan.ecosystems': 1, 'plan.mining.version': 1, 'targets': 1, 'job_id': 1,
              'pid': 1, 'pid_started': 1, 'resumes': 1, 'error': 1, 'progress': 1,
              'summary': 1, 'paused_reason': 1, 'started_at': 1, 'finished_at': 1}
    rows = coll.find(flt, fields).sort([('created_at', -1), ('_id', -1)]).skip(offset).limit(limit)
    return {'campaigns': [_from_doc(doc) for doc in rows], 'total': coll.count_documents(flt),
            'limit': limit, 'offset': offset}


def update(conn, campaign_id, fields: dict) -> None:
    if fields:
        conn.db["campaigns"].update_one({"_id": campaign_id}, {"$set": _doc_fields(fields)})
        derived_mongo._touch(conn)


def transition(conn, campaign_id, to, expect, inc: dict, fields: dict) -> bool:
    flt: dict = {"_id": campaign_id}
    if expect:
        flt["state"] = {"$in": list(expect)}
    op: dict = {"$set": _doc_fields({**fields, "state": to})}
    if inc:
        op["$inc"] = dict(inc)
    moved = conn.db["campaigns"].update_one(flt, op).matched_count == 1
    if moved:
        derived_mongo._touch(conn)
    return moved


def delete(conn, campaign_id) -> bool:
    db = conn.db
    found = db["campaigns"].count_documents({"_id": campaign_id}) > 0
    for name in ("campaign_steps", "campaign_events", "campaign_samples", "campaign_owners"):
        db[name].delete_many({"campaign_id": campaign_id})
    db["campaigns"].delete_one({"_id": campaign_id})
    derived_mongo._touch(conn)
    return found


def _step_id(campaign_id: str, target: str, stage: str) -> str:
    return f"{campaign_id}|{target}|{stage}"


def _step(d: dict) -> dict:
    return {"target": d["target"], "stage": d["stage"], "status": d["status"], "started_at": m.fmt_dt(d.get("started_at")), "finished_at": m.fmt_dt(d.get("finished_at")),
            "active_seconds": d.get("active_seconds"), "detail": m.loads(d.get("detail_json"), {}), "error": d.get("error"), "attempts": d.get("attempts") or 0, "progress": d.get("progress") or {}}


def put_step(conn, campaign_id, target, stage, status, detail, error, progress, now) -> dict:
    from app.backend.pipeline.campaigns import repo
    coll = conn.db["campaign_steps"]
    sid = _step_id(campaign_id, target, stage)
    old = coll.find_one({"_id": sid}) or {}
    started = _utc_text(now) if status == "running" or not old.get("started_at") else old["started_at"]
    doc = {"campaign_id": campaign_id, "target": target, "stage": stage, "status": status, "started_at": started,
           "attempts": (old.get("attempts") or 0) + (1 if status == "running" else 0),
           "detail_json": json.dumps(detail, sort_keys=True, default=str) if detail is not None else old.get("detail_json", "{}"),
           "progress": _clean(progress) if progress is not None else old.get("progress") or {}}
    doc["active_seconds"] = float(old.get("active_seconds") or 0)
    if old.get("status") == "running" and status != "running" and old.get("started_at"):
        doc["active_seconds"] += max(0,(_utc_text(now)-m.parse_dt(old["started_at"])).total_seconds())
    if status in repo.FINISHED_STEP:
        doc["finished_at"] = _utc_text(now)
    if error:
        doc["error"] = error
    coll.replace_one({"_id": sid}, doc, upsert=True)
    derived_mongo._touch(conn)
    return _step(doc)


def steps(conn, campaign_id) -> dict:
    return {(d["target"], d["stage"]): _step(d) for d in conn.db["campaign_steps"].find({"campaign_id": campaign_id})}


def add_event(conn, campaign_id, kind, data, now) -> int:
    eid = legacy.ids(conn.db, "campaign_events")
    conn.db["campaign_events"].insert_one({"_id": eid, "campaign_id": campaign_id, "at": _utc_text(now), "kind": kind, "data": _clean(data)})
    return eid


def events(conn, campaign_id, after, limit) -> list[dict]:
    cur = conn.db["campaign_events"].find({"campaign_id": campaign_id, "_id": {"$gt": int(after)}}).sort("_id", 1).limit(int(limit))
    return [{"id": d["_id"], "at": m.fmt_dt(d["at"]), "kind": d["kind"], "data": d.get("data") or {}} for d in cur]


def add_sample(conn, campaign_id, sample, at) -> None:
    conn.db["campaign_samples"].insert_one({"campaign_id": campaign_id, "at": _utc_text(at), "data": _clean(sample)})


def samples(conn, campaign_id, since, limit) -> list[dict]:
    flt: dict = {"campaign_id": campaign_id}
    if since:
        flt["at"] = {"$gt": _utc_text(since)}
    cur = conn.db["campaign_samples"].find(flt).sort([("at", 1), ("_id", 1)])
    if limit:
        cur = cur.limit(int(limit))
    return [{**(d.get("data") or {}), "at": m.fmt_dt(d["at"])} for d in cur]


def owners(conn) -> dict[str, str]:
    return {d["_id"]: d["campaign_id"] for d in conn.db["campaign_owners"].find()}


def claim(conn, campaign_id, wanted: list[str], holds) -> None:
    from app.backend.pipeline.db.operations import DuplicateKeyError
    from app.backend.pipeline.campaigns.repo import Taken
    coll = conn.db["campaign_owners"]
    took: list[tuple[str, str | None]] = []                 # (ecosystem, previous owner) of what this call changed, to undo on a refusal
    try:
        for eco in wanted:
            now = m.parse_dt(util.now_z())
            try:
                coll.insert_one({"_id": eco, "campaign_id": campaign_id, "claimed_at": now})
                took.append((eco, None))
                continue
            except DuplicateKeyError:
                pass
            cur = coll.find_one({"_id": eco})
            if cur is None:                                  # released between the insert and the read: try once more
                coll.insert_one({"_id": eco, "campaign_id": campaign_id, "claimed_at": now})
                took.append((eco, None))
                continue
            if cur["campaign_id"] == campaign_id:
                continue
            if holds(conn, cur["campaign_id"]):
                raise Taken(eco, cur["campaign_id"])
            swapped = coll.find_one_and_update({"_id": eco, "campaign_id": cur["campaign_id"]}, {"$set": {"campaign_id": campaign_id, "claimed_at": now}})
            if swapped is None:
                raise Taken(eco, (coll.find_one({"_id": eco}) or {}).get("campaign_id", "?"))
            took.append((eco, cur["campaign_id"]))
    except BaseException:
        for eco, previous in took:
            if previous is None:
                coll.delete_one({"_id": eco, "campaign_id": campaign_id})
            else:
                coll.update_one({"_id": eco, "campaign_id": campaign_id}, {"$set": {"campaign_id": previous}})
        raise


def release(conn, campaign_id) -> int:
    return conn.db["campaign_owners"].delete_many({"campaign_id": campaign_id}).deleted_count
