"""Copying a project folder into a container as a tar stream, without ever bind-mounting the host: shared by every subsystem that needs the same guarantees -- nothing that looks like
a credential, a version-control folder or a cache travels, no link of any kind is followed or copied, and every item is checked to still lie inside the folder after its real path is
resolved; a copy above a size or file-count limit is refused instead of cut quietly. Factored out of ``scan/sandbox_run.py`` (the scan sandbox, a fresh container per scan) so the agent
sandbox (``agent_sandbox.py``, one long-lived container per project) can seed its own copy the exact same, already-hardened way, rather than a second, independently-reasoned-about
implementation. ``sandbox.py``'s own ``_put_dir``/``_EXCLUDE_NAMES`` are a separate, weaker filter (no per-item realpath check) used only by the general-purpose Sandboxes page's own
``scan_target()`` dialog; they are left as they are, since that page's copy is reviewed by the person using it and does not sit inside a container for a whole session.
"""
from __future__ import annotations

import fnmatch
import io
import os
import pathlib
import re
import stat as statmod
import tarfile

from app.backend.pipeline import projects

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_COPY_FILES = 50_000
MAX_COPY_BYTES = 500 * 1024 * 1024

# what is not copied: whole names (any folder or file called so), and patterns for files that look like keys or credentials
SKIP_NAMES = projects.SKIP_DIRS | {".git", ".venv", "venv", "__pycache__", "node_modules", ".chrome-profile"}
SECRET_NAMES = {".env", ".aws", ".ssh", ".docker", ".gnupg", "settings.json", ".api_token"}
SECRET_PATTERNS = (".env*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc", "*.db", "*.db-wal", "*.db-shm")


class CopyRefused(ValueError):
    """The folder is too large to copy as it is, or is not a folder."""


def skip_reason(name: str, is_dir: bool = False) -> str | None:
    """Why a file or folder of this name is not copied, or None."""
    low = name.lower()
    if name in SECRET_NAMES or any(fnmatch.fnmatch(low, p) for p in SECRET_PATTERNS):
        return "secret_like"
    if name in SKIP_NAMES or (is_dir and name.startswith(".") and name != ".github"):
        return "skipped_folders_and_caches"
    return None


def is_link(path) -> bool:
    """A symbolic link, or on Windows any reparse point (a directory junction looks like an ordinary folder to ``tar`` and to ``lstat``, and can lead anywhere on the disk)."""
    try:
        if os.path.islink(path):
            return True
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
        return bool(attrs & getattr(statmod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return True                                                         # what cannot be examined is not copied


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "project"


def copy_stream(src: pathlib.Path, arcname: str, *, uid: int, max_file_bytes: int = MAX_FILE_BYTES, max_files: int = MAX_COPY_FILES, max_bytes: int = MAX_COPY_BYTES) -> tuple[bytes, dict]:
    """The folder ``src`` as a tar stream whose top folder is ``arcname``, every entry owned by ``uid``, and a summary of what was and was not copied (names of secret-like files, no
    contents). Nothing that is a link of any kind is followed or copied, and every item is checked to still lie inside the folder after its real path is resolved."""
    if not src.is_dir():
        raise CopyRefused(f"{src} is not a folder.")
    real_root = os.path.realpath(src)
    summary = {"files": 0, "bytes": 0, "skipped": {"skipped_folders_and_caches": 0, "secret_like": 0, "links": 0, "not_plain": 0, "too_large": 0}, "secret_like_names": []}

    def keep(info: tarfile.TarInfo):
        base = pathlib.PurePosixPath(info.name).name
        if info.name == arcname:
            info.uid = info.gid = uid
            info.uname = info.gname = ""
            info.mode = 0o755
            return info
        reason = skip_reason(base, info.isdir())
        if reason:
            summary["skipped"][reason] += 1
            if reason == "secret_like" and len(summary["secret_like_names"]) < 50:
                summary["secret_like_names"].append(info.name[len(arcname) + 1:])
            return None
        host = os.path.join(str(src), *info.name[len(arcname) + 1:].split("/"))
        if info.issym() or info.islnk() or is_link(host) or os.path.commonpath([real_root, os.path.realpath(host)]) != real_root:
            summary["skipped"]["links"] += 1
            return None
        if not (info.isfile() or info.isdir()):
            summary["skipped"]["not_plain"] += 1
            return None
        if info.isfile():
            if info.size > max_file_bytes:
                summary["skipped"]["too_large"] += 1
                return None
            summary["files"] += 1
            summary["bytes"] += info.size
            if summary["files"] > max_files or summary["bytes"] > max_bytes:
                raise CopyRefused(f"{src.name} is larger than a copy-in accepts ({max_files:,} files or {max_bytes // 1048576} MB of source). Use a smaller folder, or exclude what is not source.")
        info.uid = info.gid = uid
        info.uname = info.gname = ""
        info.mode = 0o755 if info.isdir() else 0o644
        return info

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(src), arcname=arcname, filter=keep)
    return buf.getvalue(), summary
