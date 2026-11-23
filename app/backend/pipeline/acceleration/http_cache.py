"""Scoped HTTP reuse and shared backoff. Cache keys never contain plaintext credentials."""
import base64
import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit

from .cache import ArtifactCache
from .metrics import current
from . import operations

_locks = [threading.RLock() for _ in range(64)]
_backoff, _guard = {}, threading.Lock()
_hosts, _condition = {}, threading.Condition()
_host_active = {}


@contextmanager
def host_slot(host):
    with _condition:
        state = _hosts.setdefault(host, {'active': 0, 'limit': 2, 'successes': 0, 'remaining': None, 'reset': 0})
        from . import mining_policy
        hostname=host[0] if isinstance(host,tuple) else host
        cap = 1 if hostname == 'api.github.com' and mining_policy.current().get('version') in (3, 4) else 8
        while state['active'] >= state['limit'] or _host_active.get(hostname,0)>=cap:
            operations.check()
            _condition.wait(.25)
        if state['remaining'] == 0 and state['reset'] > time.time():
            from app.backend.pipeline.ingest.codemine import RateLimited
            raise RateLimited(state['reset'], 'Shared API quota exhausted')
        state['active'] += 1
        _host_active[hostname]=_host_active.get(hostname,0)+1
    try:
        yield state
    finally:
        with _condition:
            state['active'] -= 1
            _host_active[hostname]-=1
            _condition.notify_all()


