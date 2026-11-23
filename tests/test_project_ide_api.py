"""Public IDE routes preserve workspace ownership and explicit review semantics."""
import json
import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.backend import project_ide_api as api
from app.backend.pipeline.project_ide import workspace
from app.backend.pipeline.project_ide.service import Service


@pytest.fixture
def ide(tmp_path, monkeypatch):
    source = tmp_path / "original"
    source.mkdir()
    (source / "calc.py").write_text("value = 1\n")
    (source / "requirements.txt").write_text("demo==1.0\n")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    service = Service(tmp_path / "managed")
    project = service.create_project("Fixture", str(source))
    chat = service.create_chat(project["id"], "First", {"model": "qwen", "kind": "local"})
    other = service.create_chat(project["id"], "Second", {"model": "qwen", "kind": "local"})
    item = lambda identity: workspace.load(Path(service.store.get("chats", identity)["workspace"]))
    app = SimpleNamespace(service=service, item=item, prepared_model=lambda _, model: model,
                          dependency_inventory=lambda _: {"packages": [{"id": "PyPI:demo"}]},
                          terminals=SimpleNamespace(list=lambda _: []), _lock=threading.RLock(),
                          prepare_runtime=service.prepare_runtime)
    monkeypatch.setattr(api, "application", lambda: app)
    # Do not launch inference in route tests; durable submissions still use the real store.
    monkeypatch.setattr(service, "_wake", lambda _: None)
    service.scan_handler = lambda *_: {}
    server = FastAPI()
    server.include_router(api.router)
    with TestClient(server) as client:
        yield client, service, source, chat, other
    service.close()


def test_chat_creation_and_changes_poll_do_not_snapshot_source(ide, monkeypatch):
    client, service, _, chat, _ = ide

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Creating or reopening the conversation copied the project.")

    monkeypatch.setattr(workspace, "provision", unexpected)
    base = f"/api/ide/projects/{chat['project_id']}"
    response = client.post(base + "/chats", json={"model": {"model": "fixture", "kind": "local"}, "preparation_id": "ready"})
    assert response.status_code == 200
    created = response.json()
    assert created["workspace"] is None
    assert created["id"] in {row["id"] for row in client.get(base + "/chats").json()["chats"]}
    assert client.get(f"/api/chats/{created['id']}/changes").json() == {"changes": []}
    assert client.get(f"/api/chats/{created['id']}").json()["chat"] == created
    assert not (service.root / "workspaces" / created["id"]).exists()


def test_search_before_workspace_uses_registered_source_and_preserves_exact_contract(ide, monkeypatch):
    client, service, source, chat, _ = ide
    (source / "routes.py").write_text("process_invoice = True\n", encoding="utf-8")
    fresh = service.create_chat(chat["project_id"], "Search", {"model": "fixture", "kind": "local"}, defer_workspace=True)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("A read-only search should leave the workspace unset.")

    monkeypatch.setattr(workspace, "provision", unexpected)
    response = client.get(f"/api/chats/{fresh['id']}/search", params={"q": "PROCESS_INVOICE"})
    assert response.status_code == 200
    body = response.json()
    assert body["matches"] == [{"path": "routes.py", "line": 1, "text": "process_invoice = True"}]
    assert body["complete"] is True
    assert service.store.get("chats", fresh["id"])["workspace"] is None


def test_search_keeps_private_worktree_edits_in_their_conversation(ide):
    client, _, source, chat, other = ide
    (source / "calc.py").write_text("value = 40\n", encoding="utf-8")
    base, second = f"/api/chats/{chat['id']}", f"/api/chats/{other['id']}"
    saved = client.get(base + "/file", params={"path": "calc.py"}).json()
    assert client.put(base + "/file", json={**saved, "text": "active_view_only = 7\n"}).status_code == 200
    match = client.get(base + "/search", params={"q": "active_view_only"}).json()["matches"]
    assert match == [{"path": "calc.py", "line": 1, "text": "active_view_only = 7"}]
    assert client.get(second + "/search", params={"q": "active_view_only"}).json()["matches"] == []
    assert (source / "calc.py").read_text() == "value = 40\n"


