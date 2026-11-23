"""Experimental bounded observations of current GitHub repository metadata.

The service never writes source records. Consumers persist only facts they use;
completed acquisition checkpoints retain those observations independently of TTL.
"""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from copy import deepcopy
from contextvars import copy_context
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import shutil
import sqlite3
import uuid
import threading
import time

import requests

from . import operations
from .metrics import current


@dataclass(frozen=True)
class Fact:
    valid: bool
    value: object = None
    reason: str = ''
    provenance: dict | None = None


def enabled():
    return os.environ.get('ZTSA_EXPERIMENTAL_REPOSITORY_FACTS', '').strip().lower() == 'graphql'


def _observed_at(response):
    now = time.time()
    try:
        value = float(response.headers.get('X-Acceleration-Observed-At', now))
        return value if math.isfinite(value) and value >= 0 else now
    except (TypeError, ValueError):
        return now


def _query(rows):
    variables, declarations, aliases = {}, [], []
    for i, (owner, repo) in enumerate(rows):
        variables.update({'owner' + str(i): owner, 'repo' + str(i): repo})
        declarations.extend(('$owner' + str(i) + ':String!', '$repo' + str(i) + ':String!'))
        aliases.append('r' + str(i) + ':repository(owner:$owner' + str(i) + ',name:$repo' + str(i) + ')'
            '{nameWithOwner licenseInfo{spdxId pseudoLicense}'
            ' languages(first:100,orderBy:{field:SIZE,direction:DESC})'
            '{totalCount pageInfo{hasNextPage} edges{size node{name}}}}')
    return 'query RepositoryFacts(' + ','.join(declarations) + '){' + ' '.join(aliases) + ' rateLimit{cost}}', variables


def _validate_license(value):
    if not isinstance(value, dict) or value.get('pseudoLicense') is not False:
        return Fact(False, reason='ambiguous_license')
    spdx = value.get('spdxId')
    if not isinstance(spdx, str) or not spdx.strip() or spdx != spdx.strip() or spdx.upper() in ('NOASSERTION', 'NONE', 'OTHER'):
        return Fact(False, reason='ambiguous_license')
    return Fact(True, spdx)


def _validate_languages(value):
    from app.backend.pipeline import languages
    from app.backend.pipeline.ingest.codemine import LANG_BY_EXT
    if not isinstance(value, dict) or not isinstance(value.get('pageInfo'), dict) or value['pageInfo'].get('hasNextPage') is not False:
        return Fact(False, reason='incomplete_languages')
    edges, count = value.get('edges'), value.get('totalCount')
    if not isinstance(edges, list) or type(count) is not int or count != len(edges) or not 0 <= count <= 100:
        return Fact(False, reason='incomplete_languages')
    stats = {}
    for edge in edges:
        if not isinstance(edge, dict) or not isinstance(edge.get('node'), dict):
            return Fact(False, reason='invalid_languages')
        name, size = edge['node'].get('name'), edge.get('size')
        if not isinstance(name, str) or not name.strip() or name in stats or type(size) is not int or size < 0:
            return Fact(False, reason='invalid_languages')
        stats[name] = size
    # shares() is stable for equal totals, while cross-API tie ordering is unspecified.
    counts = {}
    for name, size in stats.items():
        normalized = languages.normalise(name, set(LANG_BY_EXT.values()))
        if normalized:
            counts[normalized] = counts.get(normalized, 0) + size
    if len(set(counts.values())) != len(counts):
        return Fact(False, reason='language_order_tie')
    return Fact(True, stats)


