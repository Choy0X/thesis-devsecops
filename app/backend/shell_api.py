"""Routes of the window shell: today only ``POST /api/shell/focus``, which a second launch of the app uses to bring the first window forward.

The desktop launcher registers what "show the window" means (``set_focus_handler``); a headless server or a browser fallback has none, and the
answer says so instead of pretending.
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()
_focus: Callable[[], None] | None = None


def set_focus_handler(handler: Callable[[], None] | None) -> None:
    global _focus
    _focus = handler


_ack: Callable[[], None] | None = None
_quit: Callable[[], None] | None = None
_reboot: Callable[[], dict] | None = None        # app/restart.py: start the new copy and end this one
_exit: Callable[[], None] | None = None          # a copy without a window ends its process when it is told to quit
_notify: Callable[[str, str], bool] | None = None
_picker = None                                    # app.folderpicker.Picker of the window, or None without a window


def set_notify_handler(handler: Callable[[str, str], bool] | None) -> None:
    """What "show a native notification" means for this copy of the app (app/notifier.py); a server without a window has none."""
    global _notify
    _notify = handler


def set_reboot_handler(reboot: Callable[[], dict] | None, exit_: Callable[[], None] | None = None) -> None:
    """What "restart the app" and "quit a copy that has no window" mean for this copy (``app/desktop.py`` sets them; a test sets its own)."""
    global _reboot, _exit
    _reboot, _exit = reboot, exit_


def set_close_handlers(ack: Callable[[], None] | None, quit_: Callable[[], None] | None) -> None:
    """The window's close guard (app/window_close.py): ``ack`` says the page saw a close request, ``quit_`` lets the close through."""
    global _ack, _quit
    _ack, _quit = ack, quit_


@router.post("/api/shutdown")
def shutdown():
    """Begin an orderly shutdown in the background: stop the jobs and record them, close the databases, mark the exit as clean. The window polls the status."""
    from app.backend import lifecycle
    return lifecycle.start_shutdown()


@router.get("/api/shutdown/status")
def shutdown_status():
    """The steps of the shutdown so far, each with its result and time."""
    from app.backend import lifecycle
    return lifecycle.status()


@router.get("/api/lifecycle")
def lifecycle_info():
    """How the previous run ended: ``clean`` false when the app did not close properly (a crash, a power cut), with a sentence for the person."""
    from app.backend import lifecycle
    return lifecycle.previous_exit()


@router.post("/api/shell/close-ack")
def shell_close_ack():
    """The page saw a close request and is deciding; the window waits for it instead of closing on its own."""
    if _ack is None:
        return {"ok": False, "detail": "this copy of the app has no window of its own"}
    _ack()
    return {"ok": True}


@router.post("/api/shell/quit")
def shell_quit():
    """Let the window close. The page calls this after the shutdown finished, or when the person chose Force quit."""
    if _quit is None:
        if _exit is None:
            return {"ok": False, "detail": "this copy of the app has no window of its own"}
        _exit()                                           # no window to close: the process ends, a moment after this answer
        return {"ok": True, "detail": "the app is ending"}
    _quit()
    return {"ok": True}


@router.post("/api/shell/reboot")
def shell_reboot():
    """Start the app again and end this copy. The page runs the shutdown first (``POST /api/shutdown``, polled until it is done); a reboot before that is refused, because the new copy
    must not find jobs still running or the databases open. When the new copy cannot be started the answer says why and this copy goes on."""
    from app.backend import errors, lifecycle
    if _reboot is None:
        return {"ok": False, "detail": "this copy of the app cannot restart itself (it was not started by the launcher)"}
    if not lifecycle.status()["done"]:
        return errors.error("The app has not finished shutting down yet.", 409, hint="Run the shutdown first and wait for it to finish.")
    from app.backend import sandbox_runtime
    import time
    sandbox_runtime.update(lambda state: state.update(restart_handoff_until=time.time() + 60))
    got = _reboot()
    if not got.get("ok"):
        _resume()
    return got


def _resume() -> None:
    """Resume services when a restart leaves this backend open after shutdown."""
    try:
        from app.backend import lifecycle
        if not lifecycle.resume_after_failed_restart():
            return
        from app.backend.pipeline.ollama import server
        server.start_in_background()
    except Exception as e:
        print(f"could not start Ollama again: {type(e).__name__}: {e}")


def set_folder_picker(picker) -> None:
    """The box a folder window is asked through (app/folderpicker.py); a server without a window has none."""
    global _picker
    _picker = picker


class PickIn(BaseModel):
    title: str = Field(default="Choose a folder", max_length=200)


@router.post("/api/shell/pick-folder")
def shell_pick_folder(body: PickIn):
    """Ask the person for a folder with the operating system's own window. Answers at once with an id (the window is shown by the app's window thread); poll
    ``GET /api/shell/pick-folder/<id>`` for the folder or the cancel. Without a window of its own the app says so, and the page asks for the path to be typed."""
    if _picker is None:
        return {"ok": False, "detail": "this copy of the app has no window of its own, so a folder cannot be chosen here; type the path"}
    return {"ok": True, "id": _picker.request(body.title)}


@router.get("/api/shell/pick-folder/{pick_id}")
def shell_pick_folder_result(pick_id: str):
    from app.backend import errors
    r = _picker.result(pick_id) if _picker is not None else None
    return r if r is not None else errors.error("There is no such request.", 404)


class NotifyIn(BaseModel):
    title: str = Field(default="", max_length=400)
    message: str = Field(default="", max_length=2000)


@router.post("/api/shell/notify")
def shell_notify(body: NotifyIn):
    """Show a native notification and flash the taskbar button, for a job that ended while the window was in the background. The page decides when to ask."""
    if _notify is None:
        return {"ok": False, "delivered": False, "detail": "this copy of the app has no window of its own"}
    queued = _notify(body.title, body.message)
    return {"ok": True, "delivered": bool(queued), "detail": None if queued else "there was nothing to say"}


@router.post("/api/shell/focus")
def shell_focus():
    """Bring the app window to the front."""
    if _focus is None:
        return {"ok": False, "detail": "this copy of the app has no window of its own"}
    _focus()
    return {"ok": True}


class OpenIn(BaseModel):
    which: str = Field(max_length=40)


def open_in_file_manager(path) -> None:
    """The operating system's file manager on a folder (replaced in tests)."""
    import os
    import subprocess
    import sys
    if os.name == "nt":
        os.startfile(str(path))                         # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


@router.post("/api/shell/open-folder")
def shell_open_folder(body: OpenIn):
    """Show one of the app's own folders in the file manager: ``models`` (the models folder) or ``interim`` (the database folder). Only these names are accepted, never a path, so the
    page cannot make the server open anything else. A folder that does not exist yet is not made; the answer says so."""
    from app.backend import errors, paths
    from app.backend.pipeline import model_files
    folders = {"models": model_files.models_dir, "interim": lambda: paths.interim_dir()}
    if body.which not in folders:
        return errors.error("That folder is not one the app opens.", 400, hint="Use models or interim.")
    folder = folders[body.which]()
    if not folder.is_dir():
        return {"ok": False, "detail": f"{folder} does not exist yet. It is made when the first file is written there."}
    try:
        open_in_file_manager(folder)
    except OSError as e:
        return {"ok": False, "detail": f"The file manager could not be opened ({type(e).__name__}); the folder is {folder}"}
    return {"ok": True, "path": str(folder)}
