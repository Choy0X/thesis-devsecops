"""Docker is authoritative. Never report deletion merely from a local UI update."""
from __future__ import annotations

import time
import json
from docker.errors import NotFound


def owned(ct, *, installation_id=None, known_ids=None, session_id=None, only_ids=None):
    if only_ids is not None and ct.id not in only_ids:
        return False
    labels = ct.labels or {}
    if labels.get('aivdf.sandbox') != '1':
        return False
    if installation_id and labels.get('aivdf.installation') != installation_id and ct.id not in (known_ids or set()):
        return False
    return not session_id or labels.get('aivdf.session') == session_id


def stop_owned(client, **scope):
    stopped, errors = [], []
    for ct in client.containers.list(all=True, filters={'label': 'aivdf.sandbox=1'}):
        if not owned(ct, **scope):
            continue
        try:
            ct.reload()
            if ct.status == 'paused':
                ct.unpause()
            if ct.status not in ('exited', 'created', 'dead', 'removing'):
                ct.stop(timeout=5)
            ct.reload()
            if ct.status not in ('exited', 'created', 'dead'):
                raise RuntimeError('Docker still reports ' + ct.status)
            stopped.append(ct.id)
        except NotFound:
            stopped.append(ct.id)
        except Exception as error:
            errors.append({'id': ct.id, 'error': str(error)})
    return {'ok': not errors, 'stopped': stopped, 'errors': errors}


def pinned_images():
    from app.backend import sandbox_images
    return {x for x in ((sandbox_images.pinned() or {}).get('id'), sandbox_images.toolkit_id()) if x}


def image_plan(client, image_id, *, pinned_ids=None, excluding=None):
    result = {'id': image_id, 'status': 'failed'}
    try:
        image = client.images.get(image_id)
    except NotFound:
        return {**result, 'status': 'removed'}
    from app.backend import sandbox_images
    if not sandbox_images._ours(image):
        return {**result, 'status': 'retained_unowned', 'reason': 'Not an app-owned image; the base image is preserved.'}
    if image_id in (pinned_images() if pinned_ids is None else pinned_ids):
        return {**result, 'status': 'retained_pinned', 'reason': 'Image is pinned or configured as the shared toolkit.'}
    labels = (image.attrs.get('Config') or {}).get('Labels') or {}
    if labels.get('aivdf.image.kind') == 'snapshot' or any(t.startswith('aivdf-snapshot:') for t in image.tags or []):
        return {**result, 'status': 'retained_snapshot', 'reason': 'Saved snapshots require explicit image deletion.'}
    users = [c for c in client.containers.list(all=True, filters={'ancestor': image_id}) if c.id != excluding]
    if users:
        return {**result, 'status': 'retained_in_use', 'containers': [c.id for c in users], 'reason': 'Another container references this image.'}
    return {**result, 'status': 'remove', 'reason': 'Unused, unpinned app-owned image will be removed without deleting shared layers.'}


def deletion_preview(cid, client, *, pinned_ids=None, **scope):
    ct = client.containers.get(cid)
    if not owned(ct, **scope):
        raise PermissionError('not a sandbox container of this app')
    return {'container': ct.id, 'volumes': 'preserved',
            'image': image_plan(client, ct.image.id, pinned_ids=pinned_ids, excluding=ct.id)}


def cleanup_image(client, image_id, *, pinned_ids=None):
    result = image_plan(client, image_id, pinned_ids=pinned_ids)
    if result['status'] != 'remove':
        return result
    result = {**result, 'status': 'failed'}
    try:
        image = client.images.get(image_id)
        tags = list(image.tags or [])
        if len(tags) > 1:
            labels = (image.attrs.get('Config') or {}).get('Labels') or {}
            recorded = json.loads(labels.get('aivdf.image.tags') or '[]')
            app_tags = [tag for tag in tags if tag in recorded or tag.startswith(('aivdf:', 'aivdf-', 'aivdf/'))]
            for tag in app_tags:
                try: client.images.remove(tag, force=False, noprune=True)
                except NotFound: pass
                try: client.images.get(tag)
                except NotFound: continue
                return {**result, 'reason': 'Docker still lists the app-owned image tag: ' + tag}
            retained = sorted(set(tags) - set(app_tags))
            if retained:
                return {**result, 'status': 'retained_shared_tags', 'removed_tags': app_tags, 'retained_tags': retained,
                        'reason': 'App-owned tags were removed. Other image tags are preserved.' if app_tags else 'Tag ownership is unknown; shared tags are preserved.'}
        client.images.remove(image_id, force=False, noprune=True)
        try:
            client.images.get(image_id)
        except NotFound:
            return {**result, 'status': 'removed'}
        return {**result, 'reason': 'Docker still lists the image after removal.'}
    except NotFound:
        return {**result, 'status': 'removed'}
    except Exception as error:
        return {**result, 'reason': str(error)}


def delete_owned(cid, client, *, timeout=5, pinned_ids=None, **scope):
    result = {'id': cid, 'deleted': False, 'status': 'failed', 'image': {'status': 'not_attempted'}}
    try:
        ct = client.containers.get(cid)
    except NotFound:
        return {**result, 'deleted': True, 'status': 'removed'}
    if not owned(ct, **scope):
        raise PermissionError('not a sandbox container of this app')
    image_id = ct.image.id
    try:
        if ct.status == 'paused':
            ct.unpause()
        if ct.status == 'running':
            ct.stop(timeout=5)
        ct.remove(force=True, v=False)
        end = time.monotonic() + timeout
        while True:
            try:
                client.containers.get(ct.id)
            except NotFound:
                break
            if time.monotonic() >= end:
                return {**result, 'reason': 'Docker still lists the container; deletion was not confirmed.'}
            time.sleep(.1)
    except NotFound:
        pass
    except Exception as error:
        return {**result, 'reason': str(error)}
    try:
        image_result = cleanup_image(client, image_id, pinned_ids=pinned_ids)
    except Exception as error:
        image_result = {'id': image_id, 'status': 'failed', 'reason': str(error)}
    return {**result, 'id': ct.id, 'deleted': True, 'status': 'partial' if image_result['status'] == 'failed' else 'removed', 'image': image_result}