def test_conversation_history_manage_preserves_the_project_and_worktree(ide):
    client, service, source, chat, other = ide
    base = f"/api/chats/{chat['id']}"
    history = f"/api/ide/projects/{chat['project_id']}/chats"
    worktree = workspace.load(Path(chat["workspace"]))["root"]

    updated = client.patch(base, json={"title": "  Invoice follow-up  ", "pinned": True})
    assert updated.status_code == 200, updated.text
    assert updated.json()["title"] == "Invoice follow-up"
    assert updated.json()["pinned"] is True
    assert client.get(history).json()["chats"][0]["id"] == chat["id"]
    assert client.patch(base, json={"title": "   "}).status_code == 400

    run = service.store.submit(chat["id"], "pending", "turn", {"text": "Follow up"})
    assert client.delete(base).status_code == 400
    service.store.transition(run["id"], "interrupted")
    assert client.delete(base).status_code == 200
    assert client.get(base).status_code == 400
    assert [item["id"] for item in client.get(history).json()["chats"]] == [other["id"]]
    # Review P6: the conversation's own workspace copy goes with it; the project source stays.
    assert source.is_dir() and not worktree.exists()


def test_checkout_reads_project_git_state_without_using_the_chat_snapshot(ide):
    client, _, source, chat, _ = ide
    url = f"/api/ide/projects/{chat['project_id']}/checkout"
    assert client.get(url).json()["available"] is False
    workspace._git(source, "init", "-b", "reviewing")
    (source / "calc.py").write_text("value = 2\n")
    assert client.get(url).json()["branch"] == "reviewing"
    assert "+value = 2" in client.get(url, params={"path": "calc.py"}).json()["diffs"][0]["diff"]
    assert client.get(url, params={"path": "../outside"}).status_code == 400


def test_editor_conflicts_and_reviewed_integration_leave_original_until_requested(ide):
    client, service, source, chat, _ = ide
    base = "/api/chats/" + chat["id"]
    saved = client.get(base + "/file", params={"path": "calc.py"}).json()
    assert client.put(base + "/file", json={**saved, "text": "value = 2\n"}).status_code == 200
    assert client.put(base + "/file", json={**saved, "text": "lost edit"}).status_code == 409
    assert (source / "calc.py").read_text() == "value = 1\n"
    changed = client.get(base + "/changes").json()["changes"][0]
    assert client.post(base + "/integrations", json={"paths": ["calc.py"], "review": {"calc.py": "stale"}}).status_code == 409
    result = client.post(base + "/integrations", json={"paths": ["calc.py"], "review": {"calc.py": changed["after"]}})
    assert result.status_code == 200 and (source / "calc.py").read_text() == "value = 2\n"
    assert client.get(base + "/file", params={"path": "../original/calc.py"}).status_code == 400


def test_scan_selection_retry_and_chat_ownership(ide):
    client, service, _, chat, other = ide
    base = "/api/chats/" + chat["id"]
    request = {"request_key": "one", "scope": "dependencies", "dependency_mode": "source", "selected_package_ids": []}
    assert client.post(base + "/scans", json=request).status_code == 400
    request["selected_package_ids"] = ["PyPI:demo"]
    first = client.post(base + "/scans", json=request).json()
    assert client.post(base + "/scans", json=request).json()["id"] == first["id"]
    request["scope"] = "project_code"
    assert client.post(base + "/scans", json=request).status_code == 400
    assert client.get("/api/chats/" + other["id"] + "/scans/" + first["id"]).status_code == 400
    assert client.post("/api/chats/" + other["id"] + "/runs/" + first["id"] + "/cancel").status_code == 400
    assert client.post(base + "/scans/" + first["id"] + "/cancel").json()["state"] == "interrupted"
    assert client.post(base + "/permissions/stale", json={"reply": "once"}).status_code == 400


def test_model_review_scan_submits_without_dependency_arguments(ide):
    client, service, _, chat, _ = ide
    base = f"/api/chats/{chat['id']}/scans"
    request = {"request_key": "review", "scope": "model_review"}
    assert client.post(base, json={**request, "dependency_mode": "versions"}).status_code == 400
    assert client.post(base, json={**request, "selected_package_ids": ["PyPI:demo"]}).status_code == 400

    response = client.post(base, json=request)
    assert response.status_code == 200
    assert service.store.get("runs", response.json()["id"])["payload"]["scope"] == "model_review"


