"""Stage finished Drive artifacts on runtime disk before an explicit download."""
from __future__ import annotations
import hashlib
import os
import pathlib

CHUNK = 8 << 20


def digest(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''): h.update(block)
    return h.hexdigest()


def stage(source, directory, *, progress=lambda done, total: None):
    source, directory = pathlib.Path(source).resolve(), pathlib.Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / source.name
    if source == target:
        return {'path': target, 'bytes': target.stat().st_size, 'sha256': digest(target)}
    original = source.stat(); partial = target.with_suffix(target.suffix + '.partial')
    h = hashlib.sha256(); copied = 0
    try:
        with source.open('rb') as incoming, partial.open('wb') as outgoing:
            for block in iter(lambda: incoming.read(CHUNK), b''):
                outgoing.write(block); h.update(block); copied += len(block); progress(copied, original.st_size)
        current = source.stat()
        if copied != original.st_size or current.st_size != original.st_size or current.st_mtime_ns != original.st_mtime_ns:
            raise RuntimeError('The artifact changed while staging. Wait for writing to finish, then retry.')
        if digest(partial) != h.hexdigest():
            raise RuntimeError('The staged artifact failed SHA256 verification. Retry the copy.')
        os.replace(partial, target)
        return {'path': target, 'bytes': copied, 'sha256': h.hexdigest()}
    finally:
        partial.unlink(missing_ok=True)


def download(path):
    """Called explicitly for one local file, never automatically after training."""
    from google.colab import files
    files.download(str(path))
