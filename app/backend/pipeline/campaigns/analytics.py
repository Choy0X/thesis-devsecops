"""The analytics of one campaign: where it is, what went wrong, how much of what it read reached a code pair, how fast it went, and what it produced.

Everything here is read from what the campaign stored (its steps, events, samples and baseline) and from the commits and pairs it queued and mined (they carry its id). A campaign of the
first system has only its steps and the state of the ecosystems, and says so. The answers are kept for a few seconds while the campaign runs and for minutes once it has ended.
"""
from __future__ import annotations

import math
import threading
import time
from collections import Counter

from app.backend.pipeline import codestore
from app.backend.pipeline.campaigns import plan as planmod, repo, targets
from app.backend.pipeline.db import mappers as m

NOTE_LEGACY = "No history was recorded for this campaign: it was made by the earlier kind of campaign, which kept only the state of its steps."
RUNNING_TTL, FINAL_TTL, MIX_TTL = 8.0, 300.0, 60.0
MAX_BUCKETS = 288
FUNNEL_KEYS = ("advisories", "with_fix_pointer", "mined", "pairs", "usable_pairs")

_cache: dict[str, tuple[float, dict]] = {}
_mix: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def forget() -> None:
    with _lock:
        _cache.clear()
        _mix.clear()


def _epoch(text) -> float | None:
    d = m.parse_dt(text)
    return d.timestamp() if d else None


def _seconds(start, end, now) -> float | None:
    a = _epoch(start)
    if a is None:
        return None
    b = _epoch(end) if end else now
    return round(max(0.0, (b or now) - a), 1)


# -- the pieces ------------------------------------------------------------------------------------------------------------------

def timeline(c: dict, stored: dict, now: float) -> dict:
    """Every step the campaign takes, in order, with where it is and how long it took; the running step has a time left only when it reports how far it has got."""
    plan = c.get("plan")
    wanted = ([(t, s) for t in c["ecosystems"] for s in targets.stages_for(planmod.target_stages(plan), t)] + [(planmod.TARGET_ALL, s) for s in planmod.global_stages(plan)]
              if plan else sorted(stored))
    progress = c.get("progress") or {}
    rows = []
    for key in wanted:
        s = stored.get(key) or {"target": key[0], "stage": key[1], "status": "pending", "started_at": None, "finished_at": None, "error": None, "attempts": 0, "detail": {}, "progress": {}}
        running = s["status"] == "running"
        row = {"target": s["target"], "stage": s["stage"], "label": planmod.STAGE_LABELS.get(s["stage"], s["stage"]), "status": s["status"], "attempts": s.get("attempts", 0),
               "started_at": s.get("started_at"), "finished_at": s.get("finished_at"), "seconds": _seconds(s.get("started_at"), s.get("finished_at"), now) if s.get("started_at") else None,
               "error": s.get("error"), "eta_seconds": None, "fraction": None}
        if s.get("active_seconds") is not None:
            row["seconds"] = s["active_seconds"] + ((_seconds(s.get("started_at"),None,now) or 0) if running else 0)
        if running and progress.get("target") == s["target"] and progress.get("stage") == s["stage"]:
            done, total = progress.get("processed"), progress.get("total")
            if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total > 0:
                row["fraction"] = round(min(1.0, done / total), 3)
                if done > 0 and row["seconds"]:
                    row["eta_seconds"] = round(row["seconds"] * (total - done) / done)
        rows.append(row)
    counts = Counter(r["status"] for r in rows)
    return {"steps": rows, "total": len(rows), "done": counts["done"] + counts["quota"] + counts["skipped"], "errors": counts["error"], "percent": progress.get("percent"),
            "elapsed_seconds": _seconds(c.get("started_at") or c.get("created_at"), c.get("finished_at") if c["state"] in repo.RELEASING else None, now),
            "active_seconds": round(sum(r["seconds"] or 0 for r in rows if r["status"] in ("done", "quota", "error", "paused", "skipped", "running")), 1)}


def error_summary(events: list[dict], outcomes: dict) -> dict:
    """The errors of the steps by cause, the latest of them, and the reasons the campaign's own commits failed or had no code."""
    errors = [e for e in events if e["kind"] in ("stage_error","stage_retry")]
    by_cause = Counter((e["data"] or {}).get("cause", "unknown") for e in errors)
    latest = [{"at": e["at"], **{k: (e["data"] or {}).get(k) for k in ("target", "stage", "cause", "message")}} for e in errors[-20:]]
    return {"by_cause": [{"cause": k, "n": n} for k, n in by_cause.most_common()], "latest": latest, "commits": outcomes.get("reasons", []),
            "forced_kills": sum(1 for e in events if e["kind"] == "forced_kill"), "interruptions": sum(1 for e in events if e["kind"] == "interrupted")}