def observe(state, response, seconds):
    with _condition:
        if response.status_code in (403, 429) or response.status_code >= 500:
            state['limit'] = max(1, state['limit']//2)
            state['successes'] = 0
        elif response.status_code in (200, 304):
            state['successes'] += 1
            if state['successes'] >= 8 and seconds < 15:
                state['limit'] = min(8, state['limit']+1)
                state['successes'] = 0
        remaining = response.headers.get('X-RateLimit-Remaining', '')
        reset = response.headers.get('X-RateLimit-Reset', '')
        if remaining.isdigit() and reset.isdigit():
            new_reset = int(reset)
            if state['reset'] != new_reset or state['remaining'] is None:
                state['remaining'] = int(remaining)
            else:
                state['remaining'] = min(state['remaining'], int(remaining))
            state['reset'] = new_reset
            current.maximum('api_quota_reset', new_reset)
        _condition.notify_all()


class CachedSession:
    def __init__(self, session, root=None, ttl=300):
        from .http_deadline import install
        install(session)
        self.session, self.ttl = session, ttl
        if root is None:
            from .cache import default
            root = default().root / 'http'
        self.cache = ArtifactCache(root)

    @property
    def headers(self):
        return self.session.headers

    def get(self, url, **kwargs):
        import requests
        scope = hashlib.sha256(json.dumps([dict(self.session.headers), kwargs.get('headers'),
            self.session.cookies.get_dict() if hasattr(self.session, 'cookies') else {}, str(kwargs.get('auth', ''))], sort_keys=True).encode()).hexdigest()
        identity = [url, kwargs.get('params'), scope]
        path = self.cache.path('http-v1', identity)
        immutable = bool(re.search(r'/(?:commits|git/trees|git/blobs)/[0-9a-f]{40,64}(?:[/?]|$)', url))
        with operations.acquire(_locks[int(path.stem[:8], 16) % len(_locks)]):
            operations.check()
            previous = None
            try:
                previous = json.loads(path.read_text(encoding='utf-8'))
                content = base64.b64decode(previous['body'], validate=True)
                if hashlib.sha256(content).hexdigest() != previous['sha256']:
                    previous = None
            except (OSError, ValueError, KeyError, TypeError):
                previous = None
            if previous and (immutable or time.time() - previous['at'] < self.ttl):
                current.count('http_cache_hits')
                return self._response(url, previous, cached=True)
            host = urlsplit(url).hostname
            with _guard:
                reset = _backoff.get(host, 0)
            if reset > time.time():
                from app.backend.pipeline.ingest.codemine import RateLimited
                raise RateLimited(reset, 'Shared host backoff')
            if previous and previous.get('etag'):
                kwargs['headers'] = {**(kwargs.get('headers') or {}), 'If-None-Match': previous['etag']}
            with host_slot((host, 'search' if urlsplit(url).path.startswith('/search/') else 'core')) as state:
                if host=='api.github.com':
                    current.count('github_'+('search' if urlsplit(url).path.startswith('/search/') else 'core')+'_requests')
                    path_parts = urlsplit(url).path.strip('/').split('/')
                    endpoint = path_parts[3] if len(path_parts) >= 4 and path_parts[0] == 'repos' else ''
                    category = {'license': 'license', 'languages': 'languages', 'commits': 'commit'}.get(endpoint, 'other')
                    if endpoint == 'git' and len(path_parts) > 4 and path_parts[4] == 'commits':
                        category = 'commit'
                    current.count('github_endpoint_' + category + '_requests')
                else:current.count('non_github_api_requests')
                started = time.perf_counter()
                if operations.current():
                    configured = kwargs.get('timeout', (15, 60))
                    configured = configured if isinstance(configured, tuple) else (configured, configured)
                    kwargs['timeout'] = tuple(operations.timeout(value or 60) for value in configured)
                with current.timer('api_wait'):
                    if operations.current():
                        from .http_deadline import capture
                        with capture() as request:
                            response = self.session.get(url, **{**kwargs,'stream':True})
                            request.responses.append(response)
                            operations.check()
                            try:
                                response._content = b''.join(response.iter_content(chunk_size=65536))
                                response._content_consumed = True
                                operations.check()
                            finally:
                                response.close()
                    else:
                        response = self.session.get(url, **kwargs)
                operations.check()
                observe(state, response, time.perf_counter()-started)
            current.count('http_requests')
            current.count('http_bytes', len(response.content))
            if response.status_code in (403, 429):
                retry = response.headers.get('Retry-After')
                if retry and retry.isdigit():
                    with _guard:
                        _backoff[host] = max(_backoff.get(host, 0), time.time() + int(retry))
            if response.status_code == 304 and previous:
                previous['at'] = time.time()
                result = self._response(url, previous, cached=True)
                result.headers.update({k: v for k, v in response.headers.items() if k.lower() != 'content-length'})
                document = previous
            elif response.status_code == 200 and len(response.content) <= 4 << 20:
                document = {'at': time.time(), 'etag': response.headers.get('ETag'),
                            'headers': {k: v for k, v in response.headers.items() if k.lower() in ('content-type', 'etag')},
                            'body': base64.b64encode(response.content).decode(),
                            'sha256': hashlib.sha256(response.content).hexdigest()}
                result = response
            else:
                return response
            try:
                import uuid
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
                try:
                    temp.write_text(json.dumps(document), encoding='utf-8')
                    os.replace(temp, path)
                finally:
                    temp.unlink(missing_ok=True)
            except OSError:
                current.count('http_cache_write_failures')
            return result

    def graphql(self, query, variables, *, timeout=(15, 60)):
        """Execute an explicit read query only; POST mutations are never supported."""
        # Reject multiple operations and mutation tokens before entering transport.
        tokens = re.sub(r'"(?:\\.|[^"\\])*"|#[^\n]*', '', query) if isinstance(query, str) else ''
        if not re.match(r'^\s*query\b', tokens) or re.search(r'\b(?:mutation|subscription)\b', tokens) or len(re.findall(r'\bquery\b', tokens)) != 1:
            raise ValueError('Only one explicit GraphQL read query is supported')
        payload = {'query': query, 'variables': variables}
        serialized = json.dumps(payload, sort_keys=True)
        if not isinstance(variables, dict) or len(serialized.encode()) > 65536:
            raise ValueError('GraphQL query exceeds bounded request size')
        url = 'https://api.github.com/graphql'
        scope = hashlib.sha256(json.dumps([dict(self.session.headers),
            self.session.cookies.get_dict() if hasattr(self.session, 'cookies') else {}], sort_keys=True).encode()).hexdigest()
        path = self.cache.path('graphql-read-v1', [url, payload, scope])
        with operations.acquire(_locks[int(path.stem[:8], 16) % len(_locks)]):
            operations.check()
            try:
                previous = json.loads(path.read_text(encoding='utf-8'))
                content = base64.b64decode(previous['body'], validate=True)
                if hashlib.sha256(content).hexdigest() == previous['sha256'] and time.time() - previous['at'] < self.ttl:
                    current.count('http_cache_hits')
                    result = self._response(url, previous, cached=True)
                    result.headers['X-Acceleration-Observed-At'] = str(previous['at'])
                    return result
            except (OSError, ValueError, KeyError, TypeError):
                pass
            host = 'api.github.com'
            with _guard:
                reset = _backoff.get(host, 0)
            if reset > time.time():
                from app.backend.pipeline.ingest.codemine import RateLimited
                raise RateLimited(reset, 'Shared host backoff')
            with host_slot((host, 'graphql')) as state:
                current.count('github_graphql_requests')
                configured = timeout if isinstance(timeout, tuple) else (timeout, timeout)
                kwargs = {'json': payload, 'timeout': tuple(operations.timeout(value or 60) for value in configured)}
                started = time.perf_counter()
                with current.timer('api_wait'):
                    if operations.current():
                        from .http_deadline import capture
                        with capture() as request:
                            result = self.session.post(url, **kwargs, stream=True)
                            request.responses.append(result)
                            try:
                                body = bytearray()
                                for chunk in result.iter_content(chunk_size=65536):
                                    operations.check()
                                    body.extend(chunk)
                                    if len(body) > 4 << 20:
                                        raise ValueError('GraphQL response exceeds bounded size')
                                result._content, result._content_consumed = bytes(body), True
                            finally:
                                result.close()
                    else:
                        result = self.session.post(url, **kwargs)
                operations.check()
                observe(state, result, time.perf_counter() - started)
            current.count('http_requests')
            current.count('http_bytes', len(result.content))
            if len(result.content) > 4 << 20:
                raise ValueError('GraphQL response exceeds bounded size')
            try:
                document = result.json()
            except ValueError:
                document = None
            errors = document.get('errors') if isinstance(document, dict) else None
            description = json.dumps(errors or {}).lower()
            retry = result.headers.get('Retry-After')
            throttled = (result.status_code == 429 or
                bool(errors and (result.headers.get('X-RateLimit-Remaining') == '0' or
                     'rate_limit' in description or 'rate limit' in description)) or
                (result.status_code == 403 and (retry or 'rate limit' in result.text[:1000].lower())))
            if throttled:
                from app.backend.pipeline.ingest.codemine import RateLimited
                primary = result.headers.get('X-RateLimit-Remaining') == '0'
                until = (float(result.headers.get('X-RateLimit-Reset') or time.time() + 60) if primary else
                         time.time() + (int(retry) if retry and retry.isdigit() else 60))
                if not primary:
                    with _guard:
                        _backoff[host] = max(_backoff.get(host, 0), until)
                with _condition:
                    state['limit'], state['successes'] = max(1, state['limit'] // 2), 0
                current.count('repository_facts_graphql_rate_limits')
                raise RateLimited(until, 'GitHub GraphQL rate limit')
            if result.status_code != 200 or not isinstance(document, dict) or not isinstance(document.get('data'), dict) or errors:
                return result
            observed = time.time()
            result.headers['X-Acceleration-Observed-At'] = str(observed)
            envelope = {'at': observed, 'headers': {}, 'body': base64.b64encode(result.content).decode(),
                        'sha256': hashlib.sha256(result.content).hexdigest()}
            try:
                import uuid
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
                try:
                    temporary.write_text(json.dumps(envelope), encoding='utf-8')
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            except OSError:
                current.count('http_cache_write_failures')
            return result

    @staticmethod
    def _response(url, value, *, cached):
        import requests
        response = requests.Response()
        response.status_code, response.url = 200, url
        response._content = base64.b64decode(value['body'])
        response.headers.update(value.get('headers') or {})
        response.headers['X-Acceleration-Cache'] = 'hit'
        response.headers['X-Acceleration-Observed-At'] = str(value['at'])
        return response

    def __getattr__(self, name):
        return getattr(self.session, name)


def wrap(session):
    return session if isinstance(session, CachedSession) else CachedSession(session)
