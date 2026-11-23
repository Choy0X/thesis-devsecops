"""Manual review JSON files: background export, validation and selected imports."""
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from app.backend import errors, jobs
from app.backend.pipeline import fsio, review_exchange
from app.backend.pipeline.context_limits import DEFAULT_BYTES, file_bytes
from app.backend.pipeline.oslock import Busy
from app.backend.pipeline.review_exchange_jobs import ReviewTasks

router = APIRouter(prefix='/exchange')


def result(action):
    try:
        return action()
    except (Busy, jobs.TooManyJobs) as error:
        return errors.error(str(error), 409)
    except (ValueError, OSError, KeyError, TypeError) as error:
        return errors.error(str(error), 400)


@router.post('/exports')
def create_export(body: dict):
    def create():
        if set(body) - {'ecosystem', 'pair_keys', 'max_file_bytes'}:
            raise ValueError('Unsupported review export fields')
        manager = review_exchange.default()
        ids = manager.selection(ecosystem=body.get('ecosystem'), pair_keys=body.get('pair_keys'))
        if not ids:
            raise ValueError('No selected candidates are waiting for review')
        doc = manager.create(ids, max_file_bytes=body.get('max_file_bytes'))
        try:
            return manager.launch(doc['export_id'])
        except jobs.TooManyJobs:
            return manager.status(doc['export_id'])
    return result(create)


@router.get('/exports/{eid}')
def export_status(eid: str):
    return result(lambda: review_exchange.default().reconcile(eid))


@router.post('/exports/{eid}/resume')
def resume(eid: str):
    return result(lambda: review_exchange.default().launch(eid))


@router.post('/exports/{eid}/cancel')
def cancel(eid: str):
    def stop():
        manager = review_exchange.default()
        doc = manager.status(eid)
        job = jobs.get(doc.get('job_id')) if doc.get('job_id') else None
        if job and job.stage == manager.export_stage and job.params.get('export_id') == eid:
            jobs.cancel(job.id)
        return manager.reconcile(eid)
    return result(stop)


@router.get('/exports/{eid}/files/{index}')
def download(eid: str, index: int):
    def serve():
        manager = review_exchange.default()
        path = manager.download(eid, index)
        return FileResponse(path, filename=manager.status(eid)['files'][index]['name'], media_type='application/json')
    return result(serve)


@router.post('/validations')
async def create_validation(request: Request, max_file_bytes: int = DEFAULT_BYTES, filename: str = 'returned.json'):
    try:
        limit = file_bytes(max_file_bytes)
        tasks, task_id = ReviewTasks(), uuid.uuid4().hex
        target = tasks.upload_path(task_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = fsio.part_path(target)
        size = 0
        try:
            with part.open('wb') as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise ValueError('Returned JSON exceeds the selected file size')
                    stream.write(chunk)
                fsio.fsync_handle(stream)
            fsio.replace(part, target)
        finally:
            part.unlink(missing_ok=True)
        return tasks.register_validation(task_id, filename=filename, max_file_bytes=limit)
    except (ValueError, OSError, KeyError, TypeError) as error:
        return errors.error(str(error), 400)


@router.get('/validations/{task_id}')
def validation_status(task_id: str):
    return result(lambda: ReviewTasks().status('validation', task_id))


@router.post('/validations/{task_id}/start')
def start_validation(task_id: str):
    return result(lambda: ReviewTasks().start('validation', task_id))


@router.post('/imports')
def create_import(body: dict):
    if set(body) - {'validation_id', 'selected'}:
        return errors.error('Unsupported import fields', 400)
    return result(lambda: ReviewTasks().create_import(body.get('validation_id'), body.get('selected')))


@router.get('/imports/{task_id}')
def import_status(task_id: str):
    return result(lambda: ReviewTasks().status('import', task_id))


@router.post('/imports/{task_id}/start')
def start_import(task_id: str):
    return result(lambda: ReviewTasks().start('import', task_id))
