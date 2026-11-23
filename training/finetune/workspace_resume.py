"""Journaled continuation into a compatible bundle. Standard library only.

Only workspace/config binding metadata changes. Checkpoint tensors, optimizer,
scheduler, random state, sampler and training history remain in place.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re

JOURNAL = 'workspace_continuation.json'
RECEIPT = 'checkpoint_continuation.json'


def _read(path):
    if path.stat().st_size > 8 << 20:
        raise ValueError('Continuation metadata exceeds its size bound: ' + str(path))
    return path.read_bytes()


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _body(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def _atomic_write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.tmp')
    temporary.write_bytes(body)
    temporary.replace(path)


def _config(document):
    value = document.get('config')
    if not isinstance(value, dict):
        raise ValueError('Missing resolved checkpoint configuration')
    expected = document.get('config_sha256')
    if expected and expected != _sha(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()):
        raise ValueError('Resolved checkpoint configuration hash changed')
    return value


def _latest(run):
    candidates = sorted(p for p in (run / 'checkpoints').glob('ckpt-step*')
                        if p.is_dir() and re.fullmatch(r'ckpt-step[0-9]+', p.name))
    if not candidates:
        raise ValueError('Save a checkpoint in the previous notebook before continuing')
    checkpoint = candidates[-1]
    required = ('adapter', 'head.pt', 'optimizer.pt', 'scheduler.pt', 'rng.pt', 'sampler.json', 'counters.json', 'meta.json')
    if any(not (checkpoint / name).exists() for name in required):
        raise ValueError('Latest checkpoint is incomplete; workspace was not changed')
    counters = json.loads(_read(checkpoint / 'counters.json'))
    if counters.get('step', 0) < 1 or int(checkpoint.name.removeprefix('ckpt-step')) != counters['step']:
        raise ValueError('Checkpoint step is missing or inconsistent')
    return checkpoint, counters, json.loads(_read(checkpoint / 'meta.json'))


def _commit(out, journal):
    run = out / 'run'
    if json.loads(_read(run / 'run_config.json')).get('state') not in ('interrupted', 'failed', 'finished'):
        raise ValueError('Stop the previous training process before completing checkpoint continuation')
    config_path, workspace_path = run / 'resolved_config.json', out / 'workspace.json'
    for path, key in ((config_path, 'config'), (workspace_path, 'workspace')):
        old, new = (base64.b64decode(journal[side][key], validate=True) for side in ('from', 'to'))
        if _read(path) not in (old, new):
            raise ValueError('Workspace changed during interrupted checkpoint continuation')
    checkpoint = run / 'checkpoints' / journal['receipt']['checkpoint_name']
    if _sha(_read(checkpoint / 'meta.json')) != journal['checkpoint_meta_sha256']:
        raise ValueError('Checkpoint changed during continuation')
    _atomic_write(config_path, base64.b64decode(journal['to']['config'], validate=True))
    _atomic_write(workspace_path, base64.b64decode(journal['to']['workspace'], validate=True))
    _atomic_write(run / RECEIPT, _body(journal['receipt']))
    journal['state'] = 'committed'
    _atomic_write(out / JOURNAL, _body(journal))
    return journal['receipt']


def prepare_compatible_resume(out, bundle, bundle_sha256, parent_run_id='', *, allow=False, stage=1, purpose='train'):
    out, bundle = Path(out).resolve(), Path(bundle).resolve()
    workspace_path = out / 'workspace.json'
    workspace_body = _read(workspace_path)
    workspace = json.loads(workspace_body)
    if workspace.get('schema') != 'finetune-workspace/1' or workspace.get('id') != out.name:
        raise ValueError('Invalid previous workspace receipt')
    journal_path = out / JOURNAL
    journal = json.loads(_read(journal_path)) if journal_path.exists() else None
    if journal and journal.get('state') == 'prepared':
        receipt = journal['receipt']
        if receipt['plan']['stage'] != stage or receipt['plan']['purpose'] != purpose:
            raise ValueError('Target stage or purpose differs from interrupted continuation')
        if (receipt['to_bundle_sha256'] != bundle_sha256
                or receipt['to_parent_run_id'] != parent_run_id):
            raise ValueError('An interrupted continuation belongs to another target bundle')
        if base64.b64decode(journal['to']['config']) != _read(bundle / 'resolved_config.json'):
            raise ValueError('Target bundle changed during continuation recovery')
        return _commit(out, journal)
    matches = (workspace.get('bundle_sha256') == bundle_sha256
               and (not parent_run_id or workspace.get('parent_run_id') == parent_run_id))
    if matches:
        receipt_path = out / 'run' / RECEIPT
        return json.loads(_read(receipt_path)) if receipt_path.exists() else None
    if not allow:
        raise ValueError('Use CONTINUE_FROM_WORKSPACE for a compatible previous bundle')
    run = out / 'run'
    previous = json.loads(_read(run / 'run_config.json'))
    if previous.get('state') not in ('interrupted', 'failed', 'finished'):
        raise ValueError('Interrupt the previous training cell once and wait for its checkpoint first')
    if previous.get('stage') != stage or previous.get('purpose') != purpose:
        raise ValueError('Target notebook stage or purpose differs from the saved checkpoint')
    old_body, new_body = _read(run / 'resolved_config.json'), _read(bundle / 'resolved_config.json')
    old, new = json.loads(old_body), json.loads(new_body)
    cfg = _config(new)
    if (_config(old) != cfg or old.get('contract_sha256') != new.get('contract_sha256')
            or old.get('input_policy_hash') != new.get('input_policy_hash')):
        raise ValueError('Checkpoint continuation requires identical model, dataset, optimizer and training configuration')
    checkpoint, counters, meta = _latest(run)
    expected = {'base_commit': cfg['model']['revision'],
                'tokenizer_revision': cfg['model']['tokenizer_revision'],
                'dataset_manifest_sha256': cfg['data']['manifest_sha256'],
                'config_sha256': previous.get('config_sha256'),
                'serialization_hash': previous.get('prompt_text', {}).get('serialization_hash')}
    if any(not value or meta.get(key) != value for key, value in expected.items()):
        raise ValueError('Checkpoint identities do not match the previous resolved run')
    if (previous.get('stage') not in (1, 2) or previous.get('purpose') not in ('train', 'smoke')
            or len(previous.get('seeds') or []) != 1 or not previous.get('steps')
            or counters['step'] > previous['steps']):
        raise ValueError('Previous run is missing its exact training plan')
    receipt = {'schema': 'checkpoint-continuation/1', 'checkpoint_name': checkpoint.name,
               'checkpoint_step': counters['step'], 'from_bundle_sha256': workspace['bundle_sha256'],
               'from_parent_run_id': workspace.get('parent_run_id'),
               'to_bundle_sha256': bundle_sha256, 'to_parent_run_id': parent_run_id,
               'old_resolved_sha256': _sha(old_body), 'new_resolved_sha256': _sha(new_body),
               'checkpoint_identity': expected,
               'plan': {key: previous.get(key) for key in ('stage', 'purpose', 'seeds', 'steps', 'loss', 'session_minutes')},
               'checkpoint_tensors_and_history_unchanged': True}
    updated = {**workspace, 'bundle_sha256': bundle_sha256, 'parent_run_id': parent_run_id,
               'continued_from': {'bundle_sha256': workspace['bundle_sha256'],
                                  'parent_run_id': workspace.get('parent_run_id')}}
    def encoded(body):
        return base64.b64encode(body).decode()
    pending = {'schema': 'workspace-continuation/1', 'state': 'prepared', 'receipt': receipt,
               'checkpoint_meta_sha256': _sha(_read(checkpoint / 'meta.json')),
               'from': {'workspace': encoded(workspace_body), 'config': encoded(old_body)},
               'to': {'workspace': encoded(_body(updated)), 'config': encoded(new_body)}}
    if journal:
        history = out / '.workspace_continuations' / (journal['receipt']['to_bundle_sha256'] + '.json')
        if not history.exists():
            _atomic_write(history, _body(journal))
    _atomic_write(journal_path, _body(pending))
    return _commit(out, pending)


def resume_arguments(out, *, stage, purpose):
    path = Path(out) / 'run' / RECEIPT
    if not path.exists():
        return []
    plan = json.loads(_read(path))['plan']
    if plan['stage'] != stage or plan['purpose'] != purpose:
        raise ValueError('Notebook stage or purpose differs from the continued checkpoint')
    arguments = ['--seed', str(plan['seeds'][0]), '--steps', str(plan['steps'])]
    if (plan.get('loss') or {}).get('mode'):
        arguments += ['--loss-mode', plan['loss']['mode']]
    if plan.get('session_minutes'):
        arguments += ['--session-minutes', str(plan['session_minutes'])]
    return arguments
