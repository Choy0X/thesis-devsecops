"""Background preparation and individually downloadable JSON context packets."""
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.backend import errors
from app.backend.pipeline import context_json_export

router = APIRouter(prefix='/api/code/enrich/json')


def result(call):
    from app.backend.pipeline.oslock import Busy
    from app.backend.jobs import TooManyJobs
    try:
        return call()
    except (Busy, TooManyJobs) as error:
        return errors.error(str(error), 409)
    except (ValueError, OSError, KeyError, TypeError) as error:
        return errors.error(str(error), 400)


def create_export(body):
    from app.backend.pipeline.acceleration.context_gate import require_ready
    if set(body) - {'ids', 'campaign_id', 'max_file_bytes'}:
        raise ValueError('Unsupported JSON export fields')
    manager = context_json_export.default()
    # Cheap shape validation precedes the ownership query.
    created = manager.create(body.get('ids'), campaign_id=body.get('campaign_id'), max_file_bytes=body.get('max_file_bytes'))
    require_ready(body.get('campaign_id'), body['ids'])
    return manager.launch(created['export_id'])


@router.post('/exports')
def create(body: dict):
    return result(lambda: create_export(body))


@router.get('/exports/{eid}')
def status(eid: str):
    return result(lambda: context_json_export.default().reconcile(eid))


@router.post('/exports/{eid}/resume')
def resume(eid: str):
    return result(lambda: context_json_export.default().launch(eid))


@router.post('/exports/{eid}/cancel')
def cancel(eid: str):
    def stop():
        from app.backend import jobs
        manager = context_json_export.default()
        doc = manager.status(eid)
        job = jobs.get(doc['job_id']) if doc['job_id'] else None
        if job and job.stage == 'context_review_export' and job.params.get('export_id') == eid:
            jobs.cancel(job.id)
        return manager.reconcile(eid)
    return result(stop)


@router.get('/exports/{eid}/files/{index}')
def download(eid: str, index: int):
    def serve():
        manager = context_json_export.default()
        path = manager.download(eid, index)
        return FileResponse(path, filename=manager.status(eid)['files'][index]['name'], media_type='application/json')
    return result(serve)


@router.post('/imports/preview')
async def preview(request: Request, max_file_bytes: int | None = None):
    # Sequential browser uploads and a per-file bound avoid aggregate upload caps.
    raw = bytearray()
    from app.backend.pipeline.context_limits import file_bytes
    try:
        limit = file_bytes(context_json_export.MAX_BYTES if max_file_bytes is None else max_file_bytes)
    except ValueError as error:
        return errors.error(str(error), 400)
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit:
            return errors.error(f'Returned JSON exceeds {limit:,} UTF-8 bytes', 400)
        raw.extend(chunk)
    return await run_in_threadpool(lambda: result(lambda: context_json_export.default().preview(bytes(raw), max_file_bytes=limit)))


@router.post('/imports/{upload_id}')
def apply(upload_id: str, body: dict):
    if set(body) - {'selected'}:
        return errors.error('Unsupported import fields', 400)
    return result(lambda: context_json_export.default().apply(upload_id, body.get('selected')))
