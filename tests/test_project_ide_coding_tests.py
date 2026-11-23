"""Approved Python tests: what the approval shows, what is pinned, and how the test process starts (review P3)."""
import asyncio
import threading

import pytest
from pydantic_ai import ModelRetry

from app.backend.pipeline.project_ide import coding_runtime, workspace


@pytest.fixture
def item(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    source = tmp_path / "source"
    source.mkdir()
    (source / "module.py").write_text("value = 1\n", encoding="utf-8")
    (source / "helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (source / "test_module.py").write_text(
        "import unittest\nfrom module import value\n"
        "class ValueTests(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(value, 2)\n",
        encoding="utf-8")
    return workspace.provision(source, tmp_path / "managed")


def tools_for(item, approve):
    return coding_runtime.CodingTools(item, None, threading.Event(), approve, None, [], [], None)


def test_the_approval_says_where_the_test_runs_and_lists_the_python_files_edited_in_this_turn(item):
    labels = []

    async def approve(label):
        labels.append(label)
        return False

    tools = tools_for(item, approve)
    tools.edit_file("module.py", "value = 1", "value = 2")
    tools.create_file("extra.py", "EXTRA = True\n")

    assert asyncio.run(tools.run_tests("test_module.py"))["status"] == "denied"
    label, = labels
    assert "Python unittest test_module.py" in label
    assert "this computer" in label and "network access" in label
    assert "extra.py" in label and "module.py" in label and "helper.py" not in label


def test_the_approval_says_when_no_python_file_was_edited(item):
    labels = []

    async def approve(label):
        labels.append(label)
        return False

    asyncio.run(tools_for(item, approve).run_tests("test_module.py"))
    assert "No Python file was created or edited in this turn." in labels[0]


def test_a_python_file_changed_after_approval_refuses_the_run(item, monkeypatch):
    monkeypatch.setattr(coding_runtime, "_run_python_test", lambda *args: pytest.fail("The changed worktree was run"))

    async def approve(label):
        (item["root"] / "helper.py").write_text("import os\n", encoding="utf-8")
        return True

    with pytest.raises(ModelRetry, match="helper.py changed after approval"):
        asyncio.run(tools_for(item, approve).run_tests("test_module.py"))


def test_too_many_edited_files_to_show_are_refused(item, monkeypatch):
    monkeypatch.setattr(coding_runtime, "APPROVAL_FILE_LIMIT", 2)

    async def approve(label):
        pytest.fail("Approval was requested without showing every edited Python file")

    tools = tools_for(item, approve)
    for index in range(3):
        tools.create_file(f"new_{index}.py", "X = 1\n")
    with pytest.raises(ModelRetry, match="3 Python files"):
        asyncio.run(tools.run_tests("test_module.py"))


@pytest.fixture
def ignored_item(tmp_path, monkeypatch):
    """A Git project whose .gitignore hides a folder, so `git ls-files` does not list Python files inside it."""
    import subprocess
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    source = tmp_path / "source"
    source.mkdir()
    (source / ".gitignore").write_text("generated/\n", encoding="utf-8")
    (source / "test_module.py").write_text("import unittest\n", encoding="utf-8")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.hooksPath=", "-C", str(source)]
    subprocess.run(git[:-2] + ["init", "-q", str(source)], check=True)
    subprocess.run(git + ["add", ".gitignore", "test_module.py"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "init"], check=True)
    return workspace.provision(source, tmp_path / "managed")


def test_an_edited_python_file_in_an_ignored_folder_is_listed_in_the_approval(ignored_item):
    labels = []

    async def approve(label):
        labels.append(label)
        return False

    tools = tools_for(ignored_item, approve)
    tools.create_file("generated/hidden.py", "HIDDEN = True\n")
    assert "generated/hidden.py" not in workspace.files(ignored_item["root"])

    asyncio.run(tools.run_tests("test_module.py"))
    assert "generated/hidden.py" in labels[0]


def test_an_edited_python_file_in_an_ignored_folder_is_pinned(ignored_item, monkeypatch):
    monkeypatch.setattr(coding_runtime, "_run_python_test", lambda *args: pytest.fail("The changed worktree was run"))
    tools = None

    async def approve(label):
        (ignored_item["root"] / "generated" / "hidden.py").write_text("import os\n", encoding="utf-8")
        return True

    tools = tools_for(ignored_item, approve)
    tools.create_file("generated/hidden.py", "HIDDEN = True\n")
    with pytest.raises(ModelRetry, match="generated/hidden.py changed after approval"):
        asyncio.run(tools.run_tests("test_module.py"))


def test_a_later_turn_still_lists_python_files_the_assistant_wrote_earlier(ignored_item):
    # Security review of RR-2: a file written below an ignored folder in one turn must not become invisible in the next.
    async def deny(label):
        return False

    first = tools_for(ignored_item, deny)
    first.create_file("generated/hidden.py", "HIDDEN = True\n")

    labels = []

    async def approve(label):
        labels.append(label)
        return False

    asyncio.run(tools_for(ignored_item, approve).run_tests("test_module.py"))
    assert "earlier turns: generated/hidden.py." in labels[0]


def test_an_edited_file_is_recorded_under_its_name_on_disk(ignored_item):
    labels = []

    async def approve(label):
        labels.append(label)
        return False

    tools = tools_for(ignored_item, approve)
    tools.create_file("generated/./hidden.py", "HIDDEN = True\n")
    asyncio.run(tools.run_tests("test_module.py"))
    assert "in this turn: generated/hidden.py." in labels[0]
    assert tools.edited == {"generated/hidden.py"}


def test_an_ignored_python_file_changed_after_approval_refuses_the_run(ignored_item, monkeypatch):
    monkeypatch.setattr(coding_runtime, "_run_python_test", lambda *args: pytest.fail("The changed worktree was run"))
    folder = ignored_item["root"] / "generated"
    folder.mkdir()
    (folder / "existing.py").write_text("VALUE = 1\n", encoding="utf-8")

    async def approve(label):
        (folder / "existing.py").write_text("import os\n", encoding="utf-8")
        return True

    with pytest.raises(ModelRetry, match="generated/existing.py changed after approval"):
        asyncio.run(tools_for(ignored_item, approve).run_tests("test_module.py"))


def test_worktree_modules_cannot_shadow_the_standard_library(item, tmp_path):
    root = item["root"]
    (root / "module.py").write_text("value = 2\n", encoding="utf-8")
    (root / "unittest.py").write_text("raise SystemExit('the worktree replaced unittest')\n", encoding="utf-8")
    (root / "sitecustomize.py").write_text("raise SystemExit('the worktree ran sitecustomize')\n", encoding="utf-8")

    result = coding_runtime._run_python_test(root, "test_module.py", tmp_path / "private", threading.Event())

    assert result["exit_code"] == 0, result["output"]
    assert "OK" in result["output"]
