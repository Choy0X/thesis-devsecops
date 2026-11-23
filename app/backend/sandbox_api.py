"""The Docker sandbox: status, containers, images, exec, logs, stats, scan, and the log and terminal websockets."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.backend import errors, sandbox

router = APIRouter()


class CreateSandbox(BaseModel):
    image: str = "python:3.11-slim"
    name: str | None = None
    network: bool = False
    cpus: float = 1.0
    memory_mb: int = 512
    read_only: bool = False
    command: str | None = None


class ActionReq(BaseModel):
    act: str


class ExecReq(BaseModel):
    command: str


class CommitReq(BaseModel):
    repository: str
    tag: str = "latest"


class PullReq(BaseModel):
    image: str


class ScanReq(BaseModel):
    host_path: str = "."


def _guard():
    return None if sandbox.available() else JSONResponse(
        {"docker_available": False, "reason": "Docker daemon not reachable"}, status_code=200)


@router.get("/api/sandbox/status")
def sandbox_status():
    return sandbox.status()


@router.get("/api/sandbox/containers")
def sandbox_list(limit: int | None = None, offset: int = 0, search: str = ''):
    return _inventory_page(sandbox.list_containers(), 'containers', limit, offset, search)


def _inventory_page(rows, key, limit, offset, search):
    """Docker exposes inventory discovery, not offset queries. Bound the UI result.

    Keep the array-shaped SDK contract for legacy callers without a page size.
    Never retrieve source files, terminal output or history bodies to list rows.
    """
    if limit is None:
        return rows
    limit, offset = max(1, min(500, int(limit))), max(0, int(offset))
    term = search.strip().casefold()
    rows = [row for row in rows if not term or term in ' '.join(str(row.get(field) or '')
            for field in ('id', 'full_id', 'name', 'tags', 'image')).casefold()]
    return {key: rows[offset:offset + limit], 'total': len(rows), 'limit': limit, 'offset': offset}


@router.get('/api/sandbox/cleanup')
def sandbox_cleanup_status():
    from app.backend import sandbox_runtime
    value = sandbox_runtime.state()
    return {key: value.get(key) for key in ('cleanup_errors', 'pending_images', 'watchdog_error', 'interactive_containers', 'interactive_proxy_error')}


@router.post('/api/sandbox/cleanup/retry')
def sandbox_cleanup_retry():
    from app.backend import sandbox_runtime
    return _try(sandbox_runtime.reconcile, sandbox._client())


@router.get("/api/sandbox/images")
def sandbox_images():
    return sandbox.images()


@router.post("/api/sandbox/containers")
def sandbox_create(b: CreateSandbox):
    return errors.error('Interactive sandboxes are started by executing chat tasks. Open a project chat to run code.', 409)


@router.post("/api/sandbox/containers/{cid}/action")
def sandbox_action(cid: str, b: ActionReq):
    try:
        if b.act in ('start', 'restart', 'unpause'):
            from app.backend import sandbox_tasks
            sandbox_tasks.require_container(cid)
        return sandbox.action(cid, b.act)
    except PermissionError as e:
        return errors.error(str(e), 409)
    except Exception as e:
        return errors.error(str(e), 400)


@router.post("/api/sandbox/containers/{cid}/exec")
def sandbox_exec(cid: str, b: ExecReq):
    try:
        from app.backend import sandbox_tasks
        sandbox_tasks.require_container(cid)
        return sandbox.exec_run(cid, b.command)
    except PermissionError as e:
        return errors.error(str(e), 409)
    except Exception as e:
        return errors.error(str(e), 400)


@router.get("/api/sandbox/containers/{cid}/logs")
def sandbox_logs(cid: str, tail: int = 200):
    try:
        return {"logs": sandbox.logs(cid, tail)}
    except Exception as e:
        return errors.error(str(e), 400)


@router.get("/api/sandbox/containers/{cid}/stats")
def sandbox_stats(cid: str):
    try:
        return sandbox.stats(cid)
    except Exception as e:
        return errors.error(str(e), 400)


@router.post("/api/sandbox/containers/{cid}/commit")
def sandbox_commit(cid: str, b: CommitReq):
    try:
        return sandbox.commit(cid, b.repository, b.tag)
    except Exception as e:
        return errors.error(str(e), 400)


@router.post("/api/sandbox/images/pull")
def sandbox_pull(b: PullReq):
    if (g := _guard()): return g
    try:
        return sandbox.pull(b.image)
    except Exception as e:
        return errors.error(str(e), 400)


@router.post("/api/sandbox/containers/{cid}/scan")
def sandbox_scan(cid: str, b: ScanReq):
    try:
        return sandbox.scan_target(cid, b.host_path)
    except Exception as e:
        return errors.error(str(e), 400)


# -- policy, files, images and history of the sandbox ------------------------------------------------------------------------------------------------------
# Each of these acts on a container this app made (sandbox._ours) or on an image it made; the reasons for a refusal are sentences.

class LimitsReq(BaseModel):
    cpus: float | None = None
    memory_mb: int | None = None
    pids: int | None = None


class RecreateReq(BaseModel):
    network: bool | None = None
    read_only: bool | None = None


class PinReq(BaseModel):
    image: str


class BuildReq(BaseModel):
    dockerfile: str
    tag: str
    network: bool = False


class PruneReq(BaseModel):
    images: list[str]


class SessionReq(BaseModel):
    label: str


def _try(fn, *args, status: int = 400, **kwargs):
    from app.backend import sandbox_files, sandbox_images, sandbox_policy
    try:
        return fn(*args, **kwargs)
    except FileNotFoundError as e:
        return errors.error(str(e), 404)
    except (sandbox_policy.PolicyError, sandbox_files.FileError, sandbox_images.ImageError, ValueError, PermissionError) as e:
        return errors.error(str(e), status)
    except Exception as e:
        return errors.error(f"{type(e).__name__}: {str(e)[:300]}", 400)


@router.get("/api/sandbox/containers/{cid}/policy")
def sandbox_policy_get(cid: str):
    """What the container has now (CPU share, memory, processes, network, read-only root), the bounds a change may use, and what a person should know about its isolation."""
    from app.backend import sandbox_policy

    def read():
        ct = sandbox._ours(cid)
        now = sandbox_policy.current(ct)
        return {**now, "bounds": sandbox_policy.machine_bounds(), "warnings": sandbox_policy.isolation_warnings(now)}
    return _try(read)


@router.get('/api/sandbox/containers/{cid}/delete-preview')
def sandbox_delete_preview(cid: str):
    from app.backend import sandbox_cleanup
    from app.backend import sandbox_runtime
    return _try(sandbox_cleanup.deletion_preview, cid, sandbox._client(), **sandbox_runtime.cleanup_scope())


@router.put("/api/sandbox/containers/{cid}/limits")
def sandbox_limits(cid: str, b: LimitsReq):
    """Change the CPU share, memory and process limit of a running container in place."""
    from app.backend import sandbox_policy
    return _try(sandbox_policy.update_limits, cid, cpus=b.cpus, memory_mb=b.memory_mb, pids=b.pids)


@router.post("/api/sandbox/containers/{cid}/recreate")
def sandbox_recreate(cid: str, b: RecreateReq):
    """Create the container again with another network or read-only setting (Docker cannot change those on a running one): its filesystem is kept, what ran in memory is lost."""
    return errors.error('Interactive sandboxes are configured and started by their executing chat task.', 409)


@router.get("/api/sandbox/containers/{cid}/files")
def sandbox_files_list(cid: str, path: str = "/work"):
    from app.backend import sandbox_files
    return _try(sandbox_files.listing, cid, path)


@router.get("/api/sandbox/containers/{cid}/files/download")
def sandbox_file_download(cid: str, path: str):
    """One regular file of the container, as a download."""
    from fastapi.responses import Response
    from app.backend import sandbox_files
    got = _try(sandbox_files.download, cid, path)
    if isinstance(got, JSONResponse):
        return got
    name, data = got
    return Response(content=data, media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.put("/api/sandbox/containers/{cid}/files")
async def sandbox_file_upload(cid: str, request: Request, folder: str, name: str, overwrite: bool = False):
    """Put one file (the request body) into a folder of the container; refused above the size limit and over an existing file unless ``overwrite``."""
    from app.backend import sandbox_files
    chunks, size = [], 0
    async for block in request.stream():
        size += len(block)
        if size > sandbox_files.MAX_BYTES:
            return errors.error(f"The file is larger than {sandbox_files.MAX_BYTES // 1048576} MB, which is the most that can be put in.", 413)
        chunks.append(block)
    return _try(sandbox_files.upload, cid, folder, name, b"".join(chunks), overwrite)


@router.get("/api/sandbox/containers/{cid}/changes")
def sandbox_changes(cid: str):
    from app.backend import sandbox_files
    return _try(sandbox_files.changes, cid)


@router.post('/api/sandbox/containers/{cid}/files/action')
def sandbox_file_action(cid: str, body: dict):
    from app.backend import sandbox_files
    return _try(sandbox_files.mutate, cid, body.get('operation'), body.get('path'), body.get('destination'))


@router.get("/api/sandbox/image-list")
def sandbox_image_list(limit: int | None = None, offset: int = 0, search: str = ''):
    """Every image on this computer with its digest, size, age, how many containers use it, whether this app made it and whether it is the pinned verification image."""
    from app.backend import sandbox_images
    if not sandbox.available():
        page = _inventory_page([], 'images', limit, offset, search)
        return {"docker_available": False, **(page if isinstance(page, dict) else {'images': page}), "pinned": None}
    def read():
        page = _inventory_page(sandbox_images.listing(owned_only=True), 'images', limit, offset, search)
        return {'docker_available': True, **(page if isinstance(page, dict) else {'images': page}), 'pinned': sandbox_images.pinned()}
    return _try(read)


@router.post("/api/sandbox/images/pin")
def sandbox_pin(b: PinReq):
    from app.backend import sandbox_images
    return _try(sandbox_images.pin, b.image)


@router.delete("/api/sandbox/images/pin")
def sandbox_unpin():
    from app.backend import sandbox_images
    return sandbox_images.unpin()


@router.post("/api/sandbox/images/build")
def sandbox_build(b: BuildReq):
    """Build an image from a Dockerfile as a job with the build log; the steps have no network unless asked."""
    from app.backend import jobs, sandbox_images
    if not sandbox.available():
        return errors.error("Docker daemon not reachable", 503)
    if not b.dockerfile.strip() or len(b.dockerfile) > sandbox_images.MAX_DOCKERFILE:
        return errors.error("The Dockerfile is empty or too long.", 400)
    if not sandbox_images.TAG.match(b.tag or ""):
        return errors.error("The tag is lower-case letters, digits and . _ / - with an optional :version.", 400)
    try:
        job = jobs.start("sandbox_build_image", {"dockerfile": b.dockerfile, "tag": b.tag, "network": b.network})
    except jobs.TooManyJobs as e:
        return errors.error(str(e), 409)
    return {"id": job.id, "stage": job.stage, "tag": b.tag}


@router.get("/api/sandbox/images/prune-plan")
def sandbox_prune_plan():
    from app.backend import sandbox_images
    return _try(sandbox_images.prune_plan)


@router.post("/api/sandbox/images/prune")
def sandbox_prune(b: PruneReq):
    from app.backend import sandbox_images
    return _try(sandbox_images.prune, b.images)


@router.get("/api/sandbox/containers/{cid}/history")
def sandbox_history_get(cid: str, limit: int = 200):
    """The commands and changes recorded for the container, and the samples of its CPU and memory."""
    from app.backend import sandbox_history

    def read():
        name = sandbox._ours(cid).name
        return {"name": name, "events": sandbox_history.events(name, limit), "samples": sandbox_history.samples(name, 600), "sessions": sandbox_history.sessions(name)}
    return _try(read)


@router.get("/api/sandbox/history")
def sandbox_history_known(limit: int | None = None, offset: int = 0, search: str = ''):
    """Every container that has a record, deleted ones included."""
    from app.backend import sandbox_history
    page = _inventory_page(sandbox_history.known(), 'containers', limit, offset, search)
    return page if isinstance(page, dict) else {'containers': page}


@router.get("/api/sandbox/history/{name}")
def sandbox_history_named(name: str, limit: int = 200):
    from app.backend import sandbox_history
    return _try(lambda: {"name": name, "events": sandbox_history.events(name, limit), "samples": sandbox_history.samples(name, 600), "sessions": sandbox_history.sessions(name)})


@router.delete("/api/sandbox/history/{name}")
def sandbox_history_forget(name: str):
    from app.backend import sandbox_history
    return _try(sandbox_history.forget, name)


@router.post("/api/sandbox/history/{name}/sessions")
def sandbox_session_save(name: str, b: SessionReq):
    from app.backend import sandbox_history
    return _try(sandbox_history.save_session, name, b.label)


@router.get("/api/sandbox/history/{name}/sessions/{sid}")
def sandbox_session_get(name: str, sid: str):
    from app.backend import sandbox_history
    return _try(sandbox_history.session, name, sid)


@router.delete("/api/sandbox/history/{name}/sessions/{sid}")
def sandbox_session_delete(name: str, sid: str):
    from app.backend import sandbox_history
    return _try(sandbox_history.delete_session, name, sid)


@router.websocket("/ws/sandbox/{cid}/logs")
async def sandbox_log_stream(ws: WebSocket, cid: str):
    await ws.accept()
    try:
        while True:
            try:
                # Docker calls block for 1-2 s: run them off the event loop so one
                # open container view does not freeze the whole backend
                logs = await asyncio.to_thread(sandbox.logs, cid, 200)
                stats = await asyncio.to_thread(sandbox.stats, cid)
                await ws.send_json({"type": "logs", "data": logs})
                await ws.send_json({"type": "stats", "data": stats})
            except WebSocketDisconnect:
                break
            except RuntimeError as e:
                if 'close' in str(e).lower() or 'disconnect' in str(e).lower():
                    break
                raise
            except Exception as e:
                try:
                    await ws.send_json({"type": "error", "message": str(e)})
                except (WebSocketDisconnect, RuntimeError):
                    pass
                break
            await asyncio.sleep(1.5)
    except WebSocketDisconnect:
        pass


@router.websocket("/ws/sandbox/{cid}/terminal")
async def sandbox_terminal(ws: WebSocket, cid: str):
    await ws.accept()
    from app.backend import sandbox_tasks
    try:
        sandbox_tasks.require_container(cid)
    except sandbox_tasks.TaskRequired as error:
        await ws.send_text(f'{error}\r\n')
        await ws.close(code=1008)
        return
    if not sandbox.available():
        await ws.send_text("Docker is not available on this machine.\r\n")
        await ws.close()
        return
    try:
        exec_id, raw = sandbox.exec_pty(cid)
    except Exception as e:
        await ws.send_text(f"terminal error: {e}\r\n")
        await ws.close()
        return
    raw.setblocking(True)
    loop = asyncio.get_event_loop()
    state = {"stop": False}

    async def pump_out():
        while not state["stop"]:
            try:
                data = await loop.run_in_executor(None, raw.recv, 4096)
            except Exception:  # ignored: a receive error means the terminal closed, so the pump loop ends
                break
            if not data:
                break
            try:
                await ws.send_bytes(data)
            except Exception:  # ignored: a send error means the browser closed, so the pump loop ends
                break

    out_task = asyncio.create_task(pump_out())
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                raw.sendall(msg["bytes"])
            elif msg.get("text") is not None:
                t = msg["text"]
                if t.startswith("\x00resize:"):
                    try:
                        rows, cols = t.split(":", 1)[1].split("x")
                        sandbox.exec_resize(exec_id, int(rows), int(cols))
                    except Exception:  # ignored: a failed resize must not close the terminal
                        pass
                else:
                    raw.sendall(t.encode())
    except WebSocketDisconnect:
        pass
    finally:
        state["stop"] = True
        out_task.cancel()
        try:
            raw.close()
        except Exception:  # ignored: closing a socket that may already be closed
            pass
