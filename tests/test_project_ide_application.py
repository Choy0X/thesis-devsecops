"""Production composition pins the selected base and preserves partial scan evidence."""
import hashlib
import json
from pathlib import Path
import threading
import time

import pytest

from app.backend.pipeline.project_ide.application import Application
from app.backend.pipeline.project_ide.store import StoreError
from app.backend.pipeline.project_ide import workspace
from test_project_ide_service import NativeFixture, wait_for
from tests.fake_ollama import fake_ollama  # noqa: F401
from test_ollama_models import ollama_registrations  # noqa: F401


class Registry:
    def __init__(self, path):
        self.path, self.enabled = path, []
    def installed(self):
        return [{"id": "Qwen-Qwen2.5-Coder-7B-Instruct", "source": "converted", "gguf": [str(self.path)]}]
    def enable(self, *args, **kwargs):
        self.enabled.append((args, kwargs))


def test_catalog_uses_installed_tags_and_capabilities(tmp_path, fake_ollama):
    from types import SimpleNamespace
    adapter = tmp_path / "unregistered-adapter.gguf"
    adapter.write_bytes(b"GGUF-fixture")
    fake_ollama.models = {"qwen2.5-coder:7b": 1000, "nomic-embed:latest": 500,
                         "custom-proxy:latest": 100, "aivf/reviewer-v40:latest": 1100}
    fake_ollama.metadata = {
        "qwen2.5-coder:7b": {"show": {"capabilities": ["completion", "tools"]}},
        "nomic-embed:latest": {"show": {"capabilities": ["embedding"]}},
        "custom-proxy:latest": {"show": {"capabilities": ["completion", "tools"], "remote_host": "https://ollama.com"}},
        "aivf/reviewer-v40:latest": {"show": {"capabilities": ["completion"]}},
    }
    app = Application(tmp_path / "managed", registry=Registry(adapter), settings_loader=lambda: SimpleNamespace())
    try:
        rows = {row["tag"]: row for row in app.models()}
        assert set(rows) == {"qwen2.5-coder:7b", "custom-proxy:latest", "aivf/reviewer-v40:latest"}
        local, cloud, reviewer = (rows[name] for name in ("qwen2.5-coder:7b", "custom-proxy:latest", "aivf/reviewer-v40:latest"))
        assert local["location"] == "local" and local["name"] == "Qwen2.5 Coder 7B"
        assert cloud["location"] == "cloud" and not cloud["selected"]
        assert reviewer["tool_protocol"] == "text" and reviewer["name"] == "AIVF Reviewer v40"
        with pytest.raises(StoreError, match="Allow Ollama Cloud"):
            app.select(cloud)
        assert app.select({**cloud, "allow_remote": True})["allow_remote"]
        fake_ollama.models.pop(local["tag"])
        with pytest.raises(StoreError, match="no longer available"):
            app.select(local)
    finally:
        app.close()


def test_reviewer_answers_composer_prompts_without_native_runtime(tmp_path, fake_ollama, monkeypatch):
    from app.backend import paths
    monkeypatch.setattr(paths, "interim_dir", lambda: tmp_path / "runtime")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    tag = "aivf/reviewer-v40:latest"
    fake_ollama.models = {tag: 1100}
    fake_ollama.metadata[tag] = {"show": {"capabilities": ["completion"]}}
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.py").write_text("value = True\n")
    app = Application(tmp_path / "managed", registry=Registry(tmp_path / "absent.gguf"), runtime_factory=NativeFixture)
    try:
        row, = app.models()
        prepared = app.prepare(row, context=2048)
        wait_for(lambda: app.preparation(prepared["id"])["state"] != "loading")
        preparation = app.preparation(prepared["id"])
        assert preparation["state"] == "ready", preparation
        selected = app.prepared_model(prepared["id"], preparation["model"])
        project = app.service.create_project("Fixture", str(source))
        chat = app.service.create_chat(project["id"], "New conversation", selected)
        assert app.prepare_runtime(chat["id"])["ready"]
        runtime_count = len(NativeFixture.instances)
        for key, text in [("first", "Review this return statement."), ("second", "Explain the result.")]:
            run = app.service.submit(chat["id"], key, text)
            wait_for(lambda: app.service.store.get("runs", run["id"])["state"] in ("completed", "failed"))
            assert app.service.store.get("runs", run["id"])["state"] == "completed"
        assert len(NativeFixture.instances) == runtime_count
        requests = [body for _, path, body in fake_ollama.requests if path == "/api/chat"]
        assert len(requests) == 2 and all("tools" not in body for body in requests)
        assert requests[1]["messages"][0]["role"] == "system"
        assert 'Registered project "Fixture"' not in requests[1]["messages"][0]["content"]
        assert requests[1]["messages"][1:3] == [
            {"role": "user", "content": "Review this return statement."},
            {"role": "assistant", "content": "Reviewed."},
        ]
        latest = requests[1]["messages"][-1]
        assert latest["role"] == "user"
        assert latest["content"].startswith("Explain the result.\n\nProject evidence.")
        assert 'Registered project "Fixture"' in latest["content"]
        assert len(app.service.detail(chat["id"])["messages"]) == 4
    finally:
        app.close()


