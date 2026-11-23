"""Read-only full disk recount worker. Protocol and imports remain stdlib only."""
import json
import os
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext

from .disk_budget import count_used

MAX_MESSAGE = 64 << 10


def _read():
    raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
    if not raw:
        raise EOFError
    if len(raw) > MAX_MESSAGE or not raw.endswith(b'\n'):
        raise ValueError('Disk scan descriptor exceeds its bounded frame')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Invalid disk scan descriptor')
    return value


def _reply(value):
    raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode() + b'\n'
    if len(raw) > MAX_MESSAGE:
        raise ValueError('Disk scan reply exceeds its bounded frame')
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def _offline(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'subprocess.Popen', 'os.system'):
        raise PermissionError('Disk scanners cannot use network or subprocesses')


def main():
    sys.addaudithook(_offline)
    config = _read()
    root, baselines = config.get('root'), config.get('baselines')
    binding = config.get('binding')
    threads = config.get('threads', 1)
    if (config.get('operation') != 'initialize' or config.get('id') != 0 or
        not isinstance(root, str) or not Path(root).is_absolute() or
        not isinstance(binding, str) or len(binding) != 32 or
        type(threads) is not int or threads not in (1, 4) or
        not isinstance(baselines, dict) or any(not isinstance(path, str) or
            not Path(path).is_absolute() or type(size) is not int or size < 0
            for path, size in baselines.items())):
        raise ValueError('Invalid disk scan binding')
    with ThreadPoolExecutor(4) if threads == 4 else nullcontext() as executor:
        _reply({'id': 0, 'binding': binding, 'ready': True})
        _serve(root, baselines, binding, executor)


def _serve(root, baselines, binding, executor):
    previous = 0
    while True:
        try:
            request = _read()
        except EOFError:
            return
        identifier = request.get('id')
        if (request.get('operation') != 'scan' or request.get('binding') != binding or
            type(identifier) is not int or identifier <= previous):
            raise ValueError('Invalid disk scan request identity')
        previous = identifier
        try:
            if executor is None:
                value = count_used(Path(root), baselines)
            else:
                from .parallel_disk import count_used as parallel_count
                value = parallel_count(Path(root), baselines, executor)
        except Exception as error:
            _reply({'id': identifier, 'binding': binding, 'error': {
                'type': type(error).__name__, 'message': str(error)[:1000]}})
        else:
            _reply({'id': identifier, 'binding': binding, 'bytes': value})


if __name__ == '__main__':
    main()
