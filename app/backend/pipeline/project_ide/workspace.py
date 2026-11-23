"""Per-chat source snapshots, optimistic file saves, and reviewed integration.

Git worktrees separate files and history; they are not a shell sandbox.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import shutil
import stat
import subprocess
import threading
import uuid

from app.backend.pipeline.git_runtime import isolated_command, isolated_env
from .resources import Cancelled

EXCLUDED_DIRS = frozenset({".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".next", ".cache", ".tmp", ".superpowers", "graphify-out", "vendor", "dist", "build", "coverage", ".ssh", ".aws", ".azure", ".gnupg"})
EXCLUDED_FILES = frozenset({".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json", "id_rsa", "id_ed25519"})
_lock = threading.RLock()


class WorkspaceError(ValueError):
    pass


class Conflict(WorkspaceError):
    pass


def excluded(path: str) -> bool:
    parts = Path(path).parts
    return any(p.lower() in EXCLUDED_DIRS for p in parts) or any(
        p.lower() in EXCLUDED_FILES or p.lower().startswith(".env") or p.lower().endswith((".pem", ".key", ".pfx", ".p12")) for p in parts)


def linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _check_active(cancelled):
    if cancelled is not None and cancelled.is_set():
        raise Cancelled("Workspace preparation was cancelled.")


def files(root: Path, *, cancelled=None) -> dict[str, Path]:
    found = {}
    try:
        listing = subprocess.run(isolated_command("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", ".", root=root),
                                 capture_output=True, timeout=30, env=isolated_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if listing.returncode == 0:
            for name in listing.stdout.decode("utf-8", errors="surrogateescape").split("\0"):
                _check_active(cancelled)
                if not name or excluded(name):
                    continue
                try:
                    path = bound(root, name)
                except WorkspaceError:
                    continue
                if path.is_file() and not linked(path):
                    found[name] = path
            # An explicitly selected non-Git folder can live below an ignored
            # directory in the application's checkout. That parent is not its project.
            ignored_root = subprocess.run(isolated_command("check-ignore", "--quiet", "--", ".", root=root),
                                          capture_output=True, timeout=10, env=isolated_env(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if found or ignored_root.returncode != 0 or (Path(root) / ".git").exists():
                return found
    except (OSError, subprocess.TimeoutExpired):
        pass
    for folder, dirs, names in os.walk(root, followlinks=False):
        _check_active(cancelled)
        parent = Path(folder)
        dirs[:] = [name for name in dirs if not excluded(str((parent / name).relative_to(root))) and not linked(parent / name)]
        for name in names:
            _check_active(cancelled)
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if not excluded(relative) and not linked(path) and path.is_file():
                found[relative] = path
    return found


def digest(path: Path) -> str | None:
    if not path.exists():
        return None
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def bound(root: Path, relative: str) -> Path:
    root = Path(root).resolve()
    if not relative or Path(relative).is_absolute() or PureWindowsPath(relative).drive or ":" in relative or excluded(relative):
        raise WorkspaceError("The requested path is outside managed project context or is excluded.")
    path = root / relative
    if ".." in Path(relative).parts or not path.resolve().is_relative_to(root):
        raise WorkspaceError("The requested path escapes the workspace.")
    current = path
    while current != root:
        if current.exists() and linked(current):
            raise WorkspaceError("Links and junctions are not managed source files.")
        current = current.parent
    return path


def _git(root: Path, *args: str, check: bool = True):
    result = subprocess.run(isolated_command(*args, root=root), capture_output=True, text=True, env=isolated_env(),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=60)
    if check and result.returncode:
        raise WorkspaceError("Git workspace provisioning failed: " + result.stderr.strip()[:600])
    return result


def _inspect_git(root: Path, *args: str, check: bool = True):
    try:
        result = subprocess.run(
            isolated_command("--literal-pathspecs", "-c", "core.quotePath=false", *args, root=root),
            capture_output=True, encoding="utf-8", errors="replace", env=isolated_env(), timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise WorkspaceError("Git took too long to read the checkout.") from error
    if check and result.returncode:
        raise WorkspaceError(result.stderr.strip()[:400] or "Git could not read the project checkout.")
    return result


def _checkout(root: Path) -> tuple[dict, str]:
    if not root.is_dir():
        raise WorkspaceError("The project folder is unavailable.")
    prefix = _inspect_git(root, "rev-parse", "--show-prefix", check=False)
    if prefix.returncode or _inspect_git(root, "check-ignore", "--quiet", "--", ".", check=False).returncode == 0:
        return {"available": False, "branch": None, "revision": None}, ""
    branch = _inspect_git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    head = _inspect_git(root, "rev-parse", "--verify", "HEAD", check=False)
    return {"available": True, "branch": branch.stdout.rstrip("\n") if not branch.returncode else None,
            "revision": head.stdout.strip() if not head.returncode else None}, prefix.stdout.rstrip("\n")


def _checkout_status(root: Path, prefix: str, path: str = ".") -> list[dict]:
    listing = _inspect_git(root, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all", "--", path)
    rows = []
    for entry in listing.stdout.split("\0"):
        if len(entry) < 4 or "\ufffd" in entry:
            continue
        status, name = entry[:2], entry[3:]
        if not name.startswith(prefix):
            continue
        relative = name[len(prefix):]
        try:
            bound(root, relative)
        except WorkspaceError:
            continue
        rows.append({"path": relative, "status": status})
    return sorted(rows, key=lambda row: row["path"])


def checkout_changes(root: Path) -> dict:
    """List uncommitted changes from the selected project directory's active Git checkout."""
    identity, prefix = _checkout(root)
    rows = _checkout_status(root, prefix) if identity["available"] else []
    return {**identity, "files": rows[:2000], "truncated": len(rows) > 2000}


