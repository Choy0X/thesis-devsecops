"""Which OSV ecosystems exist, and what of each is already on this computer.

The list of published ecosystems comes from OSV (one small file); it is kept for an hour in memory and on disk, so the page opens at once and still
works with no internet (it then says the list may be out of date). Nothing is downloaded here: the state of an ecosystem is read from the files the
download and the ingest left behind (the dump, its snapshot date, the parsed table).
"""
from __future__ import annotations

import datetime
import json
import pathlib
import threading
import time
from typing import Callable

from app.backend.pipeline import fsio, util
from app.backend.pipeline.ingest import osv

CACHE_SECONDS = 3600.0
LIST_FILE = "osv_ecosystems.txt"
_memo: dict = {"at": 0.0, "names": None}


def fetch_online(timeout: float = 8.0) -> list[str]:
    """OSV's list of ecosystems, with a short wait so that a page that opens offline does not hang for a minute."""
    import requests
    r = requests.get(osv.ECOSYSTEMS_URL, timeout=timeout)
    r.raise_for_status()
    return _names(r.text.splitlines())


def _names(lines) -> list[str]:
    """The ecosystem names of OSV's list; a bracketed entry such as ``[EMPTY]`` stands for advisories without an ecosystem and has no dump of its own."""
    return [s for s in (line.strip() for line in lines) if s and not s.startswith("[")]


def forget() -> None:
    _memo.update(at=0.0, names=None)


def remote_list(fetch: Callable[[], list[str]] = fetch_online, now: Callable[[], float] = time.monotonic) -> tuple[list[str], str]:
    """``(names, source)``. ``source`` is "online" (just asked, or asked within the hour), "saved" (OSV could not be reached, so the list saved on disk) or
    "none" (neither). Only a successful answer replaces what was saved."""
    if _memo["names"] is not None and now() - _memo["at"] < CACHE_SECONDS:
        return _memo["names"], "online"
    saved = osv.RAW / LIST_FILE
    try:
        names = sorted(set(fetch()), key=str.lower)
    except Exception:  # ignored: any failure to reach OSV (no internet, a timeout, a refused request) means the saved list is used instead
        if saved.is_file():
            return _names(saved.read_text(encoding="utf-8").splitlines()), "saved"
        return [], "none"
    _memo.update(at=now(), names=names)
    fsio.write_text(saved, "\n".join(names) + "\n")
    return names, "online"


CHECKS_FILE = "osv_update_checks.json"
_checks_lock = threading.Lock()


def _checks() -> dict:
    data = util.read_json(osv.RAW / CHECKS_FILE)
    return data if isinstance(data, dict) else {}


def record_check(name: str, state: str, remote_etag: str | None = None, remote_modified: str | None = None) -> None:
    """Remember what the last look at OSV's copy of ``name`` found, so the page does not forget it (and does not ask again) until the dump changes."""
    with _checks_lock:
        data = _checks()
        data[name] = {"state": state, "remote_etag": remote_etag, "remote_modified": remote_modified, "checked_at": util.now_zulu(), "at": time.time()}
        fsio.write_text(osv.RAW / CHECKS_FILE, json.dumps(data, indent=1))


def mark_current(name: str) -> None:
    """A download or a not-modified answer just showed that the copy here is OSV's newest."""
    meta = osv.read_meta(name)
    record_check(name, "current", remote_etag=meta.get("etag"), remote_modified=meta.get("last_modified"))


def update_state(name: str, downloaded: bool, etag: str | None, dump_mtime: float | None) -> tuple[str, dict]:
    """``(state, last check)`` for the table: "not_downloaded", "unknown" (never looked, or the dump was replaced after the look), "current" or "update_available".
    A look that found an update is void once the dump here carries the version it saw."""
    if not downloaded:
        return "not_downloaded", {}
    check = _checks().get(name) or {}
    state = check.get("state")
    if state == "update_available" and etag and check.get("remote_etag") == etag:
        state = "current"
    if state in ("current", "update_available") and dump_mtime and check.get("at") and dump_mtime > float(check["at"]) + 1:
        state = "unknown"
    return (state if state in ("current", "update_available") else "unknown"), check


