"""Native notifications for a job that ended while the window is in the background.

The page decides when to tell (a job ended, the window is not in front, the person has not turned alerts off) and sends a title and a sentence. This module
only carries them to the window thread: a request thread puts them in an ``Outbox`` and a timer on the window thread takes them out and shows them, because the
notification and the taskbar flash belong to that thread. The text is cleaned here as well: no control characters, short, and plain.
"""
from __future__ import annotations

import re
import threading
from collections import deque

MAX_PENDING = 20                     # a burst of finished jobs is not allowed to pile up notifications
TITLE_MAX, MESSAGE_MAX = 80, 240
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def clean(text: str, limit: int) -> str:
    """One line of plain text of at most ``limit`` characters (a longer one ends in three periods)."""
    one = re.sub(r"\s+", " ", _CONTROL.sub(" ", str(text or ""))).strip()
    return one if len(one) <= limit else one[: limit - 3].rstrip() + "..."


class Outbox:
    """Notifications waiting to be shown, safe to fill from any thread. The oldest are dropped when too many wait."""

    def __init__(self, maximum: int = MAX_PENDING):
        self._items: deque[tuple[str, str]] = deque(maxlen=maximum)
        self._lock = threading.Lock()

    def post(self, title: str, message: str) -> bool:
        """Queue a notification; False when there is nothing to say once the text is cleaned."""
        title, message = clean(title, TITLE_MAX), clean(message, MESSAGE_MAX)
        if not title and not message:
            return False
        with self._lock:
            self._items.append((title or "AI Vulnerability Framework", message))
        return True

    def drain(self) -> list[tuple[str, str]]:
        """Everything waiting, oldest first, and the box is empty afterwards."""
        with self._lock:
            out = list(self._items)
            self._items.clear()
        return out
