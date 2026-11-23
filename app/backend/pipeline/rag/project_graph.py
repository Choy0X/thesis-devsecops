"""The project graph (agentic mode, Part D): a structural summary of each file -- its module declaration, imports, module-level constants, types and top-level functions -- read
without an embedder, alongside the project's own vector index. Not fuzzy search by meaning (``project_index.py``'s own job): this answers "what does this file declare", exactly,
from ``splitdeps.py``'s own symbol reader -- the same one a changed function's own context is built from -- never a guess, and never on languages it has no reader for.

Incremental the same way ``project_index.py`` already is, but on its own tracking: each document carries the ``size``/``mtime_ns`` of the file it was built from, so an unmoved file
is skipped without being read, the same shortcut ``project_index.changes()`` uses against ``rag_index_files``. Deliberately not that same collection -- it is rewritten to the
current state by ``project_index.index_project()`` itself, often called right beside this one (``jobs.py``'s own stage does both), and comparing against a collection that call just
updated would make the result depend on which of the two ran first. Rides along unconditionally wherever the vector index is built -- no separate ``project_graph_enabled`` setting,
matching how the vector index itself has no independent on/off beyond ``RAG_ENABLED``.

Stored in ``project_symbols`` (``db/schema.py``), one document per file (``_id`` = ``<project id>:<path>``), capped the same way ``splitdeps.py`` itself caps a function's own
context (``MAX_LISTED`` etc.) rather than new numbers.
"""
from __future__ import annotations

import os
import time
from typing import Callable

from app.backend.pipeline import splitdeps
from app.backend.pipeline.rag import project_index

COLLECTION = "project_symbols"


def build_file(lang: str | None, path: str, text: str) -> dict | None:
    """The project-graph document of one file's declarations, or ``None`` when there is no symbol reader for ``lang`` (most languages ``project_index.py`` indexes for search have
    none; the overview simply leaves them out) or the file could not be parsed. A constant's value, a type's definition and a function's default-valued signature are source text
    ``splitdeps.py`` copied verbatim for context, not reviewed for secrets -- masked here the same way every other tool that shows a project's own text does (``chat_tools.read_file``,
    ``project_index.masked()``), so a literal credential in the file does not reach the graph unmasked."""
    if not lang:
        return None
    sym = splitdeps.file_symbols(lang, path, text)
    if sym is None:
        return None
    from app.backend.pipeline.scan import code_rules
    imports = [{"name": d.name, "source": d.source, "member": d.member} for d in sym.import_order[:splitdeps.MAX_LISTED]]
    constants = [{"name": d.name, "text": code_rules.mask(d.text)} for d in list(sym.constants.values())[:splitdeps.MAX_CONSTANTS]]
    types = [{"name": d.name, "kind": d.kind, "text": code_rules.mask(d.text)} for d in list(sym.types.values())[:splitdeps.MAX_TYPES]]
    functions = sorted(
        ({"name": info.name, "qual": info.qual, "scope": scope, "signature": code_rules.mask(info.signature), "line": info.lines[0]}
         for (scope, _leaf), infos in sym.functions.items() for info in infos),
        key=lambda f: f["line"])[:splitdeps.MAX_FUNCTIONS]
    return {"language": lang, "module": sym.module or None, "resolution": sym.resolution, "imports": imports, "constants": constants, "types": types, "functions": functions}