def test_native_tag_preparation_pins_context_and_reports_gpu_holder(tmp_path, fake_ollama, monkeypatch):
    from app.backend import paths
    from app.backend.pipeline.ollama import exclusive

    tag = "qwen2.5-coder:7b"
    fake_ollama.models = {tag: 1000}
    fake_ollama.metadata[tag] = {"show": {"capabilities": ["completion", "tools"]}}
    monkeypatch.setattr(paths, "interim_dir", lambda: tmp_path / "runtime")
    app = Application(tmp_path / "managed", registry=Registry(tmp_path / "missing.gguf"))
    try:
        row, = app.models()
        job = app.prepare(row, context=2048)
        wait_for(lambda: app.preparation(job["id"])["state"] != "loading")
        prepared = app.preparation(job["id"])
        assert prepared["state"] == "ready", prepared
        model = prepared["model"]
        assert model["tag"] == tag and model["ollama_name"].startswith("aivf/runtime-")
        assert app.prepared_model(job["id"], model) == model
        assert len(app.models()) == 1
        assert any(path == "/api/create" and body["parameters"] == {"num_ctx": 2048}
                   for _, path, body in fake_ollama.requests)
        monkeypatch.setattr(exclusive, "current", lambda **_kwargs: {"owner": "Advisory review campaign"})
        held = app.prepare(row)
        wait_for(lambda: app.preparation(held["id"])["state"] == "failed")
        assert "Advisory review campaign" in app.preparation(held["id"])["error"]["message"]
    finally:
        app.close()


def test_project_choices_use_settings_inventory_labels_and_exclude_embeddings_and_unreadable_files(tmp_path, ollama_registrations):
    from types import SimpleNamespace
    from app.backend.pipeline import model_access
    from app.backend import serving_api
    files = []
    for name, role, label, info in [("small.gguf", "chat", "Example/Small-Coder, Q4_K_M", {"quantisation": "Q4_K_M"}),
                                    ("large.gguf", "chat", "Other/Code-Model, Q8_0", {"quantisation": "Q8_0"}),
                                    ("embed.gguf", "embedding", "Embedding model", {}),
                                    ("broken.gguf", "chat", "Broken model", None)]:
        path = tmp_path / name
        path.write_bytes(b"GGUF-fixture")
        files.append({"id": name, "name": name, "path": str(path), "source": "converted", "label": label, "role": role, "info": info})
    # Both consumers get the exact same Settings inventory, including descriptive labels.
    from unittest.mock import patch
    with patch.object(serving_api, "serving_models", return_value={"files": files, "not_ready": []}):
        registry = model_access.Registry(tmp_path / "registry")
        app = Application(tmp_path / "managed", registry=registry, settings_loader=lambda: SimpleNamespace(llm_local_model=str(tmp_path / "large.gguf")))
        try:
            choices = app._legacy_models(app.settings_loader())
            assert [row["name"] for row in choices] == [files[0]["label"], files[1]["label"]]
            assert choices[1]["selected"] and not choices[0]["selected"]
            selected = app.select(choices[0])
            assert selected["model"] == files[0]["path"] and selected["name"] == files[0]["label"]
            assert selected["id"].startswith("aivf/") and selected["id"] in ollama_registrations
            assert selected["tool_protocol"] == "native"
            assert registry.spec(selected["id"])["model"] == files[0]["path"]
            config = app.configure(selected, {"root": tmp_path, "directory": tmp_path / "chat", "source": tmp_path}, {})
            assert config["model"] == "framework/" + selected["ollama_name"]
            assert "headers" not in config["provider"]["framework"]["options"]
        finally:
            app.close()


def test_arbitrary_configured_hosted_model_is_available_without_a_family_filter(tmp_path):
    from types import SimpleNamespace
    settings = SimpleNamespace(llm_engine="external", llm_model="Other/Code-Model", llm_base_url="https://model.example.test/v1", llm_api_key="secret")
    app = Application(tmp_path / "managed", registry=Registry(tmp_path / "absent.gguf"), settings_loader=lambda: settings)
    try:
        assert app.models()[0]["name"] == "Other/Code-Model (hosted)"
        assert app.select()["model"] == "Other/Code-Model"
        assert "secret" not in str(app.models())
    finally:
        app.close()


