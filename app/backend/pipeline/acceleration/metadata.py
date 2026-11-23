"""Disk-backed advisory lookups with bounded SQLite memory and deterministic winners."""
import json
import math
import sqlite3
import threading


def clean(value):
    if hasattr(value, 'item'):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


class Lookup:
    def __init__(self, owner, kind):
        self.owner, self.kind = owner, kind

    def __getitem__(self, key):
        with self.owner.lock:
            row = self.owner.db.execute('SELECT value FROM metadata WHERE kind=? AND key=?', (self.kind, str(key))).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        return self.get(key) is not None

    def __len__(self):
        with self.owner.lock:
            return self.owner.db.execute('SELECT COUNT(*) FROM metadata WHERE kind=?', (self.kind,)).fetchone()[0]


class DiskMetadata:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        from .memory_profile import current as memory_profile
        self.db.execute(f'PRAGMA cache_size=-{memory_profile().sqlite_cache_kib}')
        self.db.execute('PRAGMA temp_store=FILE')
        self.db.execute('CREATE TABLE IF NOT EXISTS metadata (kind TEXT, key TEXT, value TEXT, PRIMARY KEY(kind,key)) WITHOUT ROWID')
        self.adv, self.by_cve, self.nvd = (Lookup(self, k) for k in ('adv', 'by_cve', 'nvd'))

    def add(self, kind, rows):
        with self.lock, self.db:
            for row in rows:
                row = clean(row)
                key = row.get('osv_id' if kind == 'adv' else 'cve_id')
                if not key:
                    continue
                value = json.dumps(row, default=str)
                cursor = self.db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?,?)' if kind == 'adv'
                                        else 'INSERT OR REPLACE INTO metadata VALUES (?,?,?)', (kind, str(key), value))
                if kind == 'adv' and cursor.rowcount and row.get('cve_id'):
                    self.db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?,?)', ('by_cve', str(row['cve_id']), value))

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
