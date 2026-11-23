"""Private CPU publication worker. Its pipe contains descriptors and receipts only."""
from collections import Counter
from contextlib import redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

MAX_MESSAGE = 64 << 10


def _read():
    raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
    if not raw or len(raw) > MAX_MESSAGE:
        raise EOFError('Publication descriptor missing or exceeds 64 KiB')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Invalid publication descriptor')
    return value


def _reply(value):
    raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode() + b'\n'
    if len(raw) > MAX_MESSAGE:
        raise ValueError('Publication response exceeds 64 KiB')
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def _offline(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'subprocess.Popen', 'os.system'):
        raise PermissionError('Publication workers cannot use network or subprocesses')


def _error(error):
    value = {'type': type(error).__name__, 'message': str(error)[:1000]}
    if isinstance(error, sqlite3.Error):
        for key in ('sqlite_errorcode', 'sqlite_errorname'):
            if hasattr(error, key):
                value[key] = getattr(error, key)
    if getattr(error, 'reason', None) is not None:
        value['reason'] = error.reason
    return {'error': value}


class QueueBinding:
    def __init__(self, root, scope):
        self.root, self.scope = Path(root).resolve(), scope
        queue = self.root / 'queue.sqlite'
        if queue.is_symlink() or not queue.is_file():
            raise ValueError('Invalid publication queue binding')
        self.db = sqlite3.connect(queue.as_uri() + '?mode=ro', uri=True, timeout=1, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA query_only=ON')

    def check(self, request):
        from .pipeline_spool import StaleAttempt, encoded
        epoch = self.db.execute('SELECT epoch FROM control WHERE id=?', (self.scope,)).fetchone()
        if epoch is None or epoch['epoch'] != request['epoch']:
            raise StaleAttempt('Mining attempt has been superseded')
        task = self.db.execute('SELECT scope,input,next,status FROM tasks WHERE id=?', (request['task'],)).fetchone()
        if task is None or task['scope'] != self.scope or task['next'] != 3 or task['status'] != 'running':
            raise StaleAttempt('Publication task is no longer running at its publish stage')
        row = json.loads(task['input'])
        if hashlib.sha256(encoded(row)).hexdigest() != request['input_sha256']:
            raise ValueError('Publication input binding changed')
        expected = hashlib.sha256(('mining-publication/3:' + request['task']).encode()).hexdigest()
        if expected != request['publication_id']:
            raise ValueError('Publication receipt identity does not match its task')
        output = self.db.execute('SELECT hash,size FROM outputs WHERE task=? AND stage=2', (request['task'],)).fetchone()
        if output is None or output['hash'] != request['context_sha256']:
            raise ValueError('Publication context binding changed')
        digest = output['hash']
        if len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest):
            raise ValueError('Invalid publication artifact fingerprint')
        path = self.root / 'artifacts' / digest[:2] / (digest + '.json')
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError('Publication artifact escaped its queue root')
        return row, path, output['size']


def _initialize(config):
    if config.get('operation') != 'initialize':
        raise ValueError('Publication worker initialization required')
    database_path = Path(config['database_path']).resolve()
    database_name = config['database_name']
    root = Path(config['spool_root']).resolve()
    if database_path.name != database_name + '.sqlite3' or database_path.is_symlink() or not database_path.is_file():
        raise ValueError('Publication database binding does not match its application name')
    os.environ.update(ZTSA_DATABASE_DIR=str(database_path.parent), ZTSA_DATABASE_NAME=database_name,
        ZTSA_SETTINGS_PATH=str(root / 'publication-unused-settings.json'), ZTSA_GATEWAY='0',
        ZTSA_INTERIM_DIR=str(Path(config['interim_root']).resolve()), ZTSA_COMPUTE_GPU='0', CUDA_VISIBLE_DEVICES='-1')
    if config.get('campaign_id'):
        os.environ['ZTSA_CAMPAIGN_ID'] = config['campaign_id']
    else:
        os.environ.pop('ZTSA_CAMPAIGN_ID', None)
    sys.addaudithook(_offline)
    from app.backend.pipeline.db import legacy
    from .publication import Publisher
    database = legacy.handle()
    if database.path != database_path:
        raise ValueError('Publication worker selected a different database')
    publisher = Publisher.borrowed(database, config['scope'], config['attempt'], config['generation'])
    return publisher, QueueBinding(root, config['scope'])