def test_selected_qwen_has_no_adapter_and_project_scan_needs_no_model_runtime(tmp_path, monkeypatch, workspace, fake_ollama):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    model = tmp_path / "qwen.gguf"
    model.write_bytes(b"GGUF-fixture")
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("result = eval(request)\n")
    excluded = source / "node_modules"
    excluded.mkdir()
    (excluded / "ignored.py").write_text("eval(untrusted)\n")
    backend = Application(tmp_path / "managed", registry=Registry(model), runtime_factory=NativeFixture)
    try:
        chosen = backend.select()
        assert chosen["id"].startswith("aivf/") and chosen["adapter"] is None and chosen["context"] == 8192
        fake_ollama.running = [chosen["ollama_name"]]
        fake_ollama.contexts[chosen["ollama_name"]] = chosen["context"]
        assert all("adapter" not in kwargs for _, kwargs in backend.registry.enabled)
        project = backend.service.create_project("Fixture", str(source))
        scratch = backend.service.create_scan_chat(project["id"])
        runtime_count = len(NativeFixture.instances)
        chat = backend.service.create_chat(project["id"], "Scan", chosen)
        run = backend.service.submit_scan(scratch["id"], "static-only", {"scope": "project_code"})
        wait_for(lambda: backend.service.store.get("runs", run["id"])["state"] in ("completed", "failed"))
        assert backend.service.store.get("runs", run["id"])["result"]["findings"]
        run = backend.service.submit_scan(chat["id"], "scan", {"scope": "project_code"})
        wait_for(lambda: backend.service.store.get("runs", run["id"])["state"] in ("completed", "failed"))
        final = backend.service.store.get("runs", run["id"])
        assert final["state"] == "completed", final
        assert final["result"]["findings"] and "agent" not in final["result"]
        assert len(NativeFixture.instances) == runtime_count
        assert (source / "app.py").read_text() == "result = eval(request)\n"
        model.write_bytes(b"GGUF-changed")
        with pytest.raises(StoreError):
            backend.configure(chosen, backend.item(chat["id"]), {})
    finally:
        backend.close()


def test_different_qwen_artifacts_receive_distinct_immutable_aliases(tmp_path):
    first, second = tmp_path / "q4.gguf", tmp_path / "q8.gguf"
    first.write_bytes(b"GGUF-four")
    second.write_bytes(b"GGUF-eight")
    registry = Registry(first)
    registry.installed = lambda: [{"id": "Qwen-Qwen2.5-Coder-7B-Instruct", "source": "converted", "gguf": [str(first), str(second)]}]
    backend = Application(tmp_path / "managed", registry=registry)
    try:
        models = backend._legacy_models(backend.settings_loader())
        left, right = backend.select(models[0]), backend.select(models[1])
        assert left["id"] != right["id"]
        assert registry.enabled[0][1]["alias"] != registry.enabled[1][1]["alias"]
        assert backend.select(left)["id"] == left["id"]
    finally:
        backend.close()


def test_existing_chat_legacy_alias_remains_bound_to_the_same_artifact(tmp_path):
    model = tmp_path / "qwen.gguf"
    model.write_bytes(b"GGUF-fixture")
    app = Application(tmp_path / "managed", registry=Registry(model))
    try:
        row = app._legacy_models(app.settings_loader())[0]
        stat = model.stat()
        identity = json.dumps([str(model.resolve()), stat.st_size, stat.st_mtime_ns], ensure_ascii=True)
        legacy = {**row, "id": "projects-qwen2.5-coder-7b-instruct-" + hashlib.sha256(identity.encode()).hexdigest()[:16]}
        selected = app.select(legacy)
        assert selected["id"] == legacy["id"] and selected["model"] == row["model"]
        assert selected["ollama_name"] == row["id"]
        assert app.registry.enabled[-1][1]["alias"] == legacy["id"]
        config = app.configure(selected, {"root": tmp_path, "directory": tmp_path / "chat", "source": tmp_path}, {})
        assert config["model"] == "framework/" + row["id"]
    finally:
        app.close()


