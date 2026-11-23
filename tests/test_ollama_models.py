"""Local GGUF registration against a fake Ollama HTTP server and CLI."""
import hashlib
import os
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from app.backend import config
from tests.fake_ollama import fake_ollama  # noqa: F401


@pytest.fixture
def gguf_file(tmp_path):
    path = tmp_path / "Example With Spaces.gguf"
    path.write_bytes(b"GGUF-fixture")
    return path


@pytest.fixture
def ollama_registrations(monkeypatch, fake_ollama):
    from app.backend.pipeline.ollama import models

    registered = {}

    def run(command, **options):
        program, operation, name, *args = command
        assert program == "fake-ollama"
        if operation == "create":
            flag, modelfile = args
            assert flag == "-f"
            registered[name] = Path(modelfile).read_text(encoding="utf-8")
            fake_ollama.models[name + ":latest"] = 123
        elif operation == "cp":
            destination, = args
            registered[destination] = registered[name]
            fake_ollama.models[destination + ":latest"] = fake_ollama.models[name + ":latest"]
        elif operation == "rm":
            assert not args
            del registered[name]
            del fake_ollama.models[name + ":latest"]
        else:
            pytest.fail(f"Unexpected CLI command: {command}")
        return subprocess.CompletedProcess(command, 0)

    register = models.register_variant
    monkeypatch.setattr(models, "executable", lambda: "fake-ollama")
    monkeypatch.setattr(models, "register_variant", lambda *args, **kwargs: register(*args, **kwargs, run=run))
    return registered


def test_name_is_stable_for_same_file(gguf_file):
    from app.backend.pipeline.ollama.models import name_for

    stat = gguf_file.stat()
    identity = (str(gguf_file.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino)
    digest = hashlib.sha256("|".join(map(str, identity)).encode()).hexdigest()[:10]
    assert name_for(gguf_file) == name_for(gguf_file.parent / "." / gguf_file.name) == "aivf/example-with-spaces-" + digest


def test_name_changes_when_file_changes(gguf_file):
    from app.backend.pipeline.ollama.models import name_for

    original = name_for(gguf_file)
    stamp = gguf_file.stat().st_mtime_ns
    gguf_file.write_bytes(b"GGUF-replace")
    os.utime(gguf_file, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))
    assert name_for(gguf_file) != original


@pytest.mark.parametrize("suffix", ["", ":latest"])
def test_register_skips_existing_name(gguf_file, fake_ollama, suffix):
    from app.backend.pipeline.ollama.models import name_for, register_gguf

    name = name_for(gguf_file)
    fake_ollama.models[name + suffix] = gguf_file.stat().st_size
    assert register_gguf(gguf_file, context=4096, run=lambda *_a, **_kw: pytest.fail("Ollama create was called")) == name


def test_modelfile_lines(gguf_file, monkeypatch, fake_ollama):
    from app.backend.pipeline.ollama import models

    adapter = gguf_file.with_name("An Adapter.gguf")
    adapter.write_bytes(b"GGUF-adapter")
    system = 'Handle "quoted" text.\nUse complete sentences.'
    name = models.name_for(gguf_file)
    monkeypatch.setattr(models, "executable", lambda: "fake-ollama")
    captured = []
    staged = []

    def run(command, **options):
        program, operation, target, *args = command
        assert program == "fake-ollama"
        if operation == "create":
            assert target.startswith("aivf/import-") and target != name
            staged.append(target)
            flag, modelfile_path = args
            assert flag == "-f"
            modelfile = Path(modelfile_path)
            captured.append(modelfile)
            assert modelfile.read_text(encoding="utf-8").splitlines() == [
                f'FROM "{gguf_file.resolve()}"',
                f'ADAPTER "{adapter.resolve()}"',
                "PARAMETER num_ctx 4096",
                'SYSTEM """Handle "quoted" text.',
                'Use complete sentences."""',
            ]
            fake_ollama.models[target + ":latest"] = 123
        elif operation == "cp":
            assert staged == [target] and args == [name]
            fake_ollama.models[name + ":latest"] = fake_ollama.models[target + ":latest"]
        elif operation == "rm":
            assert staged == [target] and not args
            del fake_ollama.models[target + ":latest"]
        else:
            pytest.fail(f"Unexpected CLI command: {command}")

    assert models.register_gguf(gguf_file, context=4096, adapter=adapter, system=system, run=run) == name
    assert len(captured) == 1 and not captured[0].exists()
    assert name + ":latest" in fake_ollama.models and staged[0] + ":latest" not in fake_ollama.models