def test_scan_only_chat_reuses_workspace_and_declines_agent_actions(ide):
    client, _, _, chat, _ = ide
    create = f"/api/ide/projects/{chat['project_id']}/scan-chat"
    response = client.post(create)
    assert response.status_code == 200
    scan_chat = response.json()
    assert scan_chat["model"]["kind"] == "scan_only"
    assert client.post(create).json()["id"] == scan_chat["id"]
    base = f"/api/chats/{scan_chat['id']}"
    turn = {"request_key": "turn", "text": "Plan my change.", "mode": "plan"}
    assert client.post(base + "/turns", json=turn).status_code == 400
    assert client.post(base + "/runtime/prepare").status_code == 400
    assert client.post(base + "/scans", json={"request_key": "static", "scope": "project_code"}).status_code == 200
    assert client.post(base + "/scans", json={"request_key": "source", "scope": "dependencies",
        "dependency_mode": "source", "selected_package_ids": ["PyPI:demo"]}).status_code == 400
    assert client.post(base + "/scans", json={"request_key": "local", "scope": "model_review"}).status_code == 200
    assert client.post(base + "/scans", json={"request_key": "versions", "scope": "dependencies",
        "dependency_mode": "versions"}).status_code == 200


def test_scan_provenance_respects_chat_scope(ide):
    client, service, _, chat, other = ide
    base = f"/api/chats/{chat['id']}"
    saved = client.get(base + "/file", params={"path": "calc.py"}).json()
    original_hash = saved["version"]

    def record(identity, key, state, result):
        run = service.submit_scan(identity, key, {"scope": "model_review"})
        service.store.claim(identity)
        service.store.transition(run["id"], state, result)
        return run["id"]

    finding = {"path": "calc.py", "line": 1, "quote": "value = 1", "class": "CWE-532",
               "gate_ok": True, "sha256": original_hash}
    first_id = record(chat["id"], "first", "completed", {"status": "partial", "reviewed": 2,
        "skipped": [{"reason": "invalid_answer"}], "findings": [finding, {**finding, "gate_ok": False}]})
    second_id = record(chat["id"], "second", "interrupted", {"reason": "cancelled"})
    other_id = record(other["id"], "other", "completed", {"status": "complete", "findings": [finding]})
    report = client.get(base + "/findings").json()
    assert {row["id"]: row["status"] for row in report["scans"]} == {first_id: "partial", second_id: "interrupted"}
    assert other_id not in str(report) and len(report["findings"]) == 2
    assert len(report["evidence"]) == 1 and report["evidence"][0]["scanId"] == first_id
    assert report["evidence"][0]["source"] == {"path": "calc.py", "line": 1, "quote": "value = 1",
                                               "sha256": original_hash, "freshness": "current"}
    assert client.put(base + "/file", json={**saved, "text": "value = 2\n"}).status_code == 200
    assert client.get(base + "/findings").json()["evidence"][0]["source"]["freshness"] == "stale"


@pytest.mark.parametrize("lookup,status,expected", [
    ("fresh", "no_known_vulnerabilities", "no_match"),
    ("offline_snapshot", "no_known_vulnerabilities", "unavailable"),
    ("fresh", "affected", "match"),
])
def test_dependency_advisory_coverage_follows_exact_source_observation(ide, lookup, status, expected):
    client, service, _, chat, _ = ide
    run = service.submit_scan(chat["id"], "dependency", {"scope": "dependencies", "dependency_mode": "source"})
    service.store.claim(chat["id"])
    finding = {"id": "candidate-1", "evidence": {"path": "demo/core.py", "line": 2, "quote": "log(value)",
               "sha256": "a" * 64}, "rule": "log-rule", "severity": "warning"}
    package = {"package_id": "PyPI:demo", "name": "demo", "ecosystem": "PyPI", "version": "1.0",
               "advisory_context": {"status": status, "lookup": {"status": lookup}, "advisories": []},
               "analysis": {"findings": [finding]}}
    service.store.transition(run["id"], "completed", {"packages": [package]})
    report = client.get(f"/api/chats/{chat['id']}/findings").json()
    assert len(report["evidence"]) == 1
    row = report["evidence"][0]
    assert row["origin"] == "dependency_source" and row["package"]["version"] == "1.0"
    assert report["advisoryCoverage"] == {row["id"]: expected}


