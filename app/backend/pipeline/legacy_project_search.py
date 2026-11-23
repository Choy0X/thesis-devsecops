"""Local search adapter for legacy Projects routes and conversations."""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.backend import paths
from app.backend.pipeline import projects
from app.backend.pipeline.chat_tools import Workspace
from app.backend.pipeline.project_ide.project_grounding import query_terms
from app.backend.pipeline.project_ide.repo_index import RepoIndex, search_project
from app.backend.pipeline.project_ide.repo_search import SearchScope

BACKEND = "local_search"


def scopes(project: dict, *, check_root: bool = True):
    projects.require_authorized(project)
    workspace = Workspace(project)
    for (label, root), folder in zip(workspace.folders, project["folders"]):
        original = Path(folder["path"])
        resolved = Path(root)
        if check_root and original != resolved:
            raise projects.ProjectError("A project folder now points somewhere else. Re-add the intended folder before searching it.")
        identity = hashlib.sha256(str(original).encode("utf-8")).hexdigest()
        cache = paths.interim_dir() / "legacy_project_search" / project["id"] / identity
        yield label if workspace.labelled else "", SearchScope(resolved if check_root else original, cache)


def status(project: dict) -> dict:
    states = [RepoIndex().status(scope) for _, scope in scopes(project)]
    built = any(state.state == "ready" for state in states)
    return {"backend": BACKEND, "built": built, "tracked": False, "stale": False,
            "files": sum(state.indexed_files for state in states)}


def refresh(project: dict) -> dict:
    index = RepoIndex()
    for _, scope in scopes(project):
        refreshed = index.refresh(scope)
        if refreshed.state != "ready":
            raise RuntimeError("The local search cache could not be refreshed.")
    return status(project)


def clear(project: dict) -> int:
    index = RepoIndex()
    return sum(index.clear(scope) for _, scope in scopes(project, check_root=False))


def retrieve(project: dict, query: str, limit: int, cancelled) -> tuple[list[dict], str | None]:
    terms = query_terms(query)
    if not terms:
        return [], None
    matches = []
    incomplete = False
    for label, scope in scopes(project):
        response = search_project(scope, terms, limit=limit, cancelled=cancelled)
        incomplete |= not response.complete
        for rank, hit in enumerate(response.hits):
            path = f"{label}/{hit.path}" if label else hit.path
            matches.append({"path": path, "line": hit.start_line, "end_line": hit.end_line,
                            "text": hit.excerpt, "score": 1 / (rank + 1),
                            "sha256": hit.sha256, "match_kind": hit.match_kind})
    matches.sort(key=lambda hit: (-hit["score"], hit["path"], hit["line"]))
    return matches[:limit], "Local project search covers part of the project." if incomplete else None