def test_non_gguf_refused(gguf_file, fake_ollama):
    from app.backend.pipeline.ollama.models import register_gguf

    gguf_file.write_bytes(b"bad-format")
    with pytest.raises(ValueError, match="not GGUF"):
        register_gguf(gguf_file, context=4096, run=lambda *_a, **_kw: pytest.fail("Ollama create was called"))
    assert not fake_ollama.requests


def test_role_model_reads_the_configured_local_model(gguf_file, monkeypatch):
    from app.backend.pipeline.ollama.models import role_model

    settings = SimpleNamespace(llm_engine="local", llm_local_model=str(gguf_file), llm_local_context="6144",
                               embed_engine="local", embed_local_model=str(gguf_file))
    monkeypatch.setattr(config.Settings, "load", lambda: settings)
    assert role_model("llm") == (gguf_file, 6144)
    assert role_model("embed") == (gguf_file, 2048)
    settings.llm_engine = "external"
    assert role_model("llm") is None
    assert role_model("unknown") is None


def test_variant_context_and_prompt_are_part_of_the_registration(gguf_file, ollama_registrations):
    from app.backend.pipeline.ollama.models import register_variant

    first = register_variant(gguf_file, context=8192)
    second = register_variant(gguf_file, context=4096)
    third = register_variant(gguf_file, context=4096, system="Use complete sentences.")
    assert len({first, second, third}) == 3
    assert "PARAMETER num_ctx 8192" in ollama_registrations[first]
    assert "PARAMETER num_ctx 4096" in ollama_registrations[second]
    assert 'SYSTEM "Use complete sentences."' in ollama_registrations[third]


@pytest.mark.parametrize("changed_source", ["base", "adapter"])
def test_replaced_source_during_import_never_publishes_final_tag(gguf_file, monkeypatch, fake_ollama, changed_source):
    from app.backend.pipeline.ollama import models
    from app.backend.pipeline.ollama.client import OllamaError

    adapter = gguf_file.with_name("adapter.gguf")
    adapter.write_bytes(b"GGUF-adapter")
    source = gguf_file if changed_source == "base" else adapter
    final_name = models.name_for_variant(gguf_file, context=4096, adapter=adapter)
    unrelated = dict(fake_ollama.models)
    monkeypatch.setattr(models, "executable", lambda: "fake-ollama")
    created = []
    removed = []

    def run(command, **options):
        _, operation, name, *_ = command
        if operation == "create":
            created.append(name)
            fake_ollama.models[name + ":latest"] = 123
            replacement = source.with_suffix(".replacement")
            replacement.write_bytes(b"GGUF-changed")
            stamp = source.stat().st_mtime_ns
            os.utime(replacement, ns=(stamp, stamp))
            replacement.replace(source)
        elif operation == "rm":
            removed.append(name)
            del fake_ollama.models[name + ":latest"]
        else:
            pytest.fail(f"Unexpected promotion after source changed: {command}")
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(OllamaError, match="changed"):
        models.register_variant(gguf_file, context=4096, adapter=adapter, run=run)
    assert len(created) == 1 and created == removed and created[0] != final_name
    assert fake_ollama.models == unrelated


