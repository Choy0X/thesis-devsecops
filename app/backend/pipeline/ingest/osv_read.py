"""Reading an OSV dump into its table in checkpoints, so that a cut (the app closed, a crash, a power cut) loses at most a few seconds of work and the memory stays bounded.

The members of the zip are read in order and their rows are gathered in a batch. When the batch holds about ``CHECKPOINT_BYTES`` of text (what the rows would take once they are columns), or
``CHECKPOINT_SECONDS`` have passed, it is written as a *part* (a small parquet file) in a work folder next to the table, and ``state.json`` says how far the read has got. The order of a checkpoint is what
makes a cut harmless: the part is written to a temporary name, flushed to the disk, renamed into place, and only then does ``state.json`` (written atomically and flushed) name it. A cut before the
rename leaves a temporary file that is deleted on the next run; a cut between the rename and the state leaves a part the state does not name, which is deleted too, and its batch is read again.

The next run continues from ``state.next`` when the dump is the same one (its content identity, not its date), the reader code is the same, and every part the state names is intact (size and sha256);
a damaged part is dropped together with the parts after it, the ones before it are kept. When all members are done the parts are streamed, one at a time, into the final table, one row group per part
(so a row group holds whole advisories), flushed and renamed over the table; a cut during that only repeats it. One operating-system lock per table keeps two processes from writing one work folder.

The engine knows nothing about OSV: ``rows_for(record, raw_bytes)`` turns one member into rows (dicts), ``schema`` is the table's schema.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import shutil
import time
import zipfile
from typing import Callable

import pyarrow as pa
import pyarrow.parquet as pq

from app.backend.pipeline import fsio, oslock, stopping, util

FORMAT = 1                                    # the layout of the state and the parts; a different number discards what an older run left
CHECKPOINT_BYTES = 64 * 1024 * 1024           # a batch is written when the text its rows would take as columns passes this ...
CHECKPOINT_SECONDS = 10.0                     # ... or when this long has passed since the last checkpoint ...
CHECKPOINT_ADVISORIES = 50_000                # ... or when it holds this many members (a guard; the two limits above decide in practice)
STOP_CHECK_MEMBERS = 64                  # a pause is looked for every this many members (the caller's check is cheap, but not free)
ROW_OVERHEAD = 700                            # bytes counted for every row besides its text (offsets, validity, python objects)


def _fault(point: str) -> None:
    """A seam for tests: they replace it to stop the run at a named point, as a cut would. It does nothing here."""


class ReadBusy(oslock.Busy):
    """Another process is reading the same dump into its table right now."""


class DumpUnreadable(ValueError):
    """A member of the dump could not be read; the message names it. What was read before it is kept, so reading continues from there once the cause is gone."""


@dataclasses.dataclass(frozen=True)
class Result:
    path: str
    rows: int
    advisories: int
    groups: int
    resumed_from: int
    est_bytes: int


def work_dir(final: pathlib.Path) -> pathlib.Path:
    return final.with_suffix(".parse")


def lock_path(final: pathlib.Path) -> pathlib.Path:
    return final.with_name(final.stem + ".parse.lock")


def _state_path(work: pathlib.Path) -> pathlib.Path:
    return work / "state.json"


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def zip_identity(path: pathlib.Path, infos: list[zipfile.ZipInfo]) -> dict:
    """What makes this dump this dump: its size and a hash of every member's name, checksum and sizes in order. Not its dates (a copy or a backup changes them) and not its ETag (it is written after
    the file)."""
    h = hashlib.sha256()
    for i in infos:
        h.update(f"{i.filename}|{i.CRC}|{i.file_size}|{i.compress_size}".encode("utf-8"))
        h.update(b"\n")
    return {"size": path.stat().st_size, "members": len(infos), "sha256": h.hexdigest()}


def _estimate(rows: list[dict]) -> int:
    n = 0
    for r in rows:
        n += ROW_OVERHEAD
        for v in r.values():
            if v.__class__ is str:
                n += len(v)
    return n


def _read_state(work: pathlib.Path) -> dict | None:
    try:
        state = json.loads(_state_path(work).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None                                # missing, torn or full of zeros: there is no state
    return state if isinstance(state, dict) and state.get("format") == FORMAT and isinstance(state.get("parts"), list) else None


def _write_state(work: pathlib.Path, state: dict) -> None:
    state["updated_at"] = util.now_z()
    fsio.write_json(_state_path(work), state)


def _wipe(work: pathlib.Path) -> None:
    """Remove the work folder completely; a folder that cannot be removed is an error (a half-removed one could mix old parts into a new run)."""
    if not work.exists():
        return
    try:
        shutil.rmtree(work)
    except OSError as e:
        raise OSError(f"The earlier work in {work.name} could not be removed ({e}). Close whatever holds those files and try again.") from e


def discard(final: str | os.PathLike) -> None:
    """Throw away the saved work of reading this table (Start over). Raises ReadBusy while another process is reading it."""
    final = pathlib.Path(final)
    work = work_dir(final)
    if not work.exists():
        return
    lock = oslock.FileLock(lock_path(final))
    try:
        lock.acquire(wait=3.0)
    except oslock.Busy as e:
        raise ReadBusy(e.path, e.pid) from None
    try:
        _wipe(work)
    finally:
        lock.release()


def _valid_prefix(work: pathlib.Path, state: dict) -> dict:
    """The state cut back to the parts that are intact (in order): the first missing, short or altered part and every part after it are dropped, together with their files."""
    good: list[dict] = []
    for part in state["parts"]:
        f = work / str(part.get("file", ""))
        try:
            ok = f.is_file() and f.stat().st_size == part["bytes"] and _sha256(f) == part["sha256"]
        except (OSError, KeyError, TypeError):
            ok = False
        if not ok:
            break
        good.append(part)
    if len(good) != len(state["parts"]):
        state["parts"] = good
        state["next"] = good[-1]["end"] if good else 0
        state["phase"] = "parsing"
    keep = {p["file"] for p in good} | {_state_path(work).name}
    for f in work.iterdir():
        if f.name not in keep:                    # temporary files, parts the state never named, parts after a damaged one
            try:
                f.unlink()
            except OSError:
                pass                              # ignored: it is overwritten or removed by the next commit or wipe
    state["rows"] = sum(p["rows"] for p in good)
    state["advisories"] = sum(p["advisories"] for p in good)
    state["est_bytes"] = sum(p["est"] for p in good)
    return state


def _compatible(state: dict, *, parser: str, layout: str, ecosystem: str, identity: dict, total: int) -> bool:
    return (state.get("parser") == parser and state.get("layout") == layout and state.get("ecosystem") == ecosystem and state.get("zip") == identity and state.get("names_total") == total
            and isinstance(state.get("next"), int) and 0 <= state["next"] <= total)


def peek(final: str | os.PathLike, dump: str | os.PathLike | None = None) -> dict | None:
    """What a stopped read has done so far, cheaply and without touching anything: ``{"done", "total", "rows", "percent", "phase", "updated_at", "active"}``, or None when there is no saved work
    (or it is for another dump: the size is compared, the full identity only when the read continues). ``active`` is True while some process holds the table's lock."""
    final = pathlib.Path(final)
    state = _read_state(work_dir(final))
    if state is None:
        return None
    if dump is not None:
        try:
            if state.get("zip", {}).get("size") != pathlib.Path(dump).stat().st_size:
                return None
        except OSError:
            return None
    total = int(state.get("names_total") or 0)
    done = int(state.get("next") or 0)
    phase = state.get("phase") or "parsing"
    percent = 99 if phase == "assembling" or (total and done >= total) else (min(99, int(100 * done / total)) if total else 0)
    return {"done": done, "total": total, "rows": int(state.get("rows") or 0), "percent": percent, "phase": phase, "updated_at": state.get("updated_at"),
            "active": oslock.held_elsewhere(lock_path(final))}


