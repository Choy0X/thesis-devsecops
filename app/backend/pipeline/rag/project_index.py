"""The isolated code index of one user project (Projects chat), in ``project_chunks__<slug>``.

It indexes the project's own files only, never the knowledge base, and the knowledge base never holds a project's code. Isolation is by
construction: every function here puts the ``project_id`` clause into the filter itself, a caller cannot leave it out, and project chunks live
in a different collection from the knowledge base chunks.

Files: text files up to ``MAX_FILE_BYTES`` outside the usual dependency, build and cache folders; secrets (``.env`` files, keys, certificates,
credential stores) are never read. A file is cut into overlapping windows of lines. Re-indexing is incremental by file hash: unchanged files
cost nothing, changed or deleted files lose their old chunks first.

Which files changed since the index was built is answered without reading them all: each file the index has seen has a record in ``rag_index_files`` (size, modification time, hash),
and ``changes`` compares a pass that only looks at sizes and times with those records, reading (and hashing) just the files that moved.
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import Callable

from app.backend.pipeline.credentials import is_kept, is_secret_dir
from app.backend.pipeline.db.operations import ReplaceOne
from app.backend.pipeline.rag import atlas
from app.backend.pipeline.rag.embed import BATCH, Embedder

IGNORE_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build", "target", ".idea", ".vscode",
               ".mypy_cache", ".pytest_cache", ".tox", ".next", ".gradle", "vendor", "bower_components", "coverage", ".cache"}
MAX_FILE_BYTES = 1_000_000
FILES = "rag_index_files"           # one record per file the index has seen: {_id: <project>:<path>, project_id, path, size, mtime_ns, hash (None for a file that was not indexed)}
WINDOW, OVERLAP = 60, 10
LANGS = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript", ".java": "java", ".go": "go",
         ".rb": "ruby", ".php": "php", ".rs": "rust", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cs": "csharp", ".kt": "kotlin",
         ".swift": "swift", ".sh": "shell", ".sql": "sql", ".yml": "yaml", ".yaml": "yaml", ".json": "json", ".md": "markdown", ".html": "html"}


def is_secret(name: str) -> bool:
    """Kept out of the index (see :mod:`credentials`, which the project chat's tools use too)."""
    return is_kept(name)


def survey(root: str, prefix: str = "") -> dict[str, tuple[str, int, int]]:
    """{relative path (forward slashes, after ``prefix`` when the project has several folders): (absolute path, size, modification time in ns)} of the files whose place and size allow
    indexing. No file is read."""
    from app.backend.pipeline import projects
    out: dict[str, tuple[str, int, int]] = {}
    real = os.path.realpath(root)
    for base, dirs, files in os.walk(real):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not is_secret_dir(d) and not os.path.islink(os.path.join(base, d)) and projects.inside(real, os.path.join(base, d))]
        for name in files:
            path = os.path.join(base, name)
            if is_secret(name) or os.path.islink(path) or not projects.inside(real, path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_size > MAX_FILE_BYTES:
                continue
            rel = os.path.relpath(path, real).replace(os.sep, "/")
            out[f"{prefix}/{rel}" if prefix else rel] = (path, st.st_size, st.st_mtime_ns)
    return out


def read_text(path: str) -> tuple[str, str] | None:
    """(sha256, text) of a file the index may hold, or None for one it does not: unreadable, binary, not UTF-8, or empty."""
    try:
        raw = open(path, "rb").read()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None                                         # binary
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return (hashlib.sha256(raw).hexdigest(), text) if text.strip() else None


def scan(root: str, prefix: str = "", seen: dict | None = None) -> dict[str, tuple[str, str]]:
    """{relative path: (sha256, text)} of the indexable files under ``root``. ``seen``, when given, is filled with the survey of every file whose place allowed it (also the ones that were
    then left out for their content), for the records of what the index has seen."""
    found = survey(root, prefix)
    if seen is not None:
        seen.update(found)
    out: dict[str, tuple[str, str]] = {}
    for rel, (path, _size, _mtime) in found.items():
        got = read_text(path)
        if got:
            out[rel] = got
    return out


def windows(text: str, size: int = WINDOW, overlap: int = OVERLAP) -> list[tuple[int, str]]:
    """(first line number, text) windows of ``size`` lines overlapping by ``overlap``."""
    lines = text.splitlines()
    step = max(1, size - overlap)
    return [(i + 1, "\n".join(lines[i:i + size])) for i in range(0, max(len(lines), 1), step) if lines[i:i + size]]


def masked(path: str, text: str) -> str:
    """A window of a file with credentials hidden: what is embedded and stored is what the chat may be shown, so a secret in a source file never reaches the index."""
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import code_rules
    return code_rules.mask(text, config=os.path.splitext(path)[1].lower() not in projects.LANGUAGES)


def index_project(db, project_id: str, root: "str | list[tuple[str, str]]", embedder: Embedder, *, progress: Callable[..., None] | None = None) -> dict:
    """Bring the project's chunks in line with the files under ``root``: one folder, or a list of ``(prefix, folder)`` for a project of several."""
    coll = atlas.ensure_collection(db, atlas.PROJECT, embedder)
    files: dict[str, tuple[str, str]] = {}
    seen: dict[str, tuple[str, int, int]] = {}
    for prefix, folder in ([("", root)] if isinstance(root, str) else root):
        files.update(scan(folder, prefix, seen))
    stored: dict[str, str] = {}
    for d in coll.aggregate([{"$match": {"project_id": project_id}}, {"$group": {"_id": "$path", "hash": {"$first": "$file_hash"}}}]):
        stored[d["_id"]] = d["hash"]
    gone = [p for p in stored if p not in files]
    changed = [p for p, (h, _) in files.items() if stored.get(p) != h]
    stale = gone + [p for p in changed if p in stored]
    if stale:
        coll.delete_many({"project_id": project_id, "path": {"$in": stale}})
    chunks: list[dict] = []
    for path in changed:
        h, text = files[path]
        ext = os.path.splitext(path)[1].lower()
        for line, part in windows(text):
            part = masked(path, part)
            chunks.append({"_id": f"{project_id}:{path}:{line}", "project_id": project_id, "path": path, "file_hash": h, "line": line,
                           "language": LANGS.get(ext), "text": part, "text_sha256": hashlib.sha256(part.encode()).hexdigest()})
    done = 0
    for i in range(0, len(chunks), BATCH * 4):
        part = chunks[i:i + BATCH * 4]
        for c, v in zip(part, embedder.encode_documents([c["text"] for c in part])):
            c["embedding"] = atlas.vector(v)
            c["embedder_key"] = embedder.key
        atlas.upsert_chunks(coll, part)
        done += len(part)
        if progress:
            progress(state="running", label="Indexing the project", processed=done, total=len(chunks))
    if chunks:
        atlas.wait_visible(coll, atlas.PROJECT, chunks)
    record_files(db, project_id, seen, files)
    db["rag_index_state"].update_one({"_id": f"project:{project_id}"}, {"$set": {
        "project_id": project_id, "embedder": embedder.as_dict(), "collection": coll.name, "files": len(files), "indexed_at": time.time(), "tracked": True}},
        upsert=True)
    return {"files": len(files), "changed": len(changed), "removed": len(gone), "chunks_written": len(chunks), "collection": coll.name}


def record_files(db, project_id: str, seen: dict[str, tuple[str, int, int]], files: dict[str, tuple[str, str]]) -> None:
    """Write what the index has seen of each file (size, time, hash; no hash for a file left out for its content), and drop the records of files that are gone."""
    coll = db[FILES]
    ops = [ReplaceOne({"_id": f"{project_id}:{rel}"}, {"_id": f"{project_id}:{rel}", "project_id": project_id, "path": rel, "size": size, "mtime_ns": mtime,
                                                         "hash": files[rel][0] if rel in files else None}, upsert=True) for rel, (_path, size, mtime) in seen.items()]
    for i in range(0, len(ops), 1000):
        coll.bulk_write(ops[i:i + 1000], ordered=False)
    gone = [d["path"] for d in coll.find({"project_id": project_id}, {"path": 1}) if d["path"] not in seen]
    for i in range(0, len(gone), 1000):
        coll.delete_many({"project_id": project_id, "path": {"$in": gone[i:i + 1000]}})


def changes(db, project_id: str, roots: "str | list[tuple[str, str]]") -> dict:
    """What changed in the project's files since the index was built, without reading them all. A pass over the folders that only looks at sizes and times is compared with the records;
    only a file that moved (or is new) is read and hashed. ``new``: files the index does not hold; ``changed``: files whose content differs from what was indexed; ``removed``: indexed
    files that are gone (or are no longer text); ``touched``: files whose time moved but whose content did not."""
    stored = {d["path"]: d for d in db[FILES].find({"project_id": project_id})}
    seen: dict[str, tuple[str, int, int]] = {}
    for prefix, folder in ([("", roots)] if isinstance(roots, str) else roots):
        seen.update(survey(folder, prefix))
    new: list[str] = []
    changed: list[str] = []
    removed: list[str] = []
    touched = read = 0
    for rel, (path, size, mtime) in seen.items():
        rec = stored.get(rel)
        if rec is not None and rec.get("size") == size and rec.get("mtime_ns") == mtime:
            continue                                                       # has not moved: not read
        got = read_text(path)
        read += 1
        now = got[0] if got else None
        before = rec.get("hash") if rec is not None else None
        if now is None:
            if before is not None:
                removed.append(rel)                                        # it was indexed and is not text any more
        elif before is None:
            new.append(rel)                                                # new, or a file that was left out and now can be indexed
        elif now != before:
            changed.append(rel)
        else:
            touched += 1
    removed += [rel for rel, rec in stored.items() if rel not in seen and rec.get("hash") is not None]
    return {"checked": len(seen), "read": read, "new": sorted(new), "changed": sorted(changed), "removed": sorted(removed), "touched": touched}


def search_project(db, project_id: str, query: str, k: int, embedder: Embedder, *, paths: list[str] | None = None,
                   languages: list[str] | None = None, exact: bool = False) -> list[dict]:
    """The ``k`` nearest chunks of this project only (the project clause is added here, not by the caller)."""
    coll = db[atlas.collection_name(atlas.PROJECT, embedder)]
    filters: list[dict] = [{"project_id": project_id}]
    if paths:
        filters.append({"path": {"$in": list(paths)}})
    if languages:
        filters.append({"language": {"$in": list(languages)}})
    hits = atlas.search(coll, embedder.encode_query(query[:4000]), k, kind=atlas.PROJECT, filters=filters, exact=exact)
    return [{"path": h["path"], "line": h["line"], "text": h["text"], "score": h["score"], "language": h.get("language")} for h in hits]


def delete_project(db, project_id: str, embedder: Embedder) -> int:
    n = db[atlas.collection_name(atlas.PROJECT, embedder)].delete_many({"project_id": project_id}).deleted_count
    db["rag_index_state"].delete_one({"_id": f"project:{project_id}"})
    db[FILES].delete_many({"project_id": project_id})
    return n


def status(db, project_id: str, current: Embedder | None = None) -> dict:
    """What is indexed for a project: ``built`` and, when it is, the files and chunks, when, with which embedder, and ``stale`` when that is not the configured one."""
    st = db["rag_index_state"].find_one({"_id": f"project:{project_id}"})
    if not st:
        return {"built": False}
    emb = st.get("embedder") or {}
    return {"built": True, "files": st.get("files", 0), "chunks": db[st["collection"]].count_documents({"project_id": project_id}), "indexed_at": st.get("indexed_at"),
            "embedder": emb, "collection": st["collection"], "stale": bool(current) and emb.get("key") != current.key, "tracked": bool(st.get("tracked"))}


def delete_index(db, project_id: str) -> int:
    """Remove a project's index, whichever embedder built it (the state says which collection holds it). Returns the chunks removed."""
    st = db["rag_index_state"].find_one({"_id": f"project:{project_id}"})
    if not st:
        return 0
    n = db[st["collection"]].delete_many({"project_id": project_id}).deleted_count
    db["rag_index_state"].delete_one({"_id": f"project:{project_id}"})
    db[FILES].delete_many({"project_id": project_id})
    return n
