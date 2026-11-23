"""Desktop shell: boot the FastAPI server and open the UI in a native window.

Run with `python -m app.desktop`. The whole app is one process on one endpoint:
  1. serve the FastAPI backend (which also serves the built React frontend),
  2. open it in a Pyloid native window (QtWebEngine; the Python analog of Electron),
  3. if Pyloid/Qt is unavailable, fall back to launching the installed Chromium in
     `--app` mode, then to the default browser.

Set ZTSA_HEADLESS=1 to start the server only (CI / servers), no window. On a
headless Linux host (no DISPLAY / WAYLAND_DISPLAY) this is detected automatically,
so the launcher serves the UI and prints the URL instead of trying to open a window
that could never appear.

Slow page summaries are computed on demand. Set ZTSA_PREWARM=1 to request the
optional background startup scans (including verification of training bundles).
"""
from __future__ import annotations
import glob
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

# Run from the repository root so data/ and experiments/ paths resolve.
ROOT = pathlib.Path(__file__).resolve().parents[1]
os.chdir(ROOT)
PORTABLE = (ROOT / "runtime" / "portable-runtime.txt").is_file()


def _prepare_portable_environment() -> None:
    if not PORTABLE:
        return
    tools = ROOT / "runtime" / "tools"
    os.environ.setdefault("ZTSA_TOOLCHAIN_DIR", str(tools))
    from app.backend.pipeline.agentic.exec import toolchain
    git = toolchain.find("git", tools)
    if git:
        os.environ["PATH"] = os.pathsep.join(filter(None, (str(git.parent), os.environ.get("PATH"))))


def _prepare_console_streams() -> None:
    """Give a window-only Python process streams for startup diagnostics and Uvicorn."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = ROOT / "data" / "logs" / "desktop.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a", encoding="utf-8", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(port: int):
    import uvicorn
    uvicorn.run("app.backend.api:app", host="127.0.0.1", port=port, log_level="warning")


def _wait_until_up(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def _has_display() -> bool:
    """Whether a GUI window can be shown. Windows/macOS always can; Linux needs
    an X or Wayland display, so a headless server/container returns False."""
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _find_chromium() -> str | None:
    for pat in ("/opt/pw-browsers/chromium-*/chrome-linux/chrome",
                "/opt/pw-browsers/chromium-*/chrome-linux/headless_shell"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _serve_headless(url: str):
    """Keep serving without a window (CI / servers / no-display containers)."""
    print(f"[desktop] serving at {url} — open this URL in a browser; Ctrl+C to stop")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def _place_window(win):
    """Keep the window inside the display's work area: the default position (200, 200) is chosen for no particular display, and on a 1080p screen it
    put the bottom of a 900 px window under the taskbar."""
    from app import window_mode
    qt = win._window._window
    screen = qt.screen()
    if screen is None:
        return
    a, g = screen.availableGeometry(), qt.geometry()
    x, y, w, h = window_mode.fit((g.x(), g.y(), g.width(), g.height()), (a.x(), a.y(), a.width(), a.height()))
    if (x, y, w, h) != (g.x(), g.y(), g.width(), g.height()):
        qt.setGeometry(x, y, w, h)


def _check_window(win, attempt: int = 1):
    """Self-test of the frameless window, a few seconds after it opened: the flag took, the page received the shell's bridge, and the window lies in the
    work area. When a check fails the window gets the operating system's frame back (no restart) and the reasons are printed (app/window_mode.py)."""
    from PySide6 import QtCore
    from app import window_mode
    qt = win._window._window
    geometry, screen = qt.geometry(), qt.screen()
    area = screen.availableGeometry() if screen else None
    facts = {"frameless_flag": bool(int(qt.windowFlags()) & int(QtCore.Qt.FramelessWindowHint)),
             "inside_work_area": window_mode.inside((geometry.x(), geometry.y(), geometry.width(), geometry.height()),
                                                    (area.x(), area.y(), area.width(), area.height())) if area else None}

    def decide(has_bridge):
        if not has_bridge and attempt < 3:                     # the page may still be loading: look again
            QtCore.QTimer.singleShot(3000, lambda: _check_window(win, attempt + 1))
            return
        ok, reasons = window_mode.evaluate({**facts, "bridge": bool(has_bridge)})
        print(f"[desktop] window check: {facts} bridge={bool(has_bridge)} geometry={(geometry.x(), geometry.y(), geometry.width(), geometry.height())} "
              f"work area={(area.x(), area.y(), area.width(), area.height()) if area else None}")
        if ok:
            print("[desktop] frameless window checked: ok")
        else:
            print("[desktop] frameless window rejected (" + "; ".join(reasons) + "); using the system frame")
            win.set_frame(True)

    win._window.web_view.page().runJavaScript("typeof window.__PYLOID__ !== 'undefined'", decide)


