"""Reuse immutable context bytes only after source, policy and tree validation.

The frozen records are a performance cache, never admission proof. New source
availability invalidates cached omissions; unchanged absence retains exact bytes.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import importlib.metadata
import sys

from . import context_builder as cb, ecosystems, funcsplit, security_snapshots as ss, versions


def signature():
    packages = {}
    for package in ('tree-sitter', 'tree-sitter-languages', 'tree-sitter-javascript', 'tree-sitter-typescript'):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    return {'schema': 'build-context-cache/1', 'policy': cb.POLICY,
            'budget': dataclasses.asdict(cb.Budget()), 'splitter': funcsplit.VERSION,
            'max_files': ss.MAX_FILES, 'max_blob_bytes': ss.MAX_BLOB_BYTES,
            'max_callees': cb.MAX_CALLEES, 'max_callers': cb.MAX_CALLERS,
            'parser_stack': {'python': sys.implementation.cache_tag, 'packages': packages},
            'implementation': {module.__name__: hashlib.sha256(pathlib.Path(module.__file__).read_bytes()).hexdigest()
                               for module in (cb, ss, funcsplit)}}


LEGACY_V7_MANIFEST_SHA256 = '00ba8b1ab3276990ee9afbdb6bb798f2e3b9d3313711745cf4ac279ed440ac37'


_LEGACY_SIGNATURE = {'schema': 'build-context-cache/1', 'policy': 'deterministic_local_snapshot_v1',
    'budget': {'total': 12000, 'imports': 1500, 'callees': 4000, 'callers': 3000, 'modules': 3000,
               'config': 2000, 'enclosing': 6000}, 'splitter': 6, 'max_files': 64,
    'max_blob_bytes': 1 << 20, 'max_callees': 12, 'max_callers': 8}


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest() if text is not None else None


def reuse_pair(row, record, *, provider=None, hashes=None, before_text=None, after_text=None):
    """Validate an exact unit and every retained context blob against current Git."""
    identity = {'pair_key': 'raw_pair_key', 'code_before': 'vulnerable_code', 'code_after': 'fixed_code',
                'parent_commit': 'relevant_parent_commit', 'fix_commit': 'fix_commit',
                'language': 'language', 'ecosystem': 'ecosystem', 'granularity': 'granularity',
                'function_name': 'symbol', 'extraction_version': 'extraction_version'}
    if any(row.get(raw) != record.get(saved) for raw, saved in identity.items()):
        return None
    if row.get('repo_url') and ecosystems.repository_identity(row['repo_url']) != record.get('repository_identity'):
        return None
    provider, hashes = provider or ss.snapshot_for, hashes if hashes is not None else {}
    result = {}
    for side, revision in (('before', row.get('parent_commit')), ('after', row.get('fix_commit'))):
        path = (row.get('old_path') if side == 'before' else None) or row.get('file_path') or 'unknown'
        if path != record.get('file_path_' + side):
            return None
        try:
            document = record.get('context_' + side)
            document = json.loads(document) if isinstance(document, str) else document
            bundle = cb.Bundle.from_dict(document)
        except (ValueError, KeyError, TypeError):
            return None
        snap = bundle.snapshot or {}
        if (bundle.policy != cb.POLICY or bundle.variant != 'standard' or not bundle.main_evaluation_allowed
                or type(snap.get('available')) is not bool or snap.get('revision') != revision
                or snap.get('side') != side or snap.get('path') != path
                or snap.get('target_sha256') != _hash(row.get('code_' + side) or '')):
            return None
        current = provider(row, revision)
        if snap['available'] is False:
            fallback = before_text if side == 'before' else after_text
            if fallback is None and row.get('granularity') == 'file':
                fallback = row.get('code_' + side) or ''
            if current.available or snap.get('files') or _hash(fallback) != snap.get('file_sha256'):
                return None
            result[side] = bundle
            continue
        if not current.available:
            return None
        if any(any(word in reason for word in ('unavailable', 'unfetched', 'unresolved', 'unparsed',
                'unknown', 'mismatch', 'parse_failed')) for reason in bundle.omissions):
            return None
        def digest(candidate):
            key = (ecosystems.repository_identity(row.get('repo_url')) or id(current), revision, candidate)
            if key not in hashes:
                hashes[key] = _hash(current.read(candidate))
            return hashes[key]
        if digest(path) != snap.get('file_sha256'):
            return None
        captured_files = snap.get('files') or {}
        if any(digest(candidate) != expected for candidate, expected in captured_files.items()):
            return None
        # A new hydration may make a previously missing manifest/config visible.
        # All current required paths must already belong to the exact capture.
        text = current.read(path)
        _, resolution, omissions = ss.compiler_resolution(current, path, text) if row.get('language') in cb.JS_LANGS else ({}, {}, [])
        if omissions:
            return None
        unit = cb.Unit(path=path, code=row.get('code_' + side) or '', file_text=text,
                       language=row.get('language'), ecosystem=row.get('ecosystem'),
                       known_paths=current.paths, resolution=resolution)
        required = set(cb.wanted_paths(unit))
        required.update(candidate for candidate in ('package.json', 'tsconfig.json', 'jsconfig.json') if candidate in current.paths)
        if row.get('language') in cb.JS_LANGS and row.get('function_name'):
            required.update(ss.caller_paths(current, path)[0])
        if not required <= set(captured_files):
            return None
        result[side] = bundle
    return result


def load(root, units, *, before_files=None, after_files=None, provenance=None, excluded_pairs=()):
    """Read only hash-registered frozen records; no raw source snapshots are parsed."""
    import pyarrow.parquet as pq
    root = pathlib.Path(root).resolve()
    wanted = {row['pair_key']: row for row in units if row['pair_key'] not in excluded_pairs}
    if not wanted:
        return {}
    before_files, after_files = before_files or {}, after_files or {}
    result, hashes = {}, {}
    frozen = versions.load_registry(root)['frozen']
    names = sorted((name for name in frozen if versions.parse_name(name) and versions.parse_name(name)[0] == 'dataset'),
                   key=lambda name: versions.parse_name(name)[1], reverse=True)
    columns = ['raw_pair_key', 'vulnerable_code', 'fixed_code', 'relevant_parent_commit', 'fix_commit',
               'file_path_before', 'file_path_after', 'language', 'ecosystem', 'granularity', 'symbol',
               'extraction_version', 'repository_identity', 'context_before', 'context_after']
    for name in names:
        entry = frozen[name]
        directory = root / name
        manifest_path, records_path = directory / 'manifest.json', directory / 'records.parquet'
        files = entry.get('files') or {}
        if entry.get('base') != name or any(not (directory / filename).resolve().is_relative_to(root) or
                not (directory / filename).is_file() or versions._sha256(directory / filename) != files.get(filename)
                for filename in ('manifest.json', 'records.parquet')):
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            prior_signature = manifest.get('context_cache_signature')
            if (prior_signature is None and name == 'dataset_v7' and files.get('manifest.json') == LEGACY_V7_MANIFEST_SHA256
                    and provenance == {'tag': 'v7', 'source_sha256': manifest.get('source', {}).get('snapshot_sha256'),
                                      'manifest_sha256': files['manifest.json']}
                    and manifest.get('versions', {}).get('build') == 'build-reviewed-3'
                    and manifest.get('versions', {}).get('context_policy') == _LEGACY_SIGNATURE['policy']
                    and all(signature().get(key) == value for key, value in _LEGACY_SIGNATURE.items())):
                prior_signature = signature()
            if prior_signature != signature():
                continue
            parquet = pq.ParquetFile(records_path)
            if not set(columns) <= set(parquet.schema.names):
                continue
            for batch in parquet.iter_batches(batch_size=128, columns=columns):
                for record in batch.to_pylist():
                    key = record['raw_pair_key']
                    if key not in wanted or key in result:
                        continue
                    unit = wanted[key]
                    fkey = (unit.get('commit_key') or unit['pair_key'], unit.get('file_path'))
                    pair = reuse_pair(unit, record, hashes=hashes, before_text=before_files.get(fkey), after_text=after_files.get(fkey))
                    if pair is not None:
                        result[key] = pair
        except (OSError, ValueError, TypeError, KeyError):
            continue
        if len(result) == len(wanted):
            break
    return result
