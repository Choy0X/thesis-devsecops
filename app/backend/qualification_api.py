"""Explicit automatic evidence workflows; never an endpoint for model training."""
from __future__ import annotations

import re
import sqlite3
import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import errors

router = APIRouter(prefix='/api/data')
_IDENTITY = re.compile(r'^[0-9a-f]{32}$')


def _root():
    from .pipeline import versions
    return versions.PROC


class QualificationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str | None = Field(default=None, min_length=1, max_length=160)


class RecoveryRetryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    item_ids: list[str] = Field(min_length=1, max_length=500)


def _recovery_phase(identity, phase_id):
    from .pipeline import npm_qualification_workflow as workflow
    if not _IDENTITY.fullmatch(identity) or not _IDENTITY.fullmatch(phase_id):
        raise ValueError('invalid workflow or phase identity')
    record = workflow.get(identity, root=_root())
    if record is None:
        raise KeyError('workflow not found')
    with sqlite3.connect(workflow.database_path(_root()).as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        phase = db.execute("SELECT id,phase,state FROM phase_outcomes WHERE id=? AND workflow_id=? AND phase IN ('repair','verify')",
                           (phase_id, identity)).fetchone()
    if phase is None:
        from .pipeline import evidence_recovery_history as history
        attempt = history.get_attempt(phase_id, root=_root())
        if attempt.get('workflow_id') != identity:
            raise KeyError('recovery phase not found in this workflow')
        return record, {'id': phase_id, 'phase': 'repair' if attempt['action'] == 'repair' else 'verify',
                        'state': attempt['state']}
    return record, dict(phase)


@router.get('/qualification-workflows/{identity}/recovery-attempts')
def recovery_attempts(identity: str, limit: Annotated[int, Query(ge=1, le=500)] = 100,
                      offset: Annotated[int, Query(ge=0)] = 0):
    from .pipeline import evidence_recovery_history as history, npm_qualification_workflow as workflow
    if not _IDENTITY.fullmatch(identity):
        return errors.error('invalid workflow identity', 422)
    if workflow.get(identity, root=_root()) is None:
        return errors.error('workflow not found', 404)
    return history.attempts(identity, root=_root(), limit=limit, offset=offset)


@router.get('/qualification-workflows/{identity}/phases/{phase_id}/items')
def recovery_items(identity: str, phase_id: str, limit: Annotated[int, Query(ge=1, le=500)] = 100,
                   offset: Annotated[int, Query(ge=0)] = 0,
                   outcome: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
                   retryable: bool | None = None):
    from .pipeline import evidence_recovery_history as history
    try:
        _, phase = _recovery_phase(identity, phase_id)
        try:
            result = history.page(phase_id, root=_root(), limit=limit, offset=offset, outcome=outcome, retryable=retryable)
        except KeyError:
            return {'items': [], 'total': 0, 'limit': limit, 'offset': offset, 'history_available': False,
                    'phase_state': phase['state'], 'reason': 'Per-item history was not recorded for this earlier attempt.'}
        return {**result, 'history_available': result.get('available') is not False, 'phase_state': phase['state']}
    except KeyError as exc:
        return errors.error(str(exc), 404)
    except ValueError as exc:
        return errors.error(str(exc), 422)


@router.get('/qualification-workflows/{identity}/phases/{phase_id}/items/{item_id}')
def recovery_item_detail(identity: str, phase_id: str, item_id: str,
                         observation_id: Annotated[str | None, Query(pattern='^[0-9a-f]{32}$')] = None):
    from .pipeline import evidence_recovery_history as history
    try:
        _recovery_phase(identity, phase_id)
        if not re.fullmatch(r'[0-9a-f]{64}', item_id):
            raise ValueError('invalid recovery item identity')
        result = history.detail(phase_id, item_id, root=_root(), observation_id=observation_id)
        return {**result, 'observations': history.observations(phase_id, item_id, root=_root())}
    except KeyError as exc:
        return errors.error(str(exc), 404)
    except (OSError, ValueError) as exc:
        return errors.error(str(exc), 422)


@router.post('/qualification-workflows/{identity}/phases/{phase_id}/retry')
def retry_recovery_items(identity: str, phase_id: str, body: RecoveryRetryRequest):
    from . import jobs
    from .pipeline import evidence_recovery_history as history, npm_qualification_workflow as workflow, oslock
    try:
        record, phase = _recovery_phase(identity, phase_id)
        if record['state'] not in ('failed', 'blocked'):
            return errors.error('Stop or finish the automatic workflow before retrying selected recovery items.', 409)
        if oslock.held_elsewhere(workflow.lock_path(_root())):
            return errors.error('Another automatic evidence worker still owns this source root.', 409)
        if any(not re.fullmatch(r'[0-9a-f]{64}', item) for item in body.item_ids):
            raise ValueError('invalid recovery item identity')
        selected = history.selected(phase_id, list(dict.fromkeys(body.item_ids)), root=_root())
        if any(item.get('retryable') is not True for item in selected):
            raise ValueError('Selection contains outcomes without an eligible retry.')
        expected = {item['record_id']: item['source_binding']['source_sha256'] for item in selected}
        attempt_id = uuid.uuid4().hex
        job = jobs.start('evidence_recovery', {'tag': record['parent_tag'], 'action': 'repair' if phase['phase'] == 'repair' else 'verify_failed',
            'build_root': str(_root()), 'attempt_id': attempt_id, 'record_ids': sorted(expected), 'expected_sources': expected,
            'include_collected_candidates': True, 'qualification_root_exclusive': True, 'recovery_workflow_id': identity})
        return {'job_id': job.id, 'attempt_id': attempt_id, 'selected': len(expected)}
    except KeyError as exc:
        return errors.error(str(exc), 404)
    except ValueError as exc:
        return errors.error(str(exc), 422)


@router.post('/builds/{tag}/qualification-workflows')
def create_workflow(tag: str, body: QualificationRequest):
    from .pipeline import npm_qualification_workflow as workflow, qualification_scheduler
    try:
        record = workflow.create(tag, root=_root(), request_id=body.request_id)
        job = qualification_scheduler.ensure_job(record, root=_root())
        value = {'workflow': workflow.get(record['id'], root=_root()), 'job_id': job.id if job else None}
        return value if job or record['state'] in ('blocked', 'completed', 'cancelled') else JSONResponse(value, status_code=202)
    except (ValueError, OSError) as exc:
        return errors.error(str(exc), 422)


@router.get('/builds/{tag}/qualification-workflows')
def build_workflows(tag: str, limit: Annotated[int, Query(ge=1, le=500)] = 100,
                    offset: Annotated[int, Query(ge=0)] = 0):
    from .pipeline import npm_qualification_workflow as workflow
    if not re.fullmatch(r'v[1-9][0-9]*', tag):
        return errors.error('invalid immutable dataset tag', 400)
    return workflow.page(root=_root(), parent_tag=tag, limit=limit, offset=offset)


@router.get('/qualification-workflows/{identity}')
def workflow_detail(identity: str, limit: Annotated[int, Query(ge=1, le=500)] = 100,
                     offset: Annotated[int, Query(ge=0)] = 0):
    from .pipeline import npm_qualification_workflow as workflow
    if not _IDENTITY.fullmatch(identity):
        return errors.error('invalid workflow identity', 400)
    record = workflow.get(identity, root=_root())
    if record is None:
        return errors.error('workflow not found', 404)
    return {'workflow': record, 'phases': workflow.phase_page(identity, root=_root(), limit=limit, offset=offset)}


@router.post('/qualification-workflows/{identity}/cancel')
def cancel_workflow(identity: str):
    from . import jobs
    from .pipeline import npm_qualification_workflow as workflow
    if not _IDENTITY.fullmatch(identity):
        return errors.error('invalid workflow identity', 400)
    try:
        record = workflow.cancel(identity, root=_root())
    except KeyError:
        return errors.error('workflow not found', 404)
    for job in jobs.running('qualify_npm_dataset'):
        if job.params.get('workflow_id') == identity:
            jobs.cancel(job.id)
    return {'workflow': record}