APP_ICON = ROOT / "app" / "assets" / "icon.png"


def _set_app_icon(app) -> None:
    """The icon of the app, set right after Pyloid is created and before any window exists: a window takes the app's icon when its page loads (``BrowserWindow._load``) and Pyloid prints
    "Icon is not set." for one that finds none, so the splash had no icon at all and the main window got its own only later. With no window open yet ``app.set_icon`` only stores the icon;
    on windows that are already open Pyloid's own call fails (found by trying it), which is why this is not done later."""
    try:
        if APP_ICON.exists():
            app.set_icon(str(APP_ICON))
    except Exception as e:  # ignored: a window without an icon is plainer, not a failed start
        print(f"[desktop] the app icon could not be set ({e})")


def _make_app(title: str):
    """The Pyloid app, the only place one is made: one instance, and its icon in place before the first window is created."""
    from pyloid import Pyloid
    app = Pyloid(app_name=title, single_instance=True)
    _set_app_icon(app)
    return app


def _install_notifications(app, win) -> None:
    """Native notifications for jobs that end while the window is in the background: the page posts a sentence to ``/api/shell/notify``, a timer on this (the window)
    thread shows it as a system notification and flashes the taskbar button. The icon is the one the tray needs; without it the notification still has a title."""
    from PySide6 import QtCore
    from app import notifier, taskbar
    from app.backend import shell_api
    try:
        if APP_ICON.exists():
            app.set_tray_icon(str(APP_ICON))
    except Exception as e:  # ignored: no icon means a plainer notification, not a failed start
        print(f"[desktop] the notification icon could not be set ({e})")
    outbox = notifier.Outbox()
    shell_api.set_notify_handler(outbox.post)

    def show():
        for title, message in outbox.drain():
            try:
                app.show_notification(title, message)
            except Exception as e:  # ignored: a notification that cannot be shown is only a missed hint; the toast and the Jobs list still say it
                print(f"[desktop] notification not shown ({e})")
            taskbar.flash(int(win._window._window.winId()))

    timer = QtCore.QTimer()
    timer.setInterval(500)
    timer.timeout.connect(show)
    timer.start()
    win._notify_timer = timer                                    # the timer lives as long as the window


def _install_folder_picker(win) -> None:
    """The folder window of the operating system for the page's "Browse" buttons: a request put in the box by the page is shown here, on the window thread, and the answer goes
    back to the box (app/folderpicker.py)."""
    from PySide6 import QtCore, QtWidgets
    from app import folderpicker
    from app.backend import shell_api
    picker = folderpicker.Picker()
    shell_api.set_folder_picker(picker)

    def show():
        nxt = picker.next_to_show()
        if nxt is None:
            return
        pid, title = nxt
        try:
            chosen = QtWidgets.QFileDialog.getExistingDirectory(win._window._window, title, os.path.expanduser("~"))
        except Exception as e:  # ignored: a dialog that cannot open is an answer of "cancelled"; the page falls back to the typed path
            print(f"[desktop] folder window failed ({e})")
            chosen = ""
        picker.finish(pid, chosen or None)

    timer = QtCore.QTimer()
    timer.setInterval(300)
    timer.timeout.connect(show)
    timer.start()
    win._picker_timer = timer


def _install_downloads(win) -> None:
    """Saving what the page downloads. QtWebEngine cancels a download that nothing accepts, so without this every ``download`` link of the app (a session's Export, an audit log, a dataset) did nothing in the
    window. A download opens the system's Save window (on the window thread, where the signal is delivered) and the page is told how it ended (app/downloads.py)."""
    from PySide6 import QtWidgets
    from app import downloads
    page = win._window.web_view.page()

    def ask_path(start: str) -> str | None:
        chosen, _ = QtWidgets.QFileDialog.getSaveFileName(win._window._window, "Save the file", start)
        return chosen or None

    def notify(event: dict) -> None:
        page.runJavaScript(downloads.page_script(event))

    def requested(request) -> None:
        downloads.handle(request, ask_path=ask_path, notify=notify, fixed_folder=downloads.from_environment())

    page.profile().downloadRequested.connect(requested)
    win._download_slot = requested                                 # kept as long as the window: a slot that is only referenced by the connection can be collected


