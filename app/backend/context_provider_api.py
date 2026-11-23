"""Opt-in free explanation profiles. No global model settings or billing APIs."""
import json
import sqlite3

from fastapi import APIRouter, Request

from app.backend import errors
from app.backend.pipeline import context_automation
from app.backend.pipeline.context_providers import catalog

router = APIRouter(prefix='/api/context-providers')


def _result(call):
    try:
        return call()
    except (ValueError, KeyError, TypeError):
        return errors.error('Invalid free explanation provider configuration.', 400)
    except (RuntimeError, OSError, sqlite3.Error):
        # Never echo provider/key exceptions or request payloads.
        return errors.error('Protected explanation provider storage is unavailable.', 503)


async def _body(request):
    try:
        # Profiles are small. Bound malformed requests before parsing or secret storage.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16 * 1024:
                raise ValueError('Profile exceeds request budget.')
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError('Profile must be an object.')
        return value
    except (ValueError, TypeError):
        return None


@router.get('/catalog')
def provider_catalog():
    return catalog()


@router.get('/config')
def config():
    return _result(lambda: context_automation.default().profiles.config())


@router.put('/config')
async def configure(request: Request):
    body = await _body(request)
    if body is None:
        return errors.error('Invalid free explanation provider configuration.', 400)
    return _result(lambda: context_automation.default().configure(body))


@router.put('/profiles/{provider_id}')
async def profile(provider_id: str, request: Request):
    body = await _body(request)
    if body is None:
        return errors.error('Invalid free explanation provider configuration.', 400)
    return _result(lambda: context_automation.default().profiles.save_profile(provider_id, body))


@router.delete('/profiles/{provider_id}/key')
def delete_key(provider_id: str):
    return _result(lambda: context_automation.default().profiles.delete_key(provider_id))


@router.get('/status')
def status():
    return _result(lambda: context_automation.default().status())


@router.post('/start')
def start():
    return _result(lambda: context_automation.default().start(enable=True))


@router.post('/queue-selected')
async def queue_selected(request: Request):
    body = await _body(request)
    if body is None or set(body) != {'ids'}:
        return errors.error('Select valid explanation items.', 400)
    return _result(lambda: context_automation.default().enqueue_selected(body['ids']))


@router.post('/stop')
def stop():
    return _result(lambda: context_automation.default().stop(disable=True))


@router.post('/resume')
def resume():
    def run():
        service = context_automation.default()
        service.resume()
        return service.start()  # Preserve the explicit enabled/disabled preference.
    return _result(run)


@router.post('/cancel')
def cancel():
    return _result(lambda: context_automation.default().cancel())