def checkout_diff(root: Path, relative: str) -> dict:
    """Read index, working tree and untracked changes without modifying the checkout."""
    path = bound(root, relative)
    identity, prefix = _checkout(root)
    if not identity["available"]:
        raise WorkspaceError("The project folder is not a Git checkout.")
    status = next((row["status"] for row in _checkout_status(root, prefix, relative) if row["path"] == relative), "")
    if not status:
        return {**identity, "diffs": []}

    size_limit = 4 * 1024 * 1024
    object_path = prefix + relative
    for object_name in (":" + object_path, "HEAD:" + object_path):
        size = _inspect_git(root, "cat-file", "-s", object_name, check=False)
        if not size.returncode and int(size.stdout) > size_limit:
            return {**identity, "diffs": [], "message": "This file exceeds the 4 MiB diff limit."}
    if path.is_file() and path.stat().st_size > size_limit:
        return {**identity, "diffs": [], "message": "This file exceeds the 4 MiB diff limit."}

    diffs = []
    if status == "??":
        with path.open("rb") as stream:
            raw = stream.read(size_limit + 1)
        if len(raw) > size_limit:
            return {**identity, "diffs": [], "message": "This file exceeds the 4 MiB diff limit."}
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = None
        if content is None or "\0" in content:
            return {**identity, "diffs": [], "message": "This untracked file is binary."}
        lines = difflib.unified_diff([], content.splitlines(keepends=True), fromfile="/dev/null", tofile="b/" + relative)
        diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
        diffs.append(("untracked", diff))
    else:
        options = ("--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--relative")
        if status[0] != " ":
            diffs.append(("staged", _inspect_git(root, "diff", "--cached", *options, "--", relative).stdout))
        if status[1] != " ":
            diffs.append(("unstaged", _inspect_git(root, "diff", *options, "--", relative).stdout))
    return {**identity, "diffs": [{"kind": kind, "diff": diff[:200000], "truncated": len(diff) > 200000} for kind, diff in diffs]}


