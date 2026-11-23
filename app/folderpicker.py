"""Asking the person for a folder with the operating system's own folder window.

The page asks over HTTP (``POST /api/shell/pick-folder``), the window thread opens the dialog (a timer takes the request from this box and shows ``QFileDialog``, because a dialog
belongs to that thread), and the page polls for the answer. The request and its answer live here, safe to use from any thread; no Qt in this file, so it is tested without a window.
A request that nobody answers is forgotten after a while.
"""
from __future__ import annotations

import secrets
import threading
import time

KEEP_SECONDS = 600.0


class Picker:
    def __init__(self, clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self._items: dict[str, dict] = {}

    def request(self, title: str = "Choose a folder") -> str:
        """A new request; the window thread will show it. Returns its id."""
        pid = secrets.token_hex(8)
        with self._lock:
            self._forget_old()
            self._items[pid] = {"title": str(title)[:120] or "Choose a folder", "state": "waiting", "shown": False, "path": None, "at": self._clock()}
        return pid

    def next_to_show(self) -> tuple[str, str] | None:
        """The oldest request not yet shown, marked as shown (so a slow dialog is not opened twice); None when there is none or a dialog is already open."""
        with self._lock:
            if any(i["state"] == "waiting" and i["shown"] for i in self._items.values()):
                return None
            for pid, i in self._items.items():
                if i["state"] == "waiting" and not i["shown"]:
                    i["shown"] = True
                    return pid, i["title"]
        return None

    def finish(self, pid: str, path: str | None) -> None:
        """Record the answer: the folder chosen, or None when the person cancelled."""
        with self._lock:
            i = self._items.get(pid)
            if i is not None and i["state"] == "waiting":
                i["state"], i["path"] = ("chosen", str(path)) if path else ("cancelled", None)

    def result(self, pid: str) -> dict | None:
        """``{state: waiting | chosen | cancelled, path}``; None for an id that is not known."""
        with self._lock:
            i = self._items.get(pid)
            return None if i is None else {"state": i["state"], "path": i["path"]}

    def _forget_old(self) -> None:
        now = self._clock()
        for pid in [p for p, i in self._items.items() if now - i["at"] > KEEP_SECONDS]:
            del self._items[pid]
