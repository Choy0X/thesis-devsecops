from pathlib import Path
import threading
import time
import pytest

from app.backend.pipeline.project_ide import service


class NativeFixture:
    instances = []
    def __init__(self, workspace, state_dir, **kwargs):
        self.workspace = Path(workspace)
        self.messages = []
        self.prompts = []
        self.process = None
        NativeFixture.instances.append(self)
    def start(self):
        return self
    def stop(self):
        pass
    def request(self, method, path, body=None, **kwargs):
        if path == "/session" and method == "POST":
            return {"id": "ses_fixture"}
        if path.endswith("/prompt_async"):
            self.prompts.append(body)
            text = body["parts"][0]["text"]
            message_id = "fixture-" + str(len(self.prompts))
            self.messages += [{"info": {"role": "user", "id": message_id}, "parts": body["parts"]},
                              {"info": {"role": "assistant", "id": "answer-" + message_id, "finish": "stop", "time": {"completed": 1}},
                               "parts": [{"type": "text", "text": "Observed: " + text}]}]
            return None
        if path.endswith("/message"):
            return self.messages
        if path == "/session/status":
            return {}
        if path in ("/permission", "/question"):
            return []
        raise AssertionError(path)


def test_added_sources_reach_native_turn_as_read_only_context_without_changing_user_text(tmp_path, monkeypatch):
    from app.backend.pipeline.project_ide.chat_context import Sources
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    original = tmp_path / "source"
    original.mkdir()
    (original / "main.py").write_text("answer = 42")
    NativeFixture.instances.clear()
    backend = service.Service(tmp_path / "managed", runtime_factory=NativeFixture, configure=lambda *args: {})
    try:
        project = backend.create_project("Fixture", str(original))
        chat = backend.create_chat(project["id"], "Inspect", {"model": "qwen", "kind": "local"})
        sources = Sources(backend.store)
        added = sources.add(chat["id"], {"kind": "project_file", "path": "main.py"})
        first = backend.submit(chat["id"], "with-source", "Explain the attached source")
        wait_for(lambda: backend.store.get("runs", first["id"])["state"] == "completed")
        parts = NativeFixture.instances[0].prompts[0]["parts"]
        assert parts[0] == {"type": "text", "text": "Explain the attached source"}
        assert len(parts) == 2 and "answer = 42" in parts[1]["text"]
        assert "untrusted reference data" in parts[1]["text"] and "read-only" in parts[1]["text"]
        sources.delete(chat["id"], added["id"])
        second = backend.submit(chat["id"], "without-source", "Explain again")
        wait_for(lambda: backend.store.get("runs", second["id"])["state"] == "completed")
        assert NativeFixture.instances[0].prompts[1]["parts"] == [{"type": "text", "text": "Explain again"}]
        assert (original / "main.py").read_text() == "answer = 42"
    finally:
        backend.close()


