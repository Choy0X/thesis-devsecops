"""Shared pytest fixtures and marker gating.

Tests never touch the developer's real data, settings or secrets:

* ``workspace`` moves the working directory to a temporary folder with the
  repository's relative data/experiment layout (the pipeline uses relative paths).
* An autouse fixture removes every secret and endpoint from ``os.environ`` and
  redirects ``config.SETTINGS_PATH``, because importing ``app.backend.config``
  overlays the real ``settings.json`` onto the environment.
* Tests marked ``network``, ``tokenizer``, ``slow``, ``docker`` or ``gpu`` are
  skipped unless the matching capability or opt-in variable is present.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

collect_ignore_glob = ["fixtures/*"]   # fixture projects (tools/agent_bench.py's) hold files named test_*.py that are data, not tests of this repository

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The slow-route cache would show a test the answer of an earlier test; it is off for the whole run.
os.environ["ZTSA_SLOW_CACHE"] = "0"
# The tests use the repository's relative layout in a temporary folder, whatever database folder the developer saved in the Settings page.
os.environ["ZTSA_INTERIM_DIR"] = "data/interim"

# Import once so the import-time environment overlay happens here, before the
# autouse fixture scrubs it for every test.
from app.backend import config as _config  # noqa: E402

# The real settings.json holds the developer's keys and endpoints. From here on, whatever a test does (including a monkeypatch
# that is undone inside the test), the settings location is a throwaway file, never the real one.
import tempfile  # noqa: E402

REAL_SETTINGS_PATH = _config.SETTINGS_PATH
_config.SETTINGS_PATH = pathlib.Path(tempfile.mkdtemp(prefix="aivdf-tests-")) / "settings.json"
# A process a test starts (the backend of the supervisor tests) imports the configuration afresh, so the variable carries the throwaway location to it. Without it that
# process read and rewrote the real settings.json (on 2026-10-09 its start applied a settings migration to the real file).
os.environ["ZTSA_SETTINGS_PATH"] = str(_config.SETTINGS_PATH)
# Every Ollama test starts at an unreachable loopback address until it installs a fake.
os.environ["OLLAMA_HOST"] = "127.0.0.1:0"
os.environ["OLLAMA_MODELS"] = str(_config.SETTINGS_PATH.with_name("ollama"))
# The models folder is an empty one inside the test workspace: what a test finds there never depends on what is downloaded on this computer.
os.environ["ZTSA_MODELS_DIR"] = str(_config.SETTINGS_PATH.with_name("models"))
os.environ["ZTSA_DATABASE_DIR"] = str(_config.SETTINGS_PATH.with_name("database"))
os.environ["ZTSA_DATABASE_NAME"] = "aivf_test_session"
_config.pin_hub_cache()                                                  # and the Hugging Face cache is the ``hub`` folder inside it


def _real_settings_state() -> dict:
    """What the real settings files are now: the bytes of each, or None when it does not exist."""
    out = {}
    for path in (REAL_SETTINGS_PATH, REAL_SETTINGS_PATH.with_name("settings_migrations.json")):
        try:
            out[path.name] = path.read_bytes()
        except OSError:
            out[path.name] = None
    return out


_REAL_SETTINGS_BEFORE = _real_settings_state()


def pytest_sessionfinish(session, exitstatus):
    """A run that changed the developer's real settings fails, whatever its tests said, and says which file."""
    changed = [name for name, before in _REAL_SETTINGS_BEFORE.items() if _real_settings_state()[name] != before]
    if changed:
        sys.stderr.write("\nERROR: this test run changed the real settings file(s) " + ", ".join(changed) + "; a test reached them (see tests/conftest.py)\n")
        session.exitstatus = 1

SECRET_ENV = (
    "GITHUB_TOKEN", "NVD_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
    "EMBED_API_KEY", "EMBED_BASE_URL", "EMBED_MODEL", "SMTP_HOST", "SMTP_PORT",
    "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO", "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN", "MONGO_URI", "MONGO_DB", "MONGO_CONTAINER", "ZTSA_MONGO_SEARCH",
)