def funnel(c: dict, outcomes: dict, mix: dict, now_funnel: dict) -> dict:
    """What the campaign added: the ecosystem funnel now against the one when it started, per ecosystem, and as the stages of a flow chart."""
    before = (c.get("baseline") or {}).get("funnel") or {}
    per = {}
    for eco in c["ecosystems"]:
        was, is_now = before.get(eco) or {}, now_funnel.get(eco) or {}
        per[eco] = {"before": {k: was.get(k, 0) for k in FUNNEL_KEYS}, "now": {k: is_now.get(k, 0) for k in FUNNEL_KEYS},
                    "gained": {k: max(0, is_now.get(k, 0) - was.get(k, 0)) for k in FUNNEL_KEYS}}
    total = {k: sum(p["gained"][k] for p in per.values()) for k in FUNNEL_KEYS}
    commit_lost = [{"id": f"{r['status']}:{r['reason']}", "label": f"{r['status'].replace('_', ' ')}: {r['reason']}", "count": r["n"]} for r in outcomes.get("reasons", [])]
    by_status = outcomes.get("by_status", {})
    flow = {"stages": [
        {"id": "advisories", "label": "Advisories read", "unit": "advisories", "count": total["advisories"], "lost": []},
        {"id": "with_fix_pointer", "label": "With a fix commit", "unit": "advisories", "count": total["with_fix_pointer"],
         "lost": [{"id": "no_fix", "label": "No fix commit found", "count": max(0, total["advisories"] - total["with_fix_pointer"])}]},
        {"id": "commits", "label": "Fix commits worked on", "unit": "commits", "count": by_status.get("mined", 0), "lost": commit_lost},
        {"id": "usable_pairs", "label": "Usable code pairs", "unit": "pairs", "count": mix.get("pairs", 0), "lost": []}], "set_aside": {}}
    return {"ecosystems": per, "total": total, "flow": flow, "has_baseline": bool(before)}


