"""Atomic file writes: a reader or a restart sees the old file or the complete new one, never half of it.

Every writer here puts the data in a temporary file in the same folder, flushes it to disk, and replaces the target
in one step. If anything fails before the replace, the old file is untouched and the temporary file is removed.
On Windows a file that antivirus or an indexer holds open briefly refuses the replace with ``PermissionError``;
the replace is retried with a growing pause before the error is given up.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
import uuid
from typing import Any, Callable

RETRIES = 40
PAUSE = 0.05

FileIdentity = tuple[str, int, int, int, int, int]


def file_identity(path: str | os.PathLike) -> FileIdentity:
    """Identify a file revision without including its access time."""
    resolved = pathlib.Path(path).resolve()
    stat = resolved.stat()
    return (str(resolved), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino)


def _retry(action: Callable[[], Any]) -> Any:
    for attempt in range(RETRIES):
        try:
            return action()
        except PermissionError:
            if attempt == RETRIES - 1:
                raise
            time.sleep(PAUSE * min(attempt + 1, 6))


def part_path(path: str | os.PathLike) -> pathlib.Path:
    """The temporary file next to ``path`` (same folder, so the replace is a rename on one volume)."""
    p = pathlib.Path(path)
    return p.with_name(f".{p.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.part")


def replace(part: str | os.PathLike, path: str | os.PathLike) -> None:
    """Replace ``path`` with the finished ``part`` file, retrying a locked target."""
    _retry(lambda: os.replace(part, path))


def write_with(path: str | os.PathLike, writer: Callable[[pathlib.Path], None]) -> pathlib.Path:
    """Run ``writer(part_path)`` and replace ``path`` with the result; the part file is removed on any failure."""
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = part_path(target)
    try:
        writer(part)
        _fsync(part)
        replace(part, target)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass  # ignored: the part file may never have been created
        raise
    return target


def _fsync(path: pathlib.Path) -> bool:
    """Flush the data of a finished file to the disk; answers whether it did. On Windows ``os.fsync`` fails on a read-only handle ("Bad file descriptor"), which is what this used to open, and the
    error was swallowed: nothing was flushed, and after a power cut the renamed file could be full of zeros. The file is opened for writing first (no byte is written); a file that only
    opens read-only is tried that way, and a failure is reported as False, never raised: the data was written, durability is best effort."""
    base = getattr(os, "O_BINARY", 0)
    for flags in (os.O_RDWR | base, os.O_RDONLY | base):
        try:
            fd = os.open(path, flags)
        except OSError:
            continue
        try:
            os.fsync(fd)
            return True
        except OSError:
            continue                                  # ignored: this handle cannot be flushed, the other kind is tried
        finally:
            os.close(fd)
    return False


def fsync_file(path: str | os.PathLike) -> bool:
    """Flush a finished file (one a library wrote by name) to the disk; False when the file system refuses."""
    return _fsync(pathlib.Path(path))


def fsync_handle(fh: Any) -> bool:
    """Flush an open file the program is writing (a download, a part): its buffers, then the disk. False when the file system refuses."""
    try:
        fh.flush()
        os.fsync(fh.fileno())
        return True
    except (OSError, ValueError):
        return False


def _create(part: pathlib.Path, data: bytes, mode: int | None) -> None:
    if mode is None:
        part.write_bytes(data)
        return
    # created with the permissions already in place, so a secret is never readable by others, not even briefly
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def write_bytes(path: str | os.PathLike, data: bytes, *, mode: int | None = None) -> pathlib.Path:
    """``mode`` (for example 0o600 for a file that holds keys) is applied when the part file is created; on Windows only the
    read-only bit of a mode has an effect."""
    return write_with(path, lambda part: _create(part, data, mode))


def write_text(path: str | os.PathLike, text: str, encoding: str = "utf-8", *, mode: int | None = None) -> pathlib.Path:
    return write_bytes(path, text.encode(encoding), mode=mode)


def write_json(path: str | os.PathLike, obj: Any, *, mode: int | None = None, **dumps: Any) -> pathlib.Path:
    """``json.dumps(obj, **dumps)`` written atomically; the text is built first so a serialisation error writes nothing."""
    return write_text(path, json.dumps(obj, **dumps), mode=mode)


def write_parquet(path: str | os.PathLike, df: Any, **kwargs: Any) -> pathlib.Path:
    """``df.to_parquet`` written atomically (index=False by default)."""
    kwargs.setdefault("index", False)
    return write_with(path, lambda part: df.to_parquet(part, **kwargs))
