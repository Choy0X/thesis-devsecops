"""Register local GGUF files with Ollama without changing Settings."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import uuid

from app.backend.config import Settings
from app.backend.pipeline.fsio import FileIdentity, file_identity

from .client import OllamaError, tags
from .server import ensure_serving, executable


_LOG = logging.getLogger(__name__)


def _name_key(identity: FileIdentity) -> str:
    return "|".join(map(str, identity))


def _base_name(identity: FileIdentity, filename: str | None = None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", Path(filename or identity[0]).stem.lower()).strip("-")[:40] or "model"
    digest = hashlib.sha256(_name_key(identity).encode()).hexdigest()[:10]
    return f"aivf/{slug}-{digest}"


def name_for(path: Path) -> str:
    """Return a stable Ollama name for this path and revision of the file."""
    return _base_name(file_identity(path))


def _variant_name(base: str, context: int, adapter: FileIdentity | None, system: str | None) -> str:
    name = f"{base}-ctx{context}"
    if adapter is not None or system is not None:
        config = json.dumps([_name_key(adapter) if adapter is not None else None, system])
        name += "-" + hashlib.sha256(config.encode()).hexdigest()[:10]
    return name


def name_for_variant(path: Path, *, context: int, adapter: Path | None = None, system: str | None = None) -> str:
    """Name a Modelfile configuration for callers that need distinct settings on the same GGUF."""
    _validate_context(context)
    return _variant_name(name_for(path), context, file_identity(adapter) if adapter is not None else None, system)


def _argument(value: str) -> str:
    # The Modelfile parser removes the surrounding quotes without decoding backslashes.
    delimiter = '"""' if '"' in value else '"'
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if any(line.rstrip().endswith(delimiter) for line in lines[:-1]):
        raise ValueError("A Modelfile argument contains a closing quote before the end of the value")
    return delimiter + value + delimiter


def _validate_context(context: int) -> None:
    if type(context) is not int or context <= 0:
        raise ValueError("Context must be a positive number of tokens")


def _verify_sources(sources: dict[Path, FileIdentity]) -> None:
    try:
        if all(file_identity(path) == identity for path, identity in sources.items()):
            return
    except OSError:
        pass
    raise OllamaError("failed", "A model source changed during Ollama registration. Select it again.")


def _has_tag(name: str, timeout: float) -> bool:
    return any(row["name"] in (name, name + ":latest") for row in tags(timeout=timeout))