@pytest.mark.parametrize("check_status,lookup,scan_status,coverage_available", [
    ("inconclusive", "failed", "partial", False),
    ("no_known_vulnerabilities", "offline_snapshot", "partial", False),
    ("no_known_vulnerabilities", "fresh", "complete", True),
    ("affected", "fresh", "complete", True),
])
def test_workbench_projects_exact_dependency_coverage(ide, check_status, lookup, scan_status, coverage_available):
    client, service, _, chat, _ = ide
    run = service.submit_scan(chat["id"], "dependency-versions", {"scope": "dependencies", "dependency_mode": "versions"})
    service.store.claim(chat["id"])
    advisory = {"id": "GHSA-demo", "ids": ["GHSA-demo", "CVE-2026-12345"], "affected": [
        {"package": {"ecosystem": "PyPI", "name": "demo"}, "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "1.0"}, {"fixed": "1.2"}]}]}
    ]}
    check = {"status": check_status, "lookup": {"status": lookup},
             "advisories": [advisory] if check_status == "affected" else []}
    package = {"id": "PyPI:demo", "name": "demo", "ecosystem": "PyPI", "resolved": ["1.1"], "checks": {"1.1": check}}
    service.store.transition(run["id"], "completed", {"packages": [package]})
    report = client.get(f"/api/chats/{chat['id']}/findings").json()
    assert report["scans"][0]["status"] == scan_status
    assert report["advisoryCoverageAvailable"] is coverage_available
    if check_status == "affected":
        evidence = report["evidence"][0]
        assert evidence["advisory"]["cve"] == "CVE-2026-12345"
        assert evidence["advisory"]["affected_versions"] == "From 1.0, before 1.2"
        assert report["advisoryCoverage"][evidence["id"]] == "match"


def test_workbench_reopens_recorded_assessment_with_bound_evidence(ide):
    client, _, _, chat, other = ide
    item = api.application().item(chat["id"])
    base = f"/api/chats/{chat['id']}"
    saved = client.get(base + "/file", params={"path": "calc.py"}).json()
    receipts = item["directory"] / "receipts"
    receipts.mkdir(exist_ok=True)
    (receipts / "assessment.json").write_text(json.dumps({
        "id": "record", "task": "discovery", "workspace": str(item["root"]),
        "assessment": {"status": "finding", "evidence": [{"quote": "value = 1"}],
                       "findings": [{"summary": "Check the stored value."}]},
        "files": {"calc.py": saved["version"]},
    }))
    evidence = client.get(base + "/findings").json()["evidence"]
    assert evidence[0]["origin"] == "assessment"
    assert evidence[0]["source"]["line"] == 1
    assert client.get(f"/api/chats/{other['id']}/findings").json()["evidence"] == []
    assert client.put(base + "/file", json={**saved, "text": "value = 2\n"}).is_success
    assert "source" not in client.get(base + "/findings").json()["evidence"][0]


def test_intact_reviewed_dependency_snapshot_can_supply_forecast_evidence(ide, monkeypatch, tmp_path):
    client, service, _, chat, _ = ide
    app = api.application()
    cache = tmp_path / "scan-cache"
    vault = cache / "sources" / ("1" * 32)
    directory = vault / ("2" * 20) / "review"
    directory.mkdir(parents=True)
    path = directory / "code.py"
    path.write_bytes(b"record(value)\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(app, "scans", SimpleNamespace(cache=cache), raising=False)
    package = {"package_id": "PyPI:demo", "name": "demo", "ecosystem": "PyPI", "version": "1.0",
               "source_path": str(directory), "files": [{"path": "code.py", "source_path": str(path),
               "sha256": digest, "status": "text"}],
               "advisory_context": {"status": "no_known_vulnerabilities", "lookup": {"status": "fresh"}},
               "analysis": {"findings": [{"evidence": {"path": "code.py", "line": 1, "quote": "record(value)",
               "sha256": digest}}]}}
    run = service.submit_scan(chat["id"], "dependency", {"scope": "dependencies", "dependency_mode": "source"})
    service.store.claim(chat["id"])
    service.store.transition(run["id"], "completed", {"vault": str(vault), "packages": [package]})
    url = f"/api/chats/{chat['id']}/findings"
    evidence = client.get(url).json()["evidence"][0]
    assert evidence["status"] == "grounded" and evidence["source"]["freshness"] == "current"
    latest = service.submit_scan(chat["id"], "dependency-versions", {"scope": "dependencies", "dependency_mode": "versions"})
    service.store.claim(chat["id"])
    service.store.transition(latest["id"], "completed", {"packages": [{
        "id": "PyPI:demo", "name": "demo", "ecosystem": "PyPI", "resolved": ["1.0"],
        "checks": {"1.0": {"status": "affected", "lookup": {"status": "fresh"}, "advisories": [{"id": "GHSA-new"}]}},
    }]})
    assert client.get(url).json()["advisoryCoverage"][evidence["id"]] == "match"
    path.write_bytes(b"record(other)\n")
    evidence = client.get(url).json()["evidence"][0]
    assert evidence["status"] == "lead" and evidence["source"]["freshness"] == "stale"


def test_project_instructions_and_memory_are_explicit_between_turns(ide):
    client, service, _, chat, _ = ide
    path = "/api/ide/projects/" + chat["project_id"]
    assert client.patch(path, json={"memory": ["Use pytest"], "instructions": "Keep public names"}).status_code == 200
    assert client.patch(path, json={"memory": ["x" * 8001]}).status_code == 400
    run = service.submit(chat["id"], "turn", "Explain calc.py", mode="plan")
    service.store.claim(chat["id"])
    assert client.patch(path, json={"instructions": "Changed during turn"}).status_code == 400
    assert client.patch("/api/chats/" + chat["id"], json={"archived": True}).status_code == 400


def test_project_rename_preserves_source_and_settings(ide):
    client, _, source, chat, _ = ide
    url = f"/api/ide/projects/{chat['project_id']}"
    client.patch(url, json={"instructions": "Use pytest"}).raise_for_status()

    renamed = client.patch(url, json={"name": "  Review workspace  "}).raise_for_status().json()
    assert renamed["name"] == "Review workspace"
    assert renamed["source"] == str(source)
    assert renamed["settings"]["instructions"] == "Use pytest"
    assert client.patch(url, json={"name": "   "}).status_code == 400
    assert client.get("/api/ide/projects").json()["projects"][0]["name"] == "Review workspace"


def test_reading_a_conversation_without_a_workspace_never_provisions_one(ide, monkeypatch):
    # Review P4: file lists, file reads, findings and patch details are read-only views of the registered source
    # until the worker or an explicit action (save, terminal, coding turn) creates the workspace.
    client, service, source, first, _ = ide
    chat = service.create_chat(first["project_id"], "Deferred", {"model": "qwen", "kind": "local"}, defer_workspace=True)
    app = api.application()

    def provision(*_args, **_kwargs):
        raise AssertionError("A read-only request provisioned a workspace.")

    monkeypatch.setattr(app, "item", provision)
    monkeypatch.setattr(service, "ensure_workspace", provision)
    monkeypatch.setattr(workspace, "provision", provision)
    base = f"/api/chats/{chat['id']}"

    listing = client.get(base + "/files")
    assert listing.status_code == 200
    body = listing.json()
    assert body["read_only"] is True and {row["path"] for row in body["files"]} == {"calc.py", "requirements.txt"}
    read = client.get(base + "/file", params={"path": "calc.py"})
    assert read.status_code == 200 and read.json()["text"] == (source / "calc.py").read_bytes().decode()
    assert client.get(base + "/findings").status_code == 200
    assert client.get(base + "/patches/missing-candidate").status_code in (400, 404, 409)
    assert service.store.get("chats", chat["id"])["workspace"] is None