def index_project(db, project_id: str, roots: "str | list[tuple[str, str]]", *, progress: Callable[..., None] | None = None) -> dict:
    """Bring the project's graph in line with its files: a new or changed file is parsed and written (or, if it has no symbol reader, has no document at all); a removed file
    loses its document. Tracks its own ``size``/``mtime_ns``/``file_hash`` per file on each document, rather than ``project_index.changes()`` -- that collection (``rag_index_files``)
    is rewritten to the current state by ``project_index.index_project()`` itself, often right beside this call (``jobs.py``'s own stage does both); comparing against it here would
    make this call's result depend on whether it ran before or after that one. Never raises for a file it could not make sense of, only leaves it out."""
    coll = db[COLLECTION]
    stored = {d["path"]: d for d in coll.find({"project_id": project_id}, {"path": 1, "file_hash": 1, "size": 1, "mtime_ns": 1})}
    seen: dict[str, tuple[str, int, int]] = {}
    for prefix, folder in ([("", roots)] if isinstance(roots, str) else roots):
        seen.update(project_index.survey(folder, prefix))
    moved = [rel for rel, (_abspath, size, mtime) in seen.items()
             if not (stored.get(rel) and stored[rel].get("size") == size and stored[rel].get("mtime_ns") == mtime)]
    written = 0
    for i, rel in enumerate(moved, 1):
        abspath, size, mtime = seen[rel]
        got = project_index.read_text(abspath)
        rec = stored.get(rel)
        if got is None:
            if rec is not None:
                coll.delete_one({"_id": f"{project_id}:{rel}"})
            continue
        file_hash, text = got
        if rec is not None and rec.get("file_hash") == file_hash:
            coll.update_one({"_id": f"{project_id}:{rel}"}, {"$set": {"size": size, "mtime_ns": mtime}})       # only the time moved: refresh the shortcut, nothing to reparse
            continue
        doc = build_file(project_index.LANGS.get(os.path.splitext(rel)[1].lower()), rel, text)
        if doc is None:
            if rec is not None:
                coll.delete_one({"_id": f"{project_id}:{rel}"})
            continue
        doc.update({"_id": f"{project_id}:{rel}", "project_id": project_id, "path": rel, "file_hash": file_hash, "size": size, "mtime_ns": mtime, "indexed_at": time.time()})
        coll.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        written += 1
        if progress:
            progress(state="running", label="Reading the project's structure", processed=i, total=len(moved))
    removed = [rel for rel in stored if rel not in seen]
    if removed:
        coll.delete_many({"project_id": project_id, "path": {"$in": removed}})
    return {"files_written": written, "files_removed": len(removed), "checked": len(seen)}


def symbols_of_file(db, project_id: str, path: str) -> dict | None:
    """The project-graph document of one file, or ``None`` when it is not there (no symbol reader for its language, or the project index has not been built)."""
    doc = db[COLLECTION].find_one({"_id": f"{project_id}:{path}"})
    if doc:
        doc.pop("_id", None)
    return doc


def project_overview(db, project_id: str) -> list[dict]:
    """Every indexed file of the project with its module and the names of its types and functions only (no bodies, no imports): a map of the project to skim before reading any
    one file closely."""
    out = []
    for doc in db[COLLECTION].find({"project_id": project_id}, {"path": 1, "language": 1, "module": 1, "types": 1, "functions": 1}).sort("path", 1):
        out.append({"path": doc["path"], "language": doc.get("language"), "module": doc.get("module"),
                    "types": [t["name"] for t in doc.get("types", [])], "functions": [f["name"] for f in doc.get("functions", [])]})
    return out


def delete_project(db, project_id: str) -> int:
    return db[COLLECTION].delete_many({"project_id": project_id}).deleted_count


def tool_result(db, project_id: str, ws, path: str = "") -> dict:
    """The read-only ``project_graph`` tool's result, shared by ``chat_tools.py`` and ``agent_tools.py``: both workspaces resolve and show a path the same way (``ws.resolve``,
    ``ws.shown``), so the one implementation serves both. An empty path is an overview of the whole project; a path that fails ``ws.resolve`` (traversal, a credential-shaped
    name) raises the same ``ToolError`` either workspace already raises for every other tool."""
    if not path:
        overview = project_overview(db, project_id)
        return {"files": overview, "summary": f"Read the project graph: {len(overview)} indexed file(s)"}
    label, _full, rel = ws.resolve(path)
    from app.backend.pipeline.chat_tools import ToolError
    if not rel:
        raise ToolError("Name a file to look at, or leave the path empty for an overview of the whole project.")
    shown = ws.shown(label, rel)
    doc = symbols_of_file(db, project_id, shown)
    if doc is None:
        return {"available": False, "path": shown, "message": "This file is not in the project graph: no symbol reader for its language, or the project index has not been built.",
                "summary": f"Looked for {shown} in the project graph: not there"}
    return {**doc, "summary": f"Read the project graph of {shown}"}
