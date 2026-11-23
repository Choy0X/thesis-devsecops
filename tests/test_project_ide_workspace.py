"""Independent chat files, optimistic saves, and reviewed conflict-safe integration."""
import subprocess
from pathlib import Path
import threading

import pytest

from app.backend.pipeline.project_ide import workspace
from app.backend.pipeline.project_ide.resources import Cancelled


@pytest.fixture(autouse=True)
def isolated_git_discovery(tmp_path, monkeypatch):
    # Test scratch lives inside this checkout. It is not itself the user's repository.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def source(tmp_path):
    root = tmp_path / "original"
    root.mkdir()
    (root / "main.py").write_bytes(b"original\r\n")
    (root / ".env").write_text("SECRET=excluded")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "package.js").write_text("dependency")
    return root


def test_non_git_chat_copies_are_independent_and_preserve_source(tmp_path):
    original = source(tmp_path)
    first = workspace.provision(original, tmp_path / "managed" / "first")
    second = workspace.provision(original, tmp_path / "managed" / "second")
    assert first["kind"] == "copy"
    assert Path(subprocess.check_output(["git", "-C", str(first["root"]), "rev-parse", "--show-toplevel"], text=True).strip()).resolve() == first["root"].resolve()
    assert not (first["root"] / ".env").exists()
    assert not (first["root"] / "node_modules").exists()
    assert (first["root"] / "main.py").read_bytes() == b"original\r\n"
    current = workspace.read(first, "main.py")
    workspace.save(first, "main.py", "changed\n", current["version"])
    assert (second["root"] / "main.py").read_bytes() == b"original\r\n"
    assert (original / "main.py").read_bytes() == b"original\r\n"
    with pytest.raises(workspace.Conflict):
        workspace.save(first, "main.py", "stale edit", current["version"])


def test_paths_and_dependency_context_cannot_escape_workspace(tmp_path):
    item = workspace.provision(source(tmp_path), tmp_path / "managed")
    for path in ("../original/main.py", ".env", "node_modules/package.js", "C:/Windows/win.ini"):
        with pytest.raises(workspace.WorkspaceError):
            workspace.read(item, path)