def test_source_change_during_tag_lookup_preserves_preexisting_tag(gguf_file, monkeypatch, fake_ollama):
    from app.backend.pipeline.ollama import models
    from app.backend.pipeline.ollama.client import OllamaError

    name = models.name_for(gguf_file)
    fake_ollama.models[name + ":latest"] = 123
    original_answer = fake_ollama.answer

    def answer(method, path, body):
        if path == "/api/tags":
            gguf_file.write_bytes(b"GGUF-a-later-version")
        return original_answer(method, path, body)

    monkeypatch.setattr(fake_ollama, "answer", answer)
    with pytest.raises(OllamaError, match="changed"):
        models.register_gguf(gguf_file, context=4096, run=lambda *_a, **_kw: pytest.fail("CLI was called"))
    assert fake_ollama.models[name + ":latest"] == 123


def test_retry_after_replacement_during_promotion_uses_a_different_tag(gguf_file, monkeypatch, fake_ollama):
    from app.backend.pipeline.ollama import models
    from app.backend.pipeline.ollama.client import OllamaError

    first = models.name_for_variant(gguf_file, context=4096)
    monkeypatch.setattr(models, "executable", lambda: "fake-ollama")

    def run(command, **options):
        _, operation, name, *args = command
        if operation == "create":
            fake_ollama.models[name + ":latest"] = 123
        elif operation == "cp":
            destination, = args
            fake_ollama.models[destination + ":latest"] = 123
            if destination == first:
                replacement = gguf_file.with_suffix(".replacement")
                replacement.write_bytes(b"GGUF-changed")
                stamp = gguf_file.stat().st_mtime_ns
                os.utime(replacement, ns=(stamp, stamp))
                replacement.replace(gguf_file)
        elif operation == "rm":
            del fake_ollama.models[name + ":latest"]
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(OllamaError, match="changed"):
        models.register_variant(gguf_file, context=4096, run=run)

    second = models.register_variant(gguf_file, context=4096, run=run)
    assert first != second
    assert {first + ":latest", second + ":latest"} <= fake_ollama.models.keys()


def test_registration_cleans_up_owned_staging_after_deadline(gguf_file, monkeypatch, fake_ollama):
    from app.backend.pipeline.ollama import models
    from app.backend.pipeline.ollama.client import OllamaError

    created, cleaned = threading.Event(), threading.Event()
    answer_original = fake_ollama.answer
    monkeypatch.setattr(models, "executable", lambda: "fake-ollama")

    def answer(method, path, body):
        if path == "/api/tags" and created.is_set():
            time.sleep(0.65)
        return answer_original(method, path, body)

    def run(command, **options):
        _, operation, name, *_ = command
        assert name.startswith("aivf/import-")
        if operation == "create":
            fake_ollama.models[name + ":latest"] = 123
            created.set()
        elif operation == "rm":
            del fake_ollama.models[name + ":latest"]
            cleaned.set()
        else:
            pytest.fail(f"Unexpected CLI command: {command}")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(fake_ollama, "answer", answer)
    with pytest.raises(OllamaError) as failure:
        models.register_variant(gguf_file, context=4096, timeout=0.20, run=run)
    assert failure.value.code == "timeout"
    assert cleaned.wait(3)
    assert all(not name.startswith("aivf/import-") for name in fake_ollama.models)


def test_cleanup_retries_until_a_late_staging_tag_is_visible(gguf_file, monkeypatch, fake_ollama):
    from app.backend.pipeline.ollama import models
    from app.backend.pipeline.ollama.client import OllamaError

    cleaned = threading.Event()
    answer_original = fake_ollama.answer
    staged = []
    monkeypatch.setattr(models, "executable", lambda: "fake-ollama")

    def answer(method, path, body):
        result = answer_original(method, path, body)
        if path == "/api/tags" and staged and not cleaned.is_set():
            fake_ollama.models[staged[0] + ":latest"] = 123
        return result

    def run(command, **options):
        _, operation, name, *_ = command
        if operation == "create":
            staged.append(name)
            raise subprocess.TimeoutExpired(command, options["timeout"])
        assert operation == "rm" and name == staged[0]
        del fake_ollama.models[name + ":latest"]
        cleaned.set()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(fake_ollama, "answer", answer)
    with pytest.raises(OllamaError) as failure:
        models.register_gguf(gguf_file, context=4096, timeout=10.0, run=run)
    assert failure.value.code == "timeout"
    assert cleaned.wait(3)