def wait_for(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    assert check()


def test_deferred_workspace_is_prepared_once_by_first_coding_turn(tmp_path, monkeypatch):
    from app.backend.pipeline.project_ide import workspace

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    original = tmp_path / "source"
    original.mkdir()
    (original / "main.py").write_text("answer = 42")
    release = threading.Event()
    provision = workspace.provision
    destinations = []

    def blocked_provision(source, directory, **kwargs):
        destinations.append(directory)
        assert release.wait(5)
        return provision(source, directory, **kwargs)

    def code(chat, *_, **_checkpoint):
        item = workspace.load(Path(chat["workspace"]))
        return [{"type": "text", "text": workspace.read(item, "main.py")["text"]}]

    monkeypatch.setattr(workspace, "provision", blocked_provision)
    backend = service.Service(tmp_path / "managed", coding_turn=code)
    try:
        project = backend.create_project("Fixture", str(original))
        chat = backend.create_chat(project["id"], "New chat", {"model": "qwen", "kind": "local", "context": 1024}, defer_workspace=True)
        assert chat["workspace"] is None
        first = backend.submit(chat["id"], "first", "Read main.py")
        wait_for(lambda: bool(destinations))
        assert backend.detail(chat["id"])["activity"]["phase"] == "preparing_workspace"
        assert backend.store.get("chats", chat["id"])["workspace"] is None
        release.set()
        second = backend.submit(chat["id"], "second", "Read again")
        wait_for(lambda: backend.store.get("runs", second["id"])["state"] == "completed")
        assert backend.store.get("runs", first["id"])["state"] == "completed"
        assert len(destinations) == 1
        assert backend.detail(chat["id"])["messages"][-1]["parts"][0]["text"] == "answer = 42"
    finally:
        release.set()
        backend.close()


def test_text_only_deferred_chat_reopens_and_runs_without_copying_source(tmp_path):
    original = tmp_path / "source"
    original.mkdir()
    backend = service.Service(tmp_path / "managed")
    try:
        project = backend.create_project("Fixture", str(original))
        model = {"model": "reviewer", "kind": "local", "context": 1024, "tool_protocol": "text"}
        chat = backend.create_chat(project["id"], "New chat", model, defer_workspace=True)
    finally:
        backend.close()
    reopened = service.Service(tmp_path / "managed", text_turn=lambda *_: "Hello")
    try:
        run = reopened.submit(chat["id"], "greeting", "Hi")
        wait_for(lambda: reopened.store.get("runs", run["id"])["state"] == "completed")
        detail = reopened.detail(chat["id"])
        assert detail["chat"]["workspace"] is None
        assert detail["messages"][-1]["parts"][0]["text"] == "Hello"
        assert not (reopened.root / "workspaces").exists()
    finally:
        reopened.close()


def test_deferred_scan_retries_without_adopting_a_partial_workspace(tmp_path, monkeypatch):
    from app.backend.pipeline.project_ide import workspace

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    original = tmp_path / "source"
    original.mkdir()
    (original / "main.py").write_text("answer = 42")

    def scan(chat, *_):
        item = workspace.load(Path(chat["workspace"]))
        return {"seen": workspace.read(item, "main.py")["text"], "changes": workspace.changes(item)}

    backend = service.Service(tmp_path / "managed", scan_handler=scan)
    try:
        project = backend.create_project("Fixture", str(original))
        chat = backend.create_scan_chat(project["id"])
        assert chat["workspace"] is None
        abandoned = backend.root / "workspaces" / chat["id"] / "files"
        abandoned.mkdir(parents=True)
        (abandoned / "main.py").write_text("partial snapshot")
        run = backend.submit_scan(chat["id"], "scan", {"scope": "project_code"})
        wait_for(lambda: backend.store.get("runs", run["id"])["state"] in {"completed", "failed"})
        saved = backend.store.get("runs", run["id"])
        assert saved["state"] == "completed", saved["result"]
        assert saved["result"] == {"seen": "answer = 42", "changes": []}
        assert backend.create_scan_chat(project["id"])["id"] == chat["id"]
        assert (abandoned / "main.py").read_text() == "partial snapshot"
    finally:
        backend.close()


def test_service_runs_one_native_session_per_chat_and_deduplicates_submission(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    original = tmp_path / "source"
    original.mkdir()
    (original / "main.py").write_text("original")
    NativeFixture.instances.clear()
    backend = service.Service(tmp_path / "managed", runtime_factory=NativeFixture, configure=lambda *a: {})
    try:
        project = backend.create_project("Fixture", str(original))
        chat = backend.create_chat(project["id"], "Inspect", {"model": "qwen", "kind": "local"})
        first = backend.submit(chat["id"], "once", "Read main.py")
        assert backend.submit(chat["id"], "once", "Read main.py")["id"] == first["id"]
        second = backend.submit(chat["id"], "second", "Explain the result")
        wait_for(lambda: backend.store.get("runs", second["id"])["state"] == "completed")
        assert len(NativeFixture.instances) == 1
        assert len(NativeFixture.instances[0].prompts) == 2
        assert backend.store.get("chats", chat["id"])["native_session"] == "ses_fixture"
        assert backend.detail(chat["id"])["messages"][-1]["parts"][0]["text"] == "Observed: Explain the result"
        assert (original / "main.py").read_text() == "original"
    finally:
        backend.close()


def test_model_configuration_change_between_turns_rebinds_native_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("original")
    NativeFixture.instances.clear()
    backend = service.Service(tmp_path / "managed", runtime_factory=NativeFixture, configure=lambda model, *args: {"selected": model["model"]})
    try:
        project = backend.create_project("Fixture", str(source))
        chat = backend.create_chat(project["id"], "Inspect", {"model": "first", "kind": "local"})
        first = backend.submit(chat["id"], "first", "Read")
        wait_for(lambda: backend.store.get("runs", first["id"])["state"] == "completed")
        backend.store.update_chat(chat["id"], model={"model": "second", "kind": "local"})
        second = backend.submit(chat["id"], "second", "Read again")
        wait_for(lambda: backend.store.get("runs", second["id"])["state"] == "completed")
        assert len(NativeFixture.instances) == 2
    finally:
        backend.close()


def test_uncertain_accepted_prompt_stops_native_and_never_replays_it(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    class Uncertain(NativeFixture):
        accepted = 0
        stopped = 0
        def request(self, method, path, body=None, **kwargs):
            if path.endswith("/prompt_async"):
                Uncertain.accepted += 1
                raise TimeoutError("Accepted but response was lost")
            return super().request(method, path, body, **kwargs)
        def stop(self):
            Uncertain.stopped += 1
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("original")
    backend = service.Service(tmp_path / "managed", runtime_factory=Uncertain, configure=lambda *a: {})
    try:
        project = backend.create_project("Fixture", str(source))
        chat = backend.create_chat(project["id"], "Inspect", {"model": "qwen", "kind": "local"})
        first = backend.submit(chat["id"], "uncertain", "Edit main.py")
        wait_for(lambda: backend.store.get("runs", first["id"])["state"] == "failed")
        assert Uncertain.accepted == 1 and Uncertain.stopped == 1
        assert chat["id"] not in backend._runtimes
        assert not backend.store.get("runs", first["id"])["result"]["automatic_retry"]
    finally:
        backend.close()


def test_acknowledged_cancellation_invalidates_pending_approval_before_native_poll(tmp_path):
    backend = service.Service(tmp_path / "managed")
    project = backend.store.project("Fixture", str(tmp_path / "source"))
    chat = backend.store.chat(project["id"], "Inspect", {"model": "qwen"})
    run = backend.store.submit(chat["id"], "request", "turn", {"text": "Test"})
    backend.store.claim(chat["id"])
    forwarded = []
    class Native:
        def request(self, *args):
            forwarded.append(args)
        def reject_commands(self, session):
            forwarded.append(("reject", session))
        def stop(self):
            pass
    backend._runtimes[chat["id"]] = (Native(), "session", {})
    backend._pending[chat["id"]] = {"run_id": run["id"], "permissions": [{"id": "appcmd_fixture"}], "questions": []}
    try:
        backend.cancel(chat["id"], run["id"])
        with pytest.raises(service.StoreError):
            backend.reply(chat["id"], "appcmd_fixture", approval="once")
        assert forwarded == [("reject", "session")]
    finally:
        backend.close()


@pytest.mark.parametrize("state", ["waiting_approval", "waiting_input"])
def test_waiting_phase_time_belongs_to_the_observed_wait_state(tmp_path, state):
    backend = service.Service(tmp_path / "managed")
    try:
        project = backend.store.project("Fixture", str(tmp_path / "source"))
        chat = backend.store.chat(project["id"], "Inspect", {"model": "qwen"})
        run = backend.store.submit(chat["id"], "request", "turn", {"text": "Test"})
        backend.store.claim(chat["id"])
        backend.store.event(chat["id"], "run.phase", {"phase": "generating"}, run["id"])
        with backend.store.connect() as connection:
            connection.execute("UPDATE events SET timestamp='2020-01-01T00:00:00.000+00:00' WHERE type='run.phase'")
        waiting = backend.store.transition(run["id"], state)
        detail = backend.detail(chat["id"])
        assert detail["activity"]["phase"] == state
        assert detail["activity"]["started_at"] == waiting["updated_at"]
        assert detail["runs"][0]["phase_started_at"] == waiting["updated_at"]
    finally:
        backend.close()


def test_cancelled_terminal_outcome_is_preserved_when_worker_read_was_stale(tmp_path, monkeypatch):
    backend = service.Service(tmp_path / "managed")
    try:
        project = backend.store.project("Fixture", str(tmp_path / "source"))
        chat = backend.store.chat(project["id"], "Inspect", {"model": "qwen"})
        run = backend.store.submit(chat["id"], "request", "turn", {"text": "Test"})
        original = backend.store.get
        changed = False
        def stale(table, identity):
            nonlocal changed
            row = original(table, identity)
            if table == "runs" and identity == run["id"] and not changed:
                changed = True
                backend.store.transition(identity, "interrupted", {"reason": "cancelled_before_execution"})
            return row
        monkeypatch.setattr(backend.store, "get", stale)
        backend._finish_error(run["id"], "interrupted", "The queue waiter noticed cancellation.")
        assert original("runs", run["id"])["result"] == {"reason": "cancelled_before_execution"}
    finally:
        backend.close()


def test_starting_a_runtime_is_active_work_with_durable_phase_in_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    configuring, resume = threading.Event(), threading.Event()
    def configure(*args):
        configuring.set()
        assert resume.wait(5)
        return {}
    source = tmp_path / "source"
    source.mkdir()
    backend = service.Service(tmp_path / "managed", runtime_factory=NativeFixture, configure=configure)
    try:
        project = backend.create_project("Fixture", str(source))
        chat = backend.create_chat(project["id"], "Inspect", {"model": "qwen", "kind": "local"})
        run = backend.submit(chat["id"], "start", "Read")
        assert configuring.wait(3)
        detail = backend.detail(chat["id"])
        assert detail["runs"][0]["state"] == "running"
        assert detail["activity"]["phase"] == "preparing_model"
        assert detail["activity"]["run_id"] == run["id"]
        assert detail["activity"]["started_at"]
        assert backend.store.events(chat["id"])[-1]["type"] == "run.phase"
        with pytest.raises(service.StoreError, match="between runs"):
            backend.store.update_chat(chat["id"], model={"model": "other"})
        resume.set()
        wait_for(lambda: backend.store.get("runs", run["id"])["state"] == "completed")
        assert backend.detail(chat["id"])["activity"] is None
    finally:
        resume.set()
        backend.close()


def test_resource_wait_stays_queued_and_never_starts_the_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    source = tmp_path / "source"
    source.mkdir()
    backend = service.Service(tmp_path / "managed", runtime_factory=NativeFixture, configure=lambda *a: {})
    try:
        project = backend.create_project("Fixture", str(source))
        chat = backend.create_chat(project["id"], "Inspect", {"model": "qwen", "kind": "local"})
        with backend.resources.acquire("local-gateway", threading.Event()):
            run = backend.submit(chat["id"], "wait", "Read")
            wait_for(lambda: any(e["type"] == "run.waiting_resource" for e in backend.store.events(chat["id"])))
            detail = backend.detail(chat["id"])
            assert detail["runs"][0]["state"] == "queued"
            assert detail["activity"]["phase"] == "waiting_resource"
            assert chat["id"] not in backend._runtimes
            backend.cancel(chat["id"], run["id"])
        wait_for(lambda: not backend._threads)
        assert backend.store.get("runs", run["id"])["state"] == "interrupted"
        assert backend.detail(chat["id"])["activity"] is None
    finally:
        backend.close()


def test_model_scan_uses_local_resource_in_a_hosted_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    source = tmp_path / "source"
    source.mkdir()
    backend = service.Service(tmp_path / "managed", runtime_factory=NativeFixture, configure=lambda *a: {})
    backend.scan_handler = lambda *_: {"scope": "model_review"}
    try:
        project = backend.create_project("Fixture", str(source))
        chat = backend.create_chat(project["id"], "Inspect", {"model": "hosted-model", "kind": "hosted", "endpoint": "https://example.com/v1"})
        with backend.resources.acquire("local-gateway", threading.Event()):
            run = backend.submit_scan(chat["id"], "model-scan", {"scope": "model_review"})
            wait_for(lambda: any(e["type"] == "run.waiting_resource" for e in backend.store.events(chat["id"])))
            assert backend.store.get("runs", run["id"])["state"] == "queued"

        wait_for(lambda: backend.store.get("runs", run["id"])["state"] == "completed")
        assert backend.store.get("runs", run["id"])["result"] == {"scope": "model_review"}
    finally:
        backend.close()


def test_model_switch_explicitly_overrides_sticky_native_model_without_losing_history(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    saved_messages = []
    class PersistentNative(NativeFixture):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.messages = saved_messages
            self.prompts = [{} for message in saved_messages if message["info"]["role"] == "user"]
        def request(self, method, path, body=None, **kwargs):
            if path.endswith("/prompt_async"):
                assert body.get("model") == {"providerID": "framework", "modelID": "second" if saved_messages else "first"}
            return super().request(method, path, body, **kwargs)
    source = tmp_path / "source"
    source.mkdir()
    backend = service.Service(tmp_path / "managed", runtime_factory=PersistentNative,
                              configure=lambda model, *a: {"model": "framework/" + model["model"]})
    try:
        project = backend.create_project("Fixture", str(source))
        chat = backend.create_chat(project["id"], "Inspect", {"model": "first", "kind": "local"})
        first = backend.submit(chat["id"], "first", "Remember this")
        wait_for(lambda: backend.store.get("runs", first["id"])["state"] in service.TERMINAL)
        assert backend.store.get("runs", first["id"])["state"] == "completed"
        session = backend.store.get("chats", chat["id"])["native_session"]
        backend.store.update_chat(chat["id"], model={"model": "second", "kind": "local"})
        second = backend.submit(chat["id"], "second", "Continue")
        wait_for(lambda: backend.store.get("runs", second["id"])["state"] in service.TERMINAL)
        assert backend.store.get("runs", second["id"])["state"] == "completed"
        assert backend.store.get("chats", chat["id"])["native_session"] == session
        assert [m["parts"][0]["text"] for m in backend.detail(chat["id"])["messages"] if m["info"]["role"] == "user"] == ["Remember this", "Continue"]
    finally:
        backend.close()


@pytest.mark.parametrize("action", ["close", "cancel"])
def test_stopping_work_stops_a_native_runtime_while_startup_is_still_pending(tmp_path, monkeypatch, action):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    started, stopped, resume = threading.Event(), threading.Event(), threading.Event()
    class StartingNative(NativeFixture):
        def start(self):
            started.set()
            assert resume.wait(5)
            return self
        def stop(self):
            stopped.set()
            resume.set()
    source = tmp_path / "source"
    source.mkdir()
    backend = service.Service(tmp_path / "managed", runtime_factory=StartingNative, configure=lambda *a: {})
    closing = None
    try:
        project = backend.create_project("Fixture", str(source))
        chat = backend.create_chat(project["id"], "Inspect", {"model": "qwen", "kind": "local"})
        run = backend.submit(chat["id"], "starting", "Read")
        assert started.wait(3)
        closing = threading.Thread(target=backend.close if action == "close" else lambda: backend.cancel(chat["id"], run["id"]))
        closing.start()
        assert stopped.wait(1)
        closing.join(3)
        assert not closing.is_alive()
        wait_for(lambda: backend.store.get("runs", run["id"])["state"] == "interrupted")
        assert backend.store.get("runs", run["id"])["state"] == "interrupted"
        assert backend.detail(chat["id"])["messages"] == []
    finally:
        resume.set()
        if closing:
            closing.join(6)
        backend.close()


def test_cancel_between_startup_phase_and_runtime_registration_prevents_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    spawned = []
    class StartingNative(NativeFixture):
        def start(self):
            spawned.append(True)
            return self
    source = tmp_path / "source"
    source.mkdir()
    backend = service.Service(tmp_path / "managed", runtime_factory=StartingNative, configure=lambda *a: {})
    original_phase = backend._phase
    def phase(chat_id, run_id, name):
        original_phase(chat_id, run_id, name)
        if name == "starting_runtime":
            backend.cancel(chat_id, run_id)
    monkeypatch.setattr(backend, "_phase", phase)
    try:
        project = backend.create_project("Fixture", str(source))
        chat = backend.create_chat(project["id"], "Inspect", {"model": "qwen", "kind": "local"})
        run = backend.submit(chat["id"], "cancel-before-registration", "Read")
        wait_for(lambda: backend.store.get("runs", run["id"])["state"] in service.TERMINAL)
        assert backend.store.get("runs", run["id"])["state"] == "interrupted"
        assert spawned == []
        assert backend.detail(chat["id"])["messages"] == []
    finally:
        backend.close()


class _Admission:
    def __init__(self, events):
        self.events = events

    def paused(self):
        from contextlib import contextmanager

        @contextmanager
        def pause():
            self.events.append("released")
            yield
            self.events.append("readmitted")
        return pause()


@pytest.mark.parametrize("wait", ["approval", "question"])
def test_waiting_for_the_user_releases_the_local_model_until_the_reply(tmp_path, monkeypatch, wait):
    # Review P2: an approval or planning wait must not hold the local model capacity or the turn lease.
    import asyncio
    from contextlib import contextmanager

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    original = tmp_path / "source"
    original.mkdir()
    (original / "main.py").write_text("answer = 42")
    events = []

    @contextmanager
    def admit_turn(model):
        events.append("admitted")
        yield _Admission(events)
        events.append("left")

    def code(chat, run, history, publish, cancelled, approve, phase, ask=None, **_):
        reply = asyncio.run(approve("Python unittest test_main.py") if wait == "approval" else ask({"question": "Which file?"}))
        return [{"type": "text", "text": f"reply {reply}"}]

    backend = service.Service(tmp_path / "managed", coding_turn=code, admit_turn=admit_turn)
    try:
        project = backend.create_project("Fixture", str(original))
        chat = backend.create_chat(project["id"], "New chat", {"model": "qwen", "kind": "local", "context": 1024}, defer_workspace=True)
        run = backend.submit(chat["id"], "first", "Run the tests")
        wait_for(lambda: bool(backend._pending.get(chat["id"])) and events == ["admitted", "released"])
        acquired = threading.Event()

        def other_chat():
            with backend.resources.acquire("local-gateway", threading.Event()):
                acquired.set()

        thread = threading.Thread(target=other_chat)
        thread.start()
        assert acquired.wait(2), "another chat could not use the local model during the wait"
        thread.join(2)
        pending = backend._pending[chat["id"]]
        if wait == "approval":
            backend.reply(chat["id"], pending["permissions"][0]["id"], approval="once")
        else:
            backend.reply(chat["id"], pending["questions"][0]["id"], answers=[["main.py"]])
        wait_for(lambda: backend.store.get("runs", run["id"])["state"] == "completed")
        assert events == ["admitted", "released", "readmitted", "left"]
    finally:
        backend.close()


def test_deleting_a_conversation_removes_its_worktree_and_refuses_unintegrated_changes(tmp_path, monkeypatch):
    # Review P6: deleting a conversation cleans up its worktree copy, Git registration and search cache, and
    # refuses while the workspace holds changes that were never integrated (unless they are discarded).
    import subprocess

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("answer = 42\n")
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "add", "main.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                    "commit", "-m", "initial"], check=True, capture_output=True)
    backend = service.Service(tmp_path / "managed")
    try:
        project = backend.create_project("Fixture", str(source))
        chat = backend.create_chat(project["id"], "First", {"model": "qwen", "kind": "local"})
        directory = Path(backend.store.get("chats", chat["id"])["workspace"])
        item = service.workspace.load(directory)
        assert item["kind"] == "worktree"
        from app.backend.pipeline.project_ide.project_grounding import conversation_search_scope
        cache = conversation_search_scope(backend, backend.store.get("chats", chat["id"])).cache_dir
        cache.mkdir(parents=True)
        (cache / "search-v1.sqlite3").write_bytes(b"index")

        def worktrees():
            return subprocess.run(["git", "-C", str(source), "worktree", "list", "--porcelain"],
                                  check=True, capture_output=True, text=True).stdout.count("worktree ")

        assert worktrees() == 2
        (item["root"] / "main.py").write_text("answer = 43\n")
        with pytest.raises(service.workspace.Conflict, match="1 change"):
            backend.delete_chat(chat["id"])
        assert backend.store.get("chats", chat["id"]) and directory.exists()

        backend.delete_chat(chat["id"], discard=True)
        assert not directory.exists() and not cache.exists()
        assert worktrees() == 1
        assert (source / "main.py").read_text() == "answer = 42\n"
        with pytest.raises(Exception):
            backend.store.get("chats", chat["id"])
    finally:
        backend.close()


@pytest.mark.parametrize("wait", ["approval", "question"])
def test_a_native_reply_reaches_the_runtime_only_after_the_turn_is_readmitted(tmp_path, monkeypatch, wait):
    # Review RR-1: forwarding the answer first lets the native runtime call the model while the turn holds no lease,
    # so an exclusive GPU job that started during the wait could see the model loaded again.
    from contextlib import contextmanager

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    source = tmp_path / "source"
    source.mkdir()
    events = []

    class AskingNative(NativeFixture):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.open = []

        def request(self, method, path, body=None, **kwargs):
            if path.endswith("/prompt_async"):
                self.prompts.append(body)
                self.messages.append({"info": {"role": "user", "id": "u1"}, "parts": body["parts"]})
                self.open = [{"id": "per_1" if wait == "approval" else "que_1", "sessionID": "ses_fixture"}]
                return None
            if path == "/permission":
                return self.open if wait == "approval" else []
            if path == "/question":
                return self.open if wait == "question" else []
            if path.endswith("/reply"):
                events.append("forwarded")
                self.open = []
                self.messages.append({"info": {"role": "assistant", "id": "a1", "finish": "stop", "time": {"completed": 1}},
                                      "parts": [{"type": "text", "text": "Done."}]})
                return True
            return super().request(method, path, body, **kwargs)

    @contextmanager
    def admit_turn(model):
        events.append("admitted")
        yield _Admission(events)
        events.append("left")

    backend = service.Service(tmp_path / "managed", runtime_factory=AskingNative, configure=lambda *a: {}, admit_turn=admit_turn)
    try:
        project = backend.create_project("Fixture", str(source))
        chat = backend.create_chat(project["id"], "Inspect", {"model": "qwen", "kind": "local"})
        run = backend.submit(chat["id"], "native-wait", "Run the tests")
        wait_for(lambda: "released" in events)
        if wait == "approval":
            backend.reply(chat["id"], "per_1", approval="once")
        else:
            backend.reply(chat["id"], "que_1", answers=[["main.py"]])
        wait_for(lambda: backend.store.get("runs", run["id"])["state"] in service.TERMINAL)
        assert backend.store.get("runs", run["id"])["state"] == "completed"
        assert events.index("readmitted") < events.index("forwarded"), events
    finally:
        backend.close()
