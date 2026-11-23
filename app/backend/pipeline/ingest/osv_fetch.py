"""A download that continues where it stopped.

The bytes go to ``<dump>.download`` (not to a random temporary name) with a small sidecar, ``<dump>.download.json``, that says what is being fetched: the address, OSV's ETag, the total size,
the checksum OSV publishes, and ``durable_bytes``, how much of the file is known to be on the disk. Every few megabytes (or seconds) the file is flushed and the sidecar rewritten, so after a cut the
next call continues from ``durable_bytes`` (a file can be longer than that after a power cut, with a tail of zeros that was never written) and asks OSV for only the rest: ``Range: bytes=N-`` with
``If-Range: <the ETag, quoted as OSV sent it>`` (an unquoted validator is ignored by the server) and ``Accept-Encoding: identity`` (so the offsets are those of the file, not of a compressed stream).

What the answer means: 206 with a ``Content-Range`` that starts at N, the same total and the same ETag is appended; 200 means the file changed or Range was ignored, so it starts again; 416 means
nothing is left to send (finalised when the file is complete) or the partial does not fit (started again). The finished file is checked (its size, and the MD5 OSV publishes when there is one),
renamed over the dump, and only then are the files that go with a dump written (``record``) and the sidecar removed; ``recover`` completes that tail when a cut fell inside it. The previous dump is
untouched until the rename. One lock per dump (``DownloadBusy``) keeps two processes from writing one partial.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import re
import time
from typing import Callable

import requests

from app.backend.pipeline import fsio, oslock, util

FLUSH_BYTES = 16 * 1024 * 1024                # the file is flushed and the sidecar rewritten after this many bytes ...
FLUSH_SECONDS = 5.0                           # ... or this many seconds
CHUNK = 1 << 20


class DownloadBusy(oslock.Busy):
    """Another process is downloading the same dump right now."""


def partial_path(out: pathlib.Path) -> pathlib.Path:
    return out.with_name(out.name + ".download")


def sidecar_path(out: pathlib.Path) -> pathlib.Path:
    return out.with_name(out.name + ".download.json")


def lock_path(out: pathlib.Path) -> pathlib.Path:
    return out.with_name(out.stem + ".download.lock")


def _read_sidecar(out: pathlib.Path) -> dict | None:
    try:
        side = json.loads(sidecar_path(out).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return side if isinstance(side, dict) and "url" in side else None


def _write_sidecar(out: pathlib.Path, side: dict) -> None:
    side["updated_at"] = util.now_z()
    fsio.write_json(sidecar_path(out), side)


def _strong(etag: str | None) -> bool:
    return bool(etag) and etag.startswith('"') and etag.endswith('"')


def _md5_hex(headers) -> str | None:
    """The MD5 OSV publishes for the object, as hex: from ``x-goog-hash: crc32c=..., md5=...``, else from an ETag that is itself a hex MD5."""
    value = headers.get("x-goog-hash") or headers.get("X-Goog-Hash") or ""
    m = re.search(r"md5=([A-Za-z0-9+/=]+)", value)
    if m:
        try:
            return base64.b64decode(m.group(1)).hex()
        except ValueError:
            return None
    m = re.fullmatch(r'"([0-9a-f]{32})"', (headers.get("ETag") or "").strip())
    return m.group(1) if m else None


def _md5_of(path: pathlib.Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def peek(out: str | os.PathLike) -> dict | None:
    """What a stopped download has fetched, cheaply: ``{"bytes", "total", "percent", "update", "active", "updated_at"}`` (``update``: a complete dump is already here and this fetches a newer copy), or None."""
    out = pathlib.Path(out)
    side = _read_sidecar(out)
    part = partial_path(out)
    if side is None or not part.is_file():
        return None
    have = min(int(side.get("durable_bytes") or 0), part.stat().st_size)
    total = side.get("total")
    percent = min(99, int(100 * have / total)) if total else None
    return {"bytes": have, "total": total, "percent": percent, "update": out.exists(), "active": oslock.held_elsewhere(lock_path(out)), "updated_at": side.get("updated_at")}


def discard(out: str | os.PathLike) -> None:
    """Throw away a stopped download (Start over). Raises DownloadBusy while another process is downloading it."""
    out = pathlib.Path(out)
    lock = oslock.FileLock(lock_path(out))
    try:
        lock.acquire(wait=3.0)
    except oslock.Busy as e:
        raise DownloadBusy(e.path, e.pid) from None
    try:
        _drop(out)
    finally:
        lock.release()


def _drop(out: pathlib.Path) -> None:
    for f in (partial_path(out), sidecar_path(out)):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            raise OSError(f"The earlier download in {f.name} could not be removed ({e}). Close whatever holds it and try again.") from e


def _verify(partial: pathlib.Path, side: dict) -> None:
    total = side.get("total")
    if total and partial.stat().st_size != total:
        raise OSError(f"The download is {partial.stat().st_size} bytes but OSV says {total}.")
    md5 = side.get("md5")
    if md5 and _md5_of(partial) != md5:
        raise OSError("The download does not match the checksum OSV published for it.")


def _verify_or_drop(out: pathlib.Path, part: pathlib.Path, side: dict) -> None:
    """Check a finished file; a file that does not match is discarded at once (it would fail the same way every time it was continued), and the error says so."""
    try:
        _verify(part, side)
    except OSError as e:
        _drop(out)
        raise OSError(f"{e} It was discarded; try again.") from None


def _finish(out: pathlib.Path, side: dict, record: Callable[[dict], None] | None) -> dict:
    """The tail of a download, in an order that a cut cannot break: the file over the dump, then the files that go with a dump, then the sidecar."""
    part = partial_path(out)
    if part.is_file():
        side["finalizing"] = True                  # durable, before the rename: it is what tells a later call that a dump without a partial is this download's, not an older copy
        _write_sidecar(out, side)
        fsio.replace(part, out)
    if record:
        record(side)
    try:
        sidecar_path(out).unlink()
    except OSError:
        pass                                      # ignored: a sidecar without a partial is recognised and removed by the next call
    return side


def recover(out: str | os.PathLike, record: Callable[[dict], None] | None = None) -> bool:
    """Complete what a cut interrupted: a partial that is complete is verified and put in place without any request; a dump that was put in place but whose companion files were not written gets them.
    Answers whether it completed a download. A sidecar that explains nothing is removed."""
    out = pathlib.Path(out)
    side = _read_sidecar(out)
    if side is None:
        return False
    part = partial_path(out)
    lock = oslock.FileLock(lock_path(out))
    try:
        lock.acquire(wait=0.5)
    except oslock.Busy:
        return False                              # somebody is downloading it right now
    try:
        if not part.is_file():
            if out.exists() and side.get("finalizing"):
                _finish(out, side, record)         # the rename happened, the rest did not
                return True
            sidecar_path(out).unlink(missing_ok=True)      # a sidecar that explains nothing (the cut came before the first byte)
            return False
        total = side.get("total")
        if side.get("finalizing") or (total and part.stat().st_size == total and int(side.get("durable_bytes") or 0) >= total):
            try:
                _verify(part, side)
            except OSError:
                _drop(out)
                return False
            _finish(out, side, record)
            return True
        return False
    finally:
        lock.release()


def _content_range(value: str | None) -> tuple[int | None, int | None, int | None]:
    m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", (value or "").strip())
    if m:
        return int(m.group(1)), int(m.group(2)), None if m.group(3) == "*" else int(m.group(3))
    m = re.fullmatch(r"bytes \*/(\d+)", (value or "").strip())
    return (None, None, int(m.group(1))) if m else (None, None, None)


def fetch(url: str, out: str | os.PathLike, *, if_none_match: str | None = None, progress: Callable[[int, int | None], None] | None = None,
          on_resume: Callable[[int, int | None], None] | None = None, fresh: bool = False, record: Callable[[dict], None] | None = None, timeout: int = 600) -> dict:
    """Download ``url`` to ``out``, continuing an earlier attempt when there is one. Returns ``{"outcome": "downloaded" | "unchanged", ...}`` where, for "downloaded", the keys are those of the sidecar
    (``etag``, ``last_modified``, ``total``, ``md5``, ``bytes``). ``if_none_match`` asks whether the dump already here is still current (a 304 is "unchanged"); it is not sent when a partial is continued.
    ``progress(done_bytes, total_bytes_or_None)`` is called about twice a second; ``on_resume(offset, total)`` once when a partial is continued. ``record(sidecar)`` writes the files that go with a dump."""
    out = pathlib.Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lock = oslock.FileLock(lock_path(out))
    try:
        lock.acquire(wait=3.0)
    except oslock.Busy as e:
        raise DownloadBusy(e.path, e.pid) from None
    try:
        if fresh:
            _drop(out)
        return _fetch_locked(url, out, if_none_match, progress, on_resume, record, timeout, allow_restart=True)
    finally:
        lock.release()


def _fetch_locked(url, out, if_none_match, progress, on_resume, record, timeout, *, allow_restart: bool) -> dict:
    part = partial_path(out)
    side = _read_sidecar(out)
    offset = 0
    if side is not None and part.is_file() and side.get("url") == url and _strong(side.get("etag")):
        offset = min(int(side.get("durable_bytes") or 0), part.stat().st_size)
    elif side is not None or part.exists():
        _drop(out)                                 # a partial that cannot be continued (another address, no usable ETag, no sidecar)
        side = None
    headers = {"Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
        headers["If-Range"] = side["etag"]
    elif if_none_match:
        headers["If-None-Match"] = if_none_match
    with requests.get(url, timeout=timeout, stream=True, headers=headers) as r:
        status = r.status_code
        if status == 304:
            return {"outcome": "unchanged"}
        if status == 416 and offset:
            _, _, total = _content_range(r.headers.get("Content-Range"))
            if total is not None and offset == total and part.stat().st_size >= total:
                side["total"] = total
                _verify_or_drop(out, part, side)
                return {"outcome": "downloaded", **_finish(out, side, record), "bytes": total}
            _drop(out)
            if allow_restart:
                return _fetch_locked(url, out, if_none_match, progress, on_resume, record, timeout, allow_restart=False)
        r.raise_for_status()
        resuming = False
        if status == 206 and offset:
            start, _, total = _content_range(r.headers.get("Content-Range"))
            same = (start == offset and (total is None or side.get("total") in (None, total)) and (r.headers.get("ETag") or side["etag"]) == side["etag"]
                    and (r.headers.get("Content-Encoding") or "identity").lower() == "identity")
            if not same:
                _drop(out)
                if allow_restart:
                    return _fetch_locked(url, out, if_none_match, progress, on_resume, record, timeout, allow_restart=False)
                raise OSError("OSV's answer to the request for the rest of the file does not fit what was already fetched.")
            resuming = True
            side["total"] = total if total is not None else side.get("total")
        else:
            offset = 0                              # a 200 sends the whole file: whatever was fetched before is not used
            try:
                length = int(r.headers.get("Content-Length") or 0) or None
            except (TypeError, ValueError):
                length = None
            side = {"url": url, "etag": r.headers.get("ETag"), "last_modified": r.headers.get("Last-Modified"), "total": length, "md5": _md5_of_headers(r.headers),
                    "durable_bytes": 0, "started_at": util.now_z()}
        if resuming and on_resume:
            on_resume(offset, side.get("total"))
        _write_sidecar(out, side)
        done = _stream(r, part, out, side, offset, progress)
    if side.get("total") and done > side["total"]:
        _drop(out)
        raise OSError(f"The download is {done} bytes but OSV says {side['total']}; it was discarded.")
    if side.get("total") and done != side["total"]:
        raise OSError(f"The download ended at {done} of {side['total']} bytes.")
    side["total"] = side.get("total") or done
    side["durable_bytes"] = done
    _verify_or_drop(out, part, side)
    _write_sidecar(out, side)
    return {"outcome": "downloaded", **_finish(out, side, record), "bytes": done}


def _md5_of_headers(headers) -> str | None:
    return _md5_hex(headers)


def _stream(r, part: pathlib.Path, out: pathlib.Path, side: dict, offset: int, progress) -> int:
    """Write the body to the partial from ``offset``; flush and rewrite the sidecar every ``FLUSH_BYTES`` or ``FLUSH_SECONDS``; returns the size of the file."""
    done = offset
    last_flush_bytes, last_flush_at, last_report = offset, time.monotonic(), 0.0
    total = side.get("total")
    mode = "r+b" if offset and part.exists() else "wb"
    with open(part, mode) as fh:
        if offset:
            fh.truncate(offset)                    # what is beyond the durable bytes was never known to be on the disk
            fh.seek(offset)
        try:
            for chunk in r.iter_content(chunk_size=CHUNK):
                fh.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if progress and now - last_report >= 0.5:
                    last_report = now
                    progress(done, total)
                if done - last_flush_bytes >= FLUSH_BYTES or now - last_flush_at >= FLUSH_SECONDS:
                    fsio.fsync_handle(fh)
                    side["durable_bytes"] = done
                    _write_sidecar(out, side)
                    last_flush_bytes, last_flush_at = done, now
        except BaseException:
            if fsio.fsync_handle(fh):              # a dropped connection or a stop keeps everything fetched so far
                side["durable_bytes"] = done
                try:
                    _write_sidecar(out, side)
                except OSError:
                    pass                           # ignored: the last periodic flush still stands
            raise
        fsio.fsync_handle(fh)
    if progress:
        progress(done, total)
    return done