def throughput(c: dict, mined: list[tuple[str, int]], samples: list[dict], timeline_rows: list[dict], *, performance=None) -> dict:
    """Commits and pairs per bucket of time (ten minutes, longer for a long campaign), the speed while mining, and what the samples saw: memory, the queue, the GitHub quota, waits."""
    times = [(_epoch(t), n) for t, n in mined if _epoch(t) is not None]
    buckets = []
    if times:
        first, last = min(t for t, _ in times), max(t for t, _ in times)
        width = 600 * max(1, math.ceil((last - first) / 600 / MAX_BUCKETS))
        start = first - first % width
        counts: dict[float, list[int]] = {}
        for t, n in times:
            b = counts.setdefault(t - t % width, [0, 0])
            b[0] += 1
            b[1] += n
        at = start
        while at <= last:
            commits, pairs = counts.get(at, [0, 0])
            buckets.append({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)), "commits": commits, "pairs": pairs})
            at += width
    mining = sum(r["seconds"] or 0 for r in timeline_rows if r["stage"] == "mine")
    cumulative=((performance or {}).get("timings",{}).get("mine") or {}).get("seconds")
    if isinstance(cumulative,(int,float)) and cumulative>0:mining=cumulative
    per_hour = {"commits": round(len(times) / (mining / 3600), 1), "pairs": round(sum(n for _, n in times) / (mining / 3600), 1)} if mining > 0 and times else None
    step = max(1, len(samples) // MAX_BUCKETS)
    series = [{"at": s["at"], "rss_mb": s.get("rss_mb"), "pending": s.get("pending"), "remaining": (s.get("quota") or {}).get("remaining"), "waiting": bool(s.get("waiting")),
               "stage": s.get("stage"), "target": s.get("target")} for s in samples[::step]]
    waits, begun = [], None
    for s in samples:
        if s.get("waiting") and begun is None:
            begun = s["at"]
        elif not s.get("waiting") and begun is not None:
            waits.append({"start": begun, "end": s["at"]})
            begun = None
    if begun is not None:
        waits.append({"start": begun, "end": None})
    return {"timing_source":"campaign_performance" if cumulative else "recorded_steps","bucket_seconds": (600 * max(1, math.ceil((max(t for t, _ in times) - min(t for t, _ in times)) / 600 / MAX_BUCKETS))) if times else 600, "buckets": buckets, "per_hour": per_hour,
            "samples": series, "waits": waits}


def context(c: dict, stored: dict) -> dict | None:
    """The model-written context: how much of the pairs had it before and after, what was written, what failed, how long it took."""
    if not (c.get("plan") or {}).get("stages", {}).get("enrich"):
        return None
    rows = [s for (t, st), s in stored.items() if st == "enrich"]
    before = {l: {"total": 0, "enriched": 0} for l in ("weak", "partial", "strong")}
    after = {l: {"total": 0, "enriched": 0} for l in before}
    written = failed = candidates = 0
    seconds = 0.0
    model = prompt = None
    for s in rows:
        d = s.get("detail") or {}
        written += d.get("enriched", 0)
        failed += d.get("failed", 0)
        candidates += d.get("candidates", 0)
        seconds += (_seconds(s.get("started_at"), s.get("finished_at"), time.time()) or 0) if s.get("finished_at") else 0
        model, prompt = d.get("model") or model, d.get("prompt_version") or prompt
        for level in before:
            for bag, key in ((before, "coverage_before"), (after, "coverage_after")):
                got = (d.get(key) or {}).get(level) or {}
                bag[level]["total"] += got.get("total", 0)
                bag[level]["enriched"] += got.get("enriched", 0)
    return {"model": model, "prompt_version": prompt, "candidates": candidates, "written": written, "failed": failed, "seconds": round(seconds, 1),
            "seconds_per_pair": round(seconds / written, 2) if written else None, "coverage_before": before, "coverage_after": after,
            "steps": [{"target": s["target"], "status": s["status"]} for s in rows]}


# -- the whole -------------------------------------------------------------------------------------------------------------------

def _mix_of(campaign_id: str) -> dict:
    with _lock:
        hit = _mix.get(campaign_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    value = codestore.campaign_mix(campaign_id)
    with _lock:
        _mix[campaign_id] = (time.monotonic() + MIX_TTL, value)
    return value


def build(conn, campaign_id: str, *, now: float | None = None) -> dict | None:
    """Everything the Analytics tab shows for one campaign, or None if there is no such campaign."""
    from app.backend.pipeline import store
    c = repo.get(conn, campaign_id)
    if c is None:
        return None
    now = time.time() if now is None else now
    stored = repo.steps(conn, campaign_id)
    head = {"campaign_id": campaign_id, "name": c["name"], "state": c["state"], "legacy": c["legacy"], "ecosystems": c["ecosystems"], "created_at": c["created_at"],
            "started_at": c["started_at"], "finished_at": c["finished_at"], "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
    tl = timeline(c, stored, now)
    try:
        now_funnel = store.funnel_cached(30)
    except Exception:  # ignored: the funnel is a display extra
        now_funnel = {}
    if c["legacy"]:
        return {**head, "note": NOTE_LEGACY, "timeline": tl, "errors": error_summary([], {}), "funnel": funnel(c, {}, {}, now_funnel), "throughput": None, "mix": None, "context": None}
    events = repo.events(conn, campaign_id, limit=1000)
    samples = repo.samples(conn, campaign_id)
    outcomes = codestore.campaign_outcomes(campaign_id)
    mix = _mix_of(campaign_id)
    from app.backend import paths
    import json
    try:
        performance=json.loads((paths.interim_dir()/"acceleration"/"campaigns"/campaign_id/"performance.json").read_text(encoding="utf-8"))
    except (OSError,ValueError):performance={}
    return {**head, "note": None, "timeline": tl, "errors": error_summary(events, outcomes), "funnel": funnel(c, outcomes, mix, now_funnel),
            "throughput": throughput(c, codestore.campaign_mined(campaign_id), samples, tl["steps"],performance=performance), "mix": mix, "context": context(c, stored), "outcomes": outcomes}


def cached(conn, campaign_id: str) -> dict | None:
    """``build``, kept for a few seconds while the campaign runs and for minutes once it has ended."""
    with _lock:
        hit = _cache.get(campaign_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    value = build(conn, campaign_id)
    if value is not None:
        ttl = RUNNING_TTL if value["state"] in ("running", "pausing") else FINAL_TTL
        with _lock:
            _cache[campaign_id] = (time.monotonic() + ttl, value)
    return value
