"""Looking at the files of a sandbox container, putting one in, taking one out, and seeing what changed against the image it started from.

Every path is checked on the server: it must be absolute, with no ``..`` and no NUL, and it is looked at inside the container only (a container's files are never mapped to the host's). A
listing is read with ``ls`` in the container and shows names, sizes, kinds and times; a download is one regular file up to ``MAX_BYTES`` (a folder, a device or a link is not offered), and an
upload is one file, up to the same size, into a folder, never over an existing file unless the person says so. The difference against the base image is Docker's own (``container.diff``):
what was added, changed or deleted since the container started, with no file contents.
"""
from __future__ import annotations

import io
import posixpath
import tarfile

from app.backend import sandbox

MAX_BYTES = 25 * 1024 * 1024
MAX_LISTED = 1000
ARCHIVE_SLACK = 1024 * 1024                  # the tar headers and padding around one file of MAX_BYTES
DIR_MODE = 1 << 31                           # Go's os.ModeDir, as Docker reports a path's mode
KINDS = {0: "changed", 1: "added", 2: "deleted"}
_TYPES = {"d": "folder", "-": "file", "l": "link"}


class FileError(ValueError):
    """A path or a transfer that is refused, with the reason in words."""


def clean(path: str) -> str:
    """The absolute, normalised path inside the container, or ``FileError``."""
    if not isinstance(path, str) or not path or "\x00" in path or "\n" in path:
        raise FileError("Give a path inside the container, such as /work.")
    if not path.startswith("/"):
        raise FileError("A path inside the container starts with /, such as /work/app.py.")
    if ".." in path.split("/"):
        raise FileError("A path may not contain '..'.")
    return posixpath.normpath(path)


_FILE_ACTION = r'''
set -eu
op=$1; source=$2; destination=$3
root=$(cd /work && pwd -P)
check_parent() {
    parent=$(dirname -- "$1")
    resolved=$(cd -P -- "$parent" && pwd -P)
    case "$resolved" in "$root"|"$root"/*) ;; *) printf '%s\n' 'The path resolves outside the workspace.' >&2; exit 3 ;; esac
}
check_parent "$source"
case "$op" in
  mkdir) mkdir -- "$source"; test -d "$source" ;;
  delete) rm -r -- "$source"; test ! -e "$source" && test ! -L "$source" ;;
  move)
    check_parent "$destination"
    if test -e "$destination" || test -L "$destination"; then printf '%s\n' 'The destination already exists.' >&2; exit 3; fi
    mv -nT -- "$source" "$destination"
    test ! -e "$source" && test ! -L "$source"
    test -e "$destination" || test -L "$destination"
    ;;
  *) exit 3 ;;
esac
printf '%s\n' confirmed
'''


def mutate(container_id, operation, path, destination=None):
    if operation not in ('mkdir', 'delete', 'move'): raise FileError('Choose mkdir, move or delete.')
    def scoped(value):
        result = clean(value)
        if not result.startswith('/work/'):
            raise FileError('File changes are limited to entries inside /work; the workspace root is preserved.')
        return result
    source = scoped(path); target = scoped(destination) if operation == 'move' else ''
    if target == source: raise FileError('Choose a different destination.')
    ct = sandbox._ours(container_id)
    labels = getattr(ct, 'labels', {}) or {}
    if labels.get('aivdf.kind') == 'verification': raise FileError('Verification files are controlled by the verifier.')
    result = ct.exec_run(['/bin/sh', '-c', _FILE_ACTION, '--', operation, source, target], demux=False)
    output = (result.output or b'').decode('utf-8', 'replace').strip()
    if result.exit_code != 0 or output.splitlines()[-1:] != ['confirmed']:
        raise FileError(output[-1000:] or 'The file change was not confirmed inside the container.')
    return {'confirmed': True, 'operation': operation, 'path': source, 'destination': target or None}


def listing(container_id: str, path: str = "/work") -> dict:
    """The entries of a folder of the container, folders first."""
    ct = sandbox._ours(container_id)
    folder = clean(path)
    res = ct.exec_run(["ls", "-la", "--time-style=+%s", "--", folder], demux=False)
    text = res.output.decode("utf-8", "replace") if res.output else ""
    if res.exit_code != 0:
        raise FileError(f"{folder} cannot be listed: {text.strip().splitlines()[-1] if text.strip() else 'not found'}")
    entries = []
    for line in text.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7 or parts[0].startswith("total") or parts[6] in (".", ".."):
            continue
        mode, size, stamp, name = parts[0], parts[4], parts[5], parts[6]
        kind = _TYPES.get(mode[:1], "other")
        entries.append({"name": name.split(" -> ")[0], "kind": kind, "size": int(size) if size.isdigit() and kind == "file" else None, "mode": mode,
                        "modified": int(stamp) if stamp.isdigit() else None, "link": name.split(" -> ")[1] if kind == "link" and " -> " in name else None})
    entries.sort(key=lambda e: (e["kind"] != "folder", e["name"].lower()))
    return {"path": folder, "parent": posixpath.dirname(folder) if folder != "/" else None, "entries": entries[:MAX_LISTED], "truncated": len(entries) > MAX_LISTED}