def _create_main_window(app, url: str, title: str = "AI Vulnerability Framework"):
    """The app's window on a running Pyloid app: frameless with its own title bar unless the system frame was asked for, centred in the work area,
    with the close guard, the self-test and the focus handler of a second launch."""
    from PySide6 import QtCore
    from app import window_close, window_mode
    from app.backend import shell_api
    frameless = window_mode.wants_frameless(os.environ.get("ZTSA_WINDOW_FRAME"))
    win = app.create_window(title=title, width=1400, height=900, frame=not frameless)
    win.load_url(url)
    win.show_and_focus()
    window_close.install(win)                                   # a close asks the page first (running jobs, the shutdown)
    QtCore.QTimer.singleShot(200, lambda: _place_window(win))
    if frameless:
        QtCore.QTimer.singleShot(4000, lambda: _check_window(win))
    shell_api.set_focus_handler(win.show_and_focus)             # a second launch asks the first window to come forward
    _install_notifications(app, win)
    _install_folder_picker(win)
    _install_downloads(win)
    from app import project_folder_drop
    project_folder_drop.install(win)
    return win


def _center(win):
    """Centre a small window (the splash) in the work area of its display."""
    qt = win._window._window
    screen = qt.screen()
    if screen is None:
        return
    a, g = screen.availableGeometry(), qt.geometry()
    qt.move(a.x() + (a.width() - g.width()) // 2, a.y() + (a.height() - g.height()) // 2)


def _boot(model, shared: dict, instance=None):
    """The start of the app as steps, run off the window thread so the splash stays alive. Fills ``shared['url']`` when everything is up; a failed
    step is recorded on the model with a message in plain words (the splash shows it with Try again and Quit)."""
    took: list[tuple[str, float]] = []
    begun = time.perf_counter()

    def run(id, fn):
        model.begin(id)
        t0 = time.perf_counter()
        try:
            detail = fn()
        except Exception as e:                                     # a step that raises is a failed start, shown on the splash
            model.fail(id, f"{type(e).__name__}: {e}"[:300])
            return False
        if detail is False:
            return False
        took.append((id, time.perf_counter() - t0))
        model.done(id, detail or "")
        return True

    def interface():
        built = _ensure_frontend_built(note=lambda text: model.note("interface", text))
        if built["state"] in ("failed", "missing"):
            model.fail("interface", built["reason"] or "The interface could not be built.")
            return False
        return "using the existing build (Node.js was not found)" if built["state"] == "stale" else "built just now" if built["state"] == "built" else "up to date"

    def server():
        if shared.get("port") and _wait_until_up(f"http://127.0.0.1:{shared['port']}/api/health", timeout=0.5):
            return f"already running on port {shared['port']}"
        shared["port"] = int(os.getenv("ZTSA_PORT", "0")) or _free_port()
        threading.Thread(target=_serve, args=(shared["port"],), daemon=True).start()
        return f"port {shared['port']}"

    def answer():
        if not _wait_until_up(f"http://127.0.0.1:{shared['port']}/api/health"):
            model.fail("answer", "The app did not answer within 20 seconds. Close other copies of it and try again.")
            return False
        return None

    def settings():
        from app.backend import config  # noqa: F401  (overlays the saved settings onto the environment)
        return None

    def database():
        from app.backend.pipeline.db import database as storage
        st = storage.status()
        return f"SQLite {st.get('version')} (local file)" if st.get("reachable") else "local storage unavailable; check the Database page"

    def machine():
        from app.backend import compute
        info = compute.machine_info()
        return f"{info['physical_cores']} cores, {info['ram_gb']} GB memory"

    def warm():
        from app.backend import slowcache
        if slowcache.prewarm_enabled():
            slowcache.prewarm(f"http://127.0.0.1:{shared['port']}")
            return "started in the background"
        return "views are computed when opened" if slowcache.enabled() else "off"

    for id, fn in (("interface", interface), ("server", server), ("answer", answer), ("settings", settings), ("database", database), ("machine", machine), ("warm", warm)):
        if not run(id, fn):
            return
    url = f"http://127.0.0.1:{shared['port']}"
    if instance is not None:
        instance.publish(url)
    from app import restart
    restart.note_serving(url, windowed=True)
    shared["url"] = url
    print(f"[desktop] serving at {url}")
    print(f"[desktop] ready after {time.perf_counter() - begun:.1f} s ({', '.join(f'{id} {s:.1f}' for id, s in took)})")          # where the time of a start goes


def _run_with_splash(instance, title: str = "AI Vulnerability Framework") -> bool:
    """Open the splash first, start the app behind it, then hand over to the main window (created before the splash is closed, because Pyloid
    ends the program when its last window goes). False when Pyloid is not available, so the caller can use a browser instead."""
    try:
        from pyloid.ipc import Bridge, PyloidIPC
        from PySide6 import QtCore
    except Exception as e:
        print(f"[desktop] Pyloid unavailable ({e}); trying Chromium app mode. Install it with `pip install -r requirements.txt`.")
        return False
    from app import splash, startup, webengine
    webengine.configure()                                                  # the setting for drawing the window without the graphics card, before the web engine starts
    model = startup.Startup()
    shared: dict = {"url": None, "retry": False, "port": None}

    class SplashIPC(PyloidIPC):
        @Bridge(result=str)
        def retry(self):
            shared["retry"] = True
            return "ok"

        @Bridge(result=str)
        def quit(self):
            QtCore.QCoreApplication.quit()
            return "ok"

    app = _make_app(title)
    screen = app.create_window(title=title, width=560, height=340, frame=False, IPCs=[SplashIPC()])
    screen.load_html(splash.html())
    screen.show_and_focus()
    _center(screen)

    def start():
        model.reset()
        shared["url"] = None
        threading.Thread(target=_boot, args=(model, shared, instance), daemon=True, name="boot").start()

    state: dict = {"main": None}

    def tick():
        if state["main"] is not None:
            timer.stop()                                           # the splash is closing: nothing more to draw on it
            return
        screen._window.web_view.page().runJavaScript(splash.update_script(model.snapshot()))
        if shared["retry"]:
            shared["retry"] = False
            start()
        elif model.ready and shared["url"] and state["main"] is None:
            screen._window.web_view.page().runJavaScript(splash.update_script(model.snapshot()))      # the last picture: everything done
            state["main"] = _create_main_window(app, shared["url"], title)
            QtCore.QTimer.singleShot(500, screen.close)            # the main window exists now; the splash may go

    timer = QtCore.QTimer()
    timer.setInterval(120)
    timer.timeout.connect(tick)
    timer.start()
    start()
    app.run()
    return True


def _open_window(url: str, title: str = "AI Vulnerability Framework"):
    # 1) Pyloid native window (QtWebEngine). One window, one endpoint.
    try:
        from app import webengine
        webengine.configure()
        app = _make_app(title)
        _create_main_window(app, url, title)
        app.run()
        return
    except Exception as e:
        print(f"[desktop] Pyloid unavailable ({e}); trying Chromium app mode. "
              f"Install it with `pip install -r requirements.txt`.")
    # 2) Chromium in --app mode (chromeless window).
    chrome = _find_chromium()
    if chrome:
        try:
            subprocess.run([chrome, f"--app={url}", "--no-first-run",
                            "--no-default-browser-check",
                            f"--user-data-dir={ROOT / '.chrome-profile'}"])
            return
        except Exception as e:
            print(f"[desktop] Chromium app mode failed ({e}); opening default browser")
    # 3) Default browser, then keep the server alive.
    import webbrowser
    webbrowser.open(url)
    _serve_headless(url)



BUILD_LOG = ROOT / "data" / "logs" / "frontend_build.log"


def _npm(args: list[str], cwd: pathlib.Path, on_line=None) -> tuple[bool, list[str]]:
    """Run npm cross-platform and answer ``(it succeeded, the last lines of its output)``. Every line of output goes to ``on_line`` as it comes (the splash shows the latest one) and the
    whole output to ``BUILD_LOG``. On Windows npm is npm.cmd (a batch file) which must go through cmd.exe, so a plain subprocess of 'npm' fails with WinError 2."""
    if not shutil.which("npm"):
        return False, []
    cmd = (["cmd", "/c", "npm", *args] if os.name == "nt" else ["npm", *args])
    BUILD_LOG.parent.mkdir(parents=True, exist_ok=True)
    tail: list[str] = []
    with open(BUILD_LOG, "a", encoding="utf-8") as log:
        log.write(f"\n$ npm {' '.join(args)}\n")
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            log.write(line + "\n")
            tail = (tail + [line])[-40:]
            if on_line and line.strip():
                on_line(line.strip())
        return proc.wait() == 0, tail


def _ensure_frontend_built(note=None) -> dict:
    """Build the React UI if the bundle is missing or its sources changed (decided by content, app/frontend_build.py). The built ``dist/`` is
    gitignored, so after pulling new code the old bundle is still on disk; ``npm install`` runs only when the lock file changed. ``note`` receives what npm prints, line by line."""
    if PORTABLE:
        if (ROOT / "app" / "frontend" / "dist" / "index.html").is_file():
            return {"state": "current", "installed": False, "reason": None}
        return {"state": "missing", "installed": False, "reason": "The interface files are missing. Extract the complete desktop package again."}
    from app import frontend_build
    say = note or print
    return frontend_build.ensure_built(ROOT / "app" / "frontend", lambda args, cwd: _npm(args, cwd, note), say=say, log=BUILD_LOG.relative_to(ROOT).as_posix())


def _claim_instance():
    """Take the single-instance lock before anything else starts. Returns the lock, or None when another copy of the app is already running
    (that copy is asked to bring its window forward, and this one has nothing more to do). A restart needs no waiting here: the next copy is started
    when the old one has ended (app/restart.py)."""
    from app import single_instance
    from app.backend import paths
    folder = ROOT / paths.INTERIM
    instance = single_instance.acquire(folder)
    if instance is not None:
        import atexit
        atexit.register(instance.release)
        return instance
    from app.backend import security
    shown = single_instance.ask_to_show(folder, security.TOKEN)
    print("[desktop] the app is already running" + ("; its window was brought forward" if shown else " (its window could not be reached; close it from the task manager if it is stuck)"))
    return None


def main():
    """The process that was started is a watcher (app/restart.py): it runs the app as a child in the same terminal and starts it again when it asks for a restart. The child, which
    is this function in another process, runs the app and ends through ``restart.leave`` once its shutdown has run."""
    _prepare_console_streams()
    from app import restart
    code = restart.supervise()                                    # first of all, before anything heavy is imported
    if code is not None:
        sys.exit(code)
    try:
        _run_app()
    except SystemExit as e:                                       # Pyloid ends its event loop with sys.exit
        restart.leave(e.code if isinstance(e.code, int) else (0 if e.code is None else 1))
    restart.leave(0)


def _run_app():
    from app import restart
    _prepare_portable_environment()
    instance = _claim_instance()
    if instance is None:
        return
    from app.backend import shell_api
    restart.remember_instance(instance)
    shell_api.set_reboot_handler(restart.reboot, restart.exit_soon)
    from app import jobobject
    jobobject.contain_current_process()             # on Windows every process the app starts ends with it, however it ends
    try:
        headless = os.getenv("ZTSA_HEADLESS") == "1"
        if not headless and _has_display() and _run_with_splash(instance):
            return
        _ensure_frontend_built()
        port = int(os.getenv("ZTSA_PORT", "0")) or _free_port()
        url = f"http://127.0.0.1:{port}"
        threading.Thread(target=_serve, args=(port,), daemon=True).start()
        if not _wait_until_up(f"{url}/api/health"):
            print("[desktop] server did not start in time")
            return
        print(f"[desktop] serving at {url}")
        instance.publish(url)
        restart.note_serving(url, windowed=not headless and _has_display())
        from app.backend import slowcache
        if slowcache.prewarm_enabled():
            slowcache.prewarm(url)
        if headless:
            print("[desktop] headless mode (ZTSA_HEADLESS=1)")
            _serve_headless(url)
            return
        if not _has_display():
            print("[desktop] no display found (headless host); serving without a window. "
                  "Set ZTSA_HEADLESS=1 to silence this, or run on a desktop for a native window.")
            _serve_headless(url)
            return
        _open_window(url)
    finally:
        from app.backend import lifecycle
        lifecycle.shutdown()                        # stop the jobs and record them, close the databases, mark the exit as clean


if __name__ == "__main__":
    main()
