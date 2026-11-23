"""Start, list, read and cancel jobs, and the websocket that streams one."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.backend import errors, jobs

router = APIRouter()


class JobRequest(BaseModel):
    stage: str
    params: dict = {}


@router.post("/api/jobs")
def start_job(req: JobRequest):
    try:
        job = jobs.start(req.stage, req.params)
    except ValueError as e:
        return errors.error(str(e), 400)
    return job.public()


@router.get("/api/jobs")
def list_jobs():
    return jobs.listing()


TAIL = 2000          # lines a reader gets when it names no starting point
PAGE = 10_000        # most lines one answer carries
CHUNK = 1000         # lines per websocket message


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str, tail: int | None = None, after: int | None = None):
    """The job and a bounded part of its output: the last ``tail`` lines (2000 by default), or the lines from number ``after`` on. The
    answer says where it starts (``log_start``) and how many lines the job has printed (``log_count``)."""
    job = jobs.get(job_id)
    if not job:
        return errors.error("not found", 404)
    total = len(job.logs)
    start = min(max(0, after), total) if after is not None else max(0, total - min(max(1, tail or TAIL), PAGE))
    return {**job.public(), "logs": job.logs.lines(start, start + PAGE), "log_start": start}


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    if not jobs.cancel(job_id):
        return errors.error("job not running", 409)
    return {"ok": True}


@router.delete("/api/jobs/{job_id}")
def forget_job(job_id: str):
    """Remove a job's record for good (it is not the one thing that owns it, like a campaign's own delete already does for its job; this is for
    one that is, like a leftover failed or cut-off entry nothing else will ever clean up). A running job is refused; an id nobody has is a no-op."""
    if not jobs.forget(job_id):
        return errors.error("job is still running", 409)
    return {"ok": True}


@router.websocket("/ws/jobs/{job_id}")
async def job_logs(ws: WebSocket, job_id: str, after: int | None = None):
    """Streams a job: log lines in chunks, structured progress when it changes, then the final status. Every ``logs`` message says the
    number of its first line (``start``). A client that reconnects names the next line it needs (``?after=N``) and gets only newer ones;
    without it the stream starts at the last 2000 lines."""
    await ws.accept()
    last_progress = None
    first = jobs.get(job_id)
    cursor = (min(max(0, after), len(first.logs)) if after is not None else max(0, len(first.logs) - TAIL)) if first else 0

    async def send_lines(job) -> int:
        nonlocal cursor
        total = len(job.logs)
        while cursor < total:
            chunk = job.logs.lines(cursor, min(total, cursor + CHUNK))
            if not chunk:
                break
            await ws.send_json({"type": "logs", "lines": chunk, "start": cursor})
            cursor += len(chunk)
        return total

    try:
        while True:
            job = jobs.get(job_id)
            if not job:
                await ws.send_json({"type": "error", "message": "job not found"})
                break
            await send_lines(job)
            if job.progress != last_progress:
                last_progress = job.progress
                await ws.send_json({"type": "progress", "progress": job.progress})
            if job.status != "running":
                await send_lines(job)
                await ws.send_json({"type": "status", "status": job.status, "error": job.error})
                break
            await asyncio.sleep(0.3)
    except WebSocketDisconnect:
        pass
