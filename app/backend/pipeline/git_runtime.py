"""Resolve Git's executable without changing the host PATH."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path

from app.backend.pipeline.agentic.exec import toolchain


def find_git(root: Path | None = None) -> Path | None:
    try:
        platform = toolchain.platform_key()
    except toolchain.ToolchainError:
        platform = ""
    if platform.startswith("windows-"):
        managed = toolchain.find("git", root)
        if managed is not None:
            return managed
    system_git = shutil.which("git")
    return Path(system_git).resolve() if system_git else None


def git_executable() -> str:
    found = find_git()
    return str(found) if found else "git"


_no_hooks: str | None = None
_no_hooks_lock = threading.Lock()


def _empty_hooks_dir() -> str:
    """A private empty folder for core.hooksPath, created once per process."""
    global _no_hooks
    with _no_hooks_lock:
        if _no_hooks is None or not Path(_no_hooks).is_dir():
            _no_hooks = tempfile.mkdtemp(prefix="aivf-git-no-hooks-")
        return _no_hooks


def isolated_command(*args: str, root: Path | str | None = None) -> list[str]:
    """A Git command that runs no repository-configured program on this computer: no fsmonitor and no hooks
    (a project repository can configure both, and Git runs them on index reads and ref updates)."""
    command = [git_executable()]
    if root is not None:
        command += ["-C", str(root)]
    return command + ["-c", "core.fsmonitor=false", "-c", f"core.hooksPath={_empty_hooks_dir()}", *args]


def isolated_env() -> dict[str, str]:
    """The environment for ``isolated_command``: no inherited GIT_* overrides (except the ceiling used to bound
    repository discovery) and no optional locks on the user's repository."""
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_") or key.upper() == "GIT_CEILING_DIRECTORIES"}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env
