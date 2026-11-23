"""Closing the window asks first.

A click on the title-bar close button, the system frame's close button and Alt+F4 all end in Qt's ``closeEvent``. The app overrides it: the first close is
ignored and the page is asked to decide (``app-close-request``). The page checks for running jobs, asks the person when there are some, runs the shutdown
(``POST /api/shutdown``) while showing its progress, and then calls ``POST /api/shell/quit``, which lets the close go through. If the page never answers
(``POST /api/shell/close-ack``) within a few seconds, because it crashed or is busy, the close is approved anyway: a window that cannot be closed is worse
than one that closes without a question, and the launcher's own shutdown still runs after the window is gone.

The override reaches into Pyloid's private window object (``win._window._window``); ``tests/test_shell_close.py`` pins the Pyloid version that was checked.
"""
from __future__ import annotations

from typing import Callable

ASK_SCRIPT = "window.dispatchEvent(new CustomEvent('app-close-request'));"
ACK_SECONDS = 3.0


class CloseGuard:
    def __init__(self, ask_page: Callable[[], None], close_window: Callable[[], None], schedule: Callable[[int, Callable[[], None]], None],
                 ack_seconds: float = ACK_SECONDS):
        self._ask, self._close, self._schedule, self._wait = ask_page, close_window, schedule, ack_seconds
        self.approved = False
        self.acknowledged = False
        self.requests = 0

    def handle(self, event) -> bool:
        """Called from ``closeEvent``. True when the window may close now; otherwise the event is ignored and the page is asked."""
        if self.approved:
            return True
        event.ignore()
        self.requests += 1
        self.acknowledged = False
        self._ask()
        self._schedule(int(self._wait * 1000), self._check)
        return False

    def acknowledge(self) -> None:
        """The page saw the request and is deciding."""
        self.acknowledged = True

    def approve(self) -> None:
        """Let the close go through (the page finished its shutdown, or the person chose to force-quit)."""
        self.approved = True
        self._close()

    def _check(self) -> None:
        if not self.acknowledged and not self.approved:
            self.approve()


def install(win) -> CloseGuard:
    """Put a guard on a Pyloid window and connect it to the routes in ``backend/shell_api.py``."""
    from PySide6 import QtCore
    from app.backend import shell_api
    qt = win._window._window
    original = qt.closeEvent
    guard = CloseGuard(
        ask_page=lambda: win._window.web_view.page().runJavaScript(ASK_SCRIPT),
        close_window=win.close,
        schedule=lambda ms, fn: QtCore.QTimer.singleShot(ms, fn))

    def close_event(event):
        if guard.handle(event):
            original(event)
            if event.isAccepted():
                # Pyloid 0.27.2 quits when its last window goes, but its quit loops over a dictionary it is changing and raises, so the process would stay
                # alive with no window: end the event loop ourselves.
                QtCore.QTimer.singleShot(0, QtCore.QCoreApplication.quit)

    qt.closeEvent = close_event
    shell_api.set_close_handlers(guard.acknowledge, guard.approve)
    return guard
