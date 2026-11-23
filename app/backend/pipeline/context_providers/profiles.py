"""Dedicated provider preferences and protected keys, outside global chat config."""
import json
import pathlib
import re
import sqlite3

from .catalog import PROVIDERS, spec
from .credentials import DPAPIProtector


class ProfileStore:
    def __init__(self, root, *, protector=None):
        self.path = pathlib.Path(root) / 'profiles.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.protector = protector or DPAPIProtector()
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS preferences (id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL, concurrency INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO preferences VALUES (1,0,1)')
            db.execute('CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, payload TEXT NOT NULL, protected_key BLOB)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def _raw(self, provider_id):
        item = spec(provider_id)
        with self.connect() as db:
            row = db.execute('SELECT payload,protected_key FROM profiles WHERE id=?', (provider_id,)).fetchone()
        values = {'provider_id': provider_id, 'model': item['models'][0], 'enabled': False,
                  'free_account_confirmed': False, 'terms_confirmed': False, 'account_id': ''}
        if row:
            values.update(json.loads(row[0]))
        return values, row[1] if row else None

    def profile(self, provider_id):
        values, key = self._raw(provider_id)
        item = spec(provider_id)
        reason = None
        if not item['automation_eligible']:
            reason = item['reason']
        elif not values['enabled']:
            reason = 'Provider is disabled.'
        elif not values['free_account_confirmed']:
            reason = 'Confirm an account restricted to the free allowance without billing.'
        elif not values['terms_confirmed']:
            reason = 'Confirm this research use and model-output reuse are permitted.'
        elif values['model'] not in item['models']:
            reason = 'Selected model is not a verified free route.'
        elif item['requires_key'] and not key:
            reason = 'A protected API key is required.'
        elif provider_id == 'cloudflare_free' and not re.fullmatch(r'[a-fA-F0-9]{32}', values['account_id']):
            reason = 'A valid Workers Free account ID is required.'
        return {**values, 'has_key': bool(key), 'key_masked': '********' if key else None,
                'eligible': reason is None, 'blocked_reason': reason}

    def config(self):
        with self.connect() as db:
            enabled, concurrency = db.execute('SELECT enabled,concurrency FROM preferences WHERE id=1').fetchone()
        return {'enabled': bool(enabled), 'max_concurrency': concurrency,
                'profiles': [self.profile(pid) for pid in PROVIDERS]}

    def configure(self, **updates):
        if set(updates) - {'enabled', 'max_concurrency'}:
            raise ValueError('Unsupported explanation automation preference.')
        if 'enabled' in updates and type(updates['enabled']) is not bool:
            raise ValueError('enabled must be boolean.')
        if 'max_concurrency' in updates and (type(updates['max_concurrency']) is not int or not 1 <= updates['max_concurrency'] <= 2):
            raise ValueError('max_concurrency must be 1 or 2.')
        current = self.config()
        current.update(updates)
        with self.connect() as db:
            db.execute('UPDATE preferences SET enabled=?,concurrency=? WHERE id=1',
                       (int(current['enabled']), current['max_concurrency']))
        return self.config()

    def save_profile(self, provider_id, updates):
        item = spec(provider_id)
        if not isinstance(updates, dict) or set(updates) - {'model', 'enabled', 'free_account_confirmed', 'terms_confirmed', 'api_key', 'account_id'}:
            raise ValueError('Unsupported free provider profile fields.')
        for field in ('enabled', 'free_account_confirmed', 'terms_confirmed'):
            if field in updates and type(updates[field]) is not bool:
                raise ValueError(field + ' must be boolean.')
        if 'model' in updates and updates['model'] not in item['models']:
            raise ValueError('Selected model is not an allowlisted verified free route.')
        if 'account_id' in updates and (not isinstance(updates['account_id'], str) or
                (updates['account_id'] and not re.fullmatch(r'[a-fA-F0-9]{32}', updates['account_id']))):
            raise ValueError('Invalid provider account ID.')
        values, key = self._raw(provider_id)
        if 'api_key' in updates:
            value = updates['api_key']
            if not isinstance(value, str) or not value.strip() or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
                raise ValueError('API key must be nonempty printable text without whitespace.')
            key = self.protector.protect(value.encode('utf-8'))
        values.update({k: v for k, v in updates.items() if k != 'api_key'})
        with self.connect() as db:
            db.execute('INSERT INTO profiles VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,protected_key=excluded.protected_key',
                       (provider_id, json.dumps(values), key))
        return self.profile(provider_id)

    def delete_key(self, provider_id):
        spec(provider_id)
        with self.connect() as db:
            db.execute('UPDATE profiles SET protected_key=NULL WHERE id=?', (provider_id,))
        return self.profile(provider_id)

    def secret(self, provider_id):
        _, key = self._raw(provider_id)
        return self.protector.unprotect(key).decode('utf-8') if key else None
