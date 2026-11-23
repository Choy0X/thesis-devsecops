"""Hidden Windows ConPTY broker. JSON control uses pipes; the shell uses ConPTY.

Launched only by TerminalManager, already assigned to a private Windows job.
Does not import app configuration, provider settings, or agent runtime state.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time


def main():
    if os.name != "nt":
        raise RuntimeError("Windows ConPTY is required.")
    from winpty import PTY, Backend

    workspace, shell, cols, rows = sys.argv[1:]
    output_lock = threading.Lock()
    stopped = threading.Event()

    def emit(event):
        with output_lock:
            sys.stdout.write(json.dumps(event, ensure_ascii=True) + "\n")
            sys.stdout.flush()

    # Explicit ConPTY. There is deliberately no WinPTY or pipe-shell fallback.
    pty = PTY(int(cols), int(rows), backend=Backend.ConPTY)
    bootstrap = ("[Console]::InputEncoding = [Text.UTF8Encoding]::new($false); "
                 "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
                 "$OutputEncoding = [Console]::OutputEncoding; "
                 "Remove-Module PSReadLine -ErrorAction SilentlyContinue")
    env = dict(os.environ)
    # pwsh's built-in modules are beside its executable.
    builtin = os.path.join(os.path.dirname(shell), "Modules")
    if os.path.isdir(builtin):
        env["PSModulePath"] = builtin
    env_string = "\0".join(k + "=" + v for k, v in sorted(env.items())) + "\0"
    command = " " + subprocess.list2cmdline(["-NoLogo", "-NoProfile", "-NoExit", "-Command", bootstrap])
    # Ignoring Ctrl+C is inherited by child processes. When the app was started by a launcher that ignores it, the
    # shell would ignore the Ctrl+C the user types too; restore normal handling so the shell receives it.
    import ctypes
    ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
    if not pty.spawn(shell, cmdline=command, cwd=workspace, env=env_string):
        raise RuntimeError("ConPTY could not spawn the PowerShell shell.")
    emit({"type": "ready", "backend": "conpty", "pid": pty.pid})

    def controls():
        try:
            for line in sys.stdin:
                request = json.loads(line)
                response = {"type": "ack", "request": request.get("request")}
                try:
                    if request["type"] == "write":
                        pty.write(request["data"])
                    elif request["type"] == "resize":
                        pty.set_size(request["cols"], request["rows"])
                    else:
                        raise ValueError("Unsupported terminal control.")
                except Exception as exc:
                    response["error"] = "ConPTY control failed: " + type(exc).__name__
                emit(response)
        finally:
            stopped.set()

    threading.Thread(target=controls, daemon=True, name="terminal-controls").start()
    finished_at = None
    exit_code = None
    while not stopped.is_set():
        try:
            data = pty.read(blocking=False)
            if data:
                # Bound protocol records, even if the native reader batches a flood.
                for offset in range(0, len(data), 4096):
                    emit({"type": "output", "data": data[offset:offset + 4096]})
        except Exception:
            if pty.isalive():
                raise
            data = ""
        if not pty.isalive():
            if finished_at is None:
                finished_at = time.monotonic()
                exit_code = pty.get_exitstatus()
            # Drain trailing VT output before reporting exit to the manager.
            if not data and time.monotonic() - finished_at >= 0.25:
                emit({"type": "exit", "code": exit_code})
                return
        time.sleep(0.01)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        sys.stdout.write(json.dumps({"type": "error", "message": "Windows ConPTY broker failed: " + str(exc)[:300]}) + "\n")
        sys.stdout.flush()
        os._exit(1)
    # Closing the job in the manager cleans every child, including descendants
    # that outlived the shell. Avoid extension teardown waiting on open ConPTY IO.
    os._exit(0)