def local_state(name: str) -> dict:
    """What is on this computer for one ecosystem. ``snapshot`` is when the dump was taken here; ``remote_modified`` is OSV's own date for the copy; ``update_state``
    and ``update_checked_at`` come from the last look at OSV (see ``check_update``)."""
    slug = osv._slug(name)
    dump = osv.RAW / f"osv_{slug}.zip"
    stamp = osv.RAW / ("osv_snapshot_date.txt" if name == "PyPI" else f"osv_{slug}_snapshot_date.txt")
    snapshot = None
    if stamp.is_file():
        snapshot = stamp.read_text(encoding="utf-8").strip() or None
    elif dump.is_file():
        snapshot = datetime.datetime.fromtimestamp(dump.stat().st_mtime, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    etag = (dump.with_suffix(".etag").read_text(encoding="utf-8").strip() or None) if dump.with_suffix(".etag").is_file() else None
    state, check = update_state(name, dump.is_file(), etag, dump.stat().st_mtime if dump.is_file() else None)
    stopped: dict = {}                                # where a read or a download that was cut had got to, only when there is one
    table = osv.table_path(name)
    if dump.is_file() and not (table.is_file() and table.stat().st_mtime >= dump.stat().st_mtime):
        reading = osv.read_partial(name)
        if reading:
            stopped["read_partial"] = reading
    downloading = osv.download_partial(name)
    if downloading:
        stopped["download_partial"] = downloading
    return {**stopped, "dump_bytes": dump.stat().st_size if dump.is_file() else None, "snapshot": snapshot, "downloaded": dump.is_file(),
            "ingested": osv.has_local_data(name), "etag": etag,
            "remote_modified": osv.read_meta(name).get("last_modified") if dump.is_file() else None,
            "update_state": state, "update_checked_at": check.get("checked_at") if state != "not_downloaded" else None}


def local_downloads() -> list[dict]:
    """The dumps on this computer, found from the files alone (no network): ``[{"name", **local_state}]``. The real name of an ecosystem (``crates.io``, ``Debian:12``) is taken
    from the list OSV gave last time; a dump whose name is not in it is shown by its file name."""
    if not osv.RAW.is_dir():
        return []
    saved = osv.RAW / LIST_FILE
    names = _names(saved.read_text(encoding="utf-8").splitlines()) if saved.is_file() else []
    by_slug = {osv._slug(n): n for n in names}
    out = []
    for z in sorted(osv.RAW.glob("osv_*.zip")):
        slug = z.stem[4:]
        name = by_slug.get(slug, slug)
        out.append({"name": name, **local_state(name)})
    return out


def catalog(fetch: Callable[[], list[str]] = fetch_online) -> dict:
    """Every ecosystem OSV publishes plus any the computer has data for that OSV no longer lists, with the state of each and whether the app collects it."""
    names, source = remote_list(fetch)
    try:
        enabled = set(osv.default_ecosystems())
    except (ValueError, KeyError, OSError):                          # a configuration that cannot be read must not hide the list
        enabled = set()
    extra = sorted({p.stem[4:] for p in osv.RAW.glob("osv_*.zip")} - {osv._slug(n) for n in names}) if osv.RAW.is_dir() else []
    rows = [{"name": n, "enabled": n in enabled, "remote": True, **local_state(n)} for n in names]
    rows += [{"name": e, "enabled": False, "remote": False, **local_state(e)} for e in extra]
    note = {"online": None, "saved": "OSV could not be reached, so this is the list saved earlier; it may be out of date.",
            "none": "OSV could not be reached and no list was saved earlier. Only ecosystems already on this computer are shown."}[source]
    return {"source": source, "note": note, "ecosystems": rows}


def check_update(name: str, head=None, known: list[str] | None = None, timeout: float = 8.0) -> dict:
    """Does OSV have a newer copy of this ecosystem's dump than the one on this computer? One conditional request, nothing is downloaded.

    ``state`` is "not_downloaded", "unknown" (no ETag was saved with the dump), "current", "update_available", "unreachable" or "not_listed" (a name OSV does not
    publish: the name becomes part of the address, so it is never asked for)."""
    import requests
    from urllib.parse import quote
    head = head or requests.head
    names = known if known is not None else remote_list()[0]
    if names and name not in names:
        return {"name": name, "state": "not_listed"}
    local = local_state(name)
    if not local["downloaded"]:
        return {"name": name, "state": "not_downloaded"}
    if not local["etag"]:
        return {"name": name, "state": "unknown", "detail": "The dump was saved without a version mark, so it cannot be compared."}
    try:
        r = head(osv.BULK_URL.format(eco=quote(name)), headers={"If-None-Match": local["etag"]}, timeout=timeout, allow_redirects=True)
    except requests.RequestException as e:
        return {"name": name, "state": "unreachable", "detail": str(e)[:160]}
    if r.status_code == 304:
        record_check(name, "current", remote_etag=local["etag"], remote_modified=local["remote_modified"])
        return {"name": name, "state": "current"}
    if r.status_code == 200:
        remote_etag = (r.headers.get("ETag") or "").strip() or None
        modified = osv.http_date_to_iso(r.headers.get("Last-Modified"))
        state = "current" if remote_etag == local["etag"] else "update_available"
        record_check(name, state, remote_etag=remote_etag, remote_modified=modified or local["remote_modified"])
        return {"name": name, "state": state, "remote_modified": modified}
    return {"name": name, "state": "unknown", "detail": f"OSV answered with status {r.status_code}."}