def test_git_workspace_includes_dirty_and_untracked_files_without_source_commit(tmp_path):
    original = source(tmp_path)
    subprocess.run(["git", "init", str(original)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(original), "add", "main.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(original), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "initial"], check=True, capture_output=True)
    initial = subprocess.check_output(["git", "-C", str(original), "rev-parse", "HEAD"])
    (original / "main.py").write_text("dirty")
    (original / "new.py").write_text("untracked")
    (original / ".gitignore").write_text("local-download.bin\n")
    (original / "local-download.bin").write_bytes(b"ignored generated artifact")
    item = workspace.provision(original, tmp_path / "managed")
    assert item["kind"] == "worktree"
    assert (item["root"] / "main.py").read_text() == "dirty"
    assert (item["root"] / "new.py").read_text() == "untracked"
    assert not (item["root"] / "local-download.bin").exists()
    assert subprocess.check_output(["git", "-C", str(original), "rev-parse", "HEAD"]) == initial
    assert (original / "main.py").read_text() == "dirty"


def test_cancelled_snapshot_removes_partial_git_worktree_and_can_retry(tmp_path, monkeypatch):
    original = source(tmp_path)
    workspace._git(original, "init")
    workspace._git(original, "add", "main.py")
    workspace._git(original, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "initial")
    destination = tmp_path / "managed"
    cancel = threading.Event()
    copy = workspace.shutil.copy2

    def cancel_after_copy(*args, **kwargs):
        result = copy(*args, **kwargs)
        cancel.set()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(workspace.shutil, "copy2", cancel_after_copy)
        with pytest.raises(Cancelled, match="Workspace preparation was cancelled"):
            workspace.provision(original, destination, cancelled=cancel)
    assert not destination.exists()
    assert workspace._git(original, "worktree", "list", "--porcelain").stdout.count("worktree ") == 1
    assert (original / "main.py").read_bytes() == b"original\r\n"
    item = workspace.provision(original, destination)
    assert (item["root"] / "main.py").read_bytes() == b"original\r\n"
    assert workspace.changes(item) == []


def test_checkout_views_are_scoped_to_the_project_and_split_git_changes(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    project = repository / "service"
    project.mkdir()
    tracked = project / "worker.py"
    tracked.write_text("value = 1\n")
    workspace._git(repository, "init", "-b", "maintenance")
    workspace._git(repository, "add", ".")
    workspace._git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "initial")
    tracked.write_text("value = 2\n")
    workspace._git(repository, "add", "service/worker.py")
    tracked.write_text("value = 3\n")
    (project / "tests.py").write_text("assert True\n")
    (project / ".env").write_text("PRIVATE=excluded\n")
    (repository / "outside.py").write_text("outside scope\n")

    listing = workspace.checkout_changes(project)
    assert listing["branch"] == "maintenance"
    assert listing["files"] == [{"path": "tests.py", "status": "??"}, {"path": "worker.py", "status": "MM"}]
    changes = workspace.checkout_diff(project, "worker.py")["diffs"]
    assert [item["kind"] for item in changes] == ["staged", "unstaged"]
    assert "-value = 1\n+value = 2" in changes[0]["diff"]
    assert "-value = 2\n+value = 3" in changes[1]["diff"]
    assert "+assert True" in workspace.checkout_diff(project, "tests.py")["diffs"][0]["diff"]
    for path in ("../outside.py", ".env"):
        with pytest.raises(workspace.WorkspaceError):
            workspace.checkout_diff(project, path)
    assert workspace._git(repository, "show", ":service/worker.py").stdout == "value = 2\n"
    workspace._git(repository, "switch", "-c", "release")
    assert workspace.checkout_changes(project)["branch"] == "release"


def test_checkout_reports_non_git_and_unborn_branch(tmp_path):
    original = source(tmp_path)
    assert workspace.checkout_changes(original)["available"] is False
    workspace._git(original, "init", "-b", "new-branch")
    workspace._git(original, "add", "main.py")
    listing = workspace.checkout_changes(original)
    assert listing["branch"] == "new-branch"
    assert listing["revision"] is None
    assert workspace.checkout_diff(original, "main.py")["diffs"][0]["kind"] == "staged"


def test_integration_refuses_source_drift_and_applies_only_reviewed_paths(tmp_path):
    original = source(tmp_path)
    item = workspace.provision(original, tmp_path / "managed")
    (item["root"] / "main.py").write_text("agent edit")
    (item["root"] / "new.py").write_text("new file")
    assert {row["path"] for row in workspace.changes(item)} == {"main.py", "new.py"}
    (original / "main.py").write_text("someone else")
    with pytest.raises(workspace.Conflict):
        workspace.integrate(item, ["main.py", "new.py"])
    assert not (original / "new.py").exists()
    (original / "main.py").write_bytes(b"original\r\n")
    workspace.integrate(item, ["main.py"])
    assert (original / "main.py").read_text() == "agent edit"
    assert not (original / "new.py").exists()


def test_failed_integration_restores_files_already_written(tmp_path, monkeypatch):
    original = source(tmp_path)
    item = workspace.provision(original, tmp_path / "managed")
    (item["root"] / "main.py").write_text("agent edit")
    (item["root"] / "new.py").write_text("new file")
    original_write = workspace._write
    def failing(path, content):
        if path == original / "new.py":
            raise OSError("fixture disk failure")
        return original_write(path, content)
    monkeypatch.setattr(workspace, "_write", failing)
    with pytest.raises(OSError, match="disk failure"):
        workspace.integrate(item, ["main.py", "new.py"])
    assert (original / "main.py").read_bytes() == b"original\r\n"
    assert not (original / "new.py").exists()


def test_reviewed_bytes_checked_under_lock_and_repeated_integration_uses_separate_cursor(tmp_path):
    original = source(tmp_path)
    item = workspace.provision(original, tmp_path / "managed")
    initial = dict(item["baseline"])
    (item["root"] / "main.py").write_text("first edit")
    reviewed = {row["path"]: row["after"] for row in workspace.changes(item)}
    (item["root"] / "main.py").write_text("intervening edit")
    with pytest.raises(workspace.Conflict, match="reviewed"):
        workspace.integrate(item, ["main.py"], reviewed)
    reviewed = {row["path"]: row["after"] for row in workspace.changes(item)}
    workspace.integrate(item, ["main.py"], reviewed)
    assert workspace.changes(item) == []
    assert item["baseline"] == initial
    assert (item["directory"] / "baseline" / "main.py").read_bytes() == b"original\r\n"
    (item["root"] / "main.py").write_text("second edit")
    reviewed = {row["path"]: row["after"] for row in workspace.changes(item)}
    workspace.integrate(item, ["main.py"], reviewed)
    assert workspace.changes(workspace.load(item["directory"])) == []
    assert (original / "main.py").read_text() == "second edit"
    (item["root"] / "added.py").write_text("new content")
    workspace.integrate(item, ["added.py"])
    (item["root"] / "added.py").unlink()
    assert workspace.changes(item)[0]["status"] == "deleted"
    workspace.integrate(item, ["added.py"])
    assert not (original / "added.py").exists() and not workspace.changes(item)


def test_repository_configured_commands_never_run_on_the_host(tmp_path):
    # Review P1: core.fsmonitor and hooks configured by the project repository run on every Git call that reads the
    # index or updates a ref. The app's own Git calls disable both.
    original = source(tmp_path)
    marker = tmp_path / "ran.txt"
    script = tmp_path / "planted.sh"
    script.write_text(f"#!/bin/sh\necho \"$0 $*\" >> '{marker.as_posix()}'\nexit 0\n", newline="\n")
    script.chmod(0o755)
    hooks = tmp_path / "planted-hooks"
    hooks.mkdir()
    for name in ("reference-transaction", "post-checkout", "post-index-change"):
        (hooks / name).write_text(script.read_text(), newline="\n")
        (hooks / name).chmod(0o755)
    subprocess.run(["git", "init", str(original)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(original), "add", "main.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(original), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                    "commit", "-m", "initial"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(original), "config", "core.fsmonitor", script.as_posix()], check=True)
    subprocess.run(["git", "-C", str(original), "config", "core.hooksPath", hooks.as_posix()], check=True)
    (original / "main.py").write_text("dirty")

    assert "main.py" in workspace.files(original)
    item = workspace.provision(original, tmp_path / "managed")
    workspace.checkout_view(original) if hasattr(workspace, "checkout_view") else workspace._checkout(original)

    assert (item["root"] / "main.py").read_text() == "dirty"
    assert not marker.exists(), marker.read_text()
