"""Read backend-bound source and advisory facts for SFT, without security proof.

Record labels, imported receipts and caller-supplied dictionaries are not issuers.
The local backend indexes and their pinned source objects are the trust boundary.
"""
from __future__ import annotations

import copy
import atexit
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import threading
import zipfile
from urllib.parse import unquote

_ZIP_LOCK = threading.RLock()
_ZIP_STATE = {}
# Pure compatibility reproduction of context_builder's
# deterministic_local_snapshot_v1 non-Python import projection. Keeping the
# rule here makes its complete identity part of this adapter's policy hash.
_CONTEXT_IMPORT_LINE = re.compile(r'^[ \t]*(?:import|using|use|require|require_relative|include|#include|from)\b[^\n]*$', re.M)


def _close_archive():
    with _ZIP_LOCK:
        if _ZIP_STATE:
            _ZIP_STATE['archive'].close()
            _ZIP_STATE.clear()


atexit.register(_close_archive)


def _raw_advisories(record):
    """One bounded ZIP metadata index, no corpus/source bodies retained globally."""
    from .ingest import osv
    archive_path = Path(osv.dump_path('npm')).resolve()
    if not archive_path.is_file():
        return [], None
    stat = archive_path.stat()
    identity = (str(archive_path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    hashes = set(unpack(record.get('source_record_hashes'), []))
    names = set(unpack(record.get('advisory_aliases'), [])) | set(unpack(record.get('raw_record_keys'), []))
    names = {value.split(':')[-1] + '.json' for value in names if isinstance(value, str)}
    if not hashes or not names:
        return [], None
    with _ZIP_LOCK:
        if _ZIP_STATE.get('identity') != identity:
            _close_archive()
            from .npm_case_collector import _dump_directory, _hash
            _dump_directory(archive_path)
            archive = zipfile.ZipFile(archive_path)
            _ZIP_STATE.update(identity=identity, archive=archive, sha256=_hash(archive_path),
                              names={Path(info.filename).name: info for info in archive.infolist()})
        rows = []
        for name in sorted(names):
            info = _ZIP_STATE['names'].get(name)
            if info is None or info.file_size > 2 << 20:
                continue
            raw = _ZIP_STATE['archive'].read(info)
            sha = hashlib.sha256(raw).hexdigest()
            if sha in hashes:
                rows.append({'sha256': sha, 'advisory': json.loads(raw)})
        after = archive_path.stat()
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != identity[1:]:
            raise ValueError('advisory_archive_changed_during_read')
        return rows, {'path': str(archive_path), 'sha256': _ZIP_STATE['sha256']}


def _object_sha(kind, data):
    return hashlib.sha1(kind.encode() + b' ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def _backend_path(root, path):
    """Registered artifacts and indexes must be ordinary local workspace paths."""
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
        raise ValueError('registered_source_path_escape')
    for current in (path, *path.parents):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('registered_source_path_redirect')
        if current == root:
            break
    return path


def _registered_exact(provider, repository, revision, path, root):
    """Read the existing providers' exact registration contract without migrations.

    Their public read_exact helpers open write-capable schema managers. Audit
    readers instead use mode=ro and the same artifact/identity/content checks.
    No registration or acquired body is cached here.
    """
    from . import npm_evidence_sources as acquired
    repository = acquired._repository(repository).lower()
    acquired._path(path)
    if not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('registered_source_revision_invalid')
    if provider == 'npm_evidence_sources':
        ledger, table, artifact_key, content_key = '.npm_evidence_sources.sqlite3', 'exact_source_files', 'artifact_sha256', 'content_sha256'
        directory, indexed_repository = '.npm_evidence_sources', repository.removeprefix('https://')
    elif provider == 'npm_case_collector':
        ledger, table, artifact_key, content_key = '.npm_case_collector.sqlite3', 'exact_files', 'artifact_sha', 'content_sha'
        directory, indexed_repository = '.npm_case_collector/artifacts', repository
    else:
        raise ValueError('registered_source_provider_invalid')
    database = _backend_path(root, root / ledger)
    if not database.is_file():
        return None
    db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute('SELECT * FROM ' + table + ' WHERE repository=? AND revision=? AND path=?',
                         (indexed_repository, revision, path)).fetchone()
    finally:
        db.close()
    if row is None:
        return None
    columns = {'repository', 'revision', 'path', artifact_key, content_key}
    if provider == 'npm_evidence_sources':
        columns.update(('bytes', 'source_id'))
    if not columns <= set(row.keys()):
        raise ValueError('registered_source_schema_invalid')
    sha = row[artifact_key]
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
        raise ValueError('registered_source_artifact_identity_invalid')
    artifact_path = _backend_path(root, root / directory / (sha + '.json'))
    artifact = _read_pinned_json(artifact_path, sha, acquired.MAX_ARTIFACT_BYTES if provider == 'npm_evidence_sources' else 64 << 20)
    if not isinstance(artifact, dict):
        raise ValueError('registered_source_artifact_invalid')
    if provider == 'npm_evidence_sources' and (artifact.get('schema') != 'npm-source-artifact/1'
            or artifact.get('policy') != acquired.POLICY or artifact.get('source_id') != row['source_id']
            or artifact.get('admission_status') != 'not_evaluated'):
        raise ValueError('registered_source_artifact_invalid')
    if provider == 'npm_case_collector':
        from .npm_case_collector import POLICY as collector_policy
        if artifact.get('policy') != collector_policy:
            raise ValueError('registered_source_artifact_invalid')
    payload = artifact.get('payload')
    if not isinstance(payload, dict) or acquired._repository(payload.get('repository_url')).lower() != repository:
        raise ValueError('registered_source_repository_mismatch')
    snapshots = payload.get('snapshots')
    if not isinstance(snapshots, dict):
        raise ValueError('registered_source_snapshot_invalid')
    for side in ('before', 'after'):
        snapshot = snapshots.get(side)
        if payload.get(side + '_revision') != revision or not isinstance(snapshot, dict) or snapshot.get('revision') != revision:
            continue
        files = snapshot.get('files')
        text = files.get(path) if isinstance(files, dict) else None
        if not isinstance(text, str):
            raise ValueError('registered_source_content_invalid')
        raw = text.encode()
        content_sha = hashlib.sha256(raw).hexdigest()
        if len(raw) > acquired.MAX_ARTIFACT_BYTES or content_sha != row[content_key]:
            raise ValueError('registered_source_content_mismatch')
        if provider == 'npm_evidence_sources':
            metadata_sides = artifact.get('files')
            metadata_files = metadata_sides.get(side) if isinstance(metadata_sides, dict) else None
            metadata = metadata_files.get(path) if isinstance(metadata_files, dict) else None
            if not isinstance(metadata, dict) or type(row['bytes']) is not int or len(raw) != row['bytes'] or len(raw) != metadata.get('bytes') or content_sha != metadata.get('sha256'):
                raise ValueError('registered_source_content_mismatch')
        reference = {'provider': provider, 'repository': repository, 'revision': revision, 'path': path,
                     'artifact_sha256': sha, 'content_sha256': content_sha, 'git_blob_hash': _object_sha('blob', raw)}
        return text, reference, {'path': str(artifact_path), 'sha256': sha}
    raise ValueError('registered_source_revision_mismatch')


def registered_sources_current(provenance, *, root=None):
    """Fail closed on revoked, changed or malformed acquired-source references."""
    if root is None:
        from .versions import PROC
        root = PROC
    try:
        root = Path(root).resolve()
        entries = provenance.get('backend_source_registrations', [])
        if not isinstance(entries, list):
            return False
        for item in entries:
            if not isinstance(item, dict):
                return False
            current = _registered_exact(item['provider'], item['repository'], item['revision'], item['path'], root)
            if current is None or current[1] != item:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
        return False


@lru_cache(maxsize=32)
def _git_tree(repository, revision):
    from .security_snapshots import GitSnapshot
    snapshot = GitSnapshot(repository, revision)
    commit = snapshot._git('--no-replace-objects', 'cat-file', 'commit', revision)
    if _object_sha('commit', commit) != revision:
        raise ValueError('git_commit_hash_mismatch')
    headers = commit.split(b'\n\n', 1)[0].decode().splitlines()
    tree_hash = next(line[5:] for line in headers if line.startswith('tree '))
    parents = [line[7:] for line in headers if line.startswith('parent ')]
    entries = snapshot._git('--no-replace-objects', 'ls-tree', '-r', '-z', revision).split(b'\0')
    if len(entries) > 100000:
        raise ValueError('git_tree_entry_budget_exceeded')
    regular, boundaries = {}, []
    for entry in entries:
        if not entry:
            continue
        meta, name = entry.split(b'\t', 1)
        mode, kind, sha = meta.split()
        logical = name.decode()
        if mode in (b'100644', b'100755') and kind == b'blob':
            regular[logical] = sha.decode()
        else:
            boundaries.append(logical)
    return snapshot, tree_hash, parents, regular, boundaries


def _legacy(record, root):
    from . import security_snapshots, npm_package_scope, funcsplit
    from .ingest import licenses
    if record.get('ecosystem') != 'npm' or str(record.get('collector_version') or '').startswith('npm-targeted-case-collector/'):
        return None
    revisions = [record.get('relevant_parent_commit'), record.get('fix_commit')]
    if any(not isinstance(value, str) or not re.fullmatch('[0-9a-f]{40}', value) for value in revisions):
        return None
    repository_url = 'https://' + str(record.get('repository_identity') or '').removeprefix('https://').rstrip('/')
    repository = security_snapshots.repository_path({'repo_url': repository_url})
    if repository is None or not repository.is_dir():
        return None
    advisories, archive = _raw_advisories(record)
    checked = copy.deepcopy(record)
    snapshots, sources, scopes, unit_attribution, unit_hashes = {}, [], [], {}, {}
    side_failures = {'before': [], 'after': []}
    backend_files = [archive] if archive else []
    registrations = []
    for side, revision, code_key in [('before', revisions[0], 'vulnerable_code'), ('after', revisions[1], 'fixed_code')]:
        snapshot, tree_hash, parents, tree, boundaries = _git_tree(str(repository), revision)
        if side == 'after' and revisions[0] not in parents:
            raise ValueError('git_parent_mismatch')
        if record.get(side + '_tree_hash') and record[side + '_tree_hash'] != tree_hash:
            side_failures[side].append(reason('git_tree_mismatch', side + '_tree_hash', 'The recorded tree identity differs from the exact commit tree.', 'Restore the exact commit tree identity.'))
        try:
            path = security_snapshots.logical_path(record.get('file_path_' + side))
        except ValueError:
            path = None
        if path is None or path not in tree:
            side_failures[side].append(reason('unit_not_regular_git_blob', 'file_path_' + side, 'The recorded unit path is not a regular file in this exact source tree.', 'Restore the exact source unit path.'))
            unit_attribution[side] = 'unknown'
            snapshots[side] = {'revision': revision, 'files': {}}
            scopes.append(None)
            continue
        context = unpack(record.get('context_' + side), {})
        context = context if isinstance(context, dict) else {}
        permission_paths = set(npm_package_scope.required_paths(tree, [path], boundaries, boundaries))
        required = permission_paths | {path}
        items = context.get('items', [])
        required.update(item['path'] for item in (items if isinstance(items, list) else [])
                        if isinstance(item, dict) and isinstance(item.get('text'), str)
                        and isinstance(item.get('path'), str) and item['path'] in tree)
        if len(required) > 64:
            raise ValueError('context_file_budget_exceeded')
        files = {}
        for logical in required:
            security_snapshots.logical_path(logical)
            if logical not in tree:
                raise ValueError('context_path_not_regular_git_blob')
            text = snapshot.read(logical)
            if text is None:
                for provider in ('npm_evidence_sources', 'npm_case_collector'):
                    recovered = _registered_exact(provider, repository_url, revision, logical, root)
                    if recovered is not None:
                        text, reference, descriptor = recovered
                        # The common blob check below separates permission
                        # failures from side-local source/context failures.
                        if reference['git_blob_hash'] == tree[logical]:
                            if reference not in registrations:
                                registrations.append(reference)
                            if descriptor not in backend_files:
                                backend_files.append(descriptor)
                        break
            if text is None or _object_sha('blob', text.encode()) != tree[logical]:
                if logical in permission_paths:
                    raise ValueError('permission_source_blob_unavailable')
                side_failures[side].append(reason('git_blob_hash_mismatch', 'source_' + side, 'A source or context blob is unavailable or does not match its exact Git identity.', 'Recover the exact source/context blob.'))
                continue
            declarations = re.findall(r'SPDX-License-Identifier:\s*([^\r\n*]+)', text)
            if any(not licenses.redistributable(claim.strip()) for claim in declarations):
                raise ValueError('source_permission_conflict')
            files[logical] = text
        scope = npm_package_scope.resolve({'packages':[str(record.get('package_identity') or '').removeprefix('npm:')],
                    'repository_url':repository_url}, files, tree, [path], boundaries, boundaries)
        scopes.append(scope)
        claimed_blob = unpack(record.get('file_blob_hashes'), {}).get(side)
        if claimed_blob and claimed_blob != tree[path]:
            side_failures[side].append(reason('record_blob_hash_mismatch', 'file_blob_hashes.' + side, 'The recorded blob identity differs from the exact source blob.', 'Restore the exact source blob identity.'))
        code = record.get(code_key)
        unit_attribution[side] = 'unknown'
        if code is not None and not isinstance(code, str):
            side_failures[side].append(reason('malformed_source_unit', code_key, 'The source unit must be a string.', 'Restore the exact source unit text.'))
        if isinstance(code, str) and code and path in files:
            raw, unit = files[path].encode(), code.encode()
            if checked.get('byte_span_' + side) is None:
                if raw.count(unit) != 1:
                    side_failures[side].append(reason('unit_not_unique_in_blob', 'byte_span_' + side, 'Exact unit bounds cannot be inferred unambiguously.', 'Restore the exact recorded byte span.'))
                else:
                    start = raw.index(unit)
                    checked['byte_span_' + side] = [start, start + len(unit)]
            span = unpack(checked.get('byte_span_' + side), [])
            valid_span = (isinstance(span, list) and len(span) == 2 and all(type(v) is int for v in span)
                          and 0 <= span[0] < span[1] <= len(raw) and raw[span[0]:span[1]] == unit)
            if not valid_span:
                side_failures[side].append(reason('unit_span_mismatch', 'byte_span_' + side, 'The source unit does not match its recorded byte bounds.', 'Restore the exact source unit and its byte span.'))
            if valid_span and checked.get('line_span_' + side) is None:
                checked['line_span_' + side] = [raw[:span[0]].count(b'\n')+1, raw[:span[1]-1].count(b'\n')+1]
            # Reuse the miner's own supported wrapper/assignment/method shapes.
            # Unknown extraction is an attribution gap, not corrupt source bytes.
            unit_attribution[side] = 'unknown'
            if valid_span and record.get('granularity') in ('file', 'module'):
                if raw[:span[0]].strip() == b'' and raw[span[1]:].strip() == b'':
                    unit_attribution[side] = 'verified'
            elif valid_span:
                try:
                    units = funcsplit.parse_units(record.get('language'), path, files[path])
                except funcsplit.Unavailable:
                    units = None
                if units is not None and not units.errors:
                    for key, info in units.info.items():
                        start, end = info.bytes
                        if (record.get('symbol') in (key, info.qual) and span[0] <= start < end <= span[1]
                                and raw[span[0]:start].strip() in (b'', b'export', b'export default')
                                and raw[end:span[1]].strip() in (b'', b';')
                                and raw[start:end].decode() == units[key][0]):
                            unit_attribution[side] = 'verified'
                            unit_hashes[side] = info.shash
                            break
        else:
            unit_attribution[side] = 'unknown'
        snapshots[side] = {'revision':revision, 'files':files}
        sources.append({'repository':str(repository.resolve()), 'revision':revision, 'tree_hash':tree_hash,
                        'path':path, 'blob_hash':tree[path], 'source_file_sha256':hashlib.sha256(files[path].encode()).hexdigest() if path in files else None})
    if all(scopes) and any(scopes[0][key] != scopes[1][key] for key in ('package', 'manifest_path', 'prefix')):
        raise ValueError('package_owner_changed')
    fact = {'schema':'sft-git-source-binding/1', 'status':'verified', 'record_id':record.get('record_id'),
            'basis':'backend_exact_git_parent_tree_blob_package_permission_unit', 'git_sources':sources,
            'backend_evidence_files':backend_files, 'backend_source_registrations':registrations,
            'permission_status':'permissive' if scopes[0] and scopes[0]['permission']['usage'] == 'permissive' else 'unknown',
            'source_sha256':digest(sources)}
    fact['binding_sha256'] = digest(fact)
    return {'foundation':fact, 'snapshots':snapshots, 'advisories':advisories, 'canonical':checked,
            'checked_record':checked, 'unit_attribution':unit_attribution, 'unit_hashes':unit_hashes, 'side_failures':side_failures}


def unpack(value, default=None):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def parse_line_span(value):
    """Accept legacy inclusive ranges and JSON/list pairs, never coercing values."""
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*-[1-9][0-9]*', value):
        try:
            value = [int(part) for part in value.split('-')]
        except ValueError:
            return None
    else:
        value = unpack(value)
    if (isinstance(value, list) and len(value) == 2
            and all(type(number) is int and number > 0 for number in value) and value[0] <= value[1]):
        return value
    return None


def permission_resolution(record, foundation):
    """Resolve raw metadata only against an already authenticated foundation.

    This function consumes backend provenance, never imported record assertions.
    Historical absence is distinct from an unrecognized or restricted license.
    """
    from .ingest import licenses
    raw_status, raw_license = record.get('data_usage_status'), record.get('repository_license')
    legacy = (foundation.get('schema') == 'sft-git-source-binding/1'
              and foundation.get('status') == 'verified' and foundation.get('permission_status') == 'permissive')
    collector = foundation.get('schema') == 'npm-exact-source-binding/2' and foundation.get('status') == 'verified'
    placeholder = raw_license is None or isinstance(raw_license, str) and raw_license.strip().upper() in ('', 'NOASSERTION', 'UNKNOWN')
    resolved_absence = raw_status == 'unknown' and placeholder and legacy
    raw_allowed = (raw_status in (None, 'usable', 'permissive')
                   and (raw_license is None or raw_license == ''
                        or isinstance(raw_license, str) and licenses.redistributable(raw_license)))
    allowed = (legacy or collector) and (raw_allowed or resolved_absence)
    basis = ('exact_legacy_permission_resolves_historical_absence' if resolved_absence else
             'authenticated_exact_source_permission' if allowed else
             'exact_source_permission_unavailable' if not (legacy or collector) else 'raw_metadata_not_resolved')
    return {'raw_status': raw_status, 'raw_repository_license': raw_license,
            'effective_status': 'permissive' if allowed else 'not_established', 'basis': basis}


def _context_used_paths(record, side, context):
    items = context.get('items', [])
    return {path for path in [record.get('file_path_' + side),
            *(item.get('path') for item in (items if isinstance(items, list) else []) if isinstance(item, dict))]
            if isinstance(path, str)}


def context_discovery_index(record):
    """Summarize broad producer discovery metadata without endorsing its claims."""
    summaries = {}
    for side in ('before', 'after'):
        context = unpack(record.get('context_' + side), {})
        snapshot = context.get('snapshot', {}) if isinstance(context, dict) else None
        claims = snapshot.get('files', {}) if isinstance(snapshot, dict) else None
        if not isinstance(claims, dict):
            continue
        used = len(claims.keys() & _context_used_paths(record, side, context))
        summaries[side] = {'role': 'non_authoritative_discovery_metadata', 'count': len(claims),
                           'used_count': used, 'uncited_count': len(claims) - used, 'sha256': digest(claims)}
    return summaries


def _imports_projection(module):
    if not isinstance(module, str) or len(module.encode()) > 1 << 20:
        return None
    projected = '\n'.join(dict.fromkeys(match.group(0).strip() for match in _CONTEXT_IMPORT_LINE.finditer(module)))
    return projected if len(projected.encode()) <= 64 << 10 else None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def reason(code, field, detail, remediation):
    return dict(code=code, field=field, detail=detail, remediation=remediation)


def _read_pinned_json(path, expected, limit):
    """The JSON of a size-capped file whose SHA-256 must equal ``expected``; raises instead of returning None (unlike util.read_json)."""
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('evidence_file_missing_or_oversized')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('evidence_file_hash_mismatch')
    return json.loads(raw)


def _canonical(record, root):
    """Find a registered original, for missing-target recovery and symbol binding."""
    ledger = root / '.npm_case_collector.sqlite3'
    if not ledger.is_file():
        return None
    db = sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True)
    try:
        rows = db.execute('SELECT DISTINCT s.path,s.sha,s.bytes FROM source_units u '
                          'JOIN shards s ON s.sha=u.shard_sha WHERE u.record_id=?', (record.get('record_id'),)).fetchall()
    finally:
        db.close()
    for logical, sha, size in rows:
        path = root / logical
        if (logical != '.npm_case_collector/shards/' + sha + '.jsonl'
                or not path.resolve().is_relative_to(root) or not path.is_file()
                or path.stat().st_size != size or size > 8 << 20):
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            continue
        for line in raw.splitlines():
            candidate = json.loads(line)
            if candidate.get('record_id') == record.get('record_id'):
                yield candidate


def _collector(record, root):
    from . import npm_case_collector as collector
    failures = []
    for original in _canonical(record, root) or ():
        candidate = copy.deepcopy(record)
        if not candidate.get('fixed_code'):
            candidate['fixed_code'] = original.get('fixed_code')
            for field in ('context_after', 'byte_span_after', 'line_span_after'):
                if not candidate.get(field):
                    candidate[field] = original.get(field)
        fields = ('symbol', 'relevant_parent_commit', 'fix_commit', 'raw_pair_key', 'language', 'ecosystem',
                  'package_identity', 'repository_identity', 'data_usage_status', 'repository_license', 'collector_version')
        mismatches = [field for field in fields if unpack(candidate.get(field), candidate.get(field)) != unpack(original.get(field), original.get(field))]
        if mismatches:
            failures.extend(reason('registered_source_mismatch', field, 'The supplied field conflicts with the backend-registered source unit.', 'Recover this field from the exact registered source evidence.') for field in mismatches)
            continue
        fact = collector.source_binding_for(original, root=root)
        if not fact:
            failures.append(reason('source_binding_mismatch', 'context', 'The source identity or snapshot metadata fails the backend source binding.', 'Recover exact source identity and snapshot metadata.'))
            continue
        sha = fact['artifact_sha256']
        artifact = _read_pinned_json(root / '.npm_case_collector/artifacts' / (sha + '.json'), sha, 64 << 20)
        advisories = []
        for item in artifact['discoveries']:
            identity = item['advisory_sha256']
            raw = _read_pinned_json(root / '.npm_case_collector/discoveries' / (identity + '.json'), identity, 2 << 20)
            advisories.append({'sha256': identity, 'advisory': raw})
        side_failures = {'before': [], 'after': []}
        for side, code_key in (('before', 'vulnerable_code'), ('after', 'fixed_code')):
            for field in (code_key, 'file_path_' + side, side + '_tree_hash', 'byte_span_' + side, 'line_span_' + side):
                if unpack(candidate.get(field), candidate.get(field)) != unpack(original.get(field), original.get(field)):
                    side_failures[side].append(reason('registered_source_mismatch', field, 'The supplied field conflicts with the backend-registered source unit.', 'Recover this field from the exact registered source evidence.'))
            if unpack(candidate.get('file_blob_hashes'), {}).get(side) != unpack(original.get('file_blob_hashes'), {}).get(side):
                side_failures[side].append(reason('registered_blob_mismatch', 'file_blob_hashes.' + side, 'The supplied blob identity conflicts with the registered source.', 'Recover the exact blob identity.'))
        return {'foundation': fact, 'snapshots': artifact['payload']['snapshots'], 'side_failures': side_failures,
                'advisories': advisories, 'canonical': original, 'checked_record': candidate}
    if failures:
        return {'failure': failures, 'source_integrity': 'invalid'}
    return None


def authenticate(record, *, root=None, provider=None):
    """Return source objects only after backend authentication. No network or writes."""
    if root is None:
        from .versions import PROC
        root = PROC
    root = Path(root).resolve()
    try:
        bound = _collector(record, root)
        if bound:
            return bound
        return _legacy(record, root)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as error:
        code = str(error).split(':', 1)[0]
        if not re.fullmatch('[a-z_]+', code):
            code = 'backend_evidence_validation_failed'
        field = ('relevant_parent_commit' if 'parent' in code else 'file_blob_hashes' if 'blob' in code
                 else 'byte_span' if 'span' in code else 'symbol' if 'symbol' in code
                 else 'data_usage_status' if 'permission' in code or 'license' in code else 'source')
        return {'failure':[reason(code, field, 'Backend evidence validation failed: ' + code.replace('_', ' ') + '.',
                                  'Recover the exact referenced source evidence and correct the named field.')],
                'source_integrity':'unknown' if 'unavailable' in code or 'missing' in code else 'invalid'}
    return None


def source_problems(record, bound, *, side_only=None):
    from . import evidence_ast
    problems = []
    checked = bound['checked_record']
    for side, code_key, revision_key in (('before', 'vulnerable_code', 'relevant_parent_commit'), ('after', 'fixed_code', 'fix_commit')):
        if side_only and side != side_only:
            continue
        problems.extend(bound.get('side_failures', {}).get(side, []))
        code = record.get(code_key)
        if side == 'after' and not code:
            continue
        path = record.get('file_path_' + side)
        files = bound['snapshots'][side]['files']
        if not isinstance(path, str):
            problems.append(reason('malformed_source_path', 'file_path_' + side, 'A source path must be a string.', 'Restore the captured source path.'))
            continue
        full = files.get(path)
        raw_context = record.get('context_' + side)
        context = {} if raw_context is None else unpack(raw_context)
        if not isinstance(context, dict):
            problems.append(reason('malformed_context', 'context_' + side, 'Context must be an object.', 'Restore the captured context object.'))
            continue
        snapshot = context.get('snapshot', {})
        if not isinstance(snapshot, dict):
            problems.append(reason('malformed_context', 'context_' + side + '.snapshot', 'Snapshot metadata must be an object.', 'Restore the captured snapshot object.'))
            continue
        span = unpack(checked.get('byte_span_' + side), [])
        raw = full.encode() if isinstance(full, str) else b''
        valid = (isinstance(code, str) and bool(code.strip()) and isinstance(span, list) and len(span) == 2
                 and all(type(n) is int for n in span) and 0 <= span[0] < span[1] <= len(raw)
                 and raw[span[0]:span[1]] == code.encode())
        if not valid:
            problems.append(reason('unit_bytes_mismatch', code_key, 'The unit does not match its exact source span.', 'Recover the exact unit and byte span.'))
            continue
        lines = [raw[:span[0]].count(b'\n') + 1, raw[:span[1] - 1].count(b'\n') + 1]
        if parse_line_span(checked.get('line_span_' + side)) != lines:
            problems.append(reason('line_span_mismatch', 'line_span_' + side, 'Recorded lines do not match the unit bytes.', 'Recover exact line bounds.'))
        for key, value in (('revision', record.get(revision_key)), ('side', side), ('path', path),
                           ('file_sha256', hashlib.sha256(raw).hexdigest()), ('target_sha256', hashlib.sha256(code.encode()).hexdigest())):
            if snapshot.get(key) is not None and snapshot[key] != value:
                problems.append(reason('mixed_snapshot', 'context_' + side + '.snapshot.' + key, 'Context belongs to a different source identity.', 'Rebuild context from the same exact snapshot.'))
        omissions = context.get('omissions', [])
        if not isinstance(omissions, list):
            problems.append(reason('malformed_context', 'context_' + side + '.omissions', 'Context omissions must be an array.', 'Restore the captured omissions array.'))
            omissions = []
        if 'target_source_mismatch' in omissions:
            problems.append(reason('context_target_mismatch', 'context_' + side, 'Context reports a mismatched target.', 'Rebuild the context from exact source.'))
        file_claims = snapshot.get('files', {})
        if not isinstance(file_claims, dict):
            problems.append(reason('malformed_context', 'context_' + side + '.snapshot.files', 'File fingerprints must be an object.', 'Restore the captured file fingerprint mapping.'))
            file_claims = {}
        # Legacy snapshots index all repository files visited during discovery,
        # including callers discarded by the context budget. Only used source
        # claims can authenticate or invalidate this selected context.
        for logical in file_claims.keys() & _context_used_paths(record, side, context):
            claimed = file_claims[logical]
            actual = files.get(logical)
            if actual is None or hashlib.sha256(actual.encode()).hexdigest() != claimed:
                problems.append(reason('context_file_hash_mismatch', 'context_' + side + '.snapshot.files', 'A context file fingerprint does not match the authenticated snapshot.', 'Recover the exact context file fingerprint.'))
        items = context.get('items', [])
        if not isinstance(items, list):
            problems.append(reason('malformed_context', 'context_' + side + '.items', 'Context items must be an array of objects.', 'Restore the captured context items.'))
            items = []
        for item in items:
            if not isinstance(item, dict) or item.get('text') is not None and not isinstance(item.get('text'), str) or item.get('path') is not None and not isinstance(item.get('path'), str):
                problems.append(reason('malformed_context', 'context_' + side + '.items', 'Each context item must contain string source/path fields.', 'Restore the captured context item object.'))
                continue
            text, logical = item.get('text'), item.get('path')
            if not text:
                continue
            source = files.get(logical)
            # Serialized manifest context can be normalized by context extraction.
            if item.get('kind') in ('manifest', 'runtime_environment'):
                matches = source is not None and (text in source or unpack(text) == unpack(source) and unpack(text) is not None)
            else:
                matches = isinstance(source, str) and text in source
                if (not matches and item.get('kind') == 'imports' and logical == path
                        and record.get('language') in ('javascript', 'typescript', 'js', 'ts', 'jsx', 'tsx')):
                    matches = text == _imports_projection(source)
            if not matches:
                problems.append(reason('context_item_unbound', 'context_' + side + '.items', 'A context item is not present in the authenticated snapshot.', 'Recover its exact revision/path bytes or omit it.'))
        language = record.get('language')
        if language in ('javascript', 'typescript', 'js', 'ts', 'jsx', 'tsx'):
            # Parse the complete module: method fragments need their enclosing syntax.
            if evidence_ast.parse_snapshot(language, full) is None:
                problems.append(reason('malformed_source', code_key, 'The exact module does not parse with its source grammar.', 'Recover complete, valid source without changing historical bytes.'))
        else:
            problems.append(reason('source_parser_unavailable', 'language', 'No source-integrity parser is installed for this language.', 'Add an independent source syntax validator.'))
    return problems


@lru_cache(maxsize=4)
def _executed_ids(path, modified, size):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        return frozenset(row[0] for row in db.execute("SELECT json_extract(binding,'$.record_id') FROM execution_runs WHERE state='completed' AND json_valid(binding)"))
    finally:
        db.close()


def authenticated_execution(record):
    """Read an existing HMAC-authenticated result; never rerun its proof or code."""
    from .verify import automatic, proof_ledger
    ledger = proof_ledger.ProofLedger()
    if not ledger.path.is_file() or not ledger.path.with_suffix(ledger.path.suffix + '.key').is_file():
        return None
    stat = ledger.path.stat()
    try:
        if record.get('record_id') not in _executed_ids(str(ledger.path), stat.st_mtime_ns, stat.st_size):
            return None
    except (OSError, sqlite3.Error):
        return None
    binding = {**automatic.source_binding(record), 'harness_sha256': automatic.harness_sha256()}
    for receipt in ledger.completed(binding):
        actual, facts = receipt.get('binding') or {}, receipt.get('facts') or {}
        if (receipt.get('schema') != automatic.SCHEMA or actual.get('mechanism') not in automatic.MECHANISMS
                or not re.fullmatch(r'sha256:[a-f0-9]{64}', actual.get('image_id', ''))
                or not receipt.get('observations') or (facts.get('effect') or {}).get('before_unsafe') is not True):
            continue
        quotes = unpack(facts.get('evidence_spans'), [])
        if not quotes or any(not isinstance(item, dict) or not isinstance(item.get('quote'), str)
                             or not item['quote'].strip() or item['quote'] not in (record.get('vulnerable_code') or '') for item in quotes):
            continue
        return receipt
    return None


def _exclusive_unit_change(record, bound):
    """Strict Route A: complete pinned commit diff confined to one exact unit.

    No retained-row counts, file-category exclusions, or semantic proof are used.
    Unavailable/incomplete coverage supplies no authority to this optional route.
    """
    from . import security_snapshots
    if (record.get('granularity') != 'function'
            or bound.get('unit_attribution') != {'before':'verified', 'after':'verified'}
            or source_problems(record, bound)):
        return None, 'exact_both_side_unit_binding_required'
    hashes = bound.get('unit_hashes', {})
    if not hashes.get('before') or not hashes.get('after') or hashes['before'] == hashes['after']:
        return None, 'substantive_unit_change_required'
    path = record.get('file_path_before')
    if not path or path != record.get('file_path_after'):
        return None, 'same_module_path_required'
    try:
        parent, fix = record['relevant_parent_commit'], record['fix_commit']
        repository = security_snapshots.repository_path({'repo_url':'https://' + str(record.get('repository_identity') or '').removeprefix('https://')})
        if repository is None or not repository.is_dir():
            return None, 'complete_git_diff_unavailable'
        _, before_tree, _, _, _ = _git_tree(str(repository), parent)
        after_snapshot, after_tree, parents, _, _ = _git_tree(str(repository), fix)
        if parent not in parents:
            return None, 'selected_parent_not_actual_parent'
        # No pathspec, rename folding, textconv, external diff, or submodule
        # exclusion. Every changed file/type/mode participates in this check.
        raw = after_snapshot._git('--no-replace-objects', 'diff-tree', '--no-commit-id', '--raw',
            '--no-abbrev', '--no-renames', '--no-ext-diff', '--no-textconv', '--ignore-submodules=none',
            '-r', '-z', parent, fix)
        if len(raw) > 2 << 20:
            return None, 'complete_git_diff_budget_exceeded'
        parts = raw.split(b'\0')
        if len(parts) != 3 or parts[-1] != b'':
            return None, 'complete_diff_not_one_file'
        fields = parts[0].decode().split()
        if (len(fields) != 5 or fields[0] not in (':100644', ':100755')
                or fields[1] != fields[0][1:] or fields[4] != 'M' or parts[1].decode() != path):
            return None, 'complete_diff_not_same_regular_module'
        modules, spans, units = {}, {}, {}
        witness = {'schema':'sft-exclusive-unit-change/1', 'parent':parent, 'fix':fix, 'path':path,
                   'mode':fields[1], 'changed_path_count':1, 'diff_sha256':hashlib.sha256(raw).hexdigest()}
        for side, tree, oid, code_key in (('before',before_tree,fields[2],'vulnerable_code'),
                                         ('after',after_tree,fields[3],'fixed_code')):
            module = bound['snapshots'][side]['files'][path].encode()
            unit = record[code_key].encode()
            span = unpack(bound['checked_record'].get('byte_span_'+side), [])
            if (not re.fullmatch('[0-9a-f]{40}', oid) or _object_sha('blob', module) != oid
                    or bound['snapshots'][side]['revision'] != (parent if side == 'before' else fix)
                    or not isinstance(span,list) or len(span) != 2 or any(type(n) is not int for n in span)
                    or not 0 <= span[0] < span[1] <= len(module) or module[span[0]:span[1]] != unit):
                return None, 'complete_diff_source_binding_mismatch'
            modules[side], spans[side], units[side] = module, span, unit
            witness[side] = {'tree':tree, 'blob':oid, 'module_sha256':hashlib.sha256(module).hexdigest(),
                             'unit_sha256':hashlib.sha256(unit).hexdigest(), 'syntax_hash':hashes[side], 'byte_span':span}
        prefix = modules['before'][:spans['before'][0]]
        suffix = modules['before'][spans['before'][1]:]
        if (prefix != modules['after'][:spans['after'][0]] or suffix != modules['after'][spans['after'][1]:]
                or units['before'] == units['after']):
            return None, 'outside_unit_bytes_changed'
        witness.update(prefix_sha256=hashlib.sha256(prefix).hexdigest(), suffix_sha256=hashlib.sha256(suffix).hexdigest())
        return witness, None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        return None, 'complete_git_diff_unavailable'


def _uncertain_claim(text):
    """Conservative report uncertainty, not ordinary descriptions of code faults.

    These bounded negative constructions confer no positive label authority.
    Affirmative unit attribution is checked independently below.
    """
    strong = r'\b(?:misreported|disputed|retracted|withdrawn|unconfirmed|alleged|allegedly|suspected|untrue|refuted)\b'
    fault_adverb = r'(?:incorrectly|erroneously|mistakenly)'
    report_verb = r'(?:reported|claimed|described|classified|identified|flagged)'
    faulty_report = (r'\b' + fault_adverb + r'\s+' + report_verb + r'\s+(?:as\s+)?(?:being\s+)?'
                     r'(?:vulnerable|affected|exploitable|(?:a\s+)?vulnerability)\b'
                     r'|\b(?:vulnerability|security\s+issue|vulnerability\s+(?:report|claim))\s+'
                     r'(?:(?:is|was|has\s+been)\s+)?' + fault_adverb + r'\s+' + report_verb + r'\b')
    false_report = (r'\bfalse\s+(?:positive\s+)?(?:report|claim|assertion|finding|assessment)\b'
                    r'|\b(?:report|claim|assertion|finding|assessment)\b(?:\s+\w+){0,12}\s+(?:is|was|are|were)\s+false\b')
    unconfirmed = (r'\b(?:cannot|can\s+not|could\s+not)\s+(?:confirm|verify|establish|substantiate)'
                   r'(?:\s+\w+){0,4}\s+(?:vulnerability|claim|report|security\s+issue)\b'
                   r'|\b(?:vulnerability|claim|report|security\s+issue)\s+(?:cannot|can\s+not|could\s+not)\s+be\s+'
                   r'(?:confirmed|verified|established|substantiated)\b'
                   r'|\b(?:attack|vulnerability|exploit)\s+(?:cannot|can\s+not|could\s+not)\s+(?:occur|arise|be\s+triggered)\b')
    return bool(re.search('|'.join((strong, faulty_report, false_report, unconfirmed)), text, re.I))


def attribute(record, bound, *, unit_evidence=False):
    from .ingest.fixrefs import parse_commit_url
    aliases = set(unpack(record.get('advisory_aliases'), []))
    requested_hashes = set(unpack(record.get('source_record_hashes'), []))
    package = str(record.get('package_identity') or '').removeprefix('npm:')
    repository = str(record.get('repository_identity') or record.get('repo_url') or '').removeprefix('https://').rstrip('/').removesuffix('.git').lower()
    symbol = record.get('symbol') or ''
    if bound.get('unit_attribution', {}).get('before', 'verified') != 'verified':
        return 'unknown', [], [reason('unit_attribution_pending', 'symbol', 'Exact bytes are authenticated, but the declared unit does not match a supported miner extraction boundary.', 'Recover the exact named miner unit and its bounds or independently establish unit attribution.')]
    unit_hashes = bound.get('unit_hashes', {})
    if unit_hashes.get('before') and unit_hashes.get('before') == unit_hashes.get('after'):
        return 'unknown', [], [reason('unit_change_unestablished', 'fixed_code', 'The exact named unit has no syntax change beyond comments or formatting.', 'Identify the actual changed causal unit; unchanged code cannot establish this repair target.')]
    matches = []
    contradicted = False
    package_contradicted = False
    identities = set()
    exclusive_checked, exclusive, exclusive_gap = False, None, None
    for item in bound['advisories']:
        advisory = item['advisory']
        advisory_ids = {advisory.get('id'), *advisory.get('aliases', [])}
        identities.update(advisory_ids)
        # Identity and unit authority must come from the SAME captured advisory.
        if requested_hashes and item['sha256'] not in requested_hashes or aliases and not aliases.intersection(advisory_ids):
            continue
        if advisory.get('withdrawn'):
            continue
        if not any(a.get('package', {}).get('name') == package and a.get('package', {}).get('ecosystem') == 'npm' for a in advisory.get('affected', [])):
            continue
        text = '\n'.join(str(advisory.get(k) or '') for k in ('summary', 'details'))
        # Uncertainty/retraction is not affirmative label authority. Do not try
        # to resolve disputed prose with an expanding semantic inference engine.
        if _uncertain_claim(text):
            continue
        # A bare word can be a verb or a different entity. Require an explicit
        # code/unit cue plus affirmative affected-behavior prose in the same sentence.
        escaped = re.escape(symbol)
        # Consume balanced delimiters as part of the exact full name, so the
        # following affirmative predicate begins at the same place as plain text.
        unit_name = r'(?:`' + escaped + r'`|\'' + escaped + r'\'|"' + escaped + r'"|' + escaped + r')'
        unit_cue = (r'(?:\b(?:function|method|routine|callback|constructor|class)\s+' + unit_name + r'(?![\w$])'
                    r'|(?<![\w$])' + unit_name + r'(?:\(\))?\s+(?:function|method|routine|callback|constructor|class)\b'
                    r'|`' + escaped + r'`|(?<![\w$])' + escaped + r'\(\))')
        # Qualified miner names can be denied by their leaf name in prose.
        # Leaf matching only vetoes attribution; it confers no positive authority.
        leaf = re.escape(re.split(r'[.#]', symbol)[-1])
        denial_cue = unit_cue if leaf == escaped else unit_cue.replace(escaped, leaf)
        # Only a complete, unqualified denial of this exact authenticated
        # package conflicts here. Version, condition and post-fix qualifiers
        # deliberately do not match this bounded construction.
        package_cue = r'(?:The\s+)?(?:package\s+' + re.escape(package) + r'|' + re.escape(package) + r'(?:\s+package)?)'
        package_denial = package_cue + r'\s+(?:is|was)\s+(?:not\s+(?:vulnerable|affected)|unaffected)[.!?]?'
        named = False
        for sentence in re.split(r'(?<=[.!?])\s+|\n+', text):
            if package and re.fullmatch(package_denial, sentence.strip(), re.I):
                package_contradicted = True
                continue
            full_named = bool(symbol and re.search(unit_cue, sentence, re.I))
            if not full_named and not (symbol and re.search(denial_cue, sentence, re.I)):
                continue
            denied = re.search(r'\b(?:not|never|no longer)\b(?:\s+\w+){0,4}\s+(?:vulnerable|affected|susceptible|exploitable|responsible)\b|\b(?:unaffected|unrelated|excluded|safe)\b|\bno vulnerability\b|\bprevents?\s+(?:(?:this|the|an?)\s+)?(?:attack|vulnerability|exploit)\b', sentence, re.I)
            if denied:
                contradicted = True
                continue
            if not full_named:
                continue
            subject_claim = r'^(?:The\s+)?' + unit_cue + r'\s+(?:(?:is|are|was|were|can|has|have|had)\s+){0,3}(?:vulnerab\w*|affected|permits?|allows?|exposes?|causes?|enables?|susceptible|exploitable|contains?\s+(?:a\s+)?(?:flaw|vulnerability))\b'
            # Only bounded affirmative constructions connect a scope to the unit.
            # In particular, no arbitrary prose gap may bridge an attack word to
            # a later unit name through denial, quotation, or disputed reporting.
            scoped_claim = (r'(?:^(?:(?:A|An|The|This)\s+)?(?:vulnerability|flaw|issue|bug|attack)\s+(?:exists|occurs|arises)\s+'
                            r'|^(?:(?:An|The|This)\s+)?(?:attack|vulnerability)\s+can\s+(?:occur|arise|be triggered)\s+'
                            r'|^(?:The\s+)?(?:package\s+)?' + re.escape(package) + r'(?:\s+before\s+[0-9][\w.+-]*)?\s+'
                            r'(?:is|are|was|were)\s+vulnerable\s+(?:to\s+(?:[A-Za-z][A-Za-z0-9_-]*\s+){1,8})?)'
                            r'(?:in|via|through)\s+(?:the\s+)?' + unit_cue)
            # One direct causal construction; no prose gap, report wrapper,
            # package-only subject or positive leaf-name alias can supply a unit.
            causal_claim = (r'^(?:The|This|A)\s+(?:issue|vulnerability|flaw)\s+(?:occurs|arises)\s+'
                            r'because\s+(?:the\s+)?' + unit_cue + r'\s+does\s+not\s+properly\s+(?:handle|validate)\b')
            if (re.search(subject_claim, sentence, re.I) or re.search(scoped_claim, sentence, re.I)
                    or re.search(causal_claim, sentence, re.I)):
                named = True
        path = record.get('file_path_before') or ''
        path_named = bool(path and path in unquote(text) and record.get('granularity') in ('file', 'module'))
        references = []
        for reference in advisory.get('references', []):
            parsed = parse_commit_url(reference.get('url', ''))
            if parsed and parsed.get('sha') == record.get('fix_commit') and parsed.get('repo_url', '').removeprefix('https://').rstrip('/').removesuffix('.git').lower() == repository:
                if reference.get('type') == 'FIX' or reference.get('type') == 'WEB' and (advisory.get('database_specific') or {}).get('github_reviewed') is True:
                    references.append(reference['url'])
        for affected in advisory.get('affected', []):
            if affected.get('package', {}).get('name') != package or affected.get('package', {}).get('ecosystem') != 'npm':
                continue
            for interval in affected.get('ranges', []):
                repo = str(interval.get('repo') or '').removeprefix('https://').rstrip('/').removesuffix('.git').lower()
                if (interval.get('type') == 'GIT' and repo == repository
                        and any(event.get('fixed') == record.get('fix_commit') for event in interval.get('events', []))):
                    references.append('https://' + repository + '/commit/' + record['fix_commit'])
        if references:
            if named or path_named or unit_evidence:
                matches.append({**item, 'references': references, 'basis': 'authenticated_execution_unit_exact_commit_parent' if unit_evidence else 'advisory_named_changed_unit_exact_commit_parent'})
            elif not contradicted and not package_contradicted:
                if not exclusive_checked:
                    exclusive, exclusive_gap = _exclusive_unit_change(record, bound)
                    exclusive_checked = True
                if exclusive:
                    matches.append({**item, 'references':references,
                        'basis':'advisory_exclusive_changed_unit_exact_commit_parent', 'exclusive_unit_change':exclusive})
    available_hashes = {item['sha256'] for item in bound['advisories']}
    if package_contradicted:
        return 'contradicted', [], [reason('advisory_package_contradicted', 'package_identity', 'The authenticated advisory explicitly excludes this exact package without a version or condition qualifier.', 'Resolve the contradictory advisory-to-package mapping.')]
    if contradicted:
        return 'contradicted', [], [reason('advisory_unit_contradicted', 'symbol', 'The authenticated advisory explicitly excludes this unit from the affected scope.', 'Resolve the contradictory advisory-to-unit mapping.')]
    if aliases and not aliases.intersection(identities) or requested_hashes and not requested_hashes.intersection(available_hashes):
        return 'contradicted', [], [reason('advisory_identity_mismatch', 'advisory_aliases', 'Claimed advisory identity does not match backend-bound advisory bytes.', 'Recover the correct advisory-to-source mapping.')]
    if not matches:
        gaps = [reason('unit_attribution_ambiguous', 'symbol', 'No authenticated named-unit or exclusive-change attribution is established with an exact fix reference.', 'Review advisory-to-unit attribution; package membership alone is insufficient.')]
        if exclusive_gap:
            gaps.append(reason('exclusive_unit_change_unestablished', 'source', exclusive_gap.replace('_',' ') + '.', 'Recover complete pinned commit coverage and both exact unit bounds; do not infer attribution from retained row counts.'))
        return 'unknown', [], gaps
    if record.get('vulnerable_code') == bound['checked_record'].get('fixed_code'):
        return 'contradicted', [], [reason('unchanged_unit', 'fixed_code', 'The source unit is unchanged across the referenced fix.', 'Supply an attributed changed unit.')]
    return 'attributed', matches, []