def _publish(publisher, binding, request):
    from . import operations
    from .staged_mining import decode_result
    from app.backend.pipeline.ingest import codemine as cm
    operation = operations.Operation(request['task'], 1)
    operation.deadline = float(request['deadline'])
    try:
        with operations.use(operation):
            operation.check()
            row, path, size = binding.check(request)
            if path.stat().st_size != size:
                raise ValueError('Publication artifact checksum size mismatch')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != request['context_sha256']:
                raise ValueError('Publication artifact checksum mismatch')
            document = json.loads(raw)
            del raw
            operation.check()
            result = decode_result(document, SimpleNamespace(s=None))
            delta = Counter()
            def check():
                operation.check()
                binding.check(request)
            def write():
                delta.clear()
                cm._store_result(row, result, delta, row['commit_key'], log=lambda *args: None)
            receipt = publisher.publish(row['commit_key'], request['publication_id'], write, check=check)
            return {'receipt': receipt, 'delta': dict(delta), 'pid': os.getpid()}
    finally:
        operation.finish()


def _publish_batch(publisher, binding, request):
    """Decode one result at a time and acknowledge only a completed FULL commit."""
    from . import operations
    from .publication import Publication
    from .staged_mining import decode_result
    from app.backend.pipeline.ingest import codemine as cm
    items = request.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= 8:
        raise ValueError('Publication batch must contain 1 to 8 descriptors')
    if any(not isinstance(item, dict) for item in items):
        raise ValueError('Invalid publication batch descriptor')
    if len({item.get('task') for item in items}) != len(items):
        raise ValueError('Duplicate publication batch task')
    epochs = {item.get('epoch') for item in items}
    if len(epochs) != 1:
        raise ValueError('Publication batch must use one attempt epoch')
    operations_list = []
    deltas = []
    entries = []
    def prepare(item):
        operation = operations.Operation(item['task'], 1)
        operation.deadline = float(item['deadline'])
        operations_list.append(operation)
        delta = Counter()
        deltas.append(delta)
        with operations.use(operation):
            operation.check()
            row, path, size = binding.check(item)
        def check():
            with operations.use(operation):
                operation.check()
                binding.check(item)
        def write():
            # Existing receipt reuse invokes no decode and produces no new counts.
            delta.clear()
            with operations.use(operation):
                operation.check()
                if path.stat().st_size != size:
                    raise ValueError('Publication artifact checksum size mismatch')
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != item['context_sha256']:
                    raise ValueError('Publication artifact checksum mismatch')
                document = json.loads(raw)
                del raw
                result = decode_result(document, SimpleNamespace(s=None))
                operation.check()
                cm._store_result(row, result, delta, row['commit_key'], log=lambda *args: None)
        return Publication(row['commit_key'], item['publication_id'], write, check), size
    input_limit_exceeded = False
    try:
        try:
            sizes = []
            for item in items:
                entry, size = prepare(item)
                entries.append(entry)
                sizes.append(size)
            input_limit_exceeded = len(items) > 1 and sum(sizes) > 32 << 20
            if input_limit_exceeded:
                raise ValueError('Publication batch exceeds 32 MiB input allowance')
            receipts = publisher.publish_many(entries)
            return {'results': [{'task': item['task'], 'receipt': receipt, 'delta': dict(delta)}
                    for item, receipt, delta in zip(items, receipts, deltas)], 'pid': os.getpid()}
        except (ValueError, TypeError, sqlite3.IntegrityError):
            if input_limit_exceeded:
                raise
            # The outer transaction has rolled back. Isolate data failures using
            # the original single-item path, in the same dispatch order. Control,
            # ownership, memory, disk, and transport failures never take this path.
            results = []
            for item in items:
                try:
                    result = _publish(publisher, binding, item)
                except (ValueError, TypeError, sqlite3.IntegrityError) as error:
                    result = _error(error)
                results.append({'task': item['task'], **result})
            return {'results': results, 'pid': os.getpid()}
    finally:
        for operation in operations_list:
            operation.finish()


def main():
    publisher = binding = None
    try:
        config = _read()
        with redirect_stdout(sys.stderr):
            publisher, binding = _initialize(config)
        _reply({'ready': True, 'pid': os.getpid(), 'device': 'cpu',
                'gpu_imported': 'torch' in sys.modules, 'network_disabled': True,
                'interim_root': os.environ['ZTSA_INTERIM_DIR']})
        while True:
            try:
                request = _read()
            except EOFError:
                break
            if request.get('operation') not in ('publish', 'publish_batch'):
                _reply(_error(ValueError('Unsupported publication worker operation')))
                continue
            try:
                with redirect_stdout(sys.stderr):
                    result = (_publish_batch(publisher, binding, request) if request['operation'] == 'publish_batch'
                              else _publish(publisher, binding, request))
                _reply(result)
            except Exception as error:
                _reply(_error(error))
    except Exception as error:
        _reply(_error(error))
    finally:
        if binding is not None:
            binding.db.close()
        if publisher is not None:
            publisher.db.close()


if __name__ == '__main__':
    main()