# set on purpose at the top of this file for the whole run, so the scrub leaves them
KEEP_ENV = {"ZTSA_INTERIM_DIR", "ZTSA_SETTINGS_PATH", "ZTSA_SLOW_CACHE", "ZTSA_MODELS_DIR", "ZTSA_DATABASE_DIR", "ZTSA_DATABASE_NAME"}


@pytest.fixture(autouse=True)
def isolate_sandbox_runtime(tmp_path, monkeypatch):
    # Ownership/watchdog tests must never adopt the developer's Docker resources.
    from app.backend import sandbox_runtime
    monkeypatch.setattr(sandbox_runtime, 'state_path', lambda: tmp_path / 'sandbox_runtime.json')
    monkeypatch.setenv('ZTSA_SANDBOX_WATCHDOG', '0')
    sandbox_runtime.set_stopping(False)
    yield
    sandbox_runtime.set_stopping(False)


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env(timeout=3).ping()
        return True
    except Exception:
        return False


def _cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _mongo_available() -> bool:
    try:
        from pymongo import MongoClient

        MongoClient("mongodb://localhost:27017/?directConnection=true", serverSelectionTimeoutMS=2000).admin.command("ping")
        return True
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    gates = {
        "network": (os.environ.get("ZTSA_TEST_NETWORK") == "1",
                    "set ZTSA_TEST_NETWORK=1 to run network tests"),
        "tokenizer": (os.environ.get("ZTSA_TEST_TOKENIZER") == "1",
                      "set ZTSA_TEST_TOKENIZER=1 to run pinned-tokenizer tests"),
        "slow": (os.environ.get("ZTSA_TEST_SLOW") == "1",
                 "set ZTSA_TEST_SLOW=1 to run slow tests"),
    }
    lazy = {"docker": (_docker_available, "no Docker daemon"),
            "gpu": (_cuda_available, "no CUDA device"),
            "mongo_migration": (_mongo_available, "optional legacy Mongo migration source unavailable")}
    cache: dict[str, bool] = {}
    for item in items:
        for name, (enabled, why) in gates.items():
            if name in item.keywords and not enabled:
                item.add_marker(pytest.mark.skip(reason=why))
        for name, (probe, why) in lazy.items():
            if name in item.keywords:
                if name not in cache:
                    cache[name] = probe()
                if not cache[name]:
                    item.add_marker(pytest.mark.skip(reason=why))


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch, tmp_path_factory):
    from app.backend import lifecycle
    from app.backend.pipeline.ollama import server
    monkeypatch.setattr(lifecycle, "_state", {"running": False, "done": False, "started": None,
                                             "steps": [], "deadline": lifecycle.DEADLINE_SECONDS})
    monkeypatch.setattr(server, "executable", lambda: None)
    monkeypatch.setattr(server, "start_in_background", lambda: None)
    for name in SECRET_ENV:
        monkeypatch.delenv(name, raising=False)
    # every other setting that the real settings.json overlays onto the environment (the provider, the retrieval choices, the compute defaults ...) is the developer's, not the test's:
    # on 2026-10-10 a test that expected no provider failed because the real settings said "managed"
    for name in _config.ENV_MAP.values():
        if name not in KEEP_ENV:
            monkeypatch.delenv(name, raising=False)
    # a throwaway settings location so no test can read or write the real file
    fake = tmp_path_factory.mktemp("settings") / "settings.json"
    monkeypatch.setattr(_config, "SETTINGS_PATH", fake)
    # the folders of GGUF conversion (scripts, converted models and adapters, fine-tuning runs) are the real ones unless a test is given others: none may read them
    from app.backend.pipeline import gguf_convert
    nothing = tmp_path_factory.mktemp("conversion")
    for name in ("TOOLS", "CONVERTED", "ADAPTERS", "RUNS"):
        monkeypatch.setattr(gguf_convert, name, nothing / name.lower())
    from app.backend.pipeline import models_local                 # and Ollama's folder, which is also read, never written
    monkeypatch.setattr(models_local, "OLLAMA_ROOT", nothing / "ollama")
    from app.backend.pipeline import model_removal                # and Ollama's programs: deleting a model can stop them, and no test may stop a real one (only the fake ones a test makes)
    real_stop = model_removal.stop_processes

    def stop_only_fakes(procs, *args, **kwargs):
        import psutil
        assert not any(isinstance(p, psutil.Process) for p in procs), "a test tried to end a real program; give it fake processes"
        return real_stop(procs, *args, **kwargs)
    monkeypatch.setattr(model_removal, "stop_processes", stop_only_fakes)
    from app.backend import sandbox_history, sandbox_images       # what a sandbox container did, and the pinned verification image: files under data/sandbox
    monkeypatch.setattr(sandbox_history, "ROOT", nothing / "sandbox_history")
    monkeypatch.setattr(sandbox_images, "PINNED", nothing / "pinned_image.json")
    yield