def run(zip_path: str | os.PathLike, final: str | os.PathLike, *, schema: pa.Schema, rows_for: Callable[[dict, bytes], list[dict]], layout: str, ecosystem: str, parser: str,
        progress: Callable[..., None] | None = None, on_resume: Callable[[int, int], None] | None = None, fresh: bool = False,
        should_stop: Callable[[], bool] | None = None) -> Result:
    """Read every ``.json`` member of the zip into the table ``final`` (see the module text). ``progress(done, total, rows, phase=...)`` is called about once a second while the members are
    read (``phase="read"``), once per part while the table is put together afterwards (``phase="assembling"``, the count keeps rising past the member total rather than going quiet once every
    member has been read, which can itself take a while on a large table) and once at the very end (``phase="done"``). ``on_resume(next, total)`` once, before the first member, when an earlier
    run is continued. Raises ``ReadBusy`` when another process is reading the same table and ``DumpUnreadable`` for a member that cannot be read.
    ``should_stop`` (see ``stopping``) is asked every ``STOP_CHECK_MEMBERS`` members: the members read so far are made durable and ``stopping.Paused`` is raised; the next run continues from them."""
    zip_path, final = pathlib.Path(zip_path), pathlib.Path(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    lock = oslock.FileLock(lock_path(final))
    try:
        lock.acquire(wait=3.0)
    except oslock.Busy as e:
        raise ReadBusy(e.path, e.pid) from None
    try:
        return _run_locked(zip_path, final, schema, rows_for, layout, ecosystem, parser, progress, on_resume, fresh, should_stop)
    finally:
        lock.release()


def _run_locked(zip_path, final, schema, rows_for, layout, ecosystem, parser, progress, on_resume, fresh, should_stop=None) -> Result:
    work = work_dir(final)
    if fresh:
        _wipe(work)
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise DumpUnreadable(f"{zip_path.name} is not a valid zip file ({e}); the download may be damaged. Start over downloads it again.") from e
    with archive as z:
        infos = [i for i in z.infolist() if i.filename.endswith(".json")]
        total = len(infos)
        identity = zip_identity(zip_path, infos)
        state = _read_state(work) if work.exists() else None
        if state is not None and _compatible(state, parser=parser, layout=layout, ecosystem=ecosystem, identity=identity, total=total):
            state = _valid_prefix(work, state)
        else:
            _wipe(work)
            state = None
        if state is None:
            work.mkdir(parents=True, exist_ok=True)
            state = {"format": FORMAT, "parser": parser, "layout": layout, "ecosystem": ecosystem, "zip": identity, "names_total": total, "next": 0, "advisories": 0, "rows": 0, "est_bytes": 0,
                     "parts": [], "phase": "parsing", "created_at": util.now_z()}
            _write_state(work, state)
        start = state["next"]
        if start and on_resume:
            on_resume(start, total)
        last_report = time.monotonic()
        if progress and start:
            progress(start, total, state["rows"], phase="read")

        batch: list[dict] = []
        batch_est = batch_adv = 0
        batch_start = state["next"]
        last_commit = time.monotonic()

        def commit(end: int) -> None:
            """Make the batch durable: part to a temporary name, flushed, renamed, then the state names it. A batch with no rows still moves ``next``."""
            nonlocal batch, batch_est, batch_adv, batch_start, last_commit
            if batch:
                table = pa.Table.from_pylist(batch, schema=schema)
                _fault("member_batch_built")
                number = len(state["parts"]) + 1
                name = f"part-{number:06d}.parquet"
                tmp = work / (name + ".tmp")
                pq.write_table(table, tmp)
                fsio.fsync_file(tmp)
                _fault("part_tmp_written")
                fsio.replace(tmp, work / name)
                _fault("part_renamed")
                size = (work / name).stat().st_size
                state["parts"].append({"file": name, "start": batch_start, "end": end, "rows": len(batch), "advisories": batch_adv, "est": batch_est, "bytes": size,
                                       "sha256": _sha256(work / name)})
                state["rows"] += len(batch)
                state["advisories"] += batch_adv
                state["est_bytes"] += batch_est
            state["next"] = end
            _write_state(work, state)
            _fault("state_written")
            batch, batch_est, batch_adv, batch_start, last_commit = [], 0, 0, end, time.monotonic()

        for index in range(state["next"], total):
            if should_stop is not None and index % STOP_CHECK_MEMBERS == 0 and should_stop():
                commit(index)                      # a pause keeps what was read, like a cut does
                raise stopping.Paused()
            info = infos[index]
            try:
                raw = z.read(info)
                rows = rows_for(json.loads(raw), raw)
            except BaseException as e:
                commit(index)                      # what was read before this member is kept
                if isinstance(e, Exception):
                    raise DumpUnreadable(f"{info.filename}: {e}") from e
                raise
            if rows:
                batch.extend(rows)
                batch_adv += 1
                batch_est += _estimate(rows)
            now = time.monotonic()
            if progress and now - last_report >= 1.0:
                last_report = now
                progress(index + 1, total, state["rows"] + len(batch), phase="read")
            if batch and (batch_est >= CHECKPOINT_BYTES or now - last_commit >= CHECKPOINT_SECONDS or batch_adv >= CHECKPOINT_ADVISORIES):
                commit(index + 1)
        if batch or state["next"] < total:
            commit(total)

    state["phase"] = "assembling"
    _write_state(work, state)
    _assemble(work, state, schema, final, progress=progress, members_total=total)
    if progress:
        progress(total + len(state["parts"]), total + len(state["parts"]), state["rows"], phase="done")
    return Result(path=str(final), rows=state["rows"], advisories=state["advisories"], groups=len(state["parts"]), resumed_from=start, est_bytes=state["est_bytes"])


def _assemble(work: pathlib.Path, state: dict, schema: pa.Schema, final: pathlib.Path, *, progress=None, members_total: int = 0) -> None:
    """Stream the parts, one at a time, into the final table (one row group per part), flush it, put it in place, then remove the work folder. Every member of the dump was already
    reported through ``progress`` by the time this runs (the file count can reach its total well before the table is actually finished): this keeps reporting, part by part, on the same
    rising count rather than going quiet, so a long assembly is never mistaken for a stall."""
    _fault("assembly_started")
    tmp = work / "final.tmp"
    writer = pq.ParquetWriter(tmp, schema)
    parts_total = len(state["parts"])
    try:
        for done, part in enumerate(state["parts"], start=1):
            table = pq.read_table(work / part["file"])
            writer.write_table(table, row_group_size=len(table))
            del table
            if progress:
                progress(members_total + done, members_total + parts_total, state["rows"], phase="assembling")
    finally:
        writer.close()
    fsio.fsync_file(tmp)
    _fault("assembly_tmp_written")
    written = pq.read_metadata(tmp).num_rows
    if written != state["rows"]:
        raise OSError(f"The table has {written} rows but {state['rows']} were read; it was not put in place.")
    fsio.replace(tmp, final)
    _fault("final_renamed")
    try:
        _state_path(work).unlink()                # a folder without a state is garbage: a cut during the removal below leaves nothing that could be mistaken for work to continue
    except OSError:
        pass                                      # ignored: the removal below takes it, or the next run finds a finished state and only assembles again
    _fault("state_removed")
    try:
        shutil.rmtree(work)
    except OSError:
        pass                                      # ignored: leftovers of a finished read are removed by the next run
