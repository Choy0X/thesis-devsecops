"""Append-only automatic recovery observations and immutable lazy context detail.

Checkpoint caches are not history. Every record call appends an observation,
including cached results; only metadata projections may be replaced. A caller's
publication guard must hold its ownership journal writer lock across each write.
No workflow/recovery imports or callbacks occur under the history writer lock.
"""
from __future__ import annotations

from contextlib import contextmanager
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

SCHEMA = 'automatic-recovery-history/1'
MAX_PAGE = 500
MAX_CONTEXT_BYTES = 8 << 20
MAX_ARTIFACT_BYTES = 20 << 20
MAX_METADATA_BYTES = 64 << 10
MAX_DIFF_BYTES = 2 << 20
MAX_DIFF_INPUT_BYTES = 256 << 10
MAX_DIFF_LINES = 4096
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_TERMINAL_FENCES = {'cancelled', 'superseded'}


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    return _sha(_json(value).encode())


def _text(value, maximum=512):
    return str(value)[:maximum] if value is not None else None


def _identifier(value, name):
    if not isinstance(value, str) or not value or len(value) > 512 or '\x00' in value:
        raise ValueError('Invalid ' + name)
    return value


def database_path(root):
    return Path(root).resolve() / '.evidence_recovery_history.sqlite3'


def _artifact_path(root, identity):
    if not isinstance(identity, str) or not _HEX.fullmatch(identity):
        raise ValueError('Invalid recovery artifact identity')
    return Path(root).resolve() / '.evidence_recovery_history' / 'artifacts' / (identity + '.json')


