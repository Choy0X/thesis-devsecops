"""Runtime admission and lifecycle boundaries, without a real model or user state."""
from types import SimpleNamespace

import pytest

from app.backend.pipeline.project_ide import runtime


def test_wrong_runtime_version_is_rejected_before_a_server_starts(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="2.0.0\n", stderr=""))
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: calls.append(a))
    server = runtime.OpenCodeRuntime(tmp_path / "workspace", tmp_path / "private", executable="fixture-opencode")
    with pytest.raises(runtime.RuntimeError, match="1.18.34"):
        server.start()
    assert calls == []


def test_gateway_provider_names_the_fine_tuned_endpoint_without_fallback():
    config = runtime.gateway_configuration("http://127.0.0.1:12345/v1", "private-key", context=16384, model_id="aivf/chat-ctx16384")
    assert config["model"] == "framework/aivf/chat-ctx16384"
    provider = config["provider"]["framework"]
    assert provider["options"]["baseURL"] == "http://127.0.0.1:12345/v1"
    assert "headers" not in provider["options"]
    assert provider["models"]["aivf/chat-ctx16384"]["limit"]["context"] == 16384
    assert config["small_model"] == config["model"]


def test_explicit_project_model_alias_cannot_fall_back_to_settings_role():
    config = runtime.gateway_configuration("http://127.0.0.1:12345/v1", "fixture", context=8192,
                                           model_id="aivf/qwen25-7b-ctx8192")
    assert config["model"] == "framework/aivf/qwen25-7b-ctx8192"
    assert config["small_model"] == config["model"]
    assert "aivf/qwen25-7b-ctx8192" in config["provider"]["framework"]["models"]


def test_small_context_profile_does_not_reserve_a_larger_models_context_budget():
    config = runtime.gateway_configuration("http://127.0.0.1:12345/v1", "fixture", context=8192, model_id="aivf/chat-ctx8192")
    assert config["compaction"]["reserved"] <= 1024
    assert config["compaction"]["preserve_recent_tokens"] <= 2048
    assert config["provider"]["framework"]["models"]["aivf/chat-ctx8192"]["limit"]["output"] <= 1024


def test_stop_without_a_started_process_is_safe(tmp_path):
    server = runtime.OpenCodeRuntime(tmp_path / "workspace", tmp_path / "private", executable="fixture-opencode")
    server.stop()
    server.stop()
    assert server.process is None


def core_dependencies(folder):
    """Minimal installed core SDK fixture; no extension or user config files."""
    import json
    folder.mkdir(parents=True)
    dependency = {"@opencode-ai/plugin": "1.18.34"}
    (folder / "package.json").write_text(json.dumps({"dependencies": dependency}))
    (folder / "package-lock.json").write_text(json.dumps({"packages": {
        "": {"dependencies": dependency}, "node_modules/@opencode-ai/plugin": {"version": "1.18.34"},
        "node_modules/@opencode-ai/sdk": {"version": "1.18.34"}}}))
    for name in ("plugin", "sdk"):
        package = folder / "node_modules" / "@opencode-ai" / name
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"name": "@opencode-ai/" + name, "version": "1.18.34"}))
        (package / "index.js").write_text("export const fixture = true;")


def test_core_sdk_cache_populates_only_new_private_dependency_directories(tmp_path):
    import json
    server = runtime.OpenCodeRuntime(tmp_path / "files", tmp_path / "first", executable="fixture")
    server.dependency_cache = tmp_path / "managed-core-cache" / "1.18.34"
    first = server.state_dir / "config" / "opencode"
    core_dependencies(first)
    (first / "opencode.json").write_text('{"plugin":["user-private-extension"]}')
    (first / "skills").mkdir()
    (first / "skills" / "private.txt").write_text("private skill")
    server.dependency_sources = [first]
    second = tmp_path / "second" / "config" / "opencode"
    second.mkdir(parents=True)
    assert server._seed_dependencies(second)
    assert json.loads((second / "package.json").read_text())["dependencies"] == {"@opencode-ai/plugin": "1.18.34"}
    assert (second / "node_modules" / "@opencode-ai" / "sdk" / "index.js").read_text() == "export const fixture = true;"
    assert not (second / "opencode.json").exists()
    assert not (second / "skills").exists()
    assert not server._seed_dependencies(first)