@pytest.fixture(autouse=True)
def _fresh_agent_store():
    """Agent sessions are kept in a store chosen on first use; each test gets a new one (an in-memory store would otherwise carry sessions from one test to the next)."""
    from app.backend.pipeline import agent_sessions
    agent_sessions.use_store(None)
    yield
    agent_sessions.use_store(None)


@pytest.fixture(autouse=True)
def _fresh_sandbox_terminal_caches():
    """What the agent's sandbox terminal learned of a container (its folders made, the programs in it) is kept by container id; the fake containers of different tests have the same ids."""
    from app.backend.pipeline.agentic.exec import sandbox_exec
    sandbox_exec.reset_caches()
    yield


_MONGO_DOWN = {"skipped": 0}
_LEFT_BEHIND: list[tuple[str, list[str]]] = []          # (the test during which a database appeared that no test owns, its names): reported at the end of the run and dropped


@pytest.fixture(autouse=True)
def _isolate_acceleration_cache(tmp_path, monkeypatch, _isolate_environment):
    # Parser/numeric caches are production inputs too. Test overrides and parser
    # availability changes must never populate the real campaign's SSD cache.
    from app.backend.pipeline.acceleration import cache
    root = tmp_path / 'acceleration-cache'
    monkeypatch.setenv('ZTSA_ACCEL_CACHE', str(root))
    monkeypatch.setattr(cache, '_default', cache.ArtifactCache(root))


@pytest.fixture(autouse=True)
def _embedded_isolation(request, monkeypatch, tmp_path, _isolate_environment):
    """Each test and its child processes use a private persistent SQLite file."""
    import uuid
    from app.backend.pipeline.db import database, legacy
    name=database.TEST_PREFIX+uuid.uuid4().hex[:10]
    monkeypatch.setenv('ZTSA_DATABASE_DIR',str((tmp_path/'database').resolve()))
    monkeypatch.setenv('ZTSA_DATABASE_NAME',name)
    legacy.forget();database.close()
    yield name
    legacy.forget();database.close()


@pytest.fixture
def _mongo_isolation(_embedded_isolation):
    """Historical fixture name retained for repository contracts; no Mongo access."""
    return _embedded_isolation


def pytest_sessionstart(session):
    """Application tests require SQLite, never an external database daemon."""
    import sqlite3
    if sqlite3.sqlite_version_info<(3,45,0):
        pytest.exit('SQLite3.45+ with JSONB is required for application storage tests',returncode=3)


def pytest_terminal_summary(terminalreporter):
    pass


@pytest.fixture
def on_mongo():
    """Kept so that the test files converted before MongoDB became the default still name what they use; it does nothing now."""
    yield


@pytest.fixture
def mongo_db(_mongo_isolation):
    """The test's own throwaway MongoDB database, ``aivf_test_<random>`` (schema not created); dropped after the test. Tests can open nothing else."""
    from app.backend.pipeline.db import mongo

    return mongo.database(_mongo_isolation)


@pytest.fixture
def client():
    """FastAPI test client that passes the local-only middleware (host and cookie)."""
    from fastapi.testclient import TestClient

    from app.backend import api, security

    c = TestClient(api.app, base_url="http://127.0.0.1")
    c.cookies.set(security.COOKIE, security.TOKEN)
    return c


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Temporary working directory with the repository's relative layout."""
    for rel in ("data/raw", "data/interim", "data/processed", "experiments/logs",
                "notes"):
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_config, "SETTINGS_PATH", tmp_path / "settings.json")
    return tmp_path
