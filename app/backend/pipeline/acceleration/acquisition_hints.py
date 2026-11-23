"""Disposable, selected-attempt SHA identities. Hints never authorize mining work.

The five-field identity intentionally does not bind a source document's database
ID. A recreated identical snapshot can be prefetched, but actual tasks still use
and validate the authoritative pending selector and full source record.
"""
import hashlib
from itertools import islice
import json
import os
import re

from . import disk_budget, operations
from .pipeline_spool import encoded

_FIELDS = frozenset(('commit_key', 'host', 'owner', 'repo', 'sha'))
_SHA = re.compile(r'[0-9a-f]{40}\Z')
_FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z')
_SCHEMA_GROWTH = 128 << 10


def enabled():
    option = 'ZTSA_EXPERIMENTAL_SELECTION_HINTS'
    value = os.environ.get(option, '')
    if value not in ('', '8'):
        raise ValueError(option + ' must be unset, empty, or exactly 8')
    return value == '8'


def _identity(value):
    if not isinstance(value, dict) or len(value) != len(_FIELDS) or set(value) != _FIELDS:
        return None
    try:
        if any(not isinstance(item, str) or not item or len(item) > 2048 or len(item.encode('utf-8')) > 2048
               or any(char in item for char in ('\0', '\r', '\n', '\\')) for item in value.values()):
            return None
    except UnicodeEncodeError:
        return None
    if len(encoded(value)) > 4096 or not _SHA.fullmatch(value['sha']) or value['sha'] == '0' * 40:
        return None
    key = value['host'] + '/' + value['owner'] + '/' + value['repo'] + '@' + value['sha']
    return dict(value) if value['commit_key'] == key else None


def _check(fence):
    if operation := operations.current():
        operation.check()
    fence()
    if operation := operations.current():
        operation.check()


