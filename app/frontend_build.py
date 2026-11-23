"""Is the built interface up to date with its sources? Decided by content, not by file times.

A modification time moves when a file is touched, checked out or copied, so it rebuilt the interface when nothing changed and, worse,
missed a change when a file came back with an old time. The decision here is a SHA-256 over the text of the sources and configuration, kept in a
stamp next to the build; ``npm install`` runs only when the lock file changed (or ``node_modules`` is missing), because that is the slow step.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
import subprocess
from typing import Callable

CONFIG_FILES = ("package.json", "index.html", "vite.config.ts", "tailwind.config.js", "postcss.config.js", "tsconfig.json", "tsconfig.app.json",
                "tsconfig.node.json", "components.json")
LOCK = "package-lock.json"
BUILD_STAMP = "dist/.build-stamp.json"
INSTALL_STAMP = "node_modules/.install-stamp.json"


def _hash_files(root: pathlib.Path, files: list[pathlib.Path]) -> str:
    h = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        h.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        h.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")        # line endings differ between checkouts, the text does not
    return h.hexdigest()


def sources_digest(fe: pathlib.Path) -> str:
    """One digest of the sources (``src/``, the configuration files and the lock file)."""
    files = [p for p in (fe / "src").rglob("*") if p.is_file()] if (fe / "src").is_dir() else []
    files += [fe / n for n in (*CONFIG_FILES, LOCK) if (fe / n).is_file()]
    return _hash_files(fe, files)


def lock_digest(fe: pathlib.Path) -> str | None:
    lock = fe / LOCK
    return hashlib.sha256(lock.read_bytes().replace(b"\r\n", b"\n")).hexdigest() if lock.is_file() else None


def _read(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_is_current(fe: pathlib.Path) -> bool:
    return (fe / "dist" / "index.html").is_file() and _read(fe / BUILD_STAMP).get("sources") == sources_digest(fe)


def install_is_current(fe: pathlib.Path) -> bool:
    return (fe / "node_modules").is_dir() and _read(fe / INSTALL_STAMP).get("lock") == lock_digest(fe)


MIN_NODE = (18, 0)


def node_version() -> tuple[int, ...] | None:
    """The version of Node.js on the path, or None when there is none."""
    exe = shutil.which("node")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else None


_CAUSES = [
    (re.compile(r"ENOTFOUND|EAI_AGAIN|ETIMEDOUT|ECONNREFUSED|ECONNRESET|network|getaddrinfo|registry\.npmjs", re.I),
     "npm could not reach the package registry. Check the network (or a proxy) and start the app again."),
    (re.compile(r"ENOSPC|no space left", re.I), "The disk is full, so npm could not write its files. Free some space and start the app again."),
    (re.compile(r"EACCES|EPERM|EBUSY", re.I), "npm was not allowed to write in the app's folder, or a file in it is in use. Close other programs that use it and start the app again."),
]


def explain_failure(step: str, tail: list[str], log: str | None = None) -> str:
    """A failed ``npm install`` or ``npm run build`` in plain words: the cause when the output names one, else the line that looks like the error, and where the full output is."""
    text = "\n".join(tail)
    for rx, words in _CAUSES:
        if rx.search(text):
            return words + (f" The full output is in {log}." if log else "")
    ts = next((l.strip() for l in tail if re.search(r"error TS\d+", l)), None)
    if ts:
        return f"The interface did not compile: {ts[:200]}" + (f" The full output is in {log}." if log else "")
    if re.search(r"Cannot find module|Module not found|Could not resolve", text):
        return "A dependency of the interface is missing or damaged. Delete app/frontend/node_modules and start the app again." + (f" The full output is in {log}." if log else "")
    last = next((l.strip() for l in reversed(tail) if l.strip() and not l.strip().startswith(">")), "")
    what = "npm install did not finish" if step == "install" else "the interface build did not finish"
    return f"{what}" + (f": {last[:200]}" if last else "") + (f". The full output is in {log}." if log else ".")


Runner = Callable[..., "bool | tuple[bool, list[str]]"]


def ensure_built(fe: pathlib.Path, run: Runner, say: Callable[[str], None] = print, log: str | None = None, version: Callable[[], tuple[int, ...] | None] = node_version) -> dict:
    """Bring ``dist/`` up to date. ``run(args, cwd)`` runs npm and answers whether it worked, or ``(worked, last lines of its output)``, which lets a failure be explained.
    Returns what happened: ``{"state": "current" | "built" | "stale" | "missing" | "failed", "installed": bool, "reason": str | None}``."""
    if build_is_current(fe):
        return {"state": "current", "installed": False, "reason": None}
    has_dist = (fe / "dist" / "index.html").is_file()
    if not shutil.which("npm") or not (fe / "package.json").is_file():
        say("[desktop] npm unavailable; serving the existing (possibly stale) UI build" if has_dist else "[desktop] frontend not built and npm unavailable; serving backend only")
        return {"state": "stale" if has_dist else "missing", "installed": False,
                "reason": "Node.js (npm) was not found, so the interface could not be rebuilt. Install Node.js 18 or newer and start the app again."}
    have = version()
    if have is not None and have < MIN_NODE:
        say(f"[desktop] Node.js {'.'.join(map(str, have))} is too old to build the UI")
        return {"state": "stale" if has_dist else "failed", "installed": False,
                "reason": f"Node.js {'.'.join(map(str, have))} is older than the 18 the interface needs to be built. Install Node.js 18 or newer and start the app again."}

    def go(args: list[str]) -> tuple[bool, list[str]]:
        res = run(args, fe)
        return (res, []) if isinstance(res, bool) else (bool(res[0]), list(res[1]))

    installed = False
    if not install_is_current(fe):
        say("[desktop] installing frontend dependencies (the lock file changed)…" if (fe / "node_modules").is_dir() else "[desktop] installing frontend dependencies (first run)…")
        ok, tail = go(["install", "--no-audit", "--no-fund"])
        if not ok:
            return {"state": "failed", "installed": False, "reason": explain_failure("install", tail, log)}
        (fe / INSTALL_STAMP).parent.mkdir(parents=True, exist_ok=True)
        (fe / INSTALL_STAMP).write_text(json.dumps({"lock": lock_digest(fe)}), encoding="utf-8")
        installed = True
    say("[desktop] building the UI (sources changed)…" if has_dist else "[desktop] building the UI…")
    digest = sources_digest(fe)
    ok, tail = go(["run", "build"])
    if not ok or not (fe / "dist" / "index.html").is_file():
        return {"state": "failed", "installed": installed, "reason": explain_failure("build", tail, log)}
    (fe / BUILD_STAMP).write_text(json.dumps({"sources": digest}), encoding="utf-8")
    return {"state": "built", "installed": installed, "reason": None}