def _register(path: Path, *, context: int, adapter: Path | None, system: str | None, variant: bool,
              timeout: float, run, target: str | None = None, filename: str | None = None) -> str:
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise OllamaError("timeout", "Ollama model registration timed out.")
        return budget

    _validate_context(context)
    sources = {path: file_identity(path)}
    if adapter is not None and adapter not in sources:
        sources[adapter] = file_identity(adapter)
    base = _base_name(sources[path], filename)
    adapter_source = sources[adapter] if adapter is not None else None
    name = target or (_variant_name(base, context, adapter_source, system) if variant else base)

    with Path(sources[path][0]).open("rb") as stream:
        if stream.read(4) != b"GGUF":
            raise ValueError("The selected file is not GGUF")
    ensure_serving(timeout=min(30.0, remaining()))
    existing = _has_tag(name, min(30.0, remaining()))
    _verify_sources(sources)
    if existing:
        if target:
            raise ValueError(f"Ollama already has {name}. Choose a new tag.")
        return name
    program = executable()
    if program is None:
        raise OllamaError("unreachable", "Ollama is not installed. Install it from ollama.com and start the app again.")

    lines = ["FROM " + _argument(sources[path][0])]
    if adapter_source is not None:
        lines.append("ADAPTER " + _argument(adapter_source[0]))
    lines.append(f"PARAMETER num_ctx {context}")
    if system is not None:
        lines.append("SYSTEM " + _argument(system))

    def call(*args: str, budget: float | None = None) -> None:
        try:
            run([program, *args], check=True, capture_output=True,
                encoding="utf-8", errors="replace", timeout=remaining() if budget is None else budget)
        except subprocess.TimeoutExpired as error:
            raise OllamaError("timeout", "Ollama model registration timed out.") from error
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "").strip().splitlines()
            raise OllamaError("failed", detail[-1] if detail else f"Ollama could not register {name}.") from None
        except OSError as error:
            raise OllamaError("unreachable", f"Ollama could not be started from {program}: {error}") from None

    # Import under a disposable name, so a changed source never publishes unverified bytes.
    staging = "aivf/import-" + uuid.uuid4().hex

    def cleanup_after_timeout() -> None:
        retry_until = time.monotonic() + 3.0
        while (left := retry_until - time.monotonic()) > 0:
            try:
                if _has_tag(staging, min(left, 1.0)):
                    call("rm", staging, budget=2.0)
                    return
            except OllamaError as error:
                if error.code not in ("timeout", "unreachable"):
                    _LOG.warning("Could not clean up temporary Ollama model %s: %s", staging, error)
                    return
            time.sleep(0.15)

    with tempfile.TemporaryDirectory(prefix="aivf-ollama-") as folder:
        modelfile = Path(folder) / "Modelfile"
        modelfile.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        _verify_sources(sources)
        wait_for_staging = False
        try:
            call("create", staging, "-f", str(modelfile))
            existing = _has_tag(name, min(30.0, remaining()))
            _verify_sources(sources)
            if existing and target:
                raise ValueError(f"Ollama already has {name}. Choose a new tag.")
            if not existing:
                call("cp", staging, name)
        except OllamaError as error:
            # The CLI can time out before Ollama finishes publishing the staged model.
            wait_for_staging = isinstance(error.__cause__, subprocess.TimeoutExpired)
            raise
        finally:
            if not wait_for_staging:
                try:
                    if _has_tag(staging, min(30.0, remaining())):
                        call("rm", staging)
                except OllamaError as error:
                    wait_for_staging = error.code == "timeout"
                    if not wait_for_staging:
                        _LOG.warning("Could not clean up temporary Ollama model %s: %s", staging, error)
            if wait_for_staging:
                threading.Thread(target=cleanup_after_timeout, daemon=True, name="ollama-import-cleanup").start()
    _verify_sources(sources)
    return name


def register_gguf(path: Path, *, context: int, adapter: Path | None = None, system: str | None = None, timeout: float = 240.0, run=subprocess.run,
                  filename: str | None = None) -> str:
    """Create the file-based Ollama name once and reuse it if already registered."""
    return _register(path, context=context, adapter=adapter, system=system, variant=False, timeout=timeout, run=run, filename=filename)


def register_variant(path: Path, *, context: int, adapter: Path | None = None, system: str | None = None, timeout: float = 240.0, run=subprocess.run) -> str:
    """Create a distinct Ollama model for the requested context, adapter and system prompt."""
    return _register(path, context=context, adapter=adapter, system=system, variant=True, timeout=timeout, run=run)


def register_named(path: Path, name: str, *, context: int = 4096, timeout: float = 240.0, run=subprocess.run) -> str:
    """Create a complete model under a new tag. Never replace an existing tag."""
    return _register(path, context=context, adapter=None, system=None, variant=False, timeout=timeout, run=run, target=name)


def role_model(role: str, *, settings: Settings | None = None) -> tuple[Path, int] | None:
    """Read the GGUF and context Settings assign to a local language or embedding role."""
    if role not in ("llm", "embed"):
        return None
    if settings is None:
        settings = Settings.load()
    if (getattr(settings, f"{role}_engine", "") or "local") != "local":
        return None
    path = (getattr(settings, f"{role}_local_model", "") or "").strip()
    if role == "embed":
        if not path:
            from app.backend.pipeline.model_files import find_embedding_gguf

            found = find_embedding_gguf()
            return (found, 2048) if found else None
        return Path(path), 2048
    if not path:
        return None
    try:
        context = int(getattr(settings, "llm_local_context", "") or 4096)
    except ValueError:
        context = 4096
    return Path(path), context


def saved_chat_name(settings: Settings) -> str:
    """A native model still selected from a migrated Ollama endpoint."""
    from app.backend.settings_migrations import matches_ollama_runtime
    if settings.llm_engine == "local" and not settings.llm_local_model and matches_ollama_runtime(settings.llm_base_url):
        return settings.llm_model.strip()
    return ""
