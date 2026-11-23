"""Durable, independently queued validation and import jobs for review JSON."""
from __future__ import annotations

import json
import time
import uuid

from app.backend.pipeline import fsio
from app.backend.pipeline.context_json_export import identity
from app.backend.pipeline.context_limits import DEFAULT_BYTES, file_bytes
from app.backend.pipeline.oslock import FileLock

STAGES = {'validation': 'review_exchange_validate', 'import': 'review_exchange_import'}


class ReviewTasks:
    def __init__(self, exports=None):
        if exports is None:
            from app.backend.pipeline.review_exchange import default
            exports = default()
        self.exports = exports
        self.root = exports.exchange.root / 'tasks'

    def path(self, kind, task_id):
        if kind not in STAGES:
            raise ValueError('unknown review task kind')
        return self.root / kind / (identity(task_id) + '.json')

    def read(self, kind, task_id):
        return json.loads(self.path(kind, task_id).read_bytes())

    def save(self, kind, doc):
        fsio.write_json(self.path(kind, doc['id']), doc)

    def upload_path(self, task_id):
        return self.root / 'uploads' / (identity(task_id) + '.json')

    def _task(self, kind, task_id=None, **values):
        doc = {'id': task_id or uuid.uuid4().hex, 'job_id': None, 'state': 'queued', 'processed': 0,
               'total': 0, 'elapsed_seconds': 0, 'items': [], 'missing': [], 'error': None,
               'imported': 0, 'unchanged': 0, 'incomplete': 0, 'conflicts': 0, 'rejected': 0, **values}
        self.save(kind, doc)
        return self.status(kind, doc['id'])

    def create_validation(self, raw, *, filename='returned.json', max_file_bytes=DEFAULT_BYTES):
        limit = file_bytes(max_file_bytes)
        if len(raw) > limit:
            raise ValueError('Returned JSON exceeds the selected file size')
        task_id = uuid.uuid4().hex
        fsio.write_with(self.upload_path(task_id), lambda part: part.write_bytes(raw))
        return self.register_validation(task_id, filename=filename, max_file_bytes=limit)

    def register_validation(self, task_id, *, filename, max_file_bytes):
        limit = file_bytes(max_file_bytes)
        if self.upload_path(task_id).stat().st_size > limit:
            raise ValueError('Returned JSON exceeds the selected file size')
        return self._task('validation', task_id, filename=str(filename).replace('\\', '/').split('/')[-1][:255],
                          max_file_bytes=limit)

    def create_import(self, validation_id, selected):
        doc = self.read('validation', validation_id)
        if doc['state'] != 'complete':
            raise ValueError('Wait for this file to finish validation before queuing its import')
        if (not isinstance(selected, list) or not selected or any(not isinstance(key, str) for key in selected)
                or len(set(selected)) != len(selected)):
            raise ValueError('Select distinct valid item IDs to import')
        allowed = {item['item_id'] for item in doc['items'] if item['status'] == 'valid'}
        if set(selected) - allowed:
            raise ValueError('The selection contains unknown or invalid review items')
        return self._task('import', validation_id=validation_id, selected=selected, total=len(selected))

    def status(self, kind, task_id):
        from app.backend import jobs
        doc = self.read(kind, task_id)
        result = {key: value for key, value in doc.items() if key != 'selected'}
        job = jobs.get(doc['job_id']) if doc.get('job_id') else None
        if doc['state'] in ('queued', 'running') and doc.get('job_id') and (job is None or job.status != 'running'):
            result.update(state='interrupted', error='The previous worker stopped. Retry to resume this saved task.')
        elif doc['state'] == 'running' and doc.get('updated_at_epoch'):
            result['elapsed_seconds'] += max(0, time.time() - doc['updated_at_epoch'])
        return result

    def start(self, kind, task_id):
        from app.backend import jobs
        with FileLock(self.path(kind, task_id).with_suffix('.lock'), note=False):
            doc = self.read(kind, task_id)
            if doc['state'] == 'complete':
                return self.status(kind, task_id)
            job = jobs.get(doc['job_id']) if doc.get('job_id') else None
            if job is not None and job.status == 'running':
                return self.status(kind, task_id)
            job = jobs.start(STAGES[kind], {'task_id': task_id})
            doc.update(state='queued', job_id=job.id, error=None)
            self.save(kind, doc)
        return self.status(kind, task_id)

    def run(self, kind, task_id, *, progress=lambda **state: None):
        with FileLock(self.path(kind, task_id).with_suffix('.lock'), note=False):
            doc = self.read(kind, task_id)
            if doc['state'] == 'complete':
                return self.status(kind, task_id)
            began, earlier, last_save = time.monotonic(), doc['elapsed_seconds'], 0.0
            doc.update(state='running', error=None, updated_at_epoch=time.time())
            self.save(kind, doc)

            def update(**state):
                nonlocal last_save
                doc.update(state)
                elapsed = time.monotonic() - began
                doc.update(elapsed_seconds=earlier + elapsed, updated_at_epoch=time.time())
                if elapsed - last_save >= 1 or doc['processed'] == doc['total']:
                    last_save = elapsed
                    self.save(kind, doc)
                    progress(state=doc['state'], label='Validating review JSON' if kind == 'validation' else 'Importing external review decisions',
                             processed=doc['processed'], total=doc['total'], elapsed_seconds=doc['elapsed_seconds'],
                             counts={key: doc[key] for key in ('imported', 'unchanged', 'incomplete', 'conflicts', 'rejected')})
            try:
                validation = doc if kind == 'validation' else self.read('validation', doc['validation_id'])
                path = self.upload_path(validation['id'])
                if path.stat().st_size > validation['max_file_bytes']:
                    raise ValueError('Returned JSON exceeds its recorded file limit')
                response = json.loads(path.read_bytes(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite JSON number')))
                if kind == 'validation':
                    report = self.exports.exchange.preview(response, max_bytes=validation['max_file_bytes'], progress=update)
                else:
                    report = self.exports.exchange.apply(response, selected=doc['selected'],
                                max_bytes=validation['max_file_bytes'], progress=update)
                doc.update(report, state='complete', error=None)
                doc['total'] = doc['processed'] = len(report['items'])
                doc['rejected'] = sum(item['status'] != 'valid' for item in report['items'])
            except Exception as error:
                doc.update(state='failed', error=str(error))
            finally:
                doc.update(elapsed_seconds=earlier + time.monotonic() - began, updated_at_epoch=time.time())
                self.save(kind, doc)
            return self.status(kind, task_id)
