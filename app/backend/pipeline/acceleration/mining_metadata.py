"""A bounded metadata index. Construction is disposable; completed source files stay immutable."""
import tempfile
import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .metadata import DiskMetadata
from .metrics import current


def build(ecosystems=None):
    import pyarrow.parquet as pq
    from app.backend.pipeline import store
    from app.backend.pipeline.db import store_mongo
    from app.backend.pipeline.ingest import codemine as cm
    root = cm.INTERIM / 'acceleration' / 'metadata'
    root.mkdir(parents=True, exist_ok=True)
    inputs = []
    for source in sorted(cm.INTERIM.glob('*.parquet')):
        if source.name == 'nvd.parquet' or source.name.startswith('osv_'):
            digest = hashlib.sha256()
            with source.open('rb') as stream:
                for block in iter(lambda: stream.read(1 << 20), b''):
                    digest.update(block)
            inputs.append([source.name, digest.hexdigest()])
    version = store.change_token()
    db = store_mongo.legacy.handle()
    identity = json.dumps([2, ecosystems, inputs, version.split(':')[0], db.name, str(db.path)])
    destination = root / (hashlib.sha256(identity.encode()).hexdigest() + '.sqlite')
    if destination.exists():
        index = None
        try:
            index = DiskMetadata(destination)
            if index.db.execute('PRAGMA user_version').fetchone()[0] != 2 or index.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise sqlite3.DatabaseError('Incomplete metadata cache')
            index.path, index.temporary = destination, False
            current.count('metadata_cache_hits')
            return index
        except sqlite3.DatabaseError:
            if index:
                index.close()
            destination.unlink(missing_ok=True)
    temp = tempfile.NamedTemporaryFile(prefix='metadata-', suffix='.sqlite', dir=root, delete=False)
    path = Path(temp.name)
    temp.close()
    index = DiskMetadata(path)
    index.path = path
    index.temporary = True
    try:
        slugs = {cm._slug(a.name) for a in cm._adapters()}
        if ecosystems is not None:
            slugs &= {cm._slug(e) for e in ecosystems}
        with current.timer('metadata_index'):
            for table in sorted(cm.INTERIM.glob('osv_*.parquet')):
                if table.stem[4:] not in slugs:
                    continue
                try:
                    source = pq.ParquetFile(table)
                    columns = [c for c in cm._ADV_COLS if c in source.schema.names]
                    for batch in source.iter_batches(batch_size=1000, columns=columns):
                        index.add('adv', batch.to_pylist())
                except (OSError, ValueError):
                    current.count('metadata_unreadable_tables')
            # Keep the public projection's semantics; no whole-store DataFrame is materialized.
            for rows in store_mongo.metadata_batches(ecosystems):
                index.add('adv', rows)
            nvd = cm.INTERIM / 'nvd.parquet'
            if nvd.exists():
                source = pq.ParquetFile(nvd)
                for batch in source.iter_batches(batch_size=1000, columns=[c for c in cm._NVD_COLS if c in source.schema.names]):
                    index.add('nvd', batch.to_pylist())
        if store.change_token().split(':')[0] == version.split(':')[0]:
            index.db.execute('PRAGMA user_version=2')
            index.db.commit()
            index.close()
            try:
                os.replace(path, destination)
                index = DiskMetadata(destination)
                index.path, index.temporary = destination, False
            except OSError:
                index = DiskMetadata(path)
                index.path, index.temporary = path, True
        return index
    except BaseException:
        index.close()
        path.unlink(missing_ok=True)
        raise


def close(index):
    index.close()
    if index.temporary:
        index.path.unlink(missing_ok=True)