def provision(source: Path, destination: Path, *, cancelled=None) -> dict:
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_dir() or source.parent == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise WorkspaceError("Choose distinct source and managed workspace folders.")
    if destination.exists():
        raise WorkspaceError("A workspace already exists at the requested destination.")
    _check_active(cancelled)
    inventory = files(source, cancelled=cancelled)
    if len(inventory) > 50000 or sum(p.stat().st_size for p in inventory.values()) > 2 * 1024 ** 3:
        raise WorkspaceError("The relevant source snapshot exceeds 50000 files or 2 GiB. Narrow the project source folder.")
    _check_active(cancelled)
    destination.mkdir(parents=True)
    checkout = destination / "files"
    registered_repository = None
    try:
        result = _git(source, "rev-parse", "--show-toplevel", check=False)
        ignored = result.returncode == 0 and _git(source, "check-ignore", "--quiet", "--", ".", check=False).returncode == 0
        root, revision = checkout, None
        if result.returncode == 0 and not ignored:
            repository = Path(result.stdout.strip()).resolve()
            head = _git(repository, "rev-parse", "HEAD", check=False)
            if head.returncode == 0:
                revision = head.stdout.strip()
                _git(repository, "worktree", "add", "--detach", "--no-checkout", str(checkout), revision)
                registered_repository = repository
                _git(checkout, "read-tree", "HEAD")
                root = checkout / source.relative_to(repository)
        root.mkdir(parents=True, exist_ok=True)
        if registered_repository is None:
            # A copy under the application's checkout needs its own Git boundary.
            _git(root, "init")
        baseline, backup = {}, destination / "baseline"
        for name, original in inventory.items():
            _check_active(cancelled)
            before = digest(original)
            target, saved = bound(root, name), bound(backup, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, saved)
            if digest(saved) != before or digest(original) != before:
                raise Conflict("Source changed during snapshot: " + name)
            shutil.copy2(saved, target)
            baseline[name] = before
        _check_active(cancelled)
        item = {"root": root, "source": source, "directory": destination, "baseline": baseline, "revision": revision,
                "kind": "worktree" if registered_repository is not None else "copy",
                "exclusions": sorted(EXCLUDED_DIRS | EXCLUDED_FILES) + [".env*", "*.pem", "*.key", "*.pfx", "*.p12", "links and junctions"]}
        _write(destination / "workspace.json", json.dumps(item, default=str, indent=2).encode("utf-8"))
        return item
    except BaseException:
        # Only clear the directory created by this attempt. Never remove the source checkout.
        if destination.resolve() == destination and checkout.resolve() == checkout:
            if registered_repository is not None:
                try:
                    _git(registered_repository, "worktree", "remove", "--force", str(checkout), check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            shutil.rmtree(destination, ignore_errors=True)
        raise


def remove(item: dict) -> None:
    """Remove a conversation workspace: its Git worktree registration (when it is one) and its managed folder.
    The source checkout is never touched."""
    directory = Path(item["directory"]).resolve()
    checkout = directory / "files"
    if item.get("kind") == "worktree":
        try:
            top = _git(Path(item["source"]), "rev-parse", "--show-toplevel", check=False)
            if top.returncode == 0:
                repository = Path(top.stdout.strip())
                _git(repository, "worktree", "remove", "--force", str(checkout), check=False)
                _git(repository, "worktree", "prune", check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    shutil.rmtree(directory, ignore_errors=True)


def load(directory: Path) -> dict:
    item = json.loads((Path(directory) / "workspace.json").read_text(encoding="utf-8"))
    for key in ("root", "source", "directory"):
        item[key] = Path(item[key])
    if not item["root"].resolve().is_relative_to(item["directory"].resolve()):
        raise WorkspaceError("The workspace manifest points outside its managed directory.")
    return item


def read(item: dict, relative: str) -> dict:
    path = bound(item["root"], relative)
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise WorkspaceError("The file is missing or exceeds the 4 MiB editor limit.")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise WorkspaceError("The editor supports UTF-8 text files only.") from exc
    if "\x00" in text:
        raise WorkspaceError("Binary files cannot be edited as text.")
    return {"path": relative, "text": text, "version": hashlib.sha256(raw).hexdigest()}


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".ide-" + uuid.uuid4().hex)
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save(item: dict, relative: str, text: str, version: str | None) -> dict:
    content = text.encode("utf-8")
    if len(content) > 4 * 1024 * 1024:
        raise WorkspaceError("The editor save exceeds 4 MiB.")
    with _lock:
        path = bound(item["root"], relative)
        if digest(path) != version:
            raise Conflict("This file changed after it was opened. Reload or keep your buffer before saving.")
        _write(path, content)
        return read(item, relative)


def changes(item: dict) -> list[dict]:
    current = files(item["root"])
    result = []
    for name in sorted(set(current) | set(item["baseline"]) | set(item.get("integration_baseline", {}))):
        cursor = item.get("integration_baseline", {})
        before = cursor.get(name, item["baseline"].get(name))
        after = digest(current[name]) if name in current else None
        if before != after:
            old = bound(item["directory"] / ("integration-baseline" if name in cursor else "baseline"), name)
            old_text = old.read_bytes().decode("utf-8", errors="replace") if old.exists() else ""
            new_text = current[name].read_bytes().decode("utf-8", errors="replace") if name in current else ""
            diff = "".join(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True), fromfile="a/" + name, tofile="b/" + name))
            result.append({"path": name, "before": before, "after": after, "status": "added" if before is None else "deleted" if after is None else "modified", "diff": diff[:200000]})
    return result


