"""Dataset versions: numbering and file hashes.

A freeze takes the next unused version number of its family and records the sha256 of every file in
``data/processed/VERSIONS.json`` (tracked in git), so a later change is detectable by comparing hashes.
An older registry may still carry a ``reserved`` list; it is ignored.

Mutable tags (``preview``, ``smoke``) are not versions. A family is numbered independently
(``dataset``, ``code_pairs``, ``dependencies``).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
from contextlib import contextmanager, nullcontext
from typing import Iterable

PROC = pathlib.Path("data/processed")
REGISTRY_NAME = "VERSIONS.json"
FAMILIES = ("dataset", "code_pairs", "dependencies")
_NAME = re.compile(r"^(?P<family>dataset|code_pairs|dependencies)_v(?P<n>\d+)(?=$|[._])")


@contextmanager
def _version_lock(root):
    from app.backend.pipeline import oslock
    lock = oslock.FileLock(pathlib.Path(root) / '.versions.lock', note=False)
    lock.acquire(wait=30)
    try:
        yield
    finally:
        lock.release()

def parse_name(name: str) -> tuple[str, int] | None:
    """('dataset', 2) for 'dataset_v2', 'dataset_v2.parquet', 'code_pairs_v1_detection.jsonl'."""
    m = _NAME.match(name)
    return (m["family"], int(m["n"])) if m else None


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_registry(root: pathlib.Path = PROC) -> dict:
    path = pathlib.Path(root) / REGISTRY_NAME
    data = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    return {"schema": 1, "frozen": dict(data.get("frozen", {}))}


def _save_registry(reg: dict, root: pathlib.Path) -> None:
    from app.backend.pipeline import fsio
    path = pathlib.Path(root) / REGISTRY_NAME
    fsio.write_bytes(path, (json.dumps(reg, indent=2, sort_keys=True) + '\n').encode('utf-8'))


def _frozen_versions(reg: dict) -> set[tuple[str, int]]:
    out = set()
    for name in reg["frozen"]:
        parsed = parse_name(name)
        if parsed:
            out.add(parsed)
    return out


def _git_tags() -> list[str]:
    try:
        out = subprocess.run(["git", "tag", "--list"], capture_output=True, text=True, timeout=10)
        return out.stdout.split() if out.returncode == 0 else []
    except Exception:  # ignored: git may be missing; no tags are known
        return []


def next_unused_version(family: str = "dataset", root: pathlib.Path = PROC,
                        tags: Iterable[str] | None = None) -> str:
    """'v<N>' for the first free number of a family; frozen versions, existing
    files or directories and git tags all count as used."""
    if family not in FAMILIES:
        raise ValueError(f"unknown dataset family {family!r}; expected one of {FAMILIES}")
    root = pathlib.Path(root)
    used = {n for fam, n in _frozen_versions(load_registry(root)) if fam == family}
    if root.exists():
        for entry in root.iterdir():
            parsed = parse_name(entry.name.removeprefix(".").removesuffix(".reserved"))
            if parsed and parsed[0] == family:
                used.add(parsed[1])
    tag_pat = re.compile(rf"^{family}[-_]v(\d+)$")
    for tag in (_git_tags() if tags is None else tags):
        m = tag_pat.match(tag)
        if m:
            used.add(int(m.group(1)))
    return f"v{max(used, default=0) + 1}"


def reserve_version(family: str = "dataset", root: pathlib.Path = PROC, *, owner: str, minimum: int = 1) -> str:
    """Durably claim the next version across threads and processes, without creating a published artifact."""
    from app.backend.pipeline import fsio
    root = pathlib.Path(root)
    with _version_lock(root):
        tag = f"v{max(minimum, int(next_unused_version(family, root)[1:]))}"
        path = root / f".{family}_{tag}.reserved"
        with path.open("x", encoding="utf-8") as stream:
            json.dump({"owner": owner, "tag": tag, "family": family}, stream)
            fsio.fsync_handle(stream)
        return tag


def publication_scope(guard):
    """Enter a workflow's transactional commit fence, or run a legacy check."""
    value = guard() if guard is not None else None
    if value is False:
        raise InterruptedError('Publication cancelled before commit.')
    return value if hasattr(value, '__enter__') else nullcontext()


def publish_reserved(artifact: pathlib.Path, tag: str, root: pathlib.Path = PROC, *, owner: str,
                     publication_guard=None) -> pathlib.Path:
    """Publish a completed directory once. A reserved name and an existing artifact are never overwritten."""
    root, artifact = pathlib.Path(root), pathlib.Path(artifact)
    if artifact.name != f'dataset_{tag}':
        # Caller staging folder names are immaterial; the publication identity is the reservation.
        name, base, files = f'dataset_{tag}', artifact, sorted(p for p in artifact.rglob('*') if p.is_file())
        entry = _entry(name, base, files, root)
    else:
        entry = _freeze_entry(artifact, root)
    entry['base'] = f'dataset_{tag}'
    with _version_lock(root), publication_scope(publication_guard):
        reservation = root / f".dataset_{tag}.reserved"
        claim = json.loads(reservation.read_text(encoding="utf-8"))
        if claim.get("owner") != owner:
            raise ValueError("dataset version reservation is owned by another build")
        final = root / f"dataset_{tag}"
        if final.exists():
            raise FileExistsError(f"immutable dataset already exists: {final.name}")
        from . import fsio
        intent = root / f'.dataset_{tag}.publication.json'
        fsio.write_bytes(intent, json.dumps({'schema': 1, 'owner': owner, 'tag': tag,
                                            'entry': entry}, sort_keys=True).encode('utf-8'))
        artifact.rename(final)
        _register_entry(entry, root)
        # The claim is retained as an allocation audit trail, including after a crash.
        return final


