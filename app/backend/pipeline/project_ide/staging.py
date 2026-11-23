"""Review and stage legacy patch candidates inside a linked chat workspace."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.backend.pipeline import projects, reviewer
from app.backend.pipeline.agent import candidates
from app.backend.pipeline.scan import code_rules
from . import workbench, workspace
from .analysis import _regular
from .store import encode


def checked_link(source: str, project_id: str) -> dict:
    stored = projects.get(project_id)
    projects.require_authorized(stored)
    folders = stored.get("folders") or []
    if len(folders) != 1 or Path(folders[0]["path"]).resolve(strict=True) != Path(source).resolve(strict=True):
        raise workspace.WorkspaceError("Link an authorized project with exactly the same source folder.")
    return stored


def linkable_projects(source: str) -> list[dict]:
    matches = []
    for project in projects.list_projects():
        try:
            checked_link(source, project["id"])
        except (OSError, ValueError):
            continue
        matches.append({"id": project["id"], "name": project["name"]})
    return matches


def _selected_project(project: dict) -> dict | None:
    project_id = project["settings"].get("legacy_project_id")
    if not project_id:
        return None
    try:
        return checked_link(project["source"], project_id)
    except (OSError, ValueError):
        return None


def listing(project: dict) -> dict:
    linked = _selected_project(project)
    return {"legacyProjectId": linked["id"] if linked else None,
            "linkableProjects": linkable_projects(project["source"]),
            "candidates": candidates.listing(linked["id"]) if linked else []}


def enable_proposals(store, project_id: str) -> dict:
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        project = store.get("projects", project_id)
        linked = _selected_project(project)
        if not linked:
            matches = linkable_projects(project["source"])
            if len(matches) > 1:
                raise workspace.Conflict("Choose which existing project to link for patch proposals.")
            linked = (checked_link(project["source"], matches[0]["id"]) if matches
                      else projects.create(project["name"][:projects.MAX_NAME], [project["source"]], authorized=True))
        settings = {**project["settings"], "legacy_project_id": linked["id"]}
        connection.execute("UPDATE projects SET settings=? WHERE id=?", (encode(settings), project_id))
    return store.get("projects", project_id)


def proposal_inputs(store, chat_id: str, finding_id: str) -> tuple[dict, dict, str]:
    chat = store.get("chats", chat_id)
    if chat.get("archived") or not chat.get("workspace"):
        raise workspace.Conflict("Open an active chat workspace to prepare a proposal.")
    project = store.get("projects", chat["project_id"])
    linked = _selected_project(project)
    if not linked:
        raise workspace.WorkspaceError("Enable patch proposals for this project's source folder first.")
    item = workspace.load(chat["workspace"])
    if Path(project["source"]).resolve(strict=True) != item["source"].resolve(strict=True):
        raise workspace.Conflict("The conversation does not belong to this source folder.")
    report = workbench.findings(item, store.runs(chat_id), [])
    row = next((row for row in report["evidence"] if row["id"] == finding_id), None)
    completed = {scan["id"] for scan in report["scans"] if scan["state"] == "completed"}
    if (row is None or row["origin"] != "model_review" or row["status"] != "grounded"
            or row["scanId"] not in completed or not row.get("source")):
        raise workspace.WorkspaceError("Choose a grounded v40 finding from a completed review in this conversation.")
    source = row["source"]
    if source["freshness"] != "current" or not source["sha256"]:
        raise workspace.Conflict("Run a fresh model review before proposing a patch for changed source.")
    try:
        current = workspace.read({"root": item["source"]}, source["path"])
        live = _file(item["source"], source["path"])
    except (OSError, ValueError) as error:
        raise workspace.Conflict("The reviewed file is unavailable in the project source.") from error
    lines = current["text"].splitlines()
    if (current["version"] != source["sha256"] or workspace.digest(live) != source["sha256"]
            or source["line"] > len(lines) or lines[source["line"] - 1].strip() != source["quote"].strip()):
        raise workspace.Conflict("The evidence does not match the current project source.")
    finding = {"id": finding_id, "file": source["path"], "line": source["line"], "rule": row["title"],
               "message": row["summary"], "function": row.get("function")}
    return linked, finding, source["sha256"]


def propose_candidate(store, chat_id: str, finding_id: str, *, run_checks: bool) -> dict:
    linked, finding, digest = proposal_inputs(store, chat_id, finding_id)

    def ask(selected, path, original, *, allow_remote):
        try:
            return reviewer.repair_local(path, original, selected)
        except reviewer.ReviewStopped as error:
            raise candidates.CandidateError(str(error)) from error

    return candidates.make(linked, finding, test=run_checks, ask=ask, expected_source_sha256=digest)


def _load(project: dict, candidate_id: str) -> tuple[dict, str]:
    linked = _selected_project(project)
    if linked is None:
        raise workspace.WorkspaceError("Link the project that owns this candidate before reviewing it.")
    record = candidates.get(linked["id"], candidate_id)
    if record.get("project_id") != linked["id"] or record.get("candidate_id") != candidate_id:
        raise workspace.Conflict("The stored patch candidate identity does not match this project.")
    return record, candidates.diff_of(linked["id"], candidate_id)


def _file(root, relative: str) -> Path:
    path = workspace.bound(root, relative)
    if _regular(path).st_size > candidates.MAX_FILE_BYTES:
        raise workspace.Conflict("The reviewed file now exceeds the patch size limit.")
    return path


def _staging_problem(record, diff, project, item, chat) -> str | None:
    if chat.get("archived"):
        return "Open an active conversation to stage a patch."
    if item.get("directory") is None:
        return "This conversation has no workspace yet. Start a coding turn or save a file to create one, then stage the patch."
    if not diff or record.get("stopped"):
        return "The candidate did not produce an applicable diff."
    receipt = record.get("staging_receipt")
    if not receipt:
        return "No matching isolated project checks were recorded for this candidate."
    try:
        relative = record["file"]
        current_diff_sha = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        expected = candidates.staging_receipt(record, Path(project["source"]))
        if not expected or receipt != expected or current_diff_sha != record["diff_sha256"]:
            return "The proposed diff or project no longer matches its staging receipt."
        if Path(project["source"]).resolve(strict=True) != item["source"].resolve(strict=True):
            return "The conversation source does not match the linked project's source."
        live = _file(item["source"], relative)
        managed = _file(item["root"], relative)
        known = item.get("integration_baseline", {}).get(relative, item["baseline"].get(relative))
        if known != record["original_sha256"] or workspace.digest(live) != known or workspace.digest(managed) != known:
            return "The source or the managed workspace changed after verification. Review a fresh candidate."
    except (KeyError, TypeError, OSError, ValueError):
        return "The staged path or verification receipt is unavailable or invalid."
    return None


def detail(chat, project, item, candidate_id: str) -> dict:
    record, diff = _load(project, candidate_id)
    reason = _staging_problem(record, diff, project, item, chat)
    visible = {key: value for key, value in record.items() if key != "staging_receipt"}
    staged = False
    receipt = record.get("staging_receipt")
    if record.get("file") and receipt:
        try:
            marker = workspace.bound(item["directory"], f"patch-staging/{candidate_id}.json")
            result = {"candidate_id": candidate_id, "staged_paths": [record["file"]], "receipt_id": receipt["id"]}
            target = workspace.bound(item["root"], record["file"])
            staged = (marker.is_file() and json.loads(marker.read_text(encoding="utf-8")) == result
                      and workspace.digest(target) == record["patched_sha256"])
        except (KeyError, OSError, TypeError, ValueError):
            pass
    if staged:
        reason = "This patch is staged in the review workspace."
    visible.update(diff=diff, can_stage=reason is None, stage_reason=reason, staged=staged)
    relative, quote = record.get("file"), record.get("source_quote")
    line, original = (record.get("finding") or {}).get("line"), record.get("original_sha256")
    if relative and original and type(line) is int and line > 0:
        freshness = "stale"
        try:
            path = _file(item["root"], relative)
            saved = workspace.read(item, relative)
            if path.is_file() and saved["version"] == original:
                freshness = "current"
                lines = saved["text"].splitlines()
                quote = quote or (code_rules.masked(lines[line - 1], 1000) if line <= len(lines) else None)
        except (OSError, ValueError):
            pass
        if quote:
            visible["source"] = {"path": relative, "line": line, "quote": quote, "sha256": original,
                                 "freshness": freshness}
    return visible


def stage_candidate(application, chat_id: str, candidate_id: str, expected_diff_sha256: str) -> dict:
    store = application.service.store
    # Reserve the chat against concurrent staging and queued agent work. The file
    # digest is also checked by workspace.save against concurrent editor writes.
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        chat = store.get("chats", chat_id)
        project = store.get("projects", chat["project_id"])
        item = application.item(chat_id)
        record, diff = _load(project, candidate_id)
        if (not diff or record.get("diff_sha256") != expected_diff_sha256
                or hashlib.sha256(diff.encode("utf-8")).hexdigest() != expected_diff_sha256):
            raise workspace.Conflict("The reviewed patch changed. Open its current diff before staging.")
        receipt = record.get("staging_receipt")
        if not receipt or receipt != candidates.staging_receipt(record, Path(project["source"])):
            raise workspace.Conflict("The project, file, or diff has no matching staging receipt.")
        if chat.get("archived"):
            raise workspace.Conflict("Open an active conversation to stage a patch.")
        relative = record["file"]
        result = {"candidate_id": candidate_id, "staged_paths": [relative], "receipt_id": receipt["id"]}
        marker = workspace.bound(item["directory"], f"patch-staging/{candidate_id}.json")
        if marker.is_file() and json.loads(marker.read_text(encoding="utf-8")) == result:
            path = workspace.bound(item["root"], relative)
            logged = connection.execute(
                "SELECT 1 FROM events WHERE chat_id=? AND type='patch.staged' AND json_extract(payload, '$.receipt_id')=?",
                (chat_id, receipt["id"])).fetchone()
            if workspace.digest(path) == record["patched_sha256"]:
                if not logged:
                    store._event(connection, chat_id, "patch.staged", result)
                return result
            if logged:
                raise workspace.Conflict("This patch was staged earlier and its workspace file has changed.")
        active = connection.execute("SELECT 1 FROM runs WHERE chat_id=? AND state IN "
                                    "('queued','running','waiting_input','waiting_approval')", (chat_id,)).fetchone()
        if active:
            raise workspace.Conflict("Wait for pending conversation work before staging a patch.")
        problem = _staging_problem(record, diff, project, item, chat)
        if problem:
            raise workspace.Conflict(problem)
        original = workspace.read(item, relative)
        if original["version"] != record["original_sha256"]:
            raise workspace.Conflict("The workspace changed during patch staging.")
        candidates.check_diff(diff, relative)
        patched = candidates.apply_to_copy(relative, original["text"], diff)
        if hashlib.sha256(patched.encode("utf-8")).hexdigest() != record["patched_sha256"]:
            raise workspace.Conflict("The staged bytes do not match the tested patch result.")
        # A prepared marker permits a safe retry if the file write finishes before
        # an interruption prevents the transaction from recording the audit event.
        workspace._write(marker, encode(result).encode("utf-8"))
        workspace.save(item, relative, patched, original["version"])
        store._event(connection, chat_id, "patch.staged", result)
        return result