def test_tampered_core_sdk_cache_falls_back_to_native_installation(tmp_path):
    server = runtime.OpenCodeRuntime(tmp_path / "files", tmp_path / "first", executable="fixture")
    server.dependency_cache = tmp_path / "cache" / "1.18.34"
    first = server.state_dir / "config" / "opencode"
    core_dependencies(first)
    server._remember_dependencies(first)
    package = server.dependency_cache / "core.zip"
    package.write_bytes(package.read_bytes() + b"tampered")
    second = tmp_path / "second"
    second.mkdir()
    assert not server._seed_dependencies(second)
    assert not (second / "node_modules").exists()
    assert not server.dependency_cache.exists()
    assert list(server.dependency_cache.parent.glob("1.18.34.invalid-*"))
    server.dependency_sources = [first]
    assert server._seed_dependencies(second)


def test_changed_sdk_version_or_extra_extension_is_never_cached(tmp_path):
    import json
    server = runtime.OpenCodeRuntime(tmp_path / "files", tmp_path / "first", executable="fixture")
    server.dependency_cache = tmp_path / "cache" / "1.18.34"
    first = server.state_dir / "config" / "opencode"
    core_dependencies(first)
    package = first / "node_modules" / "@opencode-ai" / "sdk" / "package.json"
    package.write_text('{"version":"different"}')
    server._remember_dependencies(first)
    assert not server.dependency_cache.exists()
    package.write_text('{"version":"1.18.34"}')
    (first / "package.json").write_text(json.dumps({"dependencies": {"@opencode-ai/plugin": "1.18.34", "user-extension": "1.0.0"}}))
    server._remember_dependencies(first)
    assert not server.dependency_cache.exists()


def test_unsafe_core_archive_is_rejected_before_any_private_files_are_written(tmp_path):
    import hashlib
    import json
    import zipfile
    server = runtime.OpenCodeRuntime(tmp_path / "files", tmp_path / "private", executable="fixture")
    server.dependency_cache = tmp_path / "cache" / "1.18.34"
    source = tmp_path / "source"
    core_dependencies(source)
    server._remember_dependencies(source)
    package = server.dependency_cache / "core.zip"
    with zipfile.ZipFile(package, "a") as archive:
        archive.writestr("../escaped.txt", "outside")
    (server.dependency_cache / "manifest.json").write_text(json.dumps({
        "version": "1.18.34", "sha256": hashlib.sha256(package.read_bytes()).hexdigest()}))
    target = tmp_path / "target"
    target.mkdir()
    assert not server._seed_dependencies(target)
    assert list(target.iterdir()) == []
    assert not (tmp_path / "escaped.txt").exists()


def test_interrupted_private_sdk_extraction_never_writes_a_complete_install_lock(tmp_path, monkeypatch):
    import zipfile
    server = runtime.OpenCodeRuntime(tmp_path / "files", tmp_path / "private", executable="fixture")
    server.dependency_cache = tmp_path / "cache" / "1.18.34"
    source = tmp_path / "source"
    core_dependencies(source)
    server._remember_dependencies(source)
    original = zipfile.ZipFile.extract
    def interrupted(archive, member, path=None, pwd=None):
        if member.filename.endswith("sdk/index.js"):
            raise OSError("Fixture disk interruption")
        return original(archive, member, path, pwd)
    monkeypatch.setattr(zipfile.ZipFile, "extract", interrupted)
    target = tmp_path / "target"
    target.mkdir()
    assert not server._seed_dependencies(target)
    assert (target / "node_modules").is_dir()
    assert not (target / "package-lock.json").exists()


