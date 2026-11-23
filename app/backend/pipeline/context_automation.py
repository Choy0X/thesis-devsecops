"""Opt-in, app-lifetime explanation-gap watcher with a persistent bounded queue.

Only explanation drafts cross this boundary. Source evidence, labels, tests,
verification and dataset admission remain exclusively with their existing owners.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import pathlib
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from app.backend.pipeline import context_exchange
from app.backend.pipeline.context_providers import ProfileStore, PaymentBlocked, ProviderWait, spec, send as provider_send

PAGE_SIZE = 32
MAX_QUEUE = 256
STATES = ('queued', 'running', 'completed', 'failed', 'waiting', 'cancelled', 'blocked')


class StaleSource(ValueError):
    """Retrying a response for obsolete source would never make it valid."""


_SECRET = re.compile(r'''(?ix)
    -----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE\s+KEY-----
    |\b(?:AKIA|ASIA)[A-Z0-9]{16}\b
    |\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b
    |\b(?:sk-|sk_)[A-Za-z0-9_-]{20,}\b
    |\b(?:password|passwd|api[_-]?key|access[_-]?token|session[_-]?secret|client[_-]?secret)\b
       \s*[=:]\s*["']?[A-Za-z0-9+/=_-]{8,}
    |https?://[^\s/@:]+:[^\s/@]+@
''')
_SENSITIVE_PATH = re.compile(r'(?i)(?:^|/)(?:\.env(?:\.[^/]*)?|\.npmrc|\.pypirc|id_rsa|id_ed25519|credentials(?:\.[^/]*)?|secrets?(?:\.[^/]*)?|[^/]+\.(?:pem|key|p12|pfx))$')


def public_source_problem(row, *, require_snapshot=True):
    provenance = row.get('source_provenance') or {}
    if (str(row.get('ecosystem', '')).lower() != 'npm' or provenance.get('registry') != 'npm'
            or provenance.get('visibility') != 'public' or provenance.get('origin') not in ('osv', 'ghsa')):
        return 'Public npm provenance from a captured OSV/GHSA advisory is required.'
    try:
        url = urlsplit(row.get('repo_url') or '')
        valid = (url.scheme == 'https' and url.hostname in ('github.com', 'gitlab.com', 'bitbucket.org')
                 and not url.username and not url.password and not url.query and not url.fragment
                 and url.port in (None, 443) and len(url.path.strip('/').split('/')) >= 2)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        return 'A public source repository URL without credentials is required.'
    if row.get('private') or row.get('visibility') == 'private':
        return 'Private source is excluded from hosted explanation automation.'
    if require_snapshot:
        for side, commit in [('before', 'parent_commit'), ('after', 'fix_commit')]:
            context = row.get('context_' + side) or {}
            snapshot = context.get('snapshot') or {}
            code = row.get('code_' + side)
            if (not isinstance(code, str) or not code.strip() or snapshot.get('available') is not True
                    or snapshot.get('revision') != row.get(commit) or not row.get(commit)
                    or not snapshot.get('file_sha256')
                    or snapshot.get('target_sha256') != hashlib.sha256(code.encode()).hexdigest()
                    or any('target_source_mismatch' in str(x) for x in context.get('omissions', []))):
                return 'Verified exact source snapshots are required before external generation.'
    return None


def outbound_problem(packet):
    """Fail closed on credential patterns and sensitive paths throughout the payload."""
    def sensitive(value, key=None):
        if isinstance(value, dict):
            return any(sensitive(v, k) for k, v in value.items())
        if isinstance(value, list):
            return any(sensitive(v, key) for v in value)
        if isinstance(value, str):
            return bool(_SECRET.search(value) or key in ('path', 'file_path', 'old_path') and
                        _SENSITIVE_PATH.search(value.replace('\\', '/')))
        return False
    return 'Potential secret or sensitive source path excluded from external generation.' if sensitive(packet) else None


class Automation:
    def __init__(self, root, *, profiles, exchange, candidates, send=provider_send, assess=None, clock=time.time):
        self.path = pathlib.Path(root) / 'explanation_queue.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.profiles, self.exchange, self.candidates, self.send = profiles, exchange, candidates, send
        if assess is None:
            from app.backend.pipeline.explanation_quality import assess_explanation
            assess = assess_explanation
        self.assess, self.clock = assess, clock
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._thread = None
        self._cycle_lock = threading.Lock()
        self._apply_lock = threading.RLock()
        self.last_error = None
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS cursor (id INTEGER PRIMARY KEY, after_id INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO cursor VALUES (1,0)')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                identity TEXT PRIMARY KEY, pair_id INTEGER NOT NULL, source_token TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
                provider_id TEXT, reason TEXT, updated_at REAL NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(state,next_at,updated_at)')
            db.execute('''CREATE TABLE IF NOT EXISTS quotas (
                provider_id TEXT PRIMARY KEY, next_at REAL NOT NULL, day INTEGER NOT NULL, used INTEGER NOT NULL)''')
            # Interrupted requests are retried through exact-source/idempotent exchange validation.
            db.execute("UPDATE jobs SET state='queued',reason='Interrupted by application shutdown.' WHERE state='running'")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def status(self):
        counts = dict.fromkeys(STATES, 0)
        with self.connect() as db:
            for record in db.execute('SELECT state,count(*) n FROM jobs GROUP BY state'):
                counts[record['state']] = record['n']
            recent = [dict(r) for r in db.execute('SELECT pair_id,state,reason,next_at,provider_id FROM jobs ORDER BY updated_at DESC LIMIT 20')]
        return {'enabled': self.profiles.config()['enabled'], 'running': bool(self._thread and self._thread.is_alive()),
                'counts': counts, 'last_error': self.last_error, 'recent': recent}

    def configure(self, updates):
        result = self.profiles.configure(**updates)
        if result['enabled']:
            self.start()
        else:
            self.stop()
        return result

    def discover(self):
        with self.connect() as db:
            pending = db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','waiting','running')").fetchone()[0]
            if pending >= MAX_QUEUE:
                return
            after = db.execute('SELECT after_id FROM cursor WHERE id=1').fetchone()[0]
        rows = list(self.candidates(after, min(PAGE_SIZE, MAX_QUEUE - pending)))
        with self.connect() as db:
            for row in rows:
                pair_id = int(row.get('pair_id', row.get('id')))
                if pair_id <= after:
                    continue
                after = pair_id
                if not self.assess(row)['generation_eligible']:
                    continue
                # Metadata revisions distinguish newly changed gaps without reading pair_code.
                token = hashlib.sha256(context_exchange.canonical({k: row.get(k) for k in
                    ('pair_key', 'updated', 'edited_at', 'llm_prompt_version', 'explanation', 'explanation_llm',
                     'parent_commit', 'fix_commit', 'content_hash')}).encode()).hexdigest()
                identity = hashlib.sha256(('%s:%s:%s' % (pair_id, token, context_exchange.PROMPT_VERSION)).encode()).hexdigest()
                problem = public_source_problem(row, require_snapshot=False)
                db.execute('INSERT OR IGNORE INTO jobs(identity,pair_id,source_token,state,reason,updated_at) VALUES(?,?,?,?,?,?)',
                    (identity, pair_id, token, 'blocked' if problem else 'queued', problem, self.clock()))
            db.execute('UPDATE cursor SET after_id=? WHERE id=1', (after if rows else 0,))

    def enqueue_selected(self, ids):
        """Add an explicit selection to an already-enabled free-only provider queue."""
        if not self.profiles.config()['enabled']:
            raise ValueError('Enable a permitted free provider profile before queueing selected explanations.')
        if not isinstance(ids, list) or not ids or any(type(pid) is not int or pid <= 0 for pid in ids):
            raise ValueError('Select valid source items.')
        counts = {'queued': 0, 'unchanged': 0, 'blocked': 0}
        wanted = sorted(set(ids))
        for start in range(0, len(wanted), 128):
            batch = wanted[start:start+128]
            rows = list(self.exchange.load_rows(batch))
            if {int(row.get('pair_id', row.get('id'))) for row in rows} != set(batch):
                raise ValueError('Selected source no longer exists.')
            with self.connect() as db:
                for row in rows:
                    if not self.assess(row)['generation_eligible']:
                        counts['unchanged'] += 1
                        continue
                    problem = public_source_problem(row)
                    pair_id = int(row.get('pair_id', row.get('id')))
                    token = hashlib.sha256(context_exchange.canonical({k: row.get(k) for k in
                        ('pair_key', 'updated', 'edited_at', 'llm_prompt_version', 'explanation', 'explanation_llm',
                         'parent_commit', 'fix_commit', 'content_hash')}).encode()).hexdigest()
                    identity = hashlib.sha256(('%s:%s:%s' % (pair_id, token, context_exchange.PROMPT_VERSION)).encode()).hexdigest()
                    inserted = db.execute('INSERT OR IGNORE INTO jobs(identity,pair_id,source_token,state,reason,updated_at) VALUES(?,?,?,?,?,?)',
                        (identity, pair_id, token, 'blocked' if problem else 'queued', problem, self.clock())).rowcount
                    counts['blocked' if problem else 'queued'] += inserted
                    counts['unchanged'] += not inserted
        self.start()
        return counts

    def _state(self, identity, state, *, reason=None, next_at=0, provider_id=None):
        with self.connect() as db:
            db.execute('UPDATE jobs SET state=?,reason=?,next_at=?,provider_id=?,updated_at=? WHERE identity=? AND state!=?',
                       (state, reason, next_at, provider_id, self.clock(), identity, 'cancelled'))

    def _reserve(self):
        now = self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT * FROM jobs WHERE state IN ('queued','waiting') AND next_at<=? ORDER BY updated_at,identity LIMIT 1", (now,)).fetchone()
            if not job:
                return None
            profiles = [p for p in self.profiles.config()['profiles'] if p['eligible']]
            if not profiles:
                self.last_error = 'No eligible free provider is configured.'
                return None
            waits = []
            for profile in profiles:
                pid, route = profile['provider_id'], spec(profile['provider_id'])
                quota = db.execute('SELECT * FROM quotas WHERE provider_id=?', (pid,)).fetchone()
                day = int(now // 86400)
                used = quota['used'] if quota and quota['day'] == day else 0
                next_at = quota['next_at'] if quota else 0
                if route['daily_requests'] is not None and used >= route['daily_requests']:
                    next_at = max(next_at, (day + 1) * 86400)
                if next_at > now:
                    waits.append(next_at)
                    continue
                db.execute('INSERT INTO quotas VALUES(?,?,?,?) ON CONFLICT(provider_id) DO UPDATE SET next_at=excluded.next_at,day=excluded.day,used=excluded.used',
                    (pid, now + route['interval_seconds'], day, used + 1))
                db.execute("UPDATE jobs SET state='running',attempts=attempts+1,provider_id=?,updated_at=? WHERE identity=?",
                    (pid, now, job['identity']))
                self.last_error = None
                return dict(job), profile
            if waits:
                db.execute("UPDATE jobs SET state='waiting',next_at=?,reason='Waiting for the free provider allowance.' WHERE identity=?",
                           (min(waits), job['identity']))
        return None

    def _paused(self):
        return self._cancel.is_set() or self._stop.is_set() or not self.profiles.config()['enabled']

    def _process(self, job, profile):
        identity, pid = job['identity'], profile['provider_id']
        try:
            rows = self.exchange.load_rows([job['pair_id']])
            if len(rows) != 1:
                raise ValueError('Selected source no longer exists.')
            row = rows[0]
            if not self.assess(row)['generation_eligible']:
                self._state(identity, 'completed', reason='Explanation is already usable; no provider request.')
                return
            problem = public_source_problem(row)
            if problem:
                self._state(identity, 'blocked', reason=problem)
                return
            # Export repeats the exact selected source read and binds subsequent import to it.
            packet = self.exchange.export([job['pair_id']], max_items=1, mark_waiting=False)[0]
            if packet['items'][0]['source_sha256'] != context_exchange.fingerprint(row):
                raise StaleSource('Source changed while preparing the request.')
            problem = outbound_problem(packet)
            if problem:
                self._state(identity, 'blocked', reason=problem)
                return
            if self._paused() or not self.profiles.profile(pid)['eligible']:
                self._state(identity, 'queued', reason='Explanation automation paused before request.')
                return
            current = self.exchange.load_rows([job['pair_id']])
            if (len(current) != 1 or public_source_problem(current[0])
                    or context_exchange.fingerprint(current[0]) != packet['items'][0]['source_sha256']):
                raise StaleSource('Source or public provenance changed while preparing the packet.')
            response = self.send(profile, self.profiles.secret(pid), packet)
            value = json.loads(response, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite result.')))
            if not isinstance(value, dict):
                raise ValueError('Provider returned an invalid context result.')
            # Never silently remove unsupported fields: the shared exchange rejects them.
            value['provider'], value['model'] = pid, profile['model']
            preview = self.exchange.preview(value)
            if any(item['status'] == 'stale' for item in preview['items']):
                raise StaleSource('Source changed during explanation generation.')
            if (len(preview['items']) != 1 or preview['items'][0]['status'] != 'valid'
                    or preview['items'][0].get('pair_id') != job['pair_id']):
                raise ValueError('Explanation failed exact-source draft validation.')
            with self._apply_lock:
                if self._paused():
                    self._state(identity, 'queued', reason='Paused before saving the explanation draft.')
                    return
                latest = self.exchange.load_rows([job['pair_id']])
                if len(latest) != 1 or public_source_problem(latest[0]):
                    raise StaleSource('Source provenance or snapshot changed before saving.')
                result = self.exchange.apply(value)
                if result['imported'] + result['unchanged'] != 1:
                    raise StaleSource('Source changed before saving the explanation draft.')
                self._state(identity, 'completed', reason='Model draft retained; evidence and admission unchanged.', provider_id=pid)
        except PaymentBlocked:
            self.profiles.save_profile(pid, {'enabled': False})
            self._state(identity, 'blocked', reason='Payment or access challenge blocked; provider disabled.', provider_id=pid)
        except StaleSource:
            self._state(identity, 'failed', reason='Exact source or public provenance changed; obsolete draft rejected.', provider_id=pid)
        except ProviderWait as error:
            next_at = self.clock() + error.delay
            with self.connect() as db:
                db.execute('UPDATE quotas SET next_at=max(next_at,?) WHERE provider_id=?', (next_at, pid))
            self._state(identity, 'waiting', reason='Waiting for provider quota/capacity; payment is disabled.', next_at=next_at, provider_id=pid)
        except Exception:
            # ignored: provider exception text can contain bodies or credentials; bounded sanitized retry state is recorded instead.
            attempts = job['attempts'] + 1
            self._state(identity, 'failed' if attempts >= 3 else 'waiting',
                        reason='Explanation request or exact-source draft validation failed.',
                        next_at=self.clock() + min(3600, 30 * 2 ** min(attempts, 6)), provider_id=pid)

    def run_once(self):
        if self._paused() or not self._cycle_lock.acquire(blocking=False):
            return self.status()
        try:
            self.discover()
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.profiles.config()['max_concurrency']) as pool:
                futures = []
                for _ in range(self.profiles.config()['max_concurrency']):
                    reserved = self._reserve()
                    if reserved is None:
                        break
                    futures.append(pool.submit(self._process, *reserved))
                for future in futures:
                    future.result()
        finally:
            self._cycle_lock.release()
        return self.status()

    def start(self, *, enable=False):
        if enable:
            self.profiles.configure(enabled=True)
        if not self.profiles.config()['enabled']:
            return self.status()
        self._stop.clear()
        if not self._thread or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._watch, name='explanation-gap-watcher', daemon=True)
            self._thread.start()
        return self.status()

    def _watch(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # ignored: retain a sanitized watcher error and retry at the next tick without exposing provider data.
                self.last_error = 'Explanation watcher could not read its queue or source metadata.'
            self._stop.wait(5)

    def stop(self, *, disable=False):
        with self._apply_lock:
            if disable:
                self.profiles.configure(enabled=False)
            self._stop.set()
        # In-flight HTTP is bounded by timeout and cannot apply after stop/cancel.
        return self.status()

    def cancel(self):
        with self._apply_lock:
            self._cancel.set()
            with self.connect() as db:
                db.execute("UPDATE jobs SET state='cancelled',reason='Cancelled by user.',updated_at=? WHERE state IN ('queued','running','waiting')", (self.clock(),))
        return self.status()

    def resume(self):
        with self._apply_lock:
            self._cancel.clear()
            self._stop.clear()
            with self.connect() as db:
                db.execute("UPDATE jobs SET state='queued',attempts=0,next_at=0,reason=NULL WHERE state IN ('cancelled','failed','blocked')")
        return self.status()


def _provenance(pair, advisories, commit):
    subject = pair.get('subject') or {}
    if str(subject.get('ecosystem', '')).lower() != 'npm':
        return {}
    # A manual pair cannot borrow an official advisory's public provenance for
    # a different repository by pointing at its commit key.
    repository = (commit or {}).get('repo') or {}
    captured_url = repository.get('url') or ''
    selected_url = pair.get('repo_url') or captured_url
    if not captured_url or selected_url.rstrip('/').removesuffix('.git') != captured_url.rstrip('/').removesuffix('.git'):
        return {}
    for advisory in advisories:
        origin = str(advisory.get('origin') or '').lower()
        if origin not in ('osv', 'ghsa') or not advisory.get('osv_id'):
            continue
        if any(str(p.get('ecosystem', '')).lower() == 'npm' and p.get('name') == subject.get('package')
               for p in advisory.get('packages', [])):
            return {'visibility': 'public', 'registry': 'npm', 'origin': origin,
                    'advisory_id': advisory['osv_id']}
    return {}


def metadata_page(after, limit):
    """Keyset pagination and bounded owner/enrichment metadata; never pair_code."""
    from app.backend.pipeline.db import legacy, mappers
    db = legacy.handle()
    pairs = list(db['code_pairs'].find({'_id': {'$gt': after}},
        {'pair_key': 1, 'commit_key': 1, 'subject': 1, 'unit': 1, 'flags': 1, 'repo_url': 1,
         'explanation': 1, 'overrides': 1, 'updated_at': 1, 'edited_at': 1, 'extraction.content_hash': 1})
         .sort('_id', 1).limit(min(PAGE_SIZE, max(1, limit))))
    ids = [p['_id'] for p in pairs]
    drafts = {p['_id']: p for p in db['pair_enrichments'].find({'_id': {'$in': ids}},
              {'explanation': 1, 'model': 1, 'prompt_version': 1})} if ids else {}
    commits, linked = legacy.owners_of(db, sorted({p['commit_key'] for p in pairs if p.get('commit_key')})) if pairs else ({}, {})
    result = []
    for pair in pairs:
        key = pair.get('commit_key')
        advisories = linked.get(key, []) if key else []
        row = mappers.pair_row(pair, None, drafts.get(pair['_id']), commits.get(key), advisories)
        result.append({**row, 'pair_id': row['id'], 'source_provenance': _provenance(pair, advisories, commits.get(key))})
    return result


_DEFAULT = None
_DEFAULT_LOCK = threading.Lock()


def default():
    global _DEFAULT
    from app.backend import paths
    root = paths.interim_dir() / 'context_automation'
    with _DEFAULT_LOCK:
        if _DEFAULT is None or _DEFAULT.path.parent != root:
            exchange = context_exchange.default()
            original = exchange.load_rows

            def load(ids):
                from app.backend.pipeline.db import legacy
                db = legacy.handle()
                pairs = list(db['code_pairs'].find({'_id': {'$in': ids}}, {'subject': 1, 'commit_key': 1, 'repo_url': 1}))
                commits, linked = legacy.owners_of(db, sorted({p['commit_key'] for p in pairs if p.get('commit_key')})) if pairs else ({}, {})
                provenance = {p['_id']: _provenance(p, linked.get(p.get('commit_key'), []), commits.get(p.get('commit_key'))) for p in pairs}
                return [{**r, 'source_provenance': provenance.get(r['pair_id'], {})} for r in original(ids)]

            exchange.load_rows = load
            _DEFAULT = Automation(root, profiles=ProfileStore(root / 'providers'), exchange=exchange, candidates=metadata_page)
        return _DEFAULT


def start():
    """App startup hook. Only resume a preference explicitly enabled by the user."""
    return default().start()


def stop():
    """App shutdown hook, preserving opt-in and queued work for next app launch."""
    if _DEFAULT is not None:
        return _DEFAULT.stop()