def download(container_id: str, path: str) -> tuple[str, bytes]:
    """``(file name, content)`` of one regular file of the container, up to ``MAX_BYTES``."""
    ct = sandbox._ours(container_id)
    target = clean(path)
    try:
        stream, stat = ct.get_archive(target)
    except FileNotFoundError:
        raise FileError(f"{target} is not there.") from None
    except Exception as e:  # the Docker client reports a path the container does not have as its own NotFound (a 404 with the container's id and an address in its words): said plainly here
        if type(e).__name__ == "NotFound":
            raise FileError(f"{target} is not there.") from None
        raise
    too_big = f"{posixpath.basename(target)} is larger than {MAX_BYTES // 1048576} MB, which is the most that can be taken out."
    if (stat.get("mode") or 0) & DIR_MODE:                          # a folder would be sent whole, however large: refuse it before a byte is read
        _close(stream)
        raise FileError(f"{target} is a folder or not a regular file; only one file can be taken out at a time.")
    if stat.get("size", 0) > MAX_BYTES:
        _close(stream)
        raise FileError(too_big)
    raw = _read_up_to(stream, MAX_BYTES + ARCHIVE_SLACK, too_big)
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        members = tar.getmembers()
        if len(members) != 1 or not members[0].isfile():
            raise FileError(f"{target} is a folder or not a regular file; only one file can be taken out at a time.")
        if members[0].size > MAX_BYTES:
            raise FileError(f"{posixpath.basename(target)} is larger than {MAX_BYTES // 1048576} MB, which is the most that can be taken out.")
        return posixpath.basename(target), tar.extractfile(members[0]).read()


def _close(stream) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        close()


def _read_up_to(stream, limit: int, why: str) -> bytes:
    """The bytes of ``stream``, read block by block and given up on as soon as they pass ``limit``: the size Docker reported first may not be what is sent."""
    blocks, size = [], 0
    for block in stream:
        size += len(block)
        if size > limit:
            _close(stream)
            raise FileError(why)
        blocks.append(block)
    return b"".join(blocks)


def upload(container_id: str, folder: str, name: str, content: bytes, overwrite: bool = False, *, uid: int = 1000) -> dict:
    """Put one file into a folder of the container. The name is a plain file name; an existing file is replaced only when asked. ``uid`` owns the file (1000 is the general sandbox's
    own user; the agent sandbox passes its own, so a file this writes is still ordinary-writable by the user the container actually runs as)."""
    ct = sandbox._ours(container_id)
    dest = clean(folder)
    if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise FileError("The file name must be a plain name, without folders.")
    if len(content) > MAX_BYTES:
        raise FileError(f"The file is larger than {MAX_BYTES // 1048576} MB, which is the most that can be put in.")
    if not overwrite:
        found = ct.exec_run(["test", "-e", posixpath.join(dest, name)])
        if found.exit_code == 0:
            raise FileError(f"{name} is already in {dest}. Choose another name, or say that it may be replaced.")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        info.mode = 0o644
        info.uid = info.gid = uid
        tar.addfile(info, io.BytesIO(content))
    buf.seek(0)
    if not ct.put_archive(dest, buf.read()):
        raise FileError(f"{dest} did not accept the file (does the folder exist, and may the container write to it?).")
    from app.backend import sandbox_history
    sandbox_history.record(ct.name, "upload", {"folder": dest, "name": name, "bytes": len(content)})
    return {"folder": dest, "name": name, "bytes": len(content)}


def changes(container_id: str) -> dict:
    """What was added, changed or deleted in the container since it started, by Docker's own difference against its image."""
    ct = sandbox._ours(container_id)
    rows = ct.diff() or []
    out = [{"path": r["Path"], "kind": KINDS.get(r.get("Kind"), "changed")} for r in rows]
    out.sort(key=lambda r: r["path"])
    counts = {k: sum(1 for r in out if r["kind"] == k) for k in KINDS.values()}
    return {"changes": out[:MAX_LISTED], "truncated": len(out) > MAX_LISTED, "counts": counts, "total": len(out)}