def integrate(item: dict, selected: list[str], review: dict | None = None) -> dict:
    """Apply explicitly reviewed paths; never commit and never overwrite source drift."""
    if not selected or len(selected) != len(set(selected)):
        raise WorkspaceError("Select distinct changed paths for integration.")
    from app.backend.pipeline.oslock import FileLock
    lock_name = hashlib.sha256(str(item["source"].resolve()).casefold().encode()).hexdigest()
    with _lock, FileLock(item["directory"].parent / ("integration-" + lock_name + ".lock"), note=False):
        item.update(load(item["directory"]))
        available = {row["path"]: row for row in changes(item)}
        if any(name not in available for name in selected):
            raise Conflict("The selected change set is no longer available.")
        if review is not None and any(name not in review or review[name] != available[name]["after"] for name in selected):
            raise Conflict("The reviewed workspace bytes changed. Review the diff again.")
        cursor = item.get("integration_baseline", {})
        originals, desired = {}, {}
        for name in selected:
            target = bound(item["source"], name)
            if digest(target) != cursor.get(name, item["baseline"].get(name)):
                raise Conflict("Original source changed since this chat started: " + name)
            originals[name] = target.read_bytes() if target.exists() else None
            candidate = bound(item["root"], name)
            desired[name] = candidate.read_bytes() if candidate.exists() else None
            if digest(candidate) != available[name]["after"]:
                raise Conflict("Workspace changed during review: " + name)
        applied, saved_cursor = [], {}
        try:
            for name in selected:
                target = bound(item["source"], name)
                if digest(target) != cursor.get(name, item["baseline"].get(name)):
                    raise Conflict("Original source changed during integration: " + name)
                if desired[name] is None:
                    target.unlink()
                else:
                    _write(target, desired[name])
                applied.append(name)
            for name in selected:
                snapshot = bound(item["directory"] / "integration-baseline", name)
                saved_cursor[name] = snapshot.read_bytes() if snapshot.exists() else None
                if desired[name] is None:
                    snapshot.unlink(missing_ok=True)
                else:
                    _write(snapshot, desired[name])
            updated = {**item, "integration_baseline": {**cursor, **{name: available[name]["after"] for name in selected}}}
            _write(item["directory"] / "workspace.json", json.dumps(updated, default=str, indent=2).encode())
            item.update(updated)
        except BaseException:
            for name, content in saved_cursor.items():
                snapshot = bound(item["directory"] / "integration-baseline", name)
                if content is None:
                    snapshot.unlink(missing_ok=True)
                else:
                    _write(snapshot, content)
            for name in reversed(applied):
                target = bound(item["source"], name)
                written = hashlib.sha256(desired[name]).hexdigest() if desired[name] is not None else None
                if digest(target) != written:
                    raise Conflict("Concurrent source edit prevented rollback: " + name)
                if originals[name] is None:
                    target.unlink(missing_ok=True)
                else:
                    _write(target, originals[name])
            raise
        return {"integrated": applied, "source": str(item["source"])}
