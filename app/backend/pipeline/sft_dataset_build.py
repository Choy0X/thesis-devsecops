"""Bounded, restartable SFT admission audit and immutable publication.

Historical proof qualification is retained as source metadata. This policy audits
all records, restores the original partition authority, and publishes only after
a separately pinned exact-token audit. It never executes collected source.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import pathlib
import re
import shutil
import sqlite3
import uuid
from types import ModuleType
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from itertools import zip_longest

from . import (appended_evidence, audit_checkpoint_migration, bounded_parquet,
               build_catalog, evidence_recovery, oslock, versions)

POLICY = 'sft-admission/1'
SCHEMA = 'sft-dataset-build/1'
PROTOCOL = 'sft_native_chat_v1'
PROJECTION = 'sft-source-export/1'
MAX_ROW_BYTES = 8 << 20
_COPY = ('edges.parquet', 'groups.parquet', 'split_manifest.json', 'security_partitions.json')
_VIEW_COLUMNS = {
    'view_id': 'string', 'record_id': 'string', 'family_id': 'string', 'subject_id': 'string',
    'task': 'string', 'pool': 'string', 'context_text': 'string', 'response_json': 'string',
    'split': 'string', 'val_role': 'string', 'role': 'string', 'eligible': 'bool', 'weight': 'float64',
    'lineage_json': 'string', 'messages_json': 'string', 'provenance_json': 'string',
    'source_fingerprint': 'string', 'context_fingerprint': 'string', 'issue_group_id': 'string',
    'review_status': 'string', 'context_hash': 'string', 'flags_json': 'string',
    'prompt_chars': 'int64', 'completion_chars': 'int64',
    'evaluation_exposure': 'string', 'exposure_json': 'string',
}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _json_file_hash(value):
    return hashlib.sha256((_json(value) + '\n').encode()).hexdigest()


def _write(path, value):
    from . import fsio
    fsio.write_bytes(path, (_json(value) + '\n').encode())


def _safe(root, path):
    root, path = pathlib.Path(root).absolute(), pathlib.Path(path).absolute()
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('unsafe artifact path or symlink escape')
    for current in (path, *path.parents):
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError('unsafe symlink or junction artifact path')
    return path


def _inventory(folder):
    result = {}
    for path in folder.rglob('*'):
        _safe(folder, path)
        if path.is_file():
            result[path.relative_to(folder).as_posix()] = versions._sha256(path)
    return result


def _capture(tag, root):
    if not re.fullmatch(r'v[1-9][0-9]*', str(tag)):
        raise ValueError('invalid immutable source tag')
    name = 'dataset_' + tag
    folder = _safe(root, root / name)
    entry = versions.load_registry(root)['frozen'].get(name)
    if not entry or entry.get('base') != name:
        raise ValueError('source must be a registered immutable dataset')
    files = entry.get('files') or {}
    if not {'manifest.json', 'records.parquet', *_COPY} <= files.keys():
        raise ValueError('immutable source is incomplete')
    for relative, expected in files.items():
        path = _safe(folder, folder / relative)
        if not path.is_file() or versions._sha256(path) != expected:
            raise ValueError('immutable source hash changed: ' + relative)
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('tag') != tag or manifest.get('dataset_status') != 'frozen':
        raise ValueError('immutable source manifest identity mismatch')
    return {'tag': tag, 'files': files, 'manifest': manifest}


def _authority(source, root):
    # v18 includes a derived empty-fingerprint quarantine. The genuine v11
    # frozen roles and graph remain the assignment authority for those records.
    tag = 'v11' if source['tag'] == 'v18' else source['tag']
    authority = _capture(tag, root)
    folder = root / ('dataset_' + tag)
    assignments = json.loads((folder / 'security_partitions.json').read_text(encoding='utf-8'))['records']
    if not isinstance(assignments, dict):
        raise ValueError('invalid original partition map')
    return authority, assignments


def _evidence_path(name, root):
    path = pathlib.Path(name)
    path = path if path.is_absolute() else root / path
    bases = (root, pathlib.Path(__file__).resolve().parents[3])
    for base in bases:
        if path.absolute().is_relative_to(base.absolute()):
            return _safe(base, path)
    raise ValueError('backend evidence path is outside the workspace')


def _evidence_descriptors(provenance):
    """Describe backend artifacts used by admission, not imported record labels."""
    result = {}
    descriptors = []
    # Legacy OSV ZIP hashes are not collector discovery-file identities.
    if provenance.get('schema') == 'npm-exact-source-binding/2' or provenance.get('artifact_sha256'):
        descriptors = [('artifacts', provenance.get('artifact_sha256'))]
        descriptors.extend(('discoveries', entry.get('advisory_sha256'))
                           for entry in provenance.get('attribution', []) if isinstance(entry, dict))
    for category, sha in descriptors:
        if sha is None:
            continue
        if not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha):
            raise ValueError('invalid backend evidence fingerprint')
        relative = '.npm_case_collector/' + category + '/' + sha + '.json'
        result[relative] = sha
    for entry in provenance.get('backend_evidence_files') or []:
        if (not isinstance(entry, dict) or not isinstance(entry.get('path'), str)
                or not re.fullmatch('[a-f0-9]{64}', str(entry.get('sha256') or ''))):
            raise ValueError('invalid backend evidence file descriptor')
        result[entry['path']] = entry['sha256']
    return result


def _backend_evidence(provenance, root, known=None):
    result = _evidence_descriptors(provenance)
    known = known or {}
    if any(name in known and known[name] != sha for name, sha in result.items()):
        raise ValueError('backend evidence identity changed during audit')
    _validate_evidence({name: sha for name, sha in result.items() if name not in known}, root)
    return result


def _validate_evidence(files, root):
    for name, sha in files.items():
        path = _evidence_path(name, root)
        if not path.is_file() or versions._sha256(path) != sha:
            raise ValueError('backend evidence changed before publication: ' + name)


def _registration_descriptors(provenance):
    entries = provenance.get('backend_source_registrations', [])
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError('invalid backend source registration descriptors')
    return entries


def _registration_batches(entries, size=64):
    batch = []
    for entry in entries:
        batch.append(entry)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _validate_registrations(entries, root):
    from .sft_evidence import registered_sources_current
    count = 0
    for batch in _registration_batches(entries):
        if registered_sources_current({'backend_source_registrations': batch}, root=root) is not True:
            raise ValueError('backend source registration authority changed or revoked')
        count += len(batch)
    return count


def _capture_registrations(provenance, db, root, checked):
    # Bodies are retained on SSD; only unique descriptor digests stay in memory.
    pending = {_digest(entry): entry for entry in _registration_descriptors(provenance)
               if _digest(entry) not in checked}
    for batch in _registration_batches(pending.values()):
        _validate_registrations(batch, root)
        for entry in batch:
            identity = _digest(entry)
            db.execute('INSERT OR IGNORE INTO registrations VALUES (?,?)', (identity, _json(entry)))
            checked.add(identity)
    db.commit()


def _write_registrations(database, destination):
    count = 0
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db, \
            destination.open('w', encoding='utf-8', newline='\n') as output:
        for body, in db.execute('SELECT body FROM registrations ORDER BY identity'):
            output.write(body + '\n')
            count += 1
    return count


def _validate_registration_file(path, root):
    return _validate_registrations(_jsonl(path), root)


# Every other original Arrow field remains present with a null value.
_EXPORT_METADATA = frozenset(('schema_version', 'record_id', 'raw_pair_key', 'issue_group_id',
    'repository_identity', 'package_identity', 'ecosystem', 'language', 'repository_license',
    'data_usage_status', 'source_snapshot_time', 'advisory_first_published_at', 'advisory_modified_at',
    'fix_commit', 'relevant_parent_commit', 'before_tree_hash', 'after_tree_hash', 'collector_version',
    'extraction_version', 'granularity', 'file_path_before', 'file_path_after', 'symbol', 'split',
    'split_manifest_version', 'fix_family_id', 'production_relevance', 'data_quality_status',
    'raw_record_keys', 'advisory_aliases', 'source_record_hashes', 'cwe_ids', 'cwe_source',
    'clone_group_ids', 'lineage_parent_ids', 'byte_span_before', 'byte_span_after',
    'line_span_before', 'line_span_after', 'file_blob_hashes', 'changed_lines', 'advisory_cvss'))


def _metadata_atom(value, maximum=1024):
    return value if isinstance(value, str) and len(value) <= maximum and re.fullmatch(r'[A-Za-z0-9_.:/@+%#=\\-]+', value) else None


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, dict) else None


def _metadata_value(name, value):
    if name in ('changed_lines', 'advisory_cvss'):
        return value if type(value) in (int, float) and 0 <= value <= 10000000 else None
    if name in ('byte_span_before', 'byte_span_after', 'line_span_before', 'line_span_after'):
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                return None
        valid = isinstance(parsed, list) and len(parsed) == 2 and all(type(n) is int and 0 <= n <= 1000000000 for n in parsed)
        return (_json(parsed) if isinstance(value, str) else parsed) if valid else None
    if name == 'file_blob_hashes':
        parsed = _object(value)
        if parsed is None:
            return None
        clean = {side: sha for side, sha in parsed.items() if side in ('before', 'after')
                 and isinstance(sha, str) and re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', sha)}
        return _json(clean) if isinstance(value, str) else clean
    if isinstance(value, list):
        if len(value) > 128:
            return None
        clean = [_metadata_atom(item, 256) for item in value]
        return clean if all(item is not None for item in clean) else None
    return _metadata_atom(value)


def _project_context(value):
    context = _object(value)
    if context is None:
        return None
    clean = {}
    for key in ('policy', 'variant', 'completeness'):
        if _metadata_atom(context.get(key), 128) is not None:
            clean[key] = context[key]
    if type(context.get('main_evaluation_allowed')) is bool:
        clean['main_evaluation_allowed'] = context['main_evaluation_allowed']
    if isinstance(context.get('omissions'), list):
        clean['omissions'] = [item for item in context['omissions'][:128] if _metadata_atom(item, 128) is not None]
    if isinstance(context.get('snapshot'), dict):
        snapshot = context['snapshot']
        # snapshot.files is a repository discovery index, not rendered evidence.
        # Its bounded, non-authoritative summary lives in public provenance.
        clean['snapshot'] = {key: _metadata_atom(snapshot[key], 2048) for key in
            ('revision', 'side', 'path', 'file_sha256', 'target_sha256') if _metadata_atom(snapshot.get(key), 2048) is not None}
        if type(snapshot.get('available')) is bool:
            clean['snapshot']['available'] = snapshot['available']
    if isinstance(context.get('items'), list):
        clean['items'] = []
        for item in context['items'][:128]:
            if not isinstance(item, dict) or not isinstance(item.get('text'), str):
                continue
            kind, path = _metadata_atom(item.get('kind'), 64), _metadata_atom(item.get('path'), 2048)
            if kind is None or path is None or len(item['text'].encode()) > MAX_ROW_BYTES:
                continue
            selected = {'kind': kind, 'path': path, 'name': _metadata_atom(item.get('name'), 256),
                        'text': item['text'], 'reason': 'Authenticated same-side context.'}
            if isinstance(item.get('target_sha256'), str) and re.fullmatch('[a-f0-9]{64}', item['target_sha256']):
                selected['target_sha256'] = item['target_sha256']
            clean['items'].append(selected)
    return value if isinstance(value, str) and clean == context else _json(clean)


def _project_record(row, admission):
    """Project exportable bodies only from trusted side/permission evidence."""
    from .sft_evidence import permission_resolution
    provenance = admission.get('provenance') or {}
    if provenance.get('schema') == 'mined-corpus/1':
        # Retain mined bytes with their original classifications; the corpus
        # policy does not manufacture authentication or license conclusions.
        projected = dict(row)
        for side in ('before', 'after'):
            projected['context_' + side] = _project_context(row.get('context_' + side))
        state = {'policy': 'mined-corpus/1', 'sides': {'before': 'retained', 'after': 'retained'},
                 'permission': 'original_metadata_retained', 'raw_authority': 'immutable_parent_raw'}
        projected['sft_export_state_json'] = _json(state)
        return projected, state
    permission = permission_resolution(row, provenance)
    allowed = permission['effective_status'] == 'permissive'
    sides = provenance.get('side_integrity') or {}
    projected = {key: None for key in row}
    for key in _EXPORT_METADATA & row.keys():
        projected[key] = _metadata_value(key, row[key])
    if row.get('repository_license') == '':
        projected['repository_license'] = ''
    state = {'policy': PROJECTION, 'sides': {}, 'permission': 'allowed' if allowed else 'not_established',
             'permission_resolution': {key: '' if value == '' else _metadata_atom(value, 256) for key, value in permission.items()},
             'raw_authority': 'immutable_parent_raw'}
    for side, code_field in (('before', 'vulnerable_code'), ('after', 'fixed_code')):
        keep = allowed and sides.get(side) == 'verified'
        state['sides'][side] = 'retained' if keep else 'redacted'
        if keep:
            projected[code_field] = row.get(code_field)
            projected['context_' + side] = _project_context(row.get('context_' + side))
    projected['sft_export_state_json'] = _json(state)
    return projected, state


def _public_provenance(provenance):
    clean = {}
    scalar = ('schema', 'status', 'basis', 'record_id', 'source_sha256', 'binding_sha256',
              'artifact_sha256', 'advisory_fix_status', 'permission_status', 'trusted_execution_id')
    for key in scalar:
        value = _metadata_atom(provenance.get(key), 512)
        if value is not None:
            clean[key] = value
    resolution = provenance.get('permission_resolution')
    if isinstance(resolution, dict):
        clean['permission_resolution'] = {key: '' if resolution.get(key) == '' else _metadata_atom(resolution.get(key), 256) for key in
            ('raw_status', 'raw_repository_license', 'effective_status', 'basis')}
    for key in ('side_integrity', 'unit_attribution', 'context_fingerprints', 'unit_fingerprints'):
        if isinstance(provenance.get(key), dict):
            clean[key] = {side: _metadata_atom(provenance[key].get(side), 128) for side in ('before', 'after')}
    witness = provenance.get('exclusive_unit_change')
    if (isinstance(witness, dict) and witness.get('schema') == 'sft-exclusive-unit-change/1'
            and type(witness.get('changed_path_count')) is int and witness['changed_path_count'] == 1
            and witness.get('mode') in ('100644', '100755') and _metadata_atom(witness.get('path'), 4096)):
        lengths = {'parent':40, 'fix':40, 'diff_sha256':64, 'prefix_sha256':64, 'suffix_sha256':64}
        valid = all(isinstance(witness.get(key), str) and re.fullmatch('[a-f0-9]{'+str(size)+'}', witness[key])
                    for key,size in lengths.items())
        projected = {key:witness[key] for key in ('schema','path','mode','changed_path_count')}
        if valid:
            projected.update({key:witness[key] for key in lengths})
        for side in ('before', 'after'):
            entry = witness.get(side)
            sizes = {'tree':40, 'blob':40, 'module_sha256':64, 'unit_sha256':64, 'syntax_hash':20}
            if not isinstance(entry, dict):
                valid = False
                continue
            span = entry.get('byte_span')
            if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                    or not 0 <= span[0] < span[1] <= 1 << 30
                    or not all(isinstance(entry.get(key), str) and re.fullmatch('[a-f0-9]{'+str(size)+'}', entry[key])
                               for key,size in sizes.items())):
                valid = False
                continue
            projected[side] = {**{key:entry[key] for key in sizes}, 'byte_span':list(span)}
        if valid:
            clean['exclusive_unit_change'] = projected
    discovery = provenance.get('context_discovery_index')
    if isinstance(discovery, dict):
        summaries = {}
        for side in ('before', 'after'):
            summary = discovery.get(side)
            if not isinstance(summary, dict) or summary.get('role') != 'non_authoritative_discovery_metadata':
                continue
            counts = {key: summary.get(key) for key in ('count', 'used_count', 'uncited_count')}
            if (not all(type(value) is int and 0 <= value <= 1000000000 for value in counts.values())
                    or counts['count'] != counts['used_count'] + counts['uncited_count']
                    or not isinstance(summary.get('sha256'), str)
                    or not re.fullmatch('[a-f0-9]{64}', summary['sha256'])):
                continue
            summaries[side] = {'role': 'non_authoritative_discovery_metadata', **counts,
                               'sha256': summary['sha256'], 'snapshot_files_exported': False}
        if summaries:
            clean['context_discovery_index'] = summaries
    fields = {'backend_evidence_files': ('path', 'sha256'),
              'backend_source_registrations': ('provider', 'repository', 'revision', 'path',
                  'artifact_sha256', 'content_sha256', 'git_blob_hash'),
              'git_sources': ('repository', 'revision', 'tree_hash', 'path', 'blob_hash', 'source_file_sha256'),
              'attribution': ('advisory_sha256', 'basis')}
    for key, names in fields.items():
        if isinstance(provenance.get(key), list):
            clean[key] = [{name: _metadata_atom(item.get(name), 4096) for name in names
                           if _metadata_atom(item.get(name), 4096) is not None}
                          for item in provenance[key][:256] if isinstance(item, dict)]
    return clean


def _public_reason(reason):
    code = _metadata_atom(reason.get('code'), 128) or 'review_required'
    field = _metadata_atom(reason.get('field'), 256) or 'record'
    return {**{key: reason[key] for key in ('task', 'side') if _metadata_atom(reason.get(key), 64)},
            'code': code, 'field': field, 'detail': code.replace('_', ' ') + '.',
            'remediation': 'Inspect the authoritative original source reference and recover the named evidence field.'}


def _public_disposition(disposition):
    result = copy.deepcopy(disposition)
    result['provenance'] = _public_provenance(result.get('provenance') or {})
    result['reasons'] = [_public_reason(reason) for reason in result.get('reasons') or []]
    result['view_exclusions'] = [_public_reason(reason) for reason in result.get('view_exclusions') or []]
    for decision in (result.get('task_decisions') or {}).values():
        decision['reasons'] = [_public_reason(reason) for reason in decision.get('reasons') or []]
    return result


def _function_hash(function):
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError):
        source = repr(getattr(function, '__code__', None))
    return _digest({'module': function.__module__, 'name': function.__qualname__, 'source': source})


def _exposure_policy():
    from . import validation_exposure
    return {'version': validation_exposure.VERSION, 'record_ids': sorted(validation_exposure.RECORD_IDS),
            'sha256': versions._sha256(pathlib.Path(validation_exposure.__file__))}


def _exposure_bases(row, registry):
    bases = ['development_registry'] if row['record_id'] in registry else []
    receipt = row.get('admission_provenance') or {}
    if isinstance(receipt, str):
        try:
            receipt = json.loads(receipt)
        except (ValueError, TypeError):
            receipt = {}
    for name, item in (('record', row), ('admission_provenance', receipt)):
        if not isinstance(item, dict):
            continue
        for field in ('validator_development', 'validator_development_case'):
            if item.get(field) is True:
                bases.append(name + '.' + field)
        if item.get('evaluation_exposure') == 'validator_development':
            bases.append(name + '.evaluation_exposure')
    return bases


def _authority_markers(authority, root):
    """Read original exposure metadata without retaining historical bodies."""
    import pyarrow.parquet as pq
    markers = {}
    with pq.ParquetFile(root / ('dataset_' + authority['tag']) / 'records.parquet') as parquet:
        wanted = ('record_id', 'admission_provenance', 'validator_development',
                  'validator_development_case', 'evaluation_exposure')
        columns = [name for name in wanted if name in parquet.schema_arrow.names]
        for batch in parquet.iter_batches(batch_size=1, columns=columns):
            for row in batch.to_pylist():
                bases = _exposure_bases(row, set())
                if bases:
                    markers[row['record_id']] = ['original_authority.' + base for base in bases]
    return markers


def _retryable(disposition):
    codes = {reason.get('code') for reason in disposition.get('reasons') or [] if isinstance(reason, dict)}
    if 'record_audit_failed' in codes:
        return True
    # These faults describe immutable input bytes/fields, rather than missing
    # backend capability. A new backend artifact cannot repair the frozen row.
    if codes & {'malformed_evidence_field', 'malformed_source_unit', 'malformed_source_path',
                'malformed_context', 'unit_span_mismatch', 'line_span_mismatch',
                'registered_source_mismatch', 'registered_blob_mismatch', 'record_blob_hash_mismatch',
                'context_target_mismatch', 'context_file_hash_mismatch', 'mixed_snapshot',
                'git_tree_mismatch', 'git_parent_mismatch'}:
        return False
    if disposition.get('source_integrity') == 'unknown':
        return True
    sides = (disposition.get('provenance') or {}).get('side_integrity') or {}
    unresolved_side = any(sides.get(side, 'unknown') == 'unknown' for side in ('before', 'after'))
    # The evidence boundary can call an absent binding "invalid". Preserve that
    # source decision, but retry its still-unknown backend side in a new attempt.
    return unresolved_side and bool(codes & {'source_binding_mismatch', 'backend_source_unavailable'})


def _unresolved(workspace, root, *, retry_unknown=True):
    database = workspace / 'checkpoints.sqlite3'
    if not database.exists():
        return set()
    _safe(workspace, database)
    unresolved, capabilities, registrations = set(), {}, {}
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        for rid, body in db.execute('SELECT rid,disposition FROM items'):
            disposition = json.loads(body)
            if retry_unknown and _retryable(disposition):
                unresolved.add(rid)
            provenance = disposition.get('provenance') or {}
            for entry in _registration_descriptors(provenance):
                identity = _digest(entry)
                if identity not in registrations:
                    from .sft_evidence import registered_sources_current
                    registrations[identity] = registered_sources_current(
                        {'backend_source_registrations': [entry]}, root=root) is True
                if not registrations[identity]:
                    unresolved.add(rid)
            for name, sha in _evidence_descriptors(provenance).items():
                identity = (name, sha)
                if identity not in capabilities:
                    path = _evidence_path(name, root)
                    capabilities[identity] = path.is_file() and versions._sha256(path) == sha
                if not capabilities[identity]:
                    unresolved.add(rid)
    return unresolved


def _attempt(base, root, *, resume_incomplete=False):
    """Retry unresolved evidence in a new identity, retaining old attempts intact."""
    base_key = _digest(base)
    index = _safe(root, root / '.sft_build' / ('.latest-' + base_key + '.json'))
    identity, seed = base, None
    if index.exists():
        latest = json.loads(index.read_text(encoding='utf-8'))
        key = latest.get('key')
        if latest.get('base') != base_key or not re.fullmatch('[a-f0-9]{64}', str(key)):
            raise ValueError('staging retry index identity mismatch')
        workspace = _safe(root, index.parent / key)
        state = json.loads(_safe(root, workspace / 'owner.json').read_text(encoding='utf-8'))
        identity = state['capture']
        if ({key: value for key, value in identity.items() if key != 'retry'} != base
                or _digest(identity) != state.get('source_identity') or state.get('source_identity') != key):
            raise ValueError('staging retry parent identity mismatch')
        if resume_incomplete:
            if state.get('complete') is not False:
                raise ValueError('resource continuation requires an owned incomplete attempt')
            # A resource handoff retains conservative UNKNOWN decisions. It is
            # distinct from asking whether newly available evidence repairs
            # them. Known evidence must still be current before any reuse.
            if _unresolved(workspace, root, retry_unknown=False):
                raise ValueError('known backend evidence changed before continuation; use an ordinary evidence retry')
            return identity, index, base_key, None
        if state.get('complete') and _inventory(workspace / 'artifact') != state['inventory']:
            raise ValueError('staging inventory changed before retry')
        unresolved = _unresolved(workspace, root)
        if unresolved:
            seed = workspace
            identity = {**base, 'retry': {'parent': state['source_identity'],
                'unresolved_record_ids': sorted(unresolved), 'nonce': uuid.uuid4().hex}}
    elif resume_incomplete:
        raise ValueError('resource continuation requires an existing same-capture incomplete attempt')
    return identity, index, base_key, seed


def _seed_checkpoints(db, previous, root, excluded=()):
    if not previous:
        return
    previous = _safe(root, pathlib.Path(previous))
    database = _safe(root, previous / 'checkpoints.sqlite3')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as prior:
        for rid, identity, body, views in prior.execute('SELECT rid,identity,disposition,views FROM items'):
            if rid not in excluded and not _retryable(json.loads(body)):
                db.execute('INSERT OR IGNORE INTO items VALUES (?,?,?,?)', (rid, identity, body, views))
    db.commit()


def _verify_inventory(folder, expected, kind='staging'):
    if _inventory(folder) != expected:
        raise ValueError(kind + ' snapshot inventory changed')


def _verified_views(folder, expected=None):
    """Check dual exports agree on every payload field, not only view IDs."""
    import pyarrow.parquet as pq
    membership = {}
    with pq.ParquetFile(folder / 'views.parquet') as parquet:
        rows = (row for batch in parquet.iter_batches(batch_size=1) for row in batch.to_pylist())
        for stored, native in zip_longest(rows, _jsonl(folder / 'instruction_views.jsonl')):
            if stored is None or native is None:
                raise ValueError('token snapshot view membership mismatch')
            rid = stored.get('view_id')
            if (not isinstance(rid, str) or rid in membership
                    or set(native) != set(_VIEW_COLUMNS) | {'messages', 'response'}
                    or stored != {key: native.get(key) for key in _VIEW_COLUMNS}
                    or json.loads(stored['messages_json']) != native.get('messages')
                    or json.loads(stored['response_json']) != native.get('response')):
                raise ValueError('token snapshot native/Parquet payload mismatch')
            fingerprint = _digest(stored)
            if expected is not None and expected.get(rid) != fingerprint:
                raise ValueError('token snapshot accepted payload changed')
            membership[rid] = fingerprint
    if expected is not None and membership != expected:
        raise ValueError('token snapshot accepted membership changed')
    return membership


def _parser_identity():
    from . import funcsplit
    return {'version': funcsplit.VERSION, 'fingerprint': funcsplit.parser_fingerprint(),
            'installed_versions': funcsplit._versions()}


def _injection_policy(view_builder):
    from .sft_views import build_views
    from .sft_injections import POLICY as injection_policy
    return {'schema': injection_policy, 'rate': .10, 'enabled': view_builder is build_views}


def _instruction_views(row, decision, view_builder, role):
    generated = view_builder(row, decision, role=role)
    if _injection_policy(view_builder)['enabled']:
        from . import sft_injections
        augmented = sft_injections.augment(generated.get('views', []), rate=.10)
        generated = {**generated, 'views': augmented['views'],
                     'exclusions': [*generated.get('exclusions', []), *augmented['exclusions']],
                     'injection_stats': augmented['stats']}
    return generated


def _policy(admit, view_builder):
    from . import mined_sft
    from . import (funcsplit, leakage, source_fingerprints, validation_exposure, sft_injections,
                   sft_evidence, pyparse, astdiff, splitdeps, splitmatch, sft_token_audit,
                   npm_evidence_sources, npm_case_collector, security_snapshots, npm_package_scope, sft_views)
    from .ingest import licenses, osv
    from .verify import resource_budget
    from training.finetune import sft_serialization, sft_contracts, serialization, contracts, tokenization
    from .views import rendering
    modules = [pathlib.Path(__file__), pathlib.Path(appended_evidence.__file__),
               pathlib.Path(funcsplit.__file__), pathlib.Path(leakage.__file__), pathlib.Path(source_fingerprints.__file__),
               pathlib.Path(rendering.__file__), pathlib.Path(validation_exposure.__file__),
               pathlib.Path(resource_budget.__file__), pathlib.Path(bounded_parquet.__file__),
               pathlib.Path(audit_checkpoint_migration.__file__),
               pathlib.Path(__file__).resolve().parents[3] / 'tools/build_sft_dataset.py']
    modules.extend(pathlib.Path(module.__file__) for module in
        (sft_injections, sft_evidence, sft_serialization, sft_contracts, serialization, contracts,
         pyparse, astdiff, splitdeps, splitmatch, sft_token_audit, tokenization,
         npm_evidence_sources, npm_case_collector, security_snapshots, npm_package_scope, licenses, osv, sft_views))
    for function in (admit, view_builder):
        path = inspect.getsourcefile(function)
        if path:
            modules.append(pathlib.Path(path))
        for dependency in getattr(function, '__globals__', {}).values():
            if (isinstance(dependency, ModuleType) and dependency.__name__.startswith(
                    ('app.backend.pipeline.', 'training.finetune.')) and getattr(dependency, '__file__', None)):
                modules.append(pathlib.Path(dependency.__file__))
    return {'policy': POLICY, 'protocol': PROTOCOL, 'admit': _function_hash(admit),
            'coverage_policy': mined_sft.coverage_policy() if view_builder is mined_sft.build_views else None,
            'views': _function_hash(view_builder), 'rendering': rendering.identity(),
            'sft_rendering': sft_views.rendering_identity(), 'development_exposure': _exposure_policy(),
            'injection_policy': _injection_policy(view_builder), 'parser': _parser_identity(),
            'output_layout': bounded_parquet.identity(),
            'source_projection': {'schema': PROJECTION, 'metadata_allowlist': sorted(_EXPORT_METADATA),
                                  'context_snapshot_files': 'omitted_non_authoritative_discovery_index'},
            'files': {str(path.resolve()): versions._sha256(path) for path in modules}}


def _check_cancel(cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError('SFT audit cancelled before publication')


def _rows(source, authority, assignments, root, cancelled):
    seen = set()
    for row in evidence_recovery._records(root / ('dataset_' + source['tag'])):
        _check_cancel(cancelled)
        rid = row.get('record_id')
        if not isinstance(rid, str) or not rid or rid in seen:
            raise ValueError('missing or duplicate immutable record identity')
        if len(_json(row).encode()) > MAX_ROW_BYTES:
            raise ValueError('immutable record exceeds bounded row budget')
        seen.add(rid)
        old = assignments.get(rid)
        if old:
            if old.get('split') not in ('train', 'val', 'test', 'quarantine'):
                raise ValueError('invalid original split')
            row = {**row, 'split': old['split'], 'issue_group_id': old['component']}
        elif source['tag'] != authority['tag']:
            # Never import derived quarantine components as original ancestry.
            row = {**row, 'split': '', 'issue_group_id': ''}
        yield row
    if not set(assignments) <= seen:
        raise ValueError('original partition coverage changed')
    expected = (source['manifest'].get('counts') or {}).get('records')
    if expected != len(seen):
        raise ValueError('immutable source row count mismatch')


def _role(assignment):
    split = assignment['split']
    return assignment.get('role') or ('untouched_test' if split == 'test' else split)


def _failed(rid, error):
    return {'policy': POLICY, 'record_id': rid, 'source_integrity': 'unknown', 'label_support': 'unknown',
        'training_eligibility': 'ineligible', 'execution_verification': 'not_run',
        'proof_engine_status': 'not_evaluated', 'eligible_tasks': [], 'task_decisions': {},
        'provenance': {}, 'reasons': [{'code': 'record_audit_failed', 'field': 'record',
            'detail': type(error).__name__, 'remediation': 'Inspect the immutable source and retry its evidence audit'}]}


def _view_row(view, row, assignment):
    role = _role(assignment)
    corpus_view = (view.get('provenance') or {}).get('policy') == 'mined-corpus/1'
    if view.get('record_id') != row['record_id']:
        raise ValueError('instruction view record identity mismatch')
    messages, response = view['messages'], view['response']
    if (not isinstance(messages, list) or not messages or not isinstance(response, (dict, str))
            or any(not isinstance(message, dict) or message.get('role') not in ('system', 'user', 'assistant')
                   or not isinstance(message.get('content'), str) for message in messages)):
        raise ValueError('invalid native chat view')
    context = view.get('context_text') or next((m['content'] for m in reversed(messages) if m['role'] == 'user'), '')
    subject = view.get('subject_id') or row['record_id'] + ':' + str(view.get('side') or 'before')
    result = {'view_id': view['view_id'], 'record_id': row['record_id'], 'family_id': assignment['component'],
        'issue_group_id': assignment['component'], 'subject_id': subject, 'task': view['task'],
        'pool': view.get('pool') or ('scoped_negative' if isinstance(response, dict) and response.get('decision') == 'no_finding' else 'supported'),
        'context_text': context, 'response_json': _json(response), 'messages_json': _json(messages),
        'split': assignment['split'], 'val_role': assignment.get('role'), 'role': role,
        'eligible': bool(view.get('eligible', True)) and (corpus_view or (
            not assignment.get('quarantined', False) and assignment['split'] != 'quarantine'
            and not (assignment['split'] == 'test' and assignment.get('development_exposure')))),
        'weight': float(view.get('weight', 1.0)),
        'lineage_json': _json(view.get('lineage') or json.loads(view.get('lineage_json') or '{}') or {'protocol': PROTOCOL}),
        'provenance_json': _json(view.get('provenance') or {}),
        'source_fingerprint': str(view.get('source_fingerprint') or view.get('source_sha256') or _digest({k: row.get(k) for k in ('vulnerable_code', 'fixed_code')})),
        'context_fingerprint': str(view.get('context_fingerprint') or view.get('context_sha256') or _digest(messages)),
        'context_hash': str(view.get('context_hash') or _digest(messages)), 'review_status': 'auto_checked',
        'flags_json': _json(view.get('flags') or []),
        'prompt_chars': sum(len(m['content']) for m in messages), 'completion_chars': len(_json(response)),
        'evaluation_exposure': 'validator_development' if assignment.get('development_exposure') else 'not_registered',
        'exposure_json': _json(assignment.get('exposure_sources') or {})}
    if not isinstance(result['view_id'], str) or not result['view_id'] or not isinstance(result['task'], str):
        raise ValueError('invalid view identity')
    return result


def _view_schema():
    import pyarrow as pa
    return pa.schema([(name, pa.type_for_alias(kind)) for name, kind in _VIEW_COLUMNS.items()])


def _report(dispositions, rows, raw_count):
    tasks, counts, dimensions = {}, Counter(), {key: Counter() for key in
        ('source_integrity', 'label_support', 'training_eligibility', 'execution_verification', 'proof_engine_status')}
    families, eligible_records = set(), set()
    for disposition in dispositions:
        counts['dispositions'] += 1
        counts['duplicates'] += disposition.get('duplicate_of') is not None
        counts['review_records'] += bool(disposition.get('review_required'))
        for key in dimensions:
            dimensions[key][disposition[key]] += 1
    data = defaultdict(lambda: {'views': 0, 'records': set(), 'families': set(), 'roles': defaultdict(lambda: {'views': 0, 'records': set(), 'families': set()})})
    for view in rows:
        if not view['eligible']:
            continue
        eligible_records.add(view['record_id'])
        task = data[view['task']]
        for target in (task, task['roles'][view['role']]):
            target['views'] += 1
            target['records'].add(view['record_id'])
            target['families'].add(view['family_id'])
        families.add(view['family_id'])
    def compact(value):
        return {'views': value['views'], 'rows': len(value['records']), 'independent_families': len(value['families'])}
    for name, value in data.items():
        tasks[name] = {**compact(value), 'roles': {role: compact(items) for role, items in value['roles'].items()}}
    return {'schema': 'sft-audit-report/1', 'policy': POLICY, 'counts': {**dict(counts), 'records': raw_count, 'source_records': raw_count, 'raw_records': raw_count, 'audited_records': raw_count,
        'eligible_views': sum(value['views'] for value in tasks.values()), 'eligible_records': len(eligible_records),
        'independent_families': len(families)},
        'dimensions': {key: dict(value) for key, value in dimensions.items()}, 'tasks': tasks,
        'execution': 'Collected source and proposed tests were not executed.',
        'model_performance': 'unmeasured', 'token_audit': 'pending'}


def _jsonl(path):
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            limit = (64 << 20) if path.name == 'instruction_views.jsonl' else MAX_ROW_BYTES
            if len(line.encode()) > limit:
                raise ValueError('audit row exceeds bounded row budget')
            yield json.loads(line)


def _result(state, workspace, published=False, path=None):
    folder = path or workspace / 'artifact'
    return {'schema': SCHEMA, 'tag': state['tag'], 'owner': state['owner'], 'path': str(folder),
        'workspace': str(workspace), 'published': published, 'source_identity': state['source_identity'],
        'manifest': str(folder / 'manifest.json'), 'report': str(folder / 'audit_report.json'),
        'dispositions': str(folder / 'dispositions.jsonl'), 'review_queue': str(folder / 'review_queue.jsonl')}


def build(source_tag='v18', *, root=None, progress=None, cancelled=None, admit=None, view_builder=None,
          publish=False, token_audit=None, resume_incomplete=False, migrate_output_layout=False):
    """Audit every immutable row. Restart the same policy/input from row checkpoints.

    ``publish=False`` returns owned staging for exact tokenizer inspection.
    ``publish=True`` requires an already bound audit, or fails without publishing.
    ``resume_incomplete=True`` continues an interrupted identical attempt,
    retaining its conservative unknowns while rechecking known evidence.
    The default instead requests an ordinary retry of unresolved evidence.
    ``migrate_output_layout=True`` creates a new unpublished attempt only for
    the separately pinned storage transition, retaining exact old checkpoints.
    """
    if type(resume_incomplete) is not bool:
        raise ValueError('resume_incomplete must be a boolean')
    if type(migrate_output_layout) is not bool:
        raise ValueError('migrate_output_layout must be a boolean')
    if migrate_output_layout and (resume_incomplete or publish):
        raise ValueError('output layout migration requires a separate unpublished audit, without incomplete continuation')
    if admit is None:
        from .mined_sft import admit_record
        admit = admit_record
    if view_builder is None:
        from . import mined_sft, sft_views
        view_builder = mined_sft.build_views if admit is mined_sft.admit_record else sft_views.build_views
    root = pathlib.Path(build_catalog._root(root)).absolute()
    _safe(root, root)
    _safe(root, root / versions.REGISTRY_NAME)
    _safe(root, root / '.versions.lock')
    prog = progress or (lambda **event: None)
    lock = oslock.FileLock(_safe(root, root / '.sft-build.lock'), note=False)
    lock.acquire(wait=0)
    try:
        result = _build(source_tag, root, prog, cancelled, admit, view_builder,
                        resume_incomplete=resume_incomplete, migrate_output_layout=migrate_output_layout)
    finally:
        lock.release()
    return finalize(result, token_audit, root=root, cancelled=cancelled) if publish else result


def _build(source_tag, root, prog, cancelled, admit, view_builder, *, resume_incomplete=False, migrate_output_layout=False):
    import pyarrow as pa
    import pyarrow.parquet as pq
    _check_cancel(cancelled)
    source = _capture(source_tag, root)
    authority, historical = _authority(source, root)
    policy = _policy(admit, view_builder)
    base = {'schema': SCHEMA, 'source': source, 'authority': authority, 'policy': policy}
    migration = None
    if migrate_output_layout:
        existing = _safe(root, root / '.sft_build' / ('.latest-' + _digest(base) + '.json'))
        if existing.exists():
            raise ValueError('output layout migration requires a new attempt, not an existing capture')
        migration = audit_checkpoint_migration.migration_seed(base, root)
    identity, index, base_key, seed = _attempt(base, root, resume_incomplete=resume_incomplete)
    key = _digest(identity)
    workspace = _safe(root, root / '.sft_build' / key)
    if migration is not None and (workspace.exists() or seed is not None):
        raise ValueError('output layout migration requires a new owned staging directory')
    workspace.mkdir(parents=True, exist_ok=True)
    claim_path = workspace / 'owner.json'
    if claim_path.exists():
        _safe(root, claim_path)
        state = json.loads(claim_path.read_text(encoding='utf-8'))
        if state.get('source_identity') != key or state.get('capture') != identity:
            raise ValueError('staging owner identity mismatch')
        if state.get('complete'):
            if _inventory(workspace / 'artifact') != state['inventory']:
                raise ValueError('staging inventory changed')
            return _result(state, workspace)
    else:
        if any(workspace.iterdir()):
            raise ValueError('unowned SFT staging directory')
        owner = 'sft-' + uuid.uuid4().hex
        tag = versions.reserve_version(root=root, owner=owner, minimum=int(source_tag[1:]) + 1)
        state = {'owner': owner, 'tag': tag, 'capture': identity, 'source_identity': key, 'complete': False,
                 'seed_workspace': str(seed) if seed else None}
        if migration is not None:
            state.update(seed_workspace=str(_safe(root, pathlib.Path(migration['workspace']))),
                         output_layout_migration=copy.deepcopy(migration['receipt']))
        _write(claim_path, state)
    _write(index, {'base': base_key, 'key': key})
    for item in workspace.rglob('*'):
        _safe(root, item)
    # The existing source/group machinery retains exact, meaningful-token and
    # near-clone relations, repositories, aliases and fork fix references.
    def group_progress(**event):
        _check_cancel(cancelled)
        prog(**event)
    direct_exposure = _authority_markers(authority, root)
    registry = set(policy['development_exposure']['record_ids'])
    def group_records():
        for row in _rows(source, authority, historical, root, cancelled):
            bases = _exposure_bases(row, registry)
            if bases:
                direct_exposure[row['record_id']] = sorted(set(direct_exposure.get(row['record_id'], []) + bases))
            yield row
    plan = appended_evidence.prepare(group_records(), historical, workspace=workspace, progress=group_progress)
    from . import mined_sft
    corpus = view_builder is mined_sft.build_views
    if corpus:
        mined_sft.recover_partitions(plan, historical)
    exposure_groups = defaultdict(dict)
    for rid, bases in direct_exposure.items():
        exposure_groups[plan['records'][rid]['component']][rid] = bases
    for assignment in plan['records'].values():
        sources = exposure_groups.get(assignment['component'], {})
        assignment.update(development_exposure=bool(sources), exposure_sources=dict(sources))
    _check_cancel(cancelled)
    artifact = _safe(root, workspace / 'artifact')
    artifact.mkdir(exist_ok=True)
    for item in artifact.rglob('*'):
        _safe(root, item)
    expected = len(plan['records'])
    source_folder = root / ('dataset_' + source_tag)
    with pq.ParquetFile(source_folder / 'records.parquet') as source_parquet:
        schema = source_parquet.schema_arrow
    if 'sft_export_state_json' not in schema.names:
        schema = schema.append(pa.field('sft_export_state_json', pa.string()))
    database = _safe(root, workspace / 'checkpoints.sqlite3')
    duplicates, evidence_files, checked_registrations = {}, {}, set()
    db = sqlite3.connect(database)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS items (rid TEXT PRIMARY KEY, identity TEXT, disposition TEXT, views TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS registrations (identity TEXT PRIMARY KEY, body TEXT NOT NULL)')
        if 'output_layout_migration' in state:
            descriptor = {'workspace': state.get('seed_workspace'), 'receipt': state['output_layout_migration']}
            if audit_checkpoint_migration.seed_checkpoints(db, descriptor, base, root) != state['output_layout_migration']:
                raise ValueError('output layout migration receipt changed while seeding')
        else:
            _seed_checkpoints(db, state.get('seed_workspace'), root,
                              set(identity.get('retry', {}).get('unresolved_record_ids', [])))
        with bounded_parquet.BoundedParquetWriter(artifact / 'records.parquet', schema) as records_out, \
                bounded_parquet.BoundedParquetWriter(artifact / 'views.parquet', _view_schema()) as views_out, \
                (artifact / 'dispositions.jsonl').open('w', encoding='utf-8', newline='\n') as dispositions, \
                (artifact / 'review_queue.jsonl').open('w', encoding='utf-8', newline='\n') as review_queue, \
                (artifact / 'instruction_views.jsonl').open('w', encoding='utf-8', newline='\n') as native:
            seen_views = set()
            for number, original in enumerate(_rows(source, authority, historical, root, cancelled), 1):
                rid = original['record_id']
                assignment = plan['records'][rid]
                row = {**original, 'split': assignment['split'], 'issue_group_id': assignment['component']}
                row_hash = _digest(row)
                duplicate_key = _digest({k: row.get(k) for k in ('language', 'vulnerable_code', 'fixed_code')})
                duplicate_of = duplicates.get(duplicate_key)
                duplicates.setdefault(duplicate_key, rid)
                cached = db.execute('SELECT identity,disposition,views FROM items WHERE rid=?', (rid,)).fetchone()
                if cached:
                    if cached[0] != row_hash:
                        raise ValueError('checkpoint source identity mismatch')
                    disposition, row_views = json.loads(cached[1]), json.loads(cached[2])
                else:
                    exclusions = []
                    try:
                        decision = admit(row, root=root)
                        if decision.get('policy') != POLICY or decision.get('record_id') != rid:
                            raise ValueError('admission policy or record identity mismatch')
                        generated = _instruction_views(row, decision, view_builder, _role(assignment))
                        row_views = [_view_row(view, row, assignment) for view in generated.get('views', [])]
                        exclusions = generated.get('exclusions', [])
                    except InterruptedError:
                        raise
                    except Exception as error:
                        decision, row_views = _failed(rid, error), []
                    reasons = list(decision.get('reasons') or [])
                    for task, task_decision in (decision.get('task_decisions') or {}).items():
                        if not task_decision.get('eligible'):
                            reasons.extend({**reason, 'task': task} for reason in task_decision.get('reasons') or [])
                    if assignment.get('quarantined'):
                        reasons.append({'code': 'cross_partition_group_conflict', 'field': 'assignment',
                            'detail': 'Connected source units have incompatible original exposure roles',
                            'remediation': 'Inspect the source graph; preserve original exposure roles'})
                    if assignment.get('development_exposure'):
                        reasons.append({'code': 'validator_development_exposure', 'field': 'evaluation_exposure',
                            'detail': 'The connected source group was exposed during validator development',
                            'remediation': 'Retain original assignments; exclude exposed test views from untouched evaluation'})
                    disposition = {key: copy.deepcopy(decision.get(key)) for key in
                        ('policy', 'record_id', 'source_integrity', 'label_support', 'training_eligibility',
                         'execution_verification', 'proof_engine_status', 'eligible_tasks', 'task_decisions', 'provenance')}
                    disposition.update(source_reference={'tag': source_tag, 'artifact': 'records.parquet', 'record_id': rid,
                        'sha256': source['files']['records.parquet'], 'row': number - 1,
                        'output_artifact': 'records.parquet', 'context_fields': ['context_before', 'context_after'],
                        'source_fields': ['vulnerable_code', 'fixed_code']}, original_assignment=historical.get(rid),
                        assignment=assignment, family_id=assignment['component'], duplicate_of=duplicate_of,
                        evaluation_exposure={'exposed': bool(assignment.get('development_exposure')),
                            'sources': assignment.get('exposure_sources') or {},
                            'untouched_test_eligible': not (assignment['split'] == 'test' and assignment.get('development_exposure'))},
                        reasons=reasons, view_exclusions=exclusions, view_ids=[v['view_id'] for v in row_views],
                        review_required=bool(reasons or exclusions or decision.get('training_eligibility') != 'eligible'
                                             or not row_views or assignment.get('quarantined')))
                    db.execute('INSERT INTO items VALUES (?,?,?,?)', (rid, row_hash, _json(disposition), _json(row_views)))
                    db.commit()
                provenance = disposition.get('provenance') or {}
                evidence_files.update(_backend_evidence(provenance, root, evidence_files))
                _capture_registrations(provenance, db, root, checked_registrations)
                for view in row_views:
                    if view['view_id'] in seen_views:
                        raise ValueError('duplicate instruction view identity')
                    seen_views.add(view['view_id'])
                projected, export_state = _project_record(row, disposition)
                disposition['export_state'] = export_state
                disposition['source_reference'].update(authoritative='immutable_parent_raw',
                    exported_sides=export_state['sides'],
                    original_side_fields={'before': ['vulnerable_code', 'context_before'],
                                          'after': ['fixed_code', 'context_after']})
                records_out.write_table(pa.Table.from_pylist([projected], schema=schema))
                if row_views:
                    views_out.write_table(pa.Table.from_pylist(row_views, schema=_view_schema()))
                    for view in row_views:
                        native.write(_json({**view, 'messages': json.loads(view['messages_json']),
                                            'response': json.loads(view['response_json'])}) + '\n')
                public_disposition = _public_disposition(disposition)
                dispositions.write(_json(public_disposition) + '\n')
                if disposition['review_required']:
                    review_queue.write(_json(public_disposition) + '\n')
                prog(state='records', label='Auditing immutable SFT source records', processed=number, total=expected)
    finally:
        db.close()
    registration_file = artifact / 'backend_source_registrations.jsonl'
    registration_count = _write_registrations(database, registration_file)
    _validate_registration_file(registration_file, root)
    raw_count = sum(1 for _ in _jsonl(artifact / 'dispositions.jsonl'))
    if raw_count != expected:
        raise ValueError('all-record disposition coverage mismatch')
    report = _report(_jsonl(artifact / 'dispositions.jsonl'), _jsonl(artifact / 'instruction_views.jsonl'), raw_count)
    report['counts']['backend_source_registrations'] = registration_count
    _write(artifact / 'audit_report.json', report)
    _write(artifact / 'stats.json', report)
    appended_evidence.write_graph(plan, source_folder, artifact)
    if corpus:
        split_path = artifact / 'split_manifest.json'
        split_manifest = json.loads(split_path.read_text(encoding='utf-8'))
        split_manifest['append_source'].update(
            historical_assignments='recorded separately; every mined record is assigned to training',
            coverage_policy=mined_sft.POLICY,
            resolved_historical_conflicts=plan.get('historical_conflicted_components', []))
        _write(split_path, split_manifest)
    partitions_path = artifact / 'security_partitions.json'
    partitions = json.loads(partitions_path.read_text(encoding='utf-8'))
    exposed_test = [rid for rid, assignment in plan['records'].items()
                    if assignment['split'] == 'test' and assignment.get('development_exposure')]
    partitions.update(test_is_untouched=False if corpus else not exposed_test, eligible_test_is_untouched=False if corpus else True,
                      development_exposed_test_records=exposed_test)
    if corpus:
        partitions.update(evaluation_available=False, partition_policy='all_records_train/1')
    _write(partitions_path, partitions)
    _write(artifact / 'development_exposure.json', {'policy': policy['development_exposure'],
        'direct_records': direct_exposure, 'groups': dict(exposure_groups), 'excluded_untouched_test_records': exposed_test})
    _write(artifact / 'source_capture.json', identity)
    if 'output_layout_migration' in state:
        _write(artifact / 'output_layout_migration.json', state['output_layout_migration'])
    _write(artifact / 'evidence_capture.json', evidence_files)
    _validate_evidence(evidence_files, root)
    profile = copy.deepcopy(source['manifest'].get('build_profile', {}))
    profile.pop('qualification_policy', None)
    profile.update(admission_policy=POLICY, quality_profile='sft', version='sft-build-profile/1')
    manifest = {'schema': 'dataset-manifest/1', 'tag': state['tag'], 'dataset_status': 'frozen',
        'quality_profile': 'sft', 'admission_policy': POLICY, 'serialization_protocol': PROTOCOL,
        'build_profile': profile, 'injection_policy': policy['injection_policy'],
        'source_projection': policy['source_projection'],
        'source': {'frozen_parent': source_tag, 'original_assignment_authority': authority['tag'],
                   'snapshot_sha256': key}, 'counts': report['counts'], 'execution_verification': 'not_run',
        'security_readiness': {'schema': 'sft-readiness/1', 'admission_policy': POLICY,
                               'counts': report['counts'], 'training_ready': False,
                               'blockers': ['exact_token_audit_pending'], 'model_performance': 'unmeasured'},
        'versions': {'build': SCHEMA, 'admission': POLICY}, 'tasks': report['tasks'],
        'files': {name: {'sha256': sha, 'bytes': (artifact / name).stat().st_size} for name, sha in _inventory(artifact).items()}}
    if corpus:
        manifest['coverage_policy'] = mined_sft.coverage_policy()
    _write(artifact / 'manifest.json', manifest)
    _check_cancel(cancelled)
    if _capture(source_tag, root) != source or _capture(authority['tag'], root) != authority or _policy(admit, view_builder) != policy:
        raise ValueError('source or admission policy changed during audit')
    _validate_registration_file(registration_file, root)
    state.update(complete=True, inventory=_inventory(artifact))
    _write(claim_path, state)
    return _result(state, workspace)


def _reconcile_publication(source, destination, kept_views, excluded, *, source_sha256, expected_records, cancelled=None):
    """Stream public admission records into a truthful post-audit review queue.

    Only identifiers and eligibility metadata from verified accepted views enter
    this transformation. Original source-admission findings remain unchanged.
    """
    remaining, rejections = dict(kept_views), defaultdict(list)
    for item in excluded:
        reason = str(item['reason']).split(':', 1)[0]
        reason = _metadata_atom(reason, 128) or 'view_rejected'
        rejections[item['record_id']].append({'view_id': item['view_id'], 'record_id': item['record_id'],
            'task': item['task'], 'reason': reason, 'stage': 'exact_token_and_quality_audit',
            'remediation': ('Inspect all views sharing this prompt fingerprint and their immutable source references; '
                            'recover distinguishable grounded inputs or consistent evidence before re-auditing.'
                            if reason == 'conflicting_prompt_supervision' else
                            'Inspect the exact-token audit and immutable source reference; recover a grounded bounded eligible view before re-auditing.')})
    hashes = {name: hashlib.sha256() for name in ('dispositions.jsonl', 'review_queue.jsonl')}
    original_hash, seen = hashlib.sha256(), set()
    with source.open('rb') as incoming, \
            (destination / 'dispositions.jsonl').open('w', encoding='utf-8', newline='\n') as dispositions, \
            (destination / 'review_queue.jsonl').open('w', encoding='utf-8', newline='\n') as queue:
        while line := incoming.readline(MAX_ROW_BYTES + 1):
            _check_cancel(cancelled)
            if len(line) > MAX_ROW_BYTES:
                raise ValueError('publication disposition exceeds bounded row budget')
            original_hash.update(line)
            record = json.loads(line)
            rid = record['record_id']
            if rid in seen:
                raise ValueError('duplicate publication disposition record identity')
            seen.add(rid)
            staged_ids = record['view_ids']
            kept, dropped = remaining.pop(rid, []), rejections.pop(rid, [])
            kept_ids, dropped_ids = [view['view_id'] for view in kept], [item['view_id'] for item in dropped]
            if (not isinstance(staged_ids, list) or len(staged_ids) != len(set(staged_ids))
                    or len(kept_ids) + len(dropped_ids) != len(staged_ids)
                    or set(kept_ids) & set(dropped_ids) or set(kept_ids) | set(dropped_ids) != set(staged_ids)):
                raise ValueError('publication disposition view partition mismatch')
            usable = [view for view in kept if view['eligible'] and view['weight'] > 0]
            tasks = Counter(view['task'] for view in usable)
            record.update(source_admission_stage='before_exact_token_and_quality_audit',
                staged_view_ids=list(staged_ids), view_ids=kept_ids, publication_exclusions=dropped,
                published_view_eligibility={'schema': 'sft-published-view-eligibility/1',
                    'tasks': sorted(tasks), 'task_view_counts': dict(sorted(tasks.items())),
                    'view_count': len(usable), 'retained_view_count': len(kept), 'eligible_content': bool(usable),
                    'train_allowed': any(view['split'] == 'train' and view['role'] == 'train' for view in usable),
                    'evaluation_allowed': any(view['split'] in ('val', 'test')
                        and view['role'] in ('selection', 'calibration', 'val', 'untouched_test')
                        and view.get('evaluation_exposure') != 'validator_development' for view in usable)},
                review_required=bool(record.get('review_required') or record.get('view_exclusions') or dropped))
            output = _json(record) + '\n'
            dispositions.write(output)
            hashes['dispositions.jsonl'].update(output.encode())
            if record['review_required']:
                queue.write(output)
                hashes['review_queue.jsonl'].update(output.encode())
    if original_hash.hexdigest() != source_sha256:
        raise ValueError('copied disposition snapshot changed during publication reconciliation')
    if len(seen) != expected_records or remaining or rejections:
        raise ValueError('publication reconciliation record coverage mismatch')
    return {name: digest.hexdigest() for name, digest in hashes.items()}


def finalize(staged, token_audit, *, root=None, cancelled=None, publication_guard=None):
    """Publish owned staging after validating exact-token coverage and freshness."""
    root = pathlib.Path(build_catalog._root(root)).absolute()
    _safe(root, root)
    lock = oslock.FileLock(_safe(root, root / '.sft-build.lock'), note=False)
    lock.acquire(wait=0)
    try:
        return _finalize(staged, token_audit, root, cancelled, publication_guard)
    finally:
        lock.release()


def _finalize(staged, audit, root, cancelled, publication_guard):
    import pyarrow as pa
    import pyarrow.parquet as pq
    _check_cancel(cancelled)
    if not isinstance(staged, dict) or staged.get('schema') != SCHEMA:
        raise ValueError('invalid staging identity')
    workspace = _safe(root, pathlib.Path(staged['workspace']))
    state = json.loads(_safe(root, workspace / 'owner.json').read_text(encoding='utf-8'))
    folder = _safe(root, workspace / 'artifact')
    if (staged.get('owner') != state['owner'] or staged.get('tag') != state['tag']
            or pathlib.Path(staged['path']) != folder or not state.get('complete')
            or state['source_identity'] != staged.get('source_identity')):
        raise ValueError('staging owner or identity mismatch')
    _verify_inventory(folder, state['inventory'])
    snapshot_membership = _verified_views(folder)
    _verify_inventory(folder, state['inventory'])
    capture = state['capture']
    audit = copy.deepcopy(audit)
    if _exposure_policy() != capture['policy']['development_exposure']:
        raise ValueError('development exposure policy changed before publication')
    if _parser_identity() != capture['policy']['parser']:
        raise ValueError('source parser policy changed before publication')
    evidence_files = json.loads((folder / 'evidence_capture.json').read_text(encoding='utf-8'))
    _validate_evidence(evidence_files, root)
    registration_count = _validate_registration_file(folder / 'backend_source_registrations.jsonl', root)
    for source in (capture['source'], capture['authority']):
        if _capture(source['tag'], root) != source:
            raise ValueError('source changed before publication')
    if any(versions._sha256(pathlib.Path(name)) != sha for name, sha in capture['policy']['files'].items()):
        raise ValueError('policy changed before publication')
    from .views import rendering
    if rendering.identity() != capture['policy']['rendering']:
        raise ValueError('rendering policy changed before publication')
    from . import sft_views
    if sft_views.rendering_identity() != capture['policy']['sft_rendering']:
        raise ValueError('SFT rendering policy changed before publication')
    if capture['policy']['injection_policy']['enabled'] or capture['policy'].get('coverage_policy'):
        from .sft_token_audit import implementation_identity
        implementation = implementation_identity()
        if (not isinstance(audit, dict) or audit.get('implementation') != implementation
                or any(capture['policy']['files'].get(name) != sha
                       for name, sha in implementation['files'].items())):
            raise ValueError('exact token audit implementation identity mismatch')
    tokenizer = audit.get('tokenizer') if isinstance(audit, dict) else None
    tokenizer = tokenizer if isinstance(tokenizer, dict) else {}
    if (not isinstance(audit, dict) or audit.get('schema') != 'sft-token-audit/1'
            or audit.get('policy') != POLICY or audit.get('protocol') != PROTOCOL or audit.get('passed') is not True
            or audit.get('max_tokens') != 4096 or type(audit.get('max_total_tokens')) is not int
            or not 0 < audit['max_total_tokens'] <= 4096
            or tokenizer.get('model_id') != 'Qwen/Qwen2.5-Coder-7B-Instruct'
            or not re.fullmatch('[a-f0-9]{40}', str(tokenizer.get('revision') or ''))
            or not re.fullmatch('[a-f0-9]{64}', str(tokenizer.get('tokenizer_sha256') or ''))
            or audit.get('source_sha256') != state['inventory']['records.parquet']
            or audit.get('views_sha256') != state['inventory']['views.parquet']):
        raise ValueError('exact token audit identity or bounds mismatch')
    accepted, excluded = audit.get('accepted_view_ids'), audit.get('excluded_views')
    if (not isinstance(accepted, list) or any(not isinstance(v, str) for v in accepted)
            or len(accepted) != len(set(accepted)) or not isinstance(excluded, list)
            or any(not isinstance(v, dict) or not isinstance(v.get('view_id'), str) or not v.get('reason') for v in excluded)):
        raise ValueError('invalid token audit view partition')
    accepted_ids, excluded_ids = set(accepted), [v['view_id'] for v in excluded]
    ids, train, covered_records = set(), False, set()
    references = {}
    for row in _jsonl(folder / 'instruction_views.jsonl'):
        rid = row['view_id']
        ids.add(rid)
        references[rid] = {'record_id': row['record_id'], 'task': row['task']}
        if rid in accepted_ids and row['eligible'] and row['split'] == 'train' and row['weight'] > 0:
            train = True
        if rid in accepted_ids and row['eligible'] and row['weight'] > 0:
            covered_records.add(row['record_id'])
    if (len(excluded_ids) != len(set(excluded_ids)) or accepted_ids & set(excluded_ids)
            or accepted_ids | set(excluded_ids) != ids or audit.get('considered_views') != len(ids) or not train
            or any(item.get('record_id') != references[item['view_id']]['record_id']
                   or item.get('task') != references[item['view_id']]['task'] for item in excluded if item['view_id'] in references)):
        raise ValueError('token audit partition coverage or usable training data mismatch')
    staged_manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if (staged_manifest.get('coverage_policy', {}).get('schema') == 'mined-corpus/1'
            and len(covered_records) != capture['source']['manifest']['counts']['records']):
        raise ValueError('Mined corpus publication requires usable views for every record')
    # Build a separate publication tree. A failed/cancelled publication leaves the
    # exact staged/token-audited bytes available for inspection and retry.
    destination = _safe(root, workspace / ('publication-' + uuid.uuid4().hex))
    destination.mkdir()
    for path in folder.iterdir():
        _safe(root, path)
        if path.is_file():
            shutil.copyfile(path, destination / path.name)
    _verify_inventory(folder, state['inventory'])
    _verify_inventory(destination, state['inventory'], 'copied staging')
    _verified_views(destination, snapshot_membership)
    _verify_inventory(destination, state['inventory'], 'copied staging')
    # Derive accepted rows from the verified, token-bound Parquet snapshot.
    # Temporary transformed files stay outside the snapshot until it is fenced
    # again, so no later read can silently substitute current staging messages.
    transformed = _safe(root, workspace / ('transformation-' + uuid.uuid4().hex))
    transformed.mkdir()
    accepted_membership = {rid: fingerprint for rid, fingerprint in snapshot_membership.items() if rid in accepted_ids}
    kept_metadata = defaultdict(list)
    with bounded_parquet.BoundedParquetWriter(transformed / 'views.parquet', _view_schema()) as writer, \
            (transformed / 'instruction_views.jsonl').open('w', encoding='utf-8', newline='\n') as native, \
            pq.ParquetFile(destination / 'views.parquet') as copied:
        for batch in copied.iter_batches(batch_size=1):
            for row in batch.to_pylist():
                _check_cancel(cancelled)
                if snapshot_membership.get(row['view_id']) != _digest(row):
                    raise ValueError('token audited snapshot payload changed during transformation')
                if row['view_id'] in accepted_ids:
                    writer.write_table(pa.Table.from_pylist([row], schema=_view_schema()))
                    native.write(_json({**row, 'messages': json.loads(row['messages_json']),
                                        'response': json.loads(row['response_json'])}) + '\n')
                    kept_metadata[row['record_id']].append({key: row[key] for key in
                        ('view_id', 'task', 'eligible', 'weight', 'split', 'role', 'evaluation_exposure')})
    reconciled_hashes = _reconcile_publication(destination / 'dispositions.jsonl', transformed, kept_metadata, excluded,
        source_sha256=state['inventory']['dispositions.jsonl'],
        expected_records=capture['source']['manifest']['counts']['records'], cancelled=cancelled)
    _verify_inventory(destination, state['inventory'], 'copied staging')
    _verify_inventory(folder, state['inventory'])
    for name in ('views.parquet', 'instruction_views.jsonl', 'dispositions.jsonl', 'review_queue.jsonl'):
        (transformed / name).replace(destination / name)
    _verified_views(destination, accepted_membership)
    exclusion_hash = hashlib.sha256()
    with (destination / 'token_exclusions.jsonl').open('w', encoding='utf-8', newline='\n') as output:
        for item in excluded:
            line = _json({**item, 'source_reference': {'artifact': 'records.parquet', 'record_id': item['record_id']},
                'remediation': 'Inspect the complete exact-token audit; recover a grounded bounded view'}) + '\n'
            output.write(line)
            exclusion_hash.update(line.encode())
    _write(destination / 'token_audit.json', audit)
    if any(versions._sha256(destination / name) != sha for name, sha in reconciled_hashes.items()):
        raise ValueError('publication review snapshot changed before reporting')
    report = _report(_jsonl(destination / 'dispositions.jsonl'), _jsonl(destination / 'instruction_views.jsonl'),
                     capture['source']['manifest']['counts']['records'])
    report['counts']['backend_source_registrations'] = registration_count
    report.update(token_audit='completed', token_excluded_views=len(excluded))
    _write(destination / 'audit_report.json', report)
    _write(destination / 'stats.json', report)
    manifest = json.loads((destination / 'manifest.json').read_text(encoding='utf-8'))
    manifest.update(counts=report['counts'], tasks=report['tasks'], tokenizer=tokenizer,
        token_audit={'schema': audit['schema'], 'max_tokens': 4096, 'max_total_tokens': audit['max_total_tokens'],
                     'sha256': versions._sha256(destination / 'token_audit.json')},
        security_readiness={'schema': 'sft-readiness/1', 'admission_policy': POLICY,
                            'counts': report['counts'], 'training_ready': True,
                            'blockers': [], 'model_performance': 'unmeasured'})
    controlled = {'views.parquet', 'instruction_views.jsonl', 'dispositions.jsonl', 'review_queue.jsonl',
                  'audit_report.json', 'stats.json', 'manifest.json'}
    # Copied bytes outside the defined transformation retain their original
    # hashes. Written JSON hashes come from intended values, not mutable output.
    final_inventory = {name: sha for name, sha in state['inventory'].items() if name not in controlled}
    final_inventory.update({name: versions._sha256(destination / name)
                            for name in ('views.parquet', 'instruction_views.jsonl')})
    final_inventory.update(reconciled_hashes)
    final_inventory.update({'audit_report.json': _json_file_hash(report), 'stats.json': _json_file_hash(report),
                            'token_audit.json': _json_file_hash(audit),
                            'token_exclusions.jsonl': exclusion_hash.hexdigest()})
    _verify_inventory(destination, {**final_inventory, 'manifest.json': state['inventory']['manifest.json']}, 'copied publication')
    manifest['files'] = {name: {'sha256': sha, 'bytes': (destination / name).stat().st_size}
                         for name, sha in final_inventory.items()}
    _write(destination / 'manifest.json', manifest)
    final_inventory['manifest.json'] = _json_file_hash(manifest)
    _verify_inventory(destination, final_inventory, 'publication')
    @contextmanager
    def fence():
        with versions.publication_scope(publication_guard):
            _check_cancel(cancelled)
            _verify_inventory(folder, state['inventory'])
            _verify_inventory(destination, final_inventory, 'publication')
            _verified_views(destination, accepted_membership)
            _validate_evidence(evidence_files, root)
            _validate_registration_file(folder / 'backend_source_registrations.jsonl', root)
            if _exposure_policy() != capture['policy']['development_exposure']:
                raise ValueError('development exposure policy changed at publication fence')
            if _parser_identity() != capture['policy']['parser']:
                raise ValueError('source parser policy changed at publication fence')
            if rendering.identity() != capture['policy']['rendering']:
                raise ValueError('rendering policy changed at publication fence')
            if sft_views.rendering_identity() != capture['policy']['sft_rendering']:
                raise ValueError('SFT rendering policy changed at publication fence')
            for source in (capture['source'], capture['authority']):
                if _capture(source['tag'], root) != source:
                    raise ValueError('source changed at publication fence')
            if any(versions._sha256(pathlib.Path(name)) != sha for name, sha in capture['policy']['files'].items()):
                raise ValueError('policy changed at publication fence')
            _verify_inventory(folder, state['inventory'])
            _verify_inventory(destination, final_inventory, 'publication')
            yield
    final = versions.publish_reserved(destination, state['tag'], root, owner=state['owner'], publication_guard=fence)
    return _result(state, workspace, published=True, path=final)