class SelectionHints:
    def __init__(self, spool, *, attempt, generation, implementation, selector_fingerprint,
                 selected_count, page_size=128):
        if not isinstance(attempt, str) or not attempt or len(attempt) > 512 or len(attempt.encode('utf-8')) > 512:
            raise ValueError('Invalid selection attempt')
        if type(generation) is not int or not 1 <= generation < (1 << 63):
            raise ValueError('Invalid selection generation')
        if type(selected_count) is not int or not 0 <= selected_count < (1 << 63):
            raise ValueError('Invalid selected count')
        if type(page_size) is not int or not 1 <= page_size <= 128:
            raise ValueError('Hint pages must contain between 1 and 128 identities')
        if any(not isinstance(value, str) or not _FINGERPRINT.fullmatch(value)
               for value in (implementation, selector_fingerprint)):
            raise ValueError('Invalid selection fingerprint')
        self.spool, self.page_size, self.selected_count = spool, page_size, selected_count
        self.selection_id = hashlib.sha256(encoded([spool.scope, attempt, generation,
            implementation, selector_fingerprint, selected_count])).hexdigest()
        disk_budget.check(_SCHEMA_GROWTH, fresh=True)
        with spool.lock, spool.db:
            spool.db.execute('BEGIN IMMEDIATE')
            epoch = self._epoch()
            spool.check(epoch)
            spool.db.execute('CREATE TABLE IF NOT EXISTS acquisition_hint_sets('
                'selection_id TEXT PRIMARY KEY,scope TEXT NOT NULL,attempt TEXT NOT NULL,generation INTEGER NOT NULL,'
                'implementation TEXT NOT NULL,selector TEXT NOT NULL,selected_count INTEGER NOT NULL,'
                'consumed INTEGER NOT NULL DEFAULT 0,chain TEXT NOT NULL,complete INTEGER NOT NULL DEFAULT 0)')
            spool.db.execute('CREATE TABLE IF NOT EXISTS acquisition_hints('
                'selection_id TEXT NOT NULL,position INTEGER NOT NULL,commit_key TEXT NOT NULL,host TEXT NOT NULL,'
                'owner TEXT NOT NULL,repo TEXT NOT NULL,sha TEXT NOT NULL,offered INTEGER NOT NULL DEFAULT 0,'
                'PRIMARY KEY(selection_id,position),UNIQUE(selection_id,commit_key)) WITHOUT ROWID')
            spool.db.execute('CREATE INDEX IF NOT EXISTS acquisition_hints_offer '
                'ON acquisition_hints(selection_id,host,owner,repo,offered,position)')
            spool.db.execute('INSERT OR IGNORE INTO acquisition_hint_sets'
                '(selection_id,scope,attempt,generation,implementation,selector,selected_count,chain) '
                'VALUES(?,?,?,?,?,?,?,?)', (self.selection_id, spool.scope, attempt, generation,
                    implementation, selector_fingerprint, selected_count, hashlib.sha256().hexdigest()))
            spool.check(epoch)

    def _epoch(self):
        return self.spool.db.execute('SELECT epoch FROM control WHERE id=?', (self.spool.scope,)).fetchone()[0]

    def _state(self):
        return self.spool.db.execute('SELECT consumed,chain,complete FROM acquisition_hint_sets '
            'WHERE selection_id=?', (self.selection_id,)).fetchone()

    def _page(self, rows, consumed, chain, complete, previous, epoch, fence):
        _check(fence)
        growth = _SCHEMA_GROWTH + sum(len(encoded(row)) + 1024 for _, row in rows) * 4
        disk_budget.check(growth, fresh=True)
        with self.spool.lock, self.spool.db:
            self.spool.db.execute('BEGIN IMMEDIATE')
            self.spool.check(epoch)
            state = self._state()
            if state['consumed'] != previous or state['complete']:
                raise ValueError('Concurrent or completed selection population')
            for position, row in rows:
                self.spool.db.execute('INSERT OR IGNORE INTO acquisition_hints'
                    '(selection_id,position,commit_key,host,owner,repo,sha) VALUES(?,?,?,?,?,?,?)',
                    (self.selection_id, position, row['commit_key'], row['host'], row['owner'], row['repo'], row['sha']))
            self.spool.db.execute('UPDATE acquisition_hint_sets SET consumed=?,chain=?,complete=? '
                'WHERE selection_id=?', (consumed, chain, int(complete), self.selection_id))
            self.spool.check(epoch)
        _check(fence)

    def populate(self, identity_stream, *, fence):
        _check(fence)
        with self.spool.lock:
            epoch = self._epoch()
            state = self._state()
            previous, expected_chain, was_complete = state['consumed'], state['chain'], bool(state['complete'])
        source = iter(identity_stream)
        chain = hashlib.sha256()
        consumed = 0
        written = previous
        exhausted = False
        while consumed < self.selected_count:
            _check(fence)
            page = []
            for _ in range(min(self.page_size, self.selected_count - consumed)):
                try:
                    item = next(source)
                except StopIteration:
                    exhausted = True
                    break
                value = _identity(item)
                position = consumed
                consumed += 1
                chain.update(encoded([position, value]))
                if consumed == previous and chain.hexdigest() != expected_chain:
                    raise ValueError('Selected identity prefix changed')
                if position >= previous and value is not None:
                    page.append((position, value))
            _check(fence)
            if consumed > previous:
                if was_complete:
                    raise ValueError('Completed selection identity stream changed')
                complete = exhausted or consumed == self.selected_count
                self._page(page, consumed, chain.hexdigest(), complete, written, epoch, fence)
                written = consumed
                if complete:
                    return
            if exhausted:
                break
        if consumed < previous:
            raise ValueError('Selected identity prefix was truncated')
        with self.spool.lock:
            self.spool.check(epoch)
        if not was_complete:
            self._page([], consumed, chain.hexdigest(), True, written, epoch, fence)
        _check(fence)

    def take(self, repo, current_window_keys, limit, *, epoch, validate, fence):
        if type(limit) is not int or not 0 <= limit <= 8:
            raise ValueError('Hint group capacity must be between zero and eight')
        if not isinstance(repo, (tuple, list)) or len(repo) != 3 or any(
                not isinstance(part, str) or not part or len(part) > 2048 or len(part.encode('utf-8')) > 2048 for part in repo):
            raise ValueError('Invalid hint repository identity')
        keys = list(islice(iter(current_window_keys), 257))
        if len(keys) > 256 or any(not isinstance(key, str) or len(key) > 4096 or len(key.encode('utf-8')) > 4096 for key in keys):
            raise ValueError('Invalid current window identity bounds')
        _check(fence)
        if not limit:
            return []
        statement = ('SELECT position,commit_key,host,owner,repo,sha FROM acquisition_hints '
            'WHERE selection_id=? AND host=? AND owner=? AND repo=? AND offered=0 '
            'AND commit_key NOT IN (SELECT value FROM json_each(?)) ORDER BY position LIMIT ?')
        args = (self.selection_id, *repo, json.dumps(keys, separators=(',', ':')), limit)
        with self.spool.lock:
            self.spool.check(epoch)
            if not self._state()['complete']:
                return []
            found = self.spool.db.execute(statement, args).fetchall()
        if not found:
            return []
        disk_budget.check(_SCHEMA_GROWTH, fresh=True)
        _check(fence)
        with self.spool.lock, self.spool.db:
            self.spool.db.execute('BEGIN IMMEDIATE')
            self.spool.check(epoch)
            found = self.spool.db.execute(statement, args).fetchall()
            for row in found:
                self.spool.db.execute('UPDATE acquisition_hints SET offered=1 WHERE selection_id=? AND position=?',
                    (self.selection_id, row['position']))
            self.spool.check(epoch)
        if not found:
            return []
        candidates = [{key: row[key] for key in _FIELDS} for row in found]
        _check(fence)
        accepted = validate([dict(row) for row in candidates])
        if not isinstance(accepted, (list, tuple)) or len(accepted) > limit:
            raise ValueError('Authority validation must return a bounded identity list')
        _check(fence)
        with self.spool.lock:
            self.spool.check(epoch)
        return [row for row in candidates if any(row == item for item in accepted)]

    def snapshot(self):
        with self.spool.lock:
            state = self._state()
            row = self.spool.db.execute('SELECT count(*),coalesce(sum(offered),0) FROM acquisition_hints '
                'WHERE selection_id=?', (self.selection_id,)).fetchone()
            return {'selection_id': self.selection_id, 'complete': bool(state['complete']),
                    'identities': row[0], 'offered': row[1], 'consumed': state['consumed']}