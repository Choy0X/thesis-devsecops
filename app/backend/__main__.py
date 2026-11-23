"""Run the backend as a process of its own: ``python -m app.backend [--port N] [--host H]``.

It starts the same application as the desktop launcher (the API, the built interface, the jobs) with its own uvicorn server, ends cleanly on Ctrl+C, SIGTERM
or Ctrl+Break, and prints one line, ``READY <url>``, when it answers, so a supervisor (``app/supervisor.py``) can tell the moment it is up. Startup
failures print their traceback to the same output, which the supervisor keeps in a log file.
"""
from __future__ import annotations

import argparse
import signal
import socket
import threading
import time


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        parser.error("the backend listens on this computer only")
    port = args.port or _free_port()

    import uvicorn
    server = uvicorn.Server(uvicorn.Config("app.backend.api:app", host=args.host, port=port, log_level=args.log_level))

    def stop(*_):
        server.should_exit = True

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):             # SIGBREAK is Ctrl+Break on Windows
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), stop)
            except (ValueError, OSError):
                pass                                          # ignored: signals can only be set from the main thread of the main interpreter

    def announce():
        while not server.started and not server.should_exit:
            time.sleep(0.05)
        if server.started:
            print(f"READY http://{args.host}:{port}", flush=True)

    threading.Thread(target=announce, daemon=True).start()
    server.run()
    from app.backend import lifecycle
    lifecycle.shutdown()                                      # the jobs it started are stopped and recorded, the databases closed
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
