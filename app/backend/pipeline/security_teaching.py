"""Authenticated, review-gated development teaching. Never security admission.

Minimal authored schema (all coordinates are UTF-8 byte offsets):
  name, task, side ('before'/'after'), scope, target, context, response,
  evidence_sources (parallel to response.evidence: 'target' or 'item:N').
Each source descriptor has path, revision, git_blob, byte_span=[start,end], text.
Context descriptors also have kind/name (the existing renderer's code kinds).
The target must lie within or fully contain the authenticated selected-side unit.
Containing file/module targets expose import bindings while retaining unit bounds.
For no_finding, negative_scope must equal scope. For insufficient_context,
withheld_facts contains authenticated source descriptors plus description; those
descriptions must equal response.missing_context and their source is not shown.
Repair findings require repair_target, an authenticated after-side descriptor;
the proposed diff must yield exactly that source from the displayed target.
Regression tests use sft_contracts and always execution_state='not_run'.

source_provider(record, revision) returns a backend GitSnapshot for the exact
repository under root/data/raw/repos/<repository_identity>.git. This API uses
the backend tree/blob reader, never a supplied snapshot dictionary as authority.
root is the workspace root. Source integrity does not establish usage permission,
label correctness, real application reachability, or execution verification.
"""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import re

from . import security_snapshots, sft_admission
from .views import rendering
from training.finetune import serialization, sft_contracts

POLICY = 'security-teaching/1'
PUBLICATION = 'security-teaching-publication/1'
SCOPED_NEGATIVE = ('No finding is limited to the stated review scope and supplied source. '
                   'Other security issues have not been ruled out.')
TASKS = frozenset(('discovery', 'condition_assessment', 'localization', 'repair',
                   'regression_test_design', 'insufficient_context', 'instruction_integrity'))


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':'))


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode('utf-8')).hexdigest()


def _unpack(value):
    return json.loads(value) if isinstance(value, str) else value


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(field + ' must be nonempty text')
    return value


def _range(raw, span, text):
    if (not isinstance(span, list) or len(span) != 2
            or any(type(i) is not int for i in span)
            or not 0 <= span[0] < span[1] <= len(raw)
            or not isinstance(text, str) or raw[span[0]:span[1]] != text.encode('utf-8')):
        raise ValueError('source range does not match exact fragment bytes')
    return span


def _repository(record, root):
    identity = _text(record.get('repository_identity'), 'repository_identity')
    if not re.fullmatch(r'[A-Za-z0-9.-]+(?:/[A-Za-z0-9_.-]+){2,}', identity):
        raise ValueError('invalid repository identity')
    if any(p in ('.', '..') for p in identity.split('/')):
        raise ValueError('invalid repository identity')
    repository = root / 'data/raw/repos' / (identity + '.git')
    if not repository.resolve().is_relative_to(root.resolve()):
        raise ValueError('repository escapes workspace root')
    return repository.resolve()


def _bind(descriptor, revision, repository, provider, record, cache, owner):
    if not isinstance(descriptor, dict):
        raise ValueError('source descriptor must be an object')
    if not re.fullmatch('[a-f0-9]{40,64}', revision or '') or descriptor.get('revision') != revision:
        raise ValueError('source revision does not match selected record side')
    if revision not in cache:
        snapshot = provider(record, revision)
        if (type(snapshot) is not security_snapshots.GitSnapshot
                or snapshot.repository.resolve() != repository or snapshot.revision != revision
                or not snapshot.available):
            raise ValueError('source provider must return the matching backend GitSnapshot')
        cache[revision] = snapshot
    snapshot = cache[revision]
    path = security_snapshots.logical_path(descriptor.get('path'))
    if path not in snapshot.paths:
        raise ValueError('source path absent from exact repository tree')
    source = snapshot.read(path)
    if not isinstance(source, str):
        raise ValueError('source bytes unavailable')
    raw = source.encode('utf-8')
    blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    # Recompute the object identity and bind it to the actual selected Git tree.
    tree_blob = snapshot._git('rev-parse', revision + ':' + path).decode().strip()
    if blob != tree_blob or descriptor.get('git_blob') != tree_blob:
        raise ValueError('source blob does not match backend Git tree')
    span = _range(raw, descriptor.get('byte_span'), descriptor.get('text'))
    return {'owner': owner, 'path': path, 'revision': revision, 'git_blob': blob,
            'byte_span': list(span), 'text': descriptor['text'], 'file_text': source,
            'file_sha256': _sha(raw), 'source_sha256': _sha(descriptor['text'])}


