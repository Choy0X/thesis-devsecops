"""Read-only access to the local mining store for the grounded corpus build.

The store is the authority for everything mining captured (commit messages, commit files, full file text, attribution, link evidence).
This reader opens it immutable and read-only: it can never create, journal or modify a database file.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from app.backend.pipeline.db import codec


def table_name(collection, prefix='d_'):
    return prefix + hashlib.sha256(collection.encode('utf-8')).hexdigest()[:24]


class StoreReader:
    def __init__(self, connection):
        self._con = connection

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self._con.close()

    def count(self, collection):
        return self._con.execute(f'SELECT count(*) FROM "{table_name(collection)}"').fetchone()[0]

    def docs(self, collection, *, batch=2000):
        """Every document of a collection, streamed in storage order."""
        cursor = self._con.execute(f'SELECT json(payload),types FROM "{table_name(collection)}"')
        while True:
            rows = cursor.fetchmany(batch)
            if not rows:
                return
            for payload, types in rows:
                yield codec.decode_document(payload, types, allow_missing_types=True)

    def get(self, collection, doc_id):
        row = self._con.execute(f'SELECT json(payload),types FROM "{table_name(collection)}" WHERE id_key=?',
                                (codec.id_key(doc_id),)).fetchone()
        return None if row is None else codec.decode_document(row[0], row[1], allow_missing_types=True)


def open_store(path):
    """Open the store file read-only and immutable. A missing file is an error, never an empty new database."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f'mining store not found: {path}')
    uri = path.resolve().as_uri() + '?mode=ro&immutable=1'
    return StoreReader(sqlite3.connect(uri, uri=True))