def test_private_directories_exist_before_the_version_process(tmp_path, monkeypatch):
    from pathlib import Path

    def version(*args, **kwargs):
        env = kwargs["env"]
        assert Path(env["XDG_DATA_HOME"]).is_dir()
        assert Path(env["OPENCODE_CONFIG_DIR"]).is_dir()
        return SimpleNamespace(returncode=0, stdout="wrong-version", stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", version)
    server = runtime.OpenCodeRuntime(tmp_path / "workspace", tmp_path / "private", executable="fixture-opencode")
    with pytest.raises(runtime.RuntimeError, match="1.18.34"):
        server.start()


def test_stop_closes_containment_after_parent_exit(tmp_path):
    closed = []
    server = runtime.OpenCodeRuntime(tmp_path / "workspace", tmp_path / "private", executable="fixture-opencode")
    server.process = SimpleNamespace(poll=lambda: 0)
    server._containment = SimpleNamespace(close=lambda: closed.append(True))
    server.stop()
    server.stop()
    assert closed == [True]


def test_every_session_request_is_explicitly_bound_to_its_workspace(tmp_path, monkeypatch):
    import io
    from urllib.parse import urlsplit, parse_qs
    requested = []
    def open_request(request, **kwargs):
        requested.append(request.full_url)
        return io.BytesIO(b"[]")
    monkeypatch.setattr(runtime.urllib.request, "urlopen", open_request)
    server = runtime.OpenCodeRuntime(tmp_path / "chat files", tmp_path / "private", executable="fixture")
    server.port = 12345
    server.request("GET", "/session/ses_fixture/message")
    assert parse_qs(urlsplit(requested[0]).query)["directory"] == [str(server.workspace)]


def test_application_command_permissions_require_explicit_once_and_abort_rejects_pending(tmp_path, monkeypatch):
    import io
    import json
    monkeypatch.setattr(runtime.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"[]"))
    server = runtime.OpenCodeRuntime(tmp_path / "files", tmp_path / "private", executable="fixture")
    server.port = 12345
    folder = server.state_dir / "permissions"
    folder.mkdir(parents=True)
    identity = "appcmd_" + "a" * 32
    pending = folder / (identity + ".json")
    pending.write_text(json.dumps({"id": identity, "sessionID": "session", "source": "application_command", "patterns": ["test-command"]}))
    assert server.request("GET", "/permission")[0]["id"] == identity
    with pytest.raises(runtime.RuntimeError):
        server.request("POST", "/permission/" + identity + "/reply", {"reply": "always"})
    server.request("POST", "/session/session/abort")
    assert json.loads(pending.with_suffix(".reply").read_text())["reply"] == "reject"
    assert server.request("GET", "/permission") == []
    with pytest.raises(runtime.RuntimeError):
        server.request("POST", "/permission/" + identity + "/reply", {"reply": "once"})


@pytest.mark.skipif(runtime.os.name != "nt", reason="Windows job containment")
def test_windows_job_kills_orphaned_child(tmp_path):
    import json
    import sys
    import time
    import psutil

    receipt = tmp_path / "child.json"
    script = "import subprocess,sys,json; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path(sys.argv[1]).write_text(json.dumps({'pid':p.pid}))"
    job = runtime.WindowsProcessJob()
    parent = None
    child = None
    try:
        parent = runtime.subprocess.Popen([sys.executable, "-c", script, str(receipt)],
                                         creationflags=runtime.subprocess.CREATE_NO_WINDOW | 0x00000004)
        job.assign(parent)
        parent.wait(timeout=10)
        child = psutil.Process(json.loads(receipt.read_text())["pid"])
        assert child.is_running()
        job.close()
        child.wait(timeout=10)
    finally:
        job.close()
        if parent is not None and parent.poll() is None:
            parent.kill()
        if child is not None and child.is_running():
            child.kill()