def _unit(record, side, descriptor, bound):
    key = 'vulnerable_code' if side == 'before' else 'fixed_code'
    if bound['path'] != record.get('file_path_' + side):
        raise ValueError('target source path differs from record unit')
    blobs = _unpack(record.get('file_blob_hashes'))
    if not isinstance(blobs, dict) or blobs.get(side) != bound['git_blob']:
        raise ValueError('record unit blob differs from backend source')
    code = _text(record.get(key), 'record unit')
    raw, fragment = bound['file_text'].encode(), code.encode()
    span = _unpack(record.get('byte_span_' + side))
    if span is None:
        if raw.count(fragment) != 1:
            raise ValueError('record unit range absent or ambiguous')
        start = raw.index(fragment)
        span = [start, start + len(fragment)]
    _range(raw, span, code)
    selected = bound['byte_span']
    if not (span[0] <= selected[0] < selected[1] <= span[1]
            or selected[0] <= span[0] < span[1] <= selected[1]):
        raise ValueError('displayed target range is disjoint from or cuts across record unit')
    return {'byte_span': span, 'source_sha256': _sha(code)}


def prepare_candidate(record, authored, *, source_provider, root):
    """Validate one authored example; retain all review and permission gates."""
    if not isinstance(record, dict) or not isinstance(authored, dict):
        raise ValueError('record and authored specification must be objects')
    root = pathlib.Path(root).resolve()
    repository = _repository(record, root)
    side = authored.get('side')
    if side not in ('before', 'after'):
        raise ValueError('side must be before or after')
    revision = record.get('relevant_parent_commit' if side == 'before' else 'fix_commit')
    task = authored.get('task')
    if task not in TASKS:
        raise ValueError('unsupported development teaching task')
    scope, name = _text(authored.get('scope'), 'scope'), _text(authored.get('name'), 'name')
    _text(record.get('record_id'), 'record_id')
    response = copy.deepcopy(authored.get('response'))
    problems = sft_contracts.validate_response(task, response)
    if problems:
        raise ValueError('; '.join(problems))
    cache = {}
    def bind(spec, owner, selected_revision=revision):
        return _bind(spec, selected_revision, repository, source_provider, record, cache, owner)
    target_source = bind(authored.get('target'), 'target')
    unit = _unit(record, side, authored['target'], target_source)
    context = authored.get('context', [])
    if not isinstance(context, list) or len(context) > security_snapshots.MAX_FILES:
        raise ValueError('context must be a bounded source descriptor list')
    sources, items = [target_source], []
    for index, descriptor in enumerate(context):
        bound = bind(descriptor, 'item:' + str(index))
        if descriptor.get('kind') not in rendering.CODE_KINDS:
            raise ValueError('unsupported code context kind')
        sources.append(bound)
        items.append({'kind': descriptor['kind'], 'name': descriptor.get('name'),
                      'path': bound['path'], 'text': bound['text']})
    target = {'code': target_source['text'], 'path': target_source['path'],
              'language': record.get('language'), 'symbol': record.get('symbol')}
    rendered = rendering.render_input(target, items, preserve_istanbul_comments=True)
    rendering.assert_identifier_free(rendered, {'review_scope': scope})
    context_text = '[review scope]\n' + scope + '\n\n' + serialization.render_context(
        rendered['target'], rendered['items'])
    source_renderings = [rendered['sources']['target'], *rendered['sources']['items']]
    owners = authored.get('evidence_sources')
    if not isinstance(owners, list) or len(owners) != len(response['evidence']):
        raise ValueError('evidence owner list must match each quote')
    owner_index = {source['owner']: index for index, source in enumerate(sources)}
    evidence = []
    for entry, owner in zip(response['evidence'], owners):
        if not isinstance(owner, str) or owner not in owner_index:
            raise ValueError('evidence owner is not a displayed source')
        index = owner_index[owner]
        source = sources[index]
        try:
            mapped = rendering.map_quote(source['text'], source_renderings[index], entry['quote'],
                                          preserve_istanbul_comments=True)
        except rendering.RenderingError as error:
            raise ValueError('evidence quote invalid: ' + str(error)) from error
        entry['quote'] = mapped['quote']
        evidence.append({'owner': owner, 'path': source['path'], 'revision': revision,
                         'side': side, 'source_sha256': source['source_sha256'],
                         'source_span': mapped['source_span'], 'rendered_span': mapped['rendered_span'],
                         'file_byte_span': [source['byte_span'][0] + offset
                                            for offset in mapped['source_span']['bytes']]})
    status = response['status']
    withheld = authored.get('withheld_facts', [])
    if not isinstance(withheld, list):
        raise ValueError('withheld_facts must be a list')
    withheld_sources = []
    for index, descriptor in enumerate(withheld):
        bound = bind(descriptor, 'withheld:' + str(index))
        bound['description'] = _text(descriptor.get('description'), 'withheld description')
        if (bound['text'] in context_text or any(
                bound['path'] == shown['path'] and max(bound['byte_span'][0], shown['byte_span'][0])
                < min(bound['byte_span'][1], shown['byte_span'][1]) for shown in sources)):
            raise ValueError('withheld decisive fact is visible in supplied context')
        withheld_sources.append(bound)
    if status == 'insufficient_context':
        if not withheld_sources or response['missing_context'] != [s['description'] for s in withheld_sources]:
            raise ValueError('abstention requires named, authenticated withheld decisive facts')
    elif withheld or response['missing_context']:
        raise ValueError('withheld facts and missing context require abstention')
    if status == 'no_finding':
        if authored.get('negative_scope') != scope:
            raise ValueError('negative_scope must explicitly equal the review scope')
        if not response['evidence']:
            raise ValueError('scoped negative needs visible source evidence')
        response['assumptions'].append(SCOPED_NEGATIVE)
    repair = None
    if task == 'repair' and status == 'finding':
        if side != 'before':
            raise ValueError('repair target must start from the before side')
        after = bind(authored.get('repair_target'), 'repair_target', record.get('fix_commit'))
        _unit(record, 'after', authored['repair_target'], after)
        if after['path'] != target_source['path']:
            raise ValueError('repair target path must match displayed target')
        parents = cache[after['revision']]._git('rev-list', '--parents', '-n', '1', after['revision']).decode().split()[1:]
        if revision not in parents:
            raise ValueError('repair target revision does not have the selected source parent')
        from .agent.candidates import apply_to_copy, check_diff
        try:
            check_diff(response['patch'], target_source['path'])
            patched = apply_to_copy(target_source['path'], target_source['text'], response['patch'])
        except ValueError as error:
            raise ValueError('repair patch does not match captured edit: ' + str(error)) from error
        if patched != after['text']:
            raise ValueError('repair patch result does not match authenticated fixed target')
        repair = {'state': 'exact_edit_match', 'scope': 'patch_application_only',
                  'target': after, 'security_correctness': 'not_adjudicated', 'execution_state': 'not_run'}
    # Preserve existing backend disposition. The new displayed context needs its
    # own permission review even when the original unit was admitted.
    admission = sft_admission.admit_record(record, root=root / 'data/processed')
    gaps = copy.deepcopy(admission.get('reasons', []))
    gaps.extend([{'code': 'independent_security_review_required', 'field': 'response',
                  'detail': 'Authored labels, scope, trace and expectations need independent review.'},
                 {'code': 'teaching_source_permission_admission_required', 'field': 'sources',
                  'detail': 'Resolve exact usage permission for every displayed source through the existing policy.'}])
    response = sft_contracts.normalize(task, response)
    result = {'record_id': record['record_id'], 'family_id': record.get('issue_group_id') or record['record_id'],
              'issue_group_id': record.get('issue_group_id'), 'name': name, 'side': side, 'task': task,
              'role': 'train_development', 'split': 'train', 'eligible': False,
              'review_status': 'independent_review_required',
              'pool': {'finding': 'supported', 'no_finding': 'scoped_negative',
                       'insufficient_context': 'insufficient_context'}[status],
              'context_text': context_text, 'response': response, 'response_json': _json(response),
              'messages': [{'role': 'system', 'content': sft_contracts.CHAT_SYSTEM},
                           {'role': 'user', 'content': sft_contracts.TASK_TEXT[task]
                            + '\n\nSupplied source context:\n' + context_text}],
              'source_sha256': target_source['source_sha256'], 'context_sha256': _sha(context_text),
              'admission_gaps': gaps,
              'provenance': {'policy': POLICY, 'sources': sources, 'record_unit': unit,
                  'evidence': evidence, 'withheld_facts': withheld_sources,
                  'repair_consistency': repair, 'rendering': rendering.identity(preserve_istanbul_comments=True),
                  'source_integrity': 'exact_git_tree_blob_and_utf8_range',
                  'label_origin': 'agent_authored_static_source_analysis',
                  'adjudication': 'pending_independent_review', 'execution_state': 'not_run',
                  'validation_state': 'not_validated', 'main_evaluation_allowed': False,
                  'context_policy': 'authored_teaching_trace_not_deployment_retrieval',
                  'prior_exposure': 'source_record_exposed_development_material',
                  'repository_identity': record.get('repository_identity'),
                  'raw_pair_key': record.get('raw_pair_key'),
                  'repository_license': record.get('repository_license'),
                  'data_usage_status': record.get('data_usage_status'),
                  'original_record_admission': admission}}
    digest = _sha(_json(result))
    return {'view_id': 'security_teaching_' + digest[:24], 'candidate_sha256': digest, **result}