def recover_publication(tag, root=PROC, *, owner):
    """Finish a backend-owned interrupted registration without changing its files."""
    root = pathlib.Path(root)
    if not re.fullmatch(r'v[1-9][0-9]*', tag):
        raise ValueError('invalid publication version')
    with _version_lock(root):
        claim = json.loads((root / f'.dataset_{tag}.reserved').read_text(encoding='utf-8'))
        intent = json.loads((root / f'.dataset_{tag}.publication.json').read_text(encoding='utf-8'))
        if claim.get('owner') != owner or intent.get('owner') != owner or intent.get('tag') != tag:
            raise ValueError('publication recovery owner mismatch')
        entry = intent.get('entry', {})
        final = root / f'dataset_{tag}'
        if entry.get('name') != final.name or entry.get('base') != final.name or not final.is_dir() or final.is_symlink():
            raise ValueError('publication recovery identity mismatch')
        files = entry.get('files', {})
        actual = set()
        for path in final.rglob('*'):
            if path.is_symlink() or not path.resolve().is_relative_to(final.resolve()):
                raise ValueError('unsafe publication recovery path')
            if path.is_file():
                actual.add(path.relative_to(final).as_posix())
        if not files or actual != set(files):
            raise ValueError('publication recovery file inventory mismatch')
        for name, expected in files.items():
            if _sha256(final / name) != expected:
                raise ValueError('publication recovery hash mismatch: ' + name)
        existing = load_registry(root)['frozen'].get(final.name)
        if existing and existing != entry:
            raise ValueError('publication registry conflicts with recovery intent')
        _register_entry(entry, root)
        return final


def _collect(artifact, root: pathlib.Path) -> tuple[str, pathlib.Path, list[pathlib.Path]]:
    """(version name, base folder, files) for a directory or a set of files."""
    root = pathlib.Path(root)
    if isinstance(artifact, (str, os.PathLike)):
        artifact = pathlib.Path(artifact)
        if artifact.is_dir():
            parsed = parse_name(artifact.name)
            if not parsed:
                raise ValueError(f"{artifact.name} is not a versioned dataset folder")
            files = sorted(p for p in artifact.rglob("*") if p.is_file())
            return f"{parsed[0]}_v{parsed[1]}", artifact, files
        artifact = [artifact]
    files = [pathlib.Path(p) for p in artifact]
    versions = {parse_name(p.name) for p in files}
    if len(versions) != 1 or None in versions:
        raise ValueError("files to freeze must all belong to one dataset version")
    family, n = next(iter(versions))
    return f"{family}_v{n}", root, files


def freeze(artifact, root: pathlib.Path = PROC) -> dict:
    """Hash and register a finished version (freezing a name again replaces its entry)."""
    entry = _freeze_entry(artifact, root)
    with _version_lock(root):
        _register_entry(entry, root)
    from app.backend.pipeline import build_catalog
    build_catalog.register_version(entry, root=root)
    return entry


def _freeze_entry(artifact, root: pathlib.Path = PROC) -> dict:
    root = pathlib.Path(root)
    name, base, files = _collect(artifact, root)
    return _entry(name, base, files, root)


def _entry(name, base, files, root):
    root = pathlib.Path(root)
    if not files:
        raise ValueError(f"nothing to freeze for {name}")
    try:
        base_rel = base.resolve().relative_to(root.resolve()).as_posix() or "."
    except ValueError:
        base_rel = base.as_posix()
    entry = {"name": name, "base": base_rel,
             "frozen_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
             "files": {p.resolve().relative_to(base.resolve()).as_posix(): _sha256(p)
                       for p in files}}
    return entry


def _register_entry(entry, root):
    reg = load_registry(root)
    reg['frozen'][entry['name']] = entry
    _save_registry(reg, root)


def latest_frozen(family: str, suffix: str, root: pathlib.Path = PROC) -> pathlib.Path | None:
    """Newest frozen file named ``<family>_v<N><suffix>`` (suffix e.g. '.parquet' or
    '_detection.jsonl'); None when no frozen version has such a file."""
    root = pathlib.Path(root)
    best: tuple[int, pathlib.Path] | None = None
    for name, entry in load_registry(root)["frozen"].items():
        parsed = parse_name(name)
        if not parsed or parsed[0] != family:
            continue
        base = root if entry.get("base", ".") == "." else root / entry["base"]
        want = f"{family}_v{parsed[1]}{suffix}"
        for rel in entry["files"]:
            p = base / rel
            if pathlib.PurePosixPath(rel).name == want and p.exists() \
                    and (best is None or parsed[1] > best[0]):
                best = (parsed[1], p)
    return best[1] if best else None


def output_tag(freeze: bool, *families: str, root: pathlib.Path = PROC,
               tags: Iterable[str] | None = None) -> str:
    """'preview' for a normal build; for a freeze, the first version number that is
    unused in every listed family (so files written together share one number)."""
    if not freeze:
        return "preview"
    numbers = [int(next_unused_version(f, root, tags)[1:]) for f in families]
    return f"v{max(numbers)}"


def resolve(family: str, suffix: str, preview_name: str,
            root: pathlib.Path = PROC) -> pathlib.Path:
    """The latest frozen file of a family, else the mutable preview file."""
    return latest_frozen(family, suffix, root) or pathlib.Path(root) / preview_name