class RepositoryFacts:
    def __init__(self, transport, *, should_stop=lambda: False, root=None):
        self.transport, self.should_stop = transport, should_stop
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(1, thread_name_prefix='repository-facts')
        self.groups, self.lookup, self.consumed = [], {}, {}
        self.closed = False
        self.profiles = OrderedDict()
        self.profile_locks = [threading.RLock() for _ in range(32)]
        from .cache import default
        self._memo_parent = Path(root if root is not None else default().root / 'repository-facts').resolve()
        self._memo_name = uuid.uuid4().hex
        self._memo_root = self._memo_parent / self._memo_name
        self._memo_path = self._memo_root / 'profiles.sqlite3'
        self._memo = None
        self._memo_owned = False

    def prepare(self, rows):
        if len(rows) > 256:
            raise ValueError('Repository facts lookahead is limited to 256 descriptors')
        self._drain()
        repositories = list(dict.fromkeys((row['owner'], row['repo']) for row in rows if row.get('host') == 'github.com'))
        with self.lock:
            self.groups, self.lookup, self.consumed = [], {}, {}
            for start in range(0, len(repositories), 32):
                self._group(repositories[start:start + 32])

    def _group(self, rows):
        group = {'rows': rows, 'future': None, 'operation': None}
        self.groups.append(group)
        for identity in rows:
            self.lookup[identity] = group
        return group

    def update(self,rows):
        """Keep live request ownership while replacing the bounded lookahead."""
        if len(rows)>256:raise ValueError('Repository facts lookahead is limited to 256 descriptors')
        identities=list(dict.fromkeys((row['owner'],row['repo']) for row in rows if row.get('host')=='github.com'))
        with self.lock:
            active=set(identities)
            self.lookup={identity:group for identity,group in self.lookup.items() if identity in active}
            self.consumed={identity:value for identity,value in self.consumed.items() if identity in active}
            for group in list(self.groups):
                op=group['operation'];future=group['future']
                if op is not None and op.failed_resources:raise OSError('Repository facts cleanup could not be confirmed')
                if not any(identity in active for identity in group['rows']) and (future is None or future.done()):
                    self.groups.remove(group)
            new=[identity for identity in identities if identity not in self.lookup]
            for start in range(0,len(new),32):self._group(new[start:start+32])

    def _work(self, group):
        op = operations.Operation('repository-facts', 60, lambda: self.closed or self.should_stop())
        with self.lock:
            group['operation'] = op
        try:
            with operations.use(op):
                op.phase('fetching_repository_facts', total=len(group['rows']), unit='repositories')
                query, variables = _query(group['rows'])
                try:
                    result = self.transport.graphql(query, variables, timeout=(15, 45))
                    document = result.json()
                    if result.status_code != 200 or not isinstance(document, dict) or not isinstance(document.get('data'), dict):
                        raise ValueError('Invalid GraphQL response')
                except (requests.RequestException, ValueError, TypeError):
                    operations.check()
                    current.count('repository_facts_graphql_failures')
                    op.finish('facts_fallback')
                    return {identity: {field: Fact(False, reason='graphql_transport_failed') for field in ('license', 'languages')} for identity in group['rows']}
                digest = hashlib.sha256(result.content).hexdigest()
                observed = _observed_at(result)
                errors = document.get('errors') or []
                if not isinstance(errors, list):
                    errors = [{}]
                output = {}
                for i, identity in enumerate(group['rows']):
                    alias, item = 'r' + str(i), document['data'].get('r' + str(i))
                    facts = {}
                    for field, graph_field, validate in (('license', 'licenseInfo', _validate_license), ('languages', 'languages', _validate_languages)):
                        def affects(error):
                            path = error.get('path') if isinstance(error, dict) else None
                            return not isinstance(path, list) or not path or (path[0] == alias and (len(path) == 1 or path[1] == graph_field))
                        if any(affects(error) for error in errors):
                            fact = Fact(False, reason='graphql_field_error')
                        elif not isinstance(item, dict):
                            fact = Fact(False, reason='repository_unavailable')
                        else:
                            fact = validate(item.get(graph_field))
                        provenance = {'schema': 'repository-facts/1', 'transport': 'github-graphql',
                            'requested_identity': 'github.com/' + '/'.join(identity), 'observed_at': observed,
                            'response_sha256': digest}
                        if fact.reason:
                            provenance['fallback_reason'] = fact.reason
                        facts[field] = Fact(fact.valid, fact.value, fact.reason, provenance)
                    output[identity] = facts
                current.count('repository_facts_graphql_batches')
                current.count('repository_facts_graphql_valid_fields', sum(f.valid for facts in output.values() for f in facts.values()))
                operations.check()
                return output
        except BaseException as error:
            op.cancel()
            op.finish('facts_failed', type(error).__name__)
            raise
        finally:
            if not op.finished:
                op.finish('facts_acquired')

    def get(self, owner, repo, field):
        if field not in ('license', 'languages'):
            raise ValueError('Unknown repository fact')
        from app.backend.pipeline import stopping
        with self.lock:
            if self.closed:
                raise stopping.Paused('Repository facts service is closed')
            identity = (owner, repo)
            group = self.lookup.get(identity)
            if group is None:
                # Standalone callers have no lookahead. Never grow unbounded state.
                if len(self.lookup) >= 256:
                    current.count('repository_facts_' + field + '_rest_fallbacks')
                    return Fact(False, reason='lookahead_capacity')
                group = self._group([identity])
            if group['future'] is not None and group['future'].done():
                error = group['future'].exception()
                from .failures import resource_pressure
                previous=group['operation']
                if previous is not None and previous.failed_resources:
                    raise stopping.Paused('Repository facts cleanup could not be confirmed')
                if error is not None and (resource_pressure(error) or
                        hasattr(error, 'reset') and error.reset <= time.time()):
                    group['future'] = group['operation'] = None
            if group['future'] is None:
                from .submission import submit_owned
                submit_owned(self.pool,copy_context().run,self._work,group,
                    register=lambda future:group.__setitem__('future',future))
            future = group['future']
        while True:
            operations.check()
            try:
                result = future.result(timeout=.1)
                break
            except TimeoutError:
                if future.done():
                    result = future.result()
                    break
        fact = deepcopy(result[identity][field])
        with self.lock:
            self.consumed.setdefault(identity, {})[field] = deepcopy(fact.provenance or {'fallback_reason': fact.reason})
        if fact.valid:
            current.count('repository_facts_' + field + '_hits')
        else:
            current.count('repository_facts_' + field + '_rest_fallbacks')
        return fact

    def _profile_memo(self):
        # Only this instance's UUID directory is owned or removed. No source DB.
        if self._memo is None:
            self._memo_parent.mkdir(parents=True, exist_ok=True)
            self._memo_root.mkdir(exist_ok=False)
            self._memo_owned = True
            self._memo = sqlite3.connect(self._memo_path, check_same_thread=False)
            self._memo.execute('CREATE TABLE profiles(identity TEXT PRIMARY KEY, body TEXT NOT NULL, sha256 TEXT NOT NULL)')
            self._memo.commit()
        return self._memo

    def _restore_profile(self, identity):
        row = self._profile_memo().execute('SELECT body,sha256 FROM profiles WHERE identity=?', ('/'.join(identity),)).fetchone()
        if row is None:
            return None
        body, checksum = row
        if hashlib.sha256(body.encode()).hexdigest() != checksum:
            current.count('repository_facts_frozen_profile_corruption')
            raise ValueError('Frozen repository profile checksum mismatch')
        try:
            import json
            value = json.loads(body)
            profile = {key: value['profile'][key] for key in value['profile_order']}
            if set(profile) != set(value['profile']) or not isinstance(value['observation'], dict):
                raise ValueError('Invalid frozen profile')
            return profile, value['observation']
        except (ValueError, KeyError, TypeError) as error:
            current.count('repository_facts_frozen_profile_corruption')
            raise ValueError('Frozen repository profile is invalid') from error

    def profile(self, owner, repo):
        from app.backend.pipeline import languages
        from app.backend.pipeline.ingest import codemine as cm
        identity = (owner, repo)
        lock_index = int(hashlib.sha256(('/'.join(identity)).encode()).hexdigest()[:8], 16) % len(self.profile_locks)
        with operations.acquire(self.profile_locks[lock_index]):
            with self.lock:
                from app.backend.pipeline import stopping
                if self.closed:
                    raise stopping.Paused('Repository facts service is closed')
                saved = self.profiles.get(identity)
                if saved is None:
                    saved = self._restore_profile(identity)
                if saved is not None:
                    profile, observation = saved
                    self.profiles[identity] = saved
                    self.profiles.move_to_end(identity)
                    while len(self.profiles) > 256:
                        self.profiles.popitem(last=False)
                    self.consumed.setdefault(identity, {})['languages'] = deepcopy(observation)
                    return dict(profile)
            fact = self.get(owner, repo, 'languages')
            if fact.valid:
                profile = languages.profile_from_github(fact.value, set(cm.LANG_BY_EXT.values()))
            else:
                result = self.transport.get(cm.GITHUB_API + '/repos/' + owner + '/' + repo + '/languages', timeout=(15, 60))
                limited = cm._rate_limit(result)
                if limited:
                    raise limited
                self.record_rest(owner, repo, 'languages', result)
                profile = languages.profile_from_github(result.json(), set(cm.LANG_BY_EXT.values())) if result.status_code == 200 else {}
            with self.lock:
                if self.closed:
                    raise stopping.Paused('Repository facts service is closed')
                import json
                observation = deepcopy(self.consumed[identity]['languages'])
                body = json.dumps({'profile': profile, 'profile_order': list(profile), 'observation': observation}, sort_keys=True)
                from .disk_budget import check
                check(len(body.encode()))
                with self._profile_memo() as memo:
                    memo.execute('INSERT INTO profiles(identity,body,sha256) VALUES(?,?,?)',
                                 ('/'.join(identity), body, hashlib.sha256(body.encode()).hexdigest()))
                self.profiles[identity] = (dict(profile), observation)
                while len(self.profiles) > 256:
                    self.profiles.popitem(last=False)
            return profile

    def record_rest(self, owner, repo, field, response):
        if field not in ('license', 'languages'):
            raise ValueError('Unknown repository fact')
        with self.lock:
            prior = self.consumed.get((owner, repo), {}).get(field, {})
            self.consumed.setdefault((owner, repo), {})[field] = {
                'schema': 'repository-facts/1', 'transport': 'github-rest',
                'requested_identity': 'github.com/' + owner + '/' + repo,
                'observed_at': _observed_at(response), 'response_sha256': hashlib.sha256(response.content).hexdigest(),
                'status': response.status_code, 'fallback_reason': prior.get('fallback_reason', 'graphql_unavailable')}

    def observations(self, owner, repo):
        with self.lock:
            return deepcopy(self.consumed.get((owner, repo), {}))

    def _drain(self):
        with self.lock:
            groups = list(self.groups)
        for group in groups:
            if group['future'] is not None:
                # A new window follows a fully drained engine. Any unused shared
                # request still owns its deadline and socket until it stops.
                try:
                    group['future'].result()
                except Exception:
                    pass  # ignored: The dependent already observed the failure or quota pause; cleanup ownership is checked below.
        if any(group['operation'] is not None and group['operation'].failed_resources for group in groups):
            raise OSError('Repository facts cleanup could not be confirmed')

    def close(self):
        with self.lock:
            self.closed = True
            operations_to_stop = [g['operation'] for g in self.groups if g['operation'] is not None]
        for op in operations_to_stop:
            op.cancel()
        self.pool.shutdown(wait=True, cancel_futures=True)
        with self.lock:
            operations_to_stop = [g['operation'] for g in self.groups if g['operation'] is not None]
        if any(op.failed_resources for op in operations_to_stop):
            raise OSError('Repository facts cleanup could not be confirmed')
        with self.lock:
            if self._memo is not None:
                self._memo.close()
                self._memo = None
            if self._memo_owned:
                target = self._memo_root.resolve()
                if target.parent != self._memo_parent or target.name != self._memo_name:
                    raise OSError('Repository facts cleanup target changed')
                shutil.rmtree(target)
                self._memo_owned = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
