"""A file the page asks the window to download: where it is saved, and what the page is told afterwards.

The app's window is a QtWebEngine view, and QtWebEngine cancels a download that nothing accepts: before this, every link with ``download`` in the app (a session's Export, an audit log, a dataset, a sandbox
file) did nothing in the window, with no error. The window connects the profile's ``downloadRequested`` signal to ``handle``, which asks the person where to save the file with the operating system's own
Save window, accepts the download there, and tells the page how it ended so it can say so (``app-download``, a DOM event the page listens for).

No Qt in this file: ``handle`` takes the request (the few members of ``QWebEngineDownloadRequest`` it uses), a function that asks for a path and a function that notifies the page, so it is tested without a
window. ``ZTSA_DOWNLOAD_TO`` (a folder) saves without asking, into that folder, for the real-window check (tools/window_download_probe.py) and for a computer with no one at it.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Callable

_BAD = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
FALLBACK_NAME = "download"


def safe_name(name: str | None) -> str:
    """A file name that stays a file name: no folders, no characters a file system refuses, no reserved device name, not empty."""
    text = _BAD.sub("_", str(name or "")).strip(" .")
    if not text:
        return FALLBACK_NAME
    if text.split(".")[0].lower() in _RESERVED:
        text = "_" + text
    return text[:150]


def default_folder() -> pathlib.Path:
    """Where the Save window starts: the Downloads folder when there is one, else the home folder."""
    home = pathlib.Path.home()
    return home / "Downloads" if (home / "Downloads").is_dir() else home


def unique(path: pathlib.Path) -> pathlib.Path:
    """``path``, or the same name with ``(1)``, ``(2)`` ... before its extension when it already exists (used where nobody is asked)."""
    if not path.exists():
        return path
    for n in range(1, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def page_script(event: dict) -> str:
    """The script that gives the page what happened: ``window.dispatchEvent(new CustomEvent("app-download", {detail}))``, the detail written as JSON text and read back so nothing in a file name can end the string."""
    return f"window.dispatchEvent(new CustomEvent('app-download', {{ detail: JSON.parse({json.dumps(json.dumps(event))}) }}));"


def handle(request, *, ask_path: Callable[[str], str | None], notify: Callable[[dict], None], fixed_folder: str | None = None) -> None:
    """Decide the fate of one download. ``request`` is a ``QWebEngineDownloadRequest``; ``ask_path(start)`` returns the path the person chose or an empty value when they cancelled;
    ``notify(event)`` tells the page (``{state: saved | failed | cancelled, name, path?, reason?}``). Never raises: whatever goes wrong is a ``failed`` event and a cancelled download."""
    name = safe_name(getattr(request, "downloadFileName", lambda: "")() or FALLBACK_NAME)
    try:
        if fixed_folder:
            target = unique(pathlib.Path(fixed_folder) / name)
        else:
            chosen = ask_path(str(default_folder() / name))
            if not chosen:
                request.cancel()
                notify({"state": "cancelled", "name": name})
                return
            target = pathlib.Path(chosen)
        target.parent.mkdir(parents=True, exist_ok=True)
        request.setDownloadDirectory(str(target.parent))
        request.setDownloadFileName(safe_name(target.name))
        finished = {"done": False}

        def over() -> None:
            if finished["done"] or not request.isFinished():
                return
            finished["done"] = True
            saved = pathlib.Path(request.downloadDirectory()) / request.downloadFileName()
            ok = _completed(request)
            notify({"state": "saved", "name": saved.name, "path": str(saved)} if ok else {"state": "failed", "name": saved.name, "reason": _reason(request)})

        request.isFinishedChanged.connect(over)
        request.accept()
    except Exception as e:  # ignored: a download that cannot be handled is cancelled and the page is told; it must never take the window's event loop down
        try:
            request.cancel()
        except Exception:  # ignored: the request may be over already
            pass
        notify({"state": "failed", "name": name, "reason": f"{type(e).__name__}: {e}"[:200]})


def _completed(request) -> bool:
    state = getattr(request, "state", lambda: None)()
    return "DownloadCompleted" in str(state) or state == 2 or getattr(state, "name", "") == "DownloadCompleted"


def _reason(request) -> str:
    text = getattr(request, "interruptReasonString", lambda: "")()
    return str(text or "the download did not finish")[:200]


def from_environment() -> str | None:
    folder = os.environ.get("ZTSA_DOWNLOAD_TO", "").strip()
    return folder or None
