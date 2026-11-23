"""Batch cache acquisition across exact before/after context snapshots.

This only primes immutable Git objects. hydrate_pairs remains the authoritative
reader and produces exactly the same admission, byte budgets and omission receipts.
"""
import pathlib
import posixpath
import re

from . import operations
from .metrics import current


def configurations(path):
    return [posixpath.normpath(posixpath.join(str(directory), name))
            for directory in pathlib.PurePosixPath(path).parents
            for name in ('tsconfig.json', 'jsconfig.json')]


def prepare(rows, commit, *, provider=None, max_files=128):
    from app.backend.pipeline import security_snapshots as ss
    cb = ss.cb
    from . import mining_policy
    admitted_resolution = mining_policy.current().get('version') == 4
    provider = provider or ss.snapshot_for
    method = getattr(commit.loader, 'prefetch', None)
    if method is None or max_files <= 0:
        return
    snapshots, specs, first, known = {}, [], [], set()
    for row in rows:
        if row.get('language') not in cb.JS_LANGS:
            continue
        for side, revision in (('before', row.get('parent_commit')), ('after', row.get('fix_commit'))):
            operations.check()
            if not re.fullmatch(r'[0-9a-f]{40,64}', revision or ''):
                continue
            snap = snapshots.setdefault(revision, None)
            if snap is None:
                snap = snapshots[revision] = provider(row, revision)
            path = ss.logical_path((row.get('old_path') if side == 'before' else None) or row['file_path'])
            key = (revision, path, bool(row.get('function_name')))
            if key in known:
                continue
            known.add(key)
            specs.append((row, side, revision, snap, path))
            first.extend((revision, p) for p in ([path, *configurations(path)] if admitted_resolution else [path]) if p in snap.paths)
            if len(specs) >= max_files:
                break
        if len(specs) >= max_files:
            break
    # Prime targets, then batch imports/callers/manifests across both revisions.
    # Policy 4 resolves configuration through the admission cache, so batching it
    # cannot bypass the budget. Legacy policies retain their original visibility.
    # Prefetch is deliberately independent of read admission. Failed decoding or
    # small budgets are handled by the unchanged reference reader afterwards.
    first = list(dict.fromkeys(first))[:max_files]
    if first:
        method(first)
    # Equal-content aliases must not reveal a configuration blob early either.
    trees = {}
    repository = getattr(commit.loader, 'repository', None)
    if repository is not None and not admitted_resolution:
        trees = {revision: repository.tree(revision) for revision in snapshots}
    configuration_oids = {oid for tree in trees.values() for path, oid in tree.items()
                          if posixpath.basename(path) in ('tsconfig.json', 'jsconfig.json')}
    planned = []
    for row, side, revision, snap, path in specs:
        operations.check()
        data = commit.loader.content(revision, path) if path in snap.paths else None
        try:
            target = data.decode('utf-8') if data is not None and len(data) <= ss.MAX_BLOB_BYTES else ''
        except UnicodeError:
            target = ''
        target = target or row.get('code_'+side) or ''
        _, resolution, _ = ss.compiler_resolution(snap, path, target)
        unit = cb.Unit(path=path, code=row.get('code_'+side) or '', file_text=target,
                       language=row.get('language'), ecosystem=row.get('ecosystem'),
                       known_paths=snap.paths, resolution=resolution)
        candidates = [path, *configurations(path), *cb.wanted_paths(unit)]
        if row.get('function_name'):
            for caller in ss.caller_paths(snap, path)[0]:
                candidates.extend((caller, *configurations(caller)))
        planned.extend((revision, p) for p in candidates if p in snap.paths
                       and (admitted_resolution or posixpath.basename(p) not in ('tsconfig.json', 'jsconfig.json')
                            and trees.get(revision, {}).get(p) not in configuration_oids))
    planned = list(dict.fromkeys(planned))[:max_files]
    if planned:
        method(planned)
        current.count('context_prefetch_planned_paths', len(planned))