@contextmanager
def _db(root, *, write=False):
    path = database_path(root)
    if not write and not path.is_file():
        yield None
        return
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path) if write else path.as_uri() + '?mode=ro', uri=not write, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        if write:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,workflow_id TEXT NOT NULL,action TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS attempt_workflow ON attempts(workflow_id,created);
              CREATE TABLE IF NOT EXISTS heads(workflow_id TEXT,action TEXT,fence INTEGER,attempt_id TEXT,PRIMARY KEY(workflow_id,action));
              CREATE TABLE IF NOT EXISTS attempt_state(attempt_id TEXT PRIMARY KEY,state TEXT NOT NULL,fence INTEGER NOT NULL,pid INTEGER NOT NULL,ctime REAL NOT NULL,token TEXT NOT NULL,finished REAL);
              CREATE TABLE IF NOT EXISTS observations(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,attempt_id TEXT NOT NULL,item_id TEXT NOT NULL,body TEXT NOT NULL,artifacts TEXT NOT NULL,created REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS observation_item ON observations(attempt_id,item_id,seq);
              CREATE TABLE IF NOT EXISTS items(attempt_id TEXT,item_id TEXT,first_seq INTEGER,last_seq INTEGER,status TEXT,retryable INTEGER,PRIMARY KEY(attempt_id,item_id));
              CREATE INDEX IF NOT EXISTS item_page_order ON items(attempt_id,first_seq,last_seq);
              CREATE INDEX IF NOT EXISTS item_page_status ON items(attempt_id,status,first_seq,last_seq);
              CREATE INDEX IF NOT EXISTS item_page_retryable ON items(attempt_id,retryable,first_seq,last_seq);
              CREATE INDEX IF NOT EXISTS item_page_status_retryable ON items(attempt_id,status,retryable,first_seq,last_seq);
              CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,attempt_id TEXT NOT NULL,item_id TEXT,kind TEXT NOT NULL,created REAL NOT NULL,body TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS attempt_events ON events(attempt_id,seq);
            ''')
            db.execute('BEGIN IMMEDIATE')
        else:
            # Count, existence and selected page rows belong to one short read
            # snapshot even while a recovery worker appends observations.
            db.execute('BEGIN')
        yield db
        if write:
            db.commit()
    except BaseException:
        if write:
            db.rollback()
        raise
    finally:
        db.close()


def _caller():
    import psutil
    return os.getpid(), psutil.Process().create_time()


def _alive(pid, created):
    import psutil
    try:
        return abs(psutil.Process(pid).create_time() - created) < .01
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return True  # Unknown liveness never authorizes stealing an attempt.


def _guard(guard, check):
    if guard is None:
        guard = getattr(check, 'publication_guard', None)
    if not callable(guard):
        raise ValueError('Recovery history writes require an atomic ownership publication_guard.')
    return guard


def _check(check):
    if check is not None and check() is False:
        raise InterruptedError('Recovery history ownership check refused the write.')


def _event(db, attempt_id, kind, *, item_id=None, body=None):
    db.execute('INSERT INTO events(attempt_id,item_id,kind,created,body) VALUES(?,?,?,?,?)',
               (attempt_id, item_id, kind, time.time(), _json(body or {})))


def _attempt(db, attempt_id):
    row = db.execute('SELECT a.*,s.state,s.fence,s.pid,s.ctime,s.token,s.finished FROM attempts a JOIN attempt_state s ON a.id=s.attempt_id WHERE a.id=?', (attempt_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown recovery history attempt.')
    return row


def _owned(db, attempt_id, owner=None, *, running=True):
    row = _attempt(db, attempt_id)
    head = db.execute('SELECT fence,attempt_id FROM heads WHERE workflow_id=? AND action=?', (row['workflow_id'], row['action'])).fetchone()
    pid, created = _caller()
    token = owner.get('token') if isinstance(owner, dict) else owner
    if (head is None or head['attempt_id'] != attempt_id or head['fence'] != row['fence']
            or row['state'] in _TERMINAL_FENCES or running and row['state'] != 'running'
            or row['pid'] != pid or abs(row['ctime'] - created) >= .01
            or token is not None and token != row['token']):
        raise InterruptedError('Recovery history writer is cancelled, superseded or no longer owns this attempt.')
    return row


def _state(row):
    return {**json.loads(row['body']), 'attempt_id': row['id'], 'state': row['state'], 'created': row['created'],
            'finished': row['finished'], 'owner': {'pid': row['pid'], 'created': row['ctime'], 'token': row['token']}}


@contextmanager
def owner_guard(attempt_id, *, root, owner):
    """Fence an external derived write under the history writer lock.

    Acquire the workflow publication guard first. The body may write the repair
    ledger, but must not reenter history or call workflow ownership callbacks.
    """
    _identifier(attempt_id, 'attempt identity')
    with _db(root, write=True) as db:
        _owned(db, attempt_id, owner)
        yield


def start(attempt_id, *, tag, action, root, workflow_id=None, phase_id=None, guard=None, check=None,
          manifest_sha256=None, implementation=None):
    attempt_id, action = _identifier(attempt_id, 'attempt identity'), _identifier(action, 'recovery action')
    if not isinstance(tag, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', tag):
        raise ValueError('Invalid recovery build tag')
    guard = _guard(guard, check)
    _check(check)
    if manifest_sha256 is None:
        path = Path(root) / ('dataset_' + tag) / 'manifest.json'
        if path.is_file():
            hasher = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b''):
                    hasher.update(chunk)
            manifest_sha256 = hasher.hexdigest()
    if manifest_sha256 is not None and not _HEX.fullmatch(str(manifest_sha256)):
        raise ValueError('Invalid recovery manifest identity')
    workflow_id = _identifier(workflow_id or 'build:' + tag + ':' + str(manifest_sha256), 'workflow identity')
    body = {'schema': SCHEMA, 'tag': tag, 'action': action, 'workflow_id': workflow_id,
            'phase_id': _text(phase_id), 'manifest_sha256': manifest_sha256,
            'implementation': _digest(implementation) if isinstance(implementation, (dict, list)) else _text(implementation)}
    pid, created = _caller()
    with guard():
        with _db(root, write=True) as db:
            existing = db.execute('SELECT 1 FROM attempts WHERE id=?', (attempt_id,)).fetchone()
            head = db.execute('SELECT * FROM heads WHERE workflow_id=? AND action=?', (workflow_id, action)).fetchone()
            if existing:
                previous = _attempt(db, attempt_id)
                if json.loads(previous['body']) != body:
                    raise ValueError('A recovery attempt identity cannot be reused for different inputs.')
                if previous['state'] in _TERMINAL_FENCES or not head or head['attempt_id'] != attempt_id:
                    raise InterruptedError('A cancelled or superseded recovery attempt cannot be resumed.')
                if (previous['pid'] != pid or abs(previous['ctime'] - created) >= .01) and _alive(previous['pid'], previous['ctime']):
                    raise InterruptedError('Another live process owns this recovery attempt.')
            elif head:
                old = head['attempt_id']
                changed = db.execute("UPDATE attempt_state SET state='superseded',finished=? WHERE attempt_id=? AND state IN ('running','waiting')", (time.time(), old)).rowcount
                _event(db, old, 'superseded' if changed else 'new_attempt_fence',
                    body={'reason': 'new_phase_attempt', 'next_attempt_id': attempt_id})
            fence = (head['fence'] + 1) if head else 1
            token = uuid.uuid4().hex
            if not existing:
                db.execute('INSERT INTO attempts VALUES(?,?,?,?,?)', (attempt_id, workflow_id, action, _json(body), time.time()))
            db.execute('INSERT OR REPLACE INTO attempt_state VALUES(?,?,?,?,?,?,NULL)', (attempt_id, 'running', fence, pid, created, token))
            db.execute('INSERT OR REPLACE INTO heads VALUES(?,?,?,?)', (workflow_id, action, fence, attempt_id))
            _event(db, attempt_id, 'attempt_resumed' if existing else 'attempt_started', body={'fence': fence})
            return _state(_attempt(db, attempt_id))


def _binding(record):
    # Lazy dependency only: the history module never imports its caller or the
    # workflow journal. This canonical binding matches the retry verifier.
    from .verify.automatic import source_binding
    return source_binding(record)


def _contexts(record):
    result = {}
    for name in ('context_before', 'context_after'):
        value = record.get(name)
        if value is not None and not isinstance(value, str):
            value = _json(value)
        if value is not None and len(value.encode()) > MAX_CONTEXT_BYTES:
            raise ValueError('Recovery context exceeds its immutable artifact bound.')
        result[name] = value
    return result


def _diff(original, recovered):
    def bounded_lines(text):
        raw = text.encode()
        prefix = raw[:MAX_DIFF_INPUT_BYTES].decode('utf-8', errors='ignore')
        lines = prefix.splitlines(True)
        return lines[:MAX_DIFF_LINES], len(raw) > MAX_DIFF_INPUT_BYTES or len(lines) > MAX_DIFF_LINES
    result = {}
    for name in ('context_before', 'context_after'):
        before, after = original[name] or '', recovered[name] or ''
        if before == after:
            result[name] = {'changed': False, 'unified': '', 'truncated': False}
            continue
        before_lines, before_cut = bounded_lines(before)
        after_lines, after_cut = bounded_lines(after)
        chunks, used, truncated = [], 0, before_cut or after_cut
        for part in difflib.unified_diff(before_lines, after_lines, fromfile='original/' + name, tofile='recovered/' + name):
            raw = part.encode()
            if used + len(raw) > MAX_DIFF_BYTES:
                chunks.append(raw[:max(0, MAX_DIFF_BYTES - used)].decode('utf-8', errors='ignore'))
                truncated = True
                break
            chunks.append(part)
            used += len(raw)
        result[name] = {'changed': before != after, 'unified': ''.join(chunks), 'truncated': truncated}
    return result


def _artifact(raw, root):
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError('Recovery artifact exceeds its byte bound.')
    identity = _sha(raw)
    path = _artifact_path(root, identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.stat().st_size != len(raw) or _sha(path.read_bytes()) != identity:
            raise ValueError('Immutable recovery artifact is corrupted.')
        return identity
    temporary = path.parent / (uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows rename refuses an existing destination and needs no hard-link
        # privilege. Other platforms use an exclusive hard link. All module
        # writers also hold the history DB writer lock until publication commits.
        try:
            if os.name == 'nt':
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
        except FileExistsError:
            if path.stat().st_size != len(raw) or _sha(path.read_bytes()) != identity:
                raise ValueError('Immutable recovery artifact collision.')
    finally:
        temporary.unlink(missing_ok=True)
    return identity


def _strings(values, *, count=100, length=512):
    values = [values] if isinstance(values, str) else values if isinstance(values, (tuple, list)) else []
    return [str(value)[:length] for value in values[:count] if isinstance(value, (str, int, float))]


def _strings_shortened(values, summary):
    if values is None:
        return False
    original = [values] if isinstance(values, str) else list(values) if isinstance(values, (tuple, list)) else values
    return original != summary


def _span(value):
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return None
    return value if isinstance(value, list) and len(value) == 2 and all(type(part) is int for part in value) else None


def _summary(record, result, original_binding, *, item_id, cached, repair_revision_id):
    status = _text(result.get('status') or result.get('state') or result.get('verdict') or 'unknown', 128)
    reasons = _strings(result.get('reasons'))
    missing = _strings(result.get('remaining_missing_facts', result.get('missing_facts', [])))
    category = _text(result.get('failure_category') or result.get('category'), 128)
    if not category:
        category = {'env_error': 'environment', 'unavailable': 'missing_context', 'unsupported': 'unsupported',
            'excluded': 'source_integrity', 'not_verified': 'security_not_verified', 'failed': 'execution',
            'waiting': 'capability', 'cancelled': 'cancelled'}.get(status)
    retryable = result.get('retryable')
    retryable = retryable if type(retryable) is bool else status in ('env_error', 'unavailable', 'failed', 'waiting', 'interrupted', 'cancelled')
    repair_ids = _strings(result.get('repair_ids'), count=64, length=128)
    truncated = (_strings_shortened(result.get('reasons'), reasons)
        or _strings_shortened(result.get('remaining_missing_facts', result.get('missing_facts')), missing)
        or _strings_shortened(result.get('repair_ids'), repair_ids)
        or any(value is not None and len(str(value)) > bound for value, bound in (
            (result.get('status') or result.get('state') or result.get('verdict'), 128),
            (result.get('failure_category') or result.get('category'), 128),
            (result.get('error'), 1024), (repair_revision_id, 512))))
    locations = [{'side': side, 'repository': _text(record.get('repository_identity')),
        'revision': original_binding['parent' if side == 'before' else 'fix'], 'path': _text(record.get('file_path_' + side), 1024),
        'byte_span': _span(record.get('byte_span_' + side)), 'line_span': _span(record.get('line_span_' + side))} for side in ('before', 'after')]
    return {'item_id': item_id, 'record_id': record['record_id'], 'status': status, 'reasons': reasons,
        'missing_facts': missing, 'remaining_missing_facts': missing, 'retryable': retryable,
        'failure_category': category, 'error': _text(result.get('error'), 1024), 'cached': bool(cached),
        'source_binding': original_binding, 'source_locations': locations, 'repair_revision_id': _text(repair_revision_id),
        'repair_ids': repair_ids, 'outcome_summary_truncated': truncated}


def record(attempt_id, original, recovered, result, *, root, check=None, guard=None, cached=False,
           repair_revision_id=None, owner=None):
    attempt_id = _identifier(attempt_id, 'attempt identity')
    guard = _guard(guard, check)
    _check(check)
    if not isinstance(original, dict) or not isinstance(recovered, dict) or not isinstance(result, dict):
        raise ValueError('A recovery observation needs original/recovered records and an outcome.')
    rid = _identifier(original.get('record_id'), 'record identity')
    if recovered.get('record_id') != rid:
        raise ValueError('A recovery observation cannot change record identity.')
    before, after = _contexts(original), _contexts(recovered)
    bindings = {'original': _binding(original), 'recovered': _binding(recovered)}
    item_id = _digest({key: bindings['original'].get(key) for key in ('record_id', 'pair', 'repository_identity', 'parent', 'fix', 'path_before', 'path_after')})
    bodies = {'original': _json({'schema': SCHEMA, 'contexts': before, 'source_binding': bindings['original']}).encode(),
              'recovered': _json({'schema': SCHEMA, 'contexts': after, 'source_binding': bindings['recovered']}).encode(),
              'diff': _json({'schema': SCHEMA, 'diff': _diff(before, after)}).encode(),
              'outcome': _json({'schema': SCHEMA, 'outcome': result}).encode()}
    if any(len(raw) > MAX_ARTIFACT_BYTES for raw in bodies.values()):
        raise ValueError('Recovery artifact exceeds its byte bound.')
    revision = repair_revision_id or result.get('repair_id') or (_digest({'original': _sha(bodies['original']), 'recovered': _sha(bodies['recovered'])}) if before != after else None)
    body = _summary(original, result, bindings['original'], item_id=item_id, cached=cached, repair_revision_id=revision)
    observation_id = uuid.uuid4().hex
    body.update(attempt_id=attempt_id, observation_id=observation_id)
    if len(_json(body).encode()) > MAX_METADATA_BYTES:
        raise ValueError('Recovery outcome metadata exceeds its bound.')
    with guard():
        with _db(root, write=True) as db:
            _owned(db, attempt_id, owner)
            artifacts = {key: _artifact(raw, root) for key, raw in bodies.items()}
            cursor = db.execute('INSERT INTO observations(id,attempt_id,item_id,body,artifacts,created) VALUES(?,?,?,?,?,?)',
                (observation_id, attempt_id, item_id, _json(body), _json(artifacts), time.time())).lastrowid
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?) ON CONFLICT(attempt_id,item_id) DO UPDATE SET last_seq=excluded.last_seq,status=excluded.status,retryable=excluded.retryable',
                (attempt_id, item_id, cursor, cursor, body['status'], int(body['retryable'])))
            _event(db, attempt_id, 'item_observed', item_id=item_id, body={'observation_id': observation_id,
                'status': body['status'], 'cached': bool(cached), 'retryable': body['retryable']})
    return body


def finish(attempt_id, state, *, root, guard=None, check=None, owner=None):
    _identifier(state, 'attempt state')
    if state in _TERMINAL_FENCES:
        return invalidate(attempt_id, root=root, state=state, owner=owner)
    guard = _guard(guard, check)
    _check(check)
    with guard():
        with _db(root, write=True) as db:
            _owned(db, attempt_id, owner, running=False)
            db.execute('UPDATE attempt_state SET state=?,finished=? WHERE attempt_id=?', (state, None if state == 'running' else time.time(), attempt_id))
            _event(db, attempt_id, 'attempt_' + state)
            return _state(_attempt(db, attempt_id))


def invalidate(attempt_id, *, root, state='cancelled', reason=None, owner=None):
    if state not in _TERMINAL_FENCES:
        raise ValueError('Invalid recovery invalidation state')
    with _db(root, write=True) as db:
        row = _attempt(db, attempt_id)
        if owner is not None:
            _owned(db, attempt_id, owner, running=False)
        db.execute('UPDATE attempt_state SET state=?,finished=? WHERE attempt_id=?', (state, time.time(), attempt_id))
        db.execute('UPDATE heads SET fence=fence+1 WHERE workflow_id=? AND action=? AND attempt_id=?', (row['workflow_id'], row['action'], attempt_id))
        _event(db, attempt_id, state, body={'reason': _text(reason)})
        return _state(_attempt(db, attempt_id))


def invalidate_workflow(workflow_id, *, root, state='cancelled', reason=None):
    """Fence active histories after the owning workflow commits cancellation.

    No workflow callback or artifact access occurs here. Workflow cancellation
    must release its own writer lock before calling this metadata-only helper.
    Repeated cancellation leaves already terminal attempts unchanged.
    """
    _identifier(workflow_id, 'workflow identity')
    if state not in _TERMINAL_FENCES:
        raise ValueError('Invalid recovery invalidation state')
    if not database_path(root).is_file():
        return {'workflow_id': workflow_id, 'state': state, 'invalidated': 0}
    with _db(root, write=True) as db:
        rows = db.execute("SELECT a.id,a.action FROM attempts a JOIN attempt_state s ON a.id=s.attempt_id WHERE a.workflow_id=? AND s.state IN ('running','waiting')", (workflow_id,)).fetchall()
        for row in rows:
            db.execute('UPDATE attempt_state SET state=?,finished=?,token=? WHERE attempt_id=?',
                (state, time.time(), uuid.uuid4().hex, row['id']))
            db.execute('UPDATE heads SET fence=fence+1 WHERE workflow_id=? AND action=? AND attempt_id=?',
                (workflow_id, row['action'], row['id']))
            _event(db, row['id'], state, body={'reason': _text(reason)})
        return {'workflow_id': workflow_id, 'state': state, 'invalidated': len(rows)}


def get_attempt(attempt_id, *, root):
    """Read one authorized attempt's metadata without loading context artifacts."""
    _identifier(attempt_id, 'attempt identity')
    with _db(root) as db:
        try:
            row = _attempt(db, attempt_id) if db is not None else None
        except ValueError as exc:
            raise KeyError(attempt_id) from exc
        if row is None:
            raise KeyError(attempt_id)
        return {key: value for key, value in _state(row).items() if key != 'owner'}


def _bounds(limit, offset=0):
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE or type(offset) is not int or offset < 0:
        raise ValueError('Invalid recovery history page bounds')


def _metadata(row):
    return {**json.loads(row['body']), 'cursor': row['seq']}


def page(attempt_id, *, root, limit=100, offset=0, outcome=None, retryable=None):
    _bounds(limit, offset)
    if retryable is not None and type(retryable) is not bool:
        raise ValueError('Invalid recovery retryability filter')
    with _db(root) as db:
        if db is None or db.execute('SELECT 1 FROM attempts WHERE id=?', (attempt_id,)).fetchone() is None:
            return {'items': [], 'total': 0, 'limit': limit, 'offset': offset, 'available': False, 'reason': 'item_history_not_recorded'}
        where, args = 'i.attempt_id=?', [attempt_id]
        if outcome not in (None, 'all'):
            where += ' AND i.status=?'; args.append(_identifier(outcome, 'outcome filter'))
        if retryable is not None:
            where += ' AND i.retryable=?'; args.append(int(retryable))
        total = db.execute('SELECT count(*) FROM items i WHERE ' + where, args).fetchone()[0]
        # LIMIT inside the subquery prevents joining and sorting every matching
        # observation body. Only this page's selected primary keys reach o.
        rows = db.execute('SELECT o.* FROM (SELECT i.last_seq,i.first_seq FROM items i WHERE ' + where
            + ' ORDER BY i.first_seq LIMIT ? OFFSET ?) page_items JOIN observations o ON o.seq=page_items.last_seq ORDER BY page_items.first_seq',
            (*args, limit, offset)).fetchall()
        return {'items': [_metadata(row) for row in rows], 'total': total, 'limit': limit, 'offset': offset, 'available': True,
                'next_offset': offset + len(rows) if offset + len(rows) < total else None}


def selected(attempt_id, item_ids, *, root):
    if not isinstance(item_ids, (list, tuple)) or not 1 <= len(item_ids) <= MAX_PAGE or any(not isinstance(item, str) or not _HEX.fullmatch(item) for item in item_ids):
        raise ValueError('Invalid recovery item selection')
    wanted = list(dict.fromkeys(item_ids))
    with _db(root) as db:
        if db is None:
            raise ValueError('Unknown recovery history items')
        rows = db.execute('SELECT o.* FROM items i JOIN observations o ON o.seq=i.last_seq WHERE i.attempt_id=? AND i.item_id IN (' + ','.join('?' for _ in wanted) + ')', (attempt_id, *wanted)).fetchall()
        values = {row['item_id']: _metadata(row) for row in rows}
        if set(values) != set(wanted):
            raise ValueError('Unknown recovery history items')
        return [values[item] for item in wanted]


def _read_artifact(root, identity):
    path = _artifact_path(root, identity)
    if not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError('Recovery detail artifact is missing or exceeds its bound.')
    raw = path.read_bytes()
    if _sha(raw) != identity:
        raise ValueError('Recovery detail artifact hash mismatch.')
    return json.loads(raw)


def detail(attempt_id, item_id, *, root, observation_id=None):
    if not isinstance(item_id, str) or not _HEX.fullmatch(item_id):
        raise ValueError('Invalid recovery item identity')
    with _db(root) as db:
        row = None if db is None else db.execute('SELECT * FROM observations WHERE attempt_id=? AND item_id=?' + (' AND id=?' if observation_id else '') + ' ORDER BY seq DESC LIMIT 1',
            (attempt_id, item_id, observation_id) if observation_id else (attempt_id, item_id)).fetchone()
        if row is None:
            raise ValueError('Unknown recovery history item detail')
        metadata, refs = _metadata(row), json.loads(row['artifacts'])
    original, recovered, diff = (_read_artifact(root, refs[key]) for key in ('original', 'recovered', 'diff'))
    outcome = _read_artifact(root, refs['outcome'])['outcome'] if 'outcome' in refs else None
    return {**metadata, 'schema': SCHEMA, 'original': original['contexts'], 'recovered': recovered['contexts'],
            'diff': diff['diff'], 'source_bindings': {'original': original['source_binding'], 'recovered': recovered['source_binding']},
            'artifacts': refs, 'outcome': outcome}


def observations(attempt_id, item_id, *, root, limit=100, after=0):
    _bounds(limit, after)
    with _db(root) as db:
        rows = [] if db is None else db.execute('SELECT * FROM observations WHERE attempt_id=? AND item_id=? AND seq>? ORDER BY seq LIMIT ?', (attempt_id, item_id, after, limit)).fetchall()
        return {'items': [_metadata(row) for row in rows], 'next_cursor': rows[-1]['seq'] if rows else after, 'limit': limit}


def events(attempt_id, *, root, limit=100, after=0):
    _bounds(limit, after)
    with _db(root) as db:
        rows = [] if db is None else db.execute('SELECT * FROM events WHERE attempt_id=? AND seq>? ORDER BY seq LIMIT ?', (attempt_id, after, limit)).fetchall()
        return {'items': [{'cursor': row['seq'], 'kind': row['kind'], 'item_id': row['item_id'], 'created': row['created'], **json.loads(row['body'])} for row in rows],
                'next_cursor': rows[-1]['seq'] if rows else after, 'limit': limit}


def attempts(workflow_id, *, root, limit=100, offset=0):
    _bounds(limit, offset)
    with _db(root) as db:
        total = 0 if db is None else db.execute('SELECT count(*) FROM attempts WHERE workflow_id=?', (workflow_id,)).fetchone()[0]
        rows = [] if db is None else db.execute('SELECT a.*,s.* FROM attempts a JOIN attempt_state s ON a.id=s.attempt_id WHERE a.workflow_id=? ORDER BY a.created LIMIT ? OFFSET ?', (workflow_id, limit, offset)).fetchall()
        return {'items': [{key: value for key, value in _state(row).items() if key != 'owner'} for row in rows], 'total': total, 'limit': limit, 'offset': offset}
