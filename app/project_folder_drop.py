"""Observe native project-folder drops without changing Qt's drop handling.

Only the project's DOM dropzone receives an absolute path. Qt imports stay in
``install`` so URL validation and script generation work without a desktop.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path


def directory_from_urls(urls) -> str | None:
    """Accept exactly one existing absolute directory from local OS file URLs."""
    if len(urls) != 1:
        return None
    url = urls[0]
    if not url.isLocalFile() or url.host().lower() not in ("", "localhost"):
        return None
    local = url.toLocalFile()
    # QUrl considers file://server/share local too; do not adopt network shares.
    if not local or local.startswith(("\\\\", "//")):
        return None
    try:
        path = Path(local)
        return os.path.abspath(path) if path.is_absolute() and path.is_dir() else None
    except (OSError, ValueError):
        return None


def css_position(x: float, y: float, zoom: float) -> tuple[float, float]:
    """Qt widget positions already use logical pixels; only page zoom differs."""
    if not all(math.isfinite(value) for value in (x, y, zoom)) or zoom <= 0:
        raise ValueError("Drop position and page zoom must be finite, with positive zoom")
    return x / zoom, y / zoom


def page_script(path: str, x: float, y: float, zoom: float = 1.0) -> str:
    """Hit-test the project dropzone and dispatch JSON-encoded path data to it."""
    x, y = css_position(x, y, zoom)
    payload = json.dumps({"path": path, "x": x, "y": y}, allow_nan=False)
    return (
        "((drop) => {"
        "const target = document.elementFromPoint(drop.x, drop.y)"
        "?.closest('[data-project-source-drop]');"
        "if (target) target.dispatchEvent(new CustomEvent('project-source-folder', "
        "{bubbles: true, detail: {path: drop.path}}));"
        f"}})({payload});"
    )


def install(win):
    """Keep a window-owned filter for its webview and dynamically made render widgets.

    The application filter sees descendant render widgets even when WebEngine
    replaces them. It never consumes, accepts or otherwise alters a Qt event.
    The DOM drop handler prevents navigation only inside its own dropzone.
    """
    from PySide6 import QtCore, QtWidgets

    existing = getattr(win, "_project_folder_drop_filter", None)
    if existing is not None:
        return existing
    view = win._window.web_view

    class FolderDropFilter(QtCore.QObject):
        def eventFilter(self, watched, event):
            if event.type() != QtCore.QEvent.Type.Drop:
                return False
            if not isinstance(watched, QtWidgets.QWidget):
                return False
            if watched is not view and not view.isAncestorOf(watched):
                return False
            # A Qt drag source denotes an in-app drag, not an OS folder drop.
            if event.source() is not None:
                return False
            path = directory_from_urls(event.mimeData().urls())
            if path is None:
                return False
            try:
                point = event.position()
                if watched is not view:
                    point = watched.mapTo(view, point)
                view.page().runJavaScript(page_script(path, point.x(), point.y(), view.zoomFactor()))
            except (RuntimeError, ValueError):
                # The native view can disappear during close; drops still pass through.
                pass
            return False

    observer = FolderDropFilter(view)
    QtWidgets.QApplication.instance().installEventFilter(observer)
    win._project_folder_drop_filter = observer
    return observer