def test_replacement_after_project_checksum_does_not_persist_a_registry_mapping(tmp_path, monkeypatch, ollama_registrations):
    from app.backend.pipeline import model_access

    path = tmp_path / "code.gguf"
    path.write_bytes(b"GGUF-fixture")
    registry = model_access.Registry(tmp_path / "registry", installed=lambda: [{"id": "example", "gguf": [str(path)]}])
    app = Application(tmp_path / "managed", registry=registry)
    enable = registry.enable

    def replace_before_enable(*args, **kwargs):
        path.write_bytes(b"GGUF-a-different-file")
        return enable(*args, **kwargs)

    monkeypatch.setattr(registry, "enable", replace_before_enable)
    try:
        with pytest.raises(StoreError, match="changed"):
            app.select()
        assert not registry.read()["models"]
        assert not ollama_registrations
    finally:
        app.close()


def test_source_tool_reads_only_selected_hashed_vault_files(tmp_path):
    import json
    from app.backend.pipeline.project_ide.tools import ToolService
    root, receipts, vault = tmp_path / "files", tmp_path / "receipts", tmp_path / "vault"
    for path in (root, receipts, vault):
        path.mkdir()
    saved = vault / "module.py"
    saved.write_text("safe = True\n")
    document = {"packages": [{"package_id": "PyPI:demo", "version": "1.0", "origin": "installed", "files": [
        {"path": "module.py", "source_path": str(saved), "sha256": workspace.digest(saved)}]}]}
    (receipts / "selected-sources.json").write_text(json.dumps(document))
    tools = ToolService(root, receipts, cache=vault)
    assert tools.call("selected_dependency_source", {"package_id": "PyPI:demo", "path": "module.py"})["read_only"]
    with pytest.raises(ValueError, match="user-selected"):
        tools.call("selected_dependency_source", {"package_id": "PyPI:other", "path": "module.py"})
    saved.write_text("changed = True")
    with pytest.raises(ValueError, match="changed"):
        tools.call("selected_dependency_source", {"package_id": "PyPI:demo", "path": "module.py"})


def test_hosted_qwen_uses_explicit_model_and_existing_private_credentials(tmp_path):
    from types import SimpleNamespace
    settings = SimpleNamespace(llm_engine="external", llm_model="Qwen/Qwen2.5-Coder-7B-Instruct", llm_base_url="https://model.example.test/v1", llm_api_key="private-fixture-key")
    registry = Registry(tmp_path / "not-installed.gguf")
    backend = Application(tmp_path / "managed", registry=registry, settings_loader=lambda: settings)
    root = tmp_path / "files"
    root.mkdir()
    item = {"root": root, "directory": tmp_path / "chat", "source": root}
    try:
        chosen = backend.select()
        assert chosen["kind"] == "hosted" and chosen["adapter"] is None
        assert "private-fixture-key" not in str(backend.models()) and not registry.enabled
        config = backend.configure(chosen, item, {})
        assert config["model"] == config["small_model"] == "framework/Qwen/Qwen2.5-Coder-7B-Instruct"
        options = config["provider"]["framework"]["options"]
        assert options["baseURL"] == settings.llm_base_url and options["apiKey"] == settings.llm_api_key
        assert "headers" not in options
    finally:
        backend.close()


def test_an_expired_runner_is_loaded_again_when_the_turn_starts(tmp_path, fake_ollama, monkeypatch):
    # Review O4: Ollama unloads an idle runner after keep_alive; the next turn loads it again instead of refusing.
    from app.backend import paths
    monkeypatch.setattr(paths, "interim_dir", lambda: tmp_path / "runtime")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    tag = "aivf/reviewer-v40:latest"
    fake_ollama.models = {tag: 1100}
    fake_ollama.metadata[tag] = {"show": {"capabilities": ["completion"]}}
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.py").write_text("value = True\n")
    app = Application(tmp_path / "managed", registry=Registry(tmp_path / "absent.gguf"), runtime_factory=NativeFixture)
    try:
        row, = app.models()
        prepared = app.prepare(row, context=2048)
        wait_for(lambda: app.preparation(prepared["id"])["state"] != "loading")
        selected = app.prepared_model(prepared["id"], app.preparation(prepared["id"])["model"])
        project = app.service.create_project("Fixture", str(source))
        chat = app.service.create_chat(project["id"], "New conversation", selected)
        fake_ollama.running.clear()
        before = len(fake_ollama.requests)

        run = app.service.submit(chat["id"], "first", "Review this return statement.")
        wait_for(lambda: app.service.store.get("runs", run["id"])["state"] in ("completed", "failed"))

        assert app.service.store.get("runs", run["id"])["state"] == "completed", app.service.store.get("runs", run["id"])
        assert any(path == "/api/generate" and body.get("model", "").startswith(selected["ollama_name"])
                   for _, path, body in fake_ollama.requests[before:])
    finally:
        app.close()