def publish(candidates, out_dir):
    """Publish validated candidates once. Never admit them or overwrite a path."""
    out = pathlib.Path(out_dir)
    if out.exists():
        raise ValueError('output already exists; choose a new immutable version')
    candidates = list(candidates)
    if not candidates:
        raise ValueError('publication requires candidates')
    seen = set()
    for candidate in candidates:
        if (candidate.get('eligible') is not False
                or candidate.get('review_status') != 'independent_review_required'
                or candidate.get('role') != 'train_development'
                or candidate.get('provenance', {}).get('main_evaluation_allowed') is not False
                or candidate.get('provenance', {}).get('execution_state') != 'not_run'
                or not candidate.get('admission_gaps')):
            raise ValueError('candidate review/permission gate invalid')
        body = {k: v for k, v in candidate.items() if k not in ('view_id', 'candidate_sha256')}
        digest = _sha(_json(body))
        if (candidate.get('candidate_sha256') != digest
                or candidate.get('view_id') != 'security_teaching_' + digest[:24]):
            raise ValueError('candidate content fingerprint mismatch')
        if candidate['view_id'] in seen:
            raise ValueError('duplicate candidate identity')
        seen.add(candidate['view_id'])
    queue = [{'view_id': c['view_id'], 'record_id': c['record_id'],
              'review_status': c['review_status'], 'reasons': c['admission_gaps']} for c in candidates]
    contents = {'candidate_views.jsonl': ''.join(_json(c) + '\n' for c in candidates).encode(),
                'review_queue.json': (_json(queue) + '\n').encode()}
    manifest = {'schema': PUBLICATION, 'state': 'review_required', 'policy': POLICY,
                'immutable_version': out.name, 'candidate_views': len(candidates), 'admitted_views': 0,
                'independent_evaluation_eligible': False, 'model_improvement': 'unmeasured',
                'files': {name: {'sha256': _sha(raw), 'bytes': len(raw)} for name, raw in contents.items()}}
    # Exclusive creation prevents concurrent publication replacing an old version.
    # A failed write is incomplete and must use a fresh version, never overwrite.
    out.mkdir(parents=True, exist_ok=False)
    for name, raw in contents.items():
        with (out / name).open('xb') as handle:
            handle.write(raw)
    with (out / 'manifest.json').open('xb') as handle:
        handle.write((_json(manifest) + '\n').encode())
    return manifest
