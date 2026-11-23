"""Exact, single-package ownership inside a pinned npm source tree.

The result authenticates package and permission scope only. It never assigns a
security label, silently drops a changed file or follows a nonregular boundary.
"""
from __future__ import annotations

import json
import posixpath
import re
from urllib.parse import urlsplit

POLICY = 'npm-exact-package-scope/1'


class UnsupportedPackageScope(ValueError):
    pass


def _owner(tree, changes, boundaries):
    from .npm_evidence_sources import _path
    if not isinstance(changes, (list, tuple)) or not changes:
        raise ValueError('collector_no_changed_production_source')
    markers = {path for path in tree if path == 'package.json' or path.endswith('/package.json')}
    for path in boundaries:
        _path(path)
        if path == 'package.json' or path.endswith('/package.json'):
            markers.add(path)
    owners = set()
    for path in changes:
        _path(path)
        if path not in tree:
            raise ValueError('collector_changed_source_not_in_tree')
        directory = posixpath.dirname(path)
        while True:
            manifest = posixpath.join(directory, 'package.json') if directory else 'package.json'
            if manifest in markers:
                if manifest not in tree:
                    raise UnsupportedPackageScope('collector_nonregular_package_boundary:' + manifest)
                owners.add(manifest)
                break
            if not directory:
                raise UnsupportedPackageScope('collector_package_manifest_unavailable')
            directory = posixpath.dirname(directory)
    if len(owners) != 1:
        raise UnsupportedPackageScope('collector_multiple_package_owners')
    return next(iter(owners))


def _notices(tree, changes, notice_boundaries):
    from .npm_evidence_sources import _NOTICES
    from .npm_evidence_sources import _path
    markers = {path for path in tree if posixpath.basename(path) in _NOTICES}
    for path in notice_boundaries:
        _path(path)
        if posixpath.basename(path) in _NOTICES:
            markers.add(path)
    scopes = set()
    for path in changes:
        directory = posixpath.dirname(path)
        while True:
            candidates = tuple(posixpath.join(directory, name) if directory else name for name in _NOTICES)
            found = tuple(candidate for candidate in candidates if candidate in markers)
            if found:
                if any(candidate not in tree for candidate in found):
                    raise UnsupportedPackageScope('collector_nonregular_notice_boundary')
                scopes.add(found)
                break
            if not directory:
                raise ValueError('pinned_source_permission_not_established')
            directory = posixpath.dirname(directory)
    if len(scopes) != 1:
        raise UnsupportedPackageScope('collector_multiple_notice_scopes')
    return next(iter(scopes))


def required_paths(tree, changes, boundaries=(), notice_boundaries=()):
    manifest = _owner(tree, changes, boundaries)
    notices = _notices(tree, changes, notice_boundaries)
    return list(dict.fromkeys([manifest, *(['package.json'] if 'package.json' in tree else []), *notices]))


def _manifest(files, path):
    try:
        result = json.loads(files[path])
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError('collector_exact_package_manifest_unavailable:' + path) from error
    if not isinstance(result, dict):
        raise ValueError('collector_invalid_package_manifest:' + path)
    return result


def _repository(value):
    value = value.get('url') if isinstance(value, dict) else value
    if not isinstance(value, str):
        return None
    value = value.removeprefix('git+')
    if value.startswith('git@github.com:'):
        value = 'https://github.com/' + value[len('git@github.com:'):]
    if value.startswith('github:'):
        value = 'https://github.com/' + value[len('github:'):]
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('https', 'http', 'git', 'ssh') or parsed.hostname != 'github.com'
                or parsed.password or parsed.port is not None or parsed.username not in (None, 'git')):
            return None
    except ValueError:
        return None
    parts = parsed.path.strip('/').removesuffix('.git').split('/')
    if len(parts) != 2 or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) or p in ('.', '..') for p in parts):
        return None
    return 'https://github.com/' + '/'.join(parts).lower()


def resolve(task, files, tree, changes, boundaries=(), notice_boundaries=()):
    from .npm_evidence_sources import _license, _NOTICES, _path
    manifest = _owner(tree, changes, boundaries)
    prefix = posixpath.dirname(manifest)
    if prefix and manifest not in files:
        raise UnsupportedPackageScope('collector_exact_package_manifest_unavailable:' + manifest)
    package = _manifest(files, manifest)
    if not isinstance(package.get('name'), str) or not package['name'] or package['name'] not in task['packages']:
        if not prefix and not any(path.endswith('/package.json') for path in (*tree, *boundaries)):
            raise ValueError('collector_package_repository_attribution_mismatch')
        raise UnsupportedPackageScope('collector_package_owner_not_affected:' + manifest)
    notices = _notices(tree, changes, notice_boundaries)
    notice_prefix = posixpath.dirname(notices[0])
    if not prefix and not notice_prefix:
        return {'package': package['name'], 'manifest_path': manifest, 'prefix': '', 'permission': _license(files)}
    root = _manifest(files, 'package.json') if 'package.json' in tree else {}
    expected = _repository(task['repository_url'])
    own = package.get('repository')
    if expected is None or (prefix and own is not None and _repository(own) != expected):
        raise ValueError('collector_package_repository_attribution_mismatch')
    if prefix and own is None and _repository(root.get('repository')) != expected:
        raise ValueError('collector_package_repository_attribution_unavailable')
    if prefix and isinstance(own, dict) and 'directory' in own:
        directory = own['directory']
        _path(directory)
        if directory.rstrip('/') != prefix:
            raise ValueError('collector_package_repository_directory_mismatch')
    inherited = bool(prefix and not notice_prefix)
    paths = {posixpath.basename(path): path for path in notices}
    scoped = {'package.json': files[manifest]}
    for name, path in paths.items():
        if path not in files:
            raise ValueError('collector_exact_notice_unavailable:' + path)
        scoped[name] = files[path]
    permission = _license(scoped)
    if inherited:
        root_permission = _license({'package.json': files['package.json'],
            **{name: files[name] for name in _NOTICES if name in tree and name in files}})
        if root_permission['spdx'].lower() != permission['spdx'].lower():
            raise ValueError('conflicting_pinned_source_permission_claims')
    permission.update(manifest_path=manifest, notice_path=paths[permission['notice_path']],
        package_scope={'policy': POLICY, 'prefix': prefix, 'package': package['name'],
            'repository': expected, 'changed_paths': sorted(changes), 'inherited_root_notice': inherited,
            'notice_prefix': notice_prefix})
    return {'package': package['name'], 'manifest_path': manifest, 'prefix': prefix, 'permission': permission}
