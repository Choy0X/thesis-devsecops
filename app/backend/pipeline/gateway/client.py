"""Gateway compatibility for the local Ollama runtime."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
import uuid

from app.backend import paths
from app.backend.config import Settings
from app.backend.pipeline import finetune_deletion, fsio, model_access, oslock
from app.backend.pipeline.gateway.errors import GatewayError
from app.backend.pipeline.ollama import catalog, client as ollama, exclusive, models, server

_ROLES = ("llm", "embed")
_STARTUP_TIMEOUT = 26.0  # Leave time for Ollama's version probes within the startup budget.
_LOAD_TIMEOUT = 240.0


@dataclass(frozen=True)
class _Selection:
    alias: str
    path: Path
    context: int
    adapter: Path | None = None

    def name(self) -> str:
        return models.name_for_variant(self.path, context=self.context, adapter=self.adapter)

    def register(self, timeout: float) -> str:
        with finetune_deletion.serving_source({"model": self.path, "adapter": self.adapter}):
            return models.register_variant(self.path, context=self.context, adapter=self.adapter, timeout=timeout)


# Remember which alias this process prepared when several registry aliases share a runner.
# The source files remain available for the callers that use them to guard model deletion.
_selected: dict[str, tuple[str, Path, str, _Selection]] = {}


def base_url() -> str:
    return ollama.base_url()


def v1_base() -> str:
    return base_url() + "/v1"


def key() -> str:
    return "ollama"


def address() -> dict:
    parsed = urlsplit(base_url())
    return {"host": parsed.hostname, "port": parsed.port or (443 if parsed.scheme == "https" else 80), "key": key()}


def healthy(timeout: float = 1.0) -> bool:
    return ollama.version(timeout=timeout) is not None


def _name(name: str) -> str:
    return name.removesuffix(":latest")


def _settings_selection(role: str, settings: Settings) -> _Selection | None:
    chosen = models.role_model(role, settings=settings)
    if chosen is None:
        return None
    path, context = chosen
    adapter = (getattr(settings, "llm_local_adapter", "") or "").strip() if role == "llm" else ""
    return _Selection(role, path, context, Path(adapter) if adapter else None)


def _registry_selection(alias: str, row: dict) -> _Selection:
    adapter = row.get("adapter")
    return _Selection(alias, Path(row["model"]), row["context"], Path(adapter) if adapter else None)


def _scope() -> tuple[str, Path]:
    return base_url(), paths.interim_dir().resolve()


def _matching_slots(running: dict[str, dict], roles: tuple[str, ...] = _ROLES) -> dict[str, dict[str, dict]]:
    slots: dict[str, dict[str, dict]] = {role: {} for role in roles}
    if not running:
        return slots

    def use(role: str, choice: _Selection, name: str | None = None) -> None:
        try:
            model_name = _name(name or choice.name())
        except (ValueError, OSError):
            return
        if model_name in running and model_name not in slots[role]:
            slots[role][model_name] = _slot(choice, running[model_name])

    scope = _scope()
    settings = Settings.load()
    for role in roles:
        selected = _selected.get(role)
        if selected and selected[:2] == scope:
            use(role, selected[3], selected[2])
        chosen = _settings_selection(role, settings)
        if chosen:
            use(role, chosen)

    for alias, row in model_access.default().read()["models"].items():
        # Disabling registration prevents new use, but the runner can still be loaded.
        role = "embed" if row.get("embedding") else "llm"
        if role not in slots:
            continue
        chosen = _registry_selection(row.get("id") or alias, row)
        use(role, chosen)
        if row.get("ollama_name"):
            use(role, chosen, row["ollama_name"])
    return slots


def _slot(choice: _Selection | None = None, runner: dict | None = None) -> dict:
    if runner is None:
        return {"state": "absent", "model": None, "alias": None, "context": None, "gpu_bytes": 0}
    gpu_bytes = runner.get("size_vram") or 0
    size = runner.get("size") or 0
    runs_on = "cpu" if not gpu_bytes else "split" if gpu_bytes < size else "gpu"
    context = runner.get("context_length") or None
    name = _name(runner["name"])
    return {"state": "ready", "model": str(choice.path), "alias": choice.alias, "context": context, "gpu_bytes": gpu_bytes,
            "ollama_name": name, "adapter": str(choice.adapter) if choice.adapter else None, "runs_on": runs_on,
            "engine": {"name": "Ollama", "model_file": choice.path.name, "loaded": True, "context": context,
                       "adapter": choice.adapter.name if choice.adapter else None, "runs_on": runs_on},
            "message": "The selected model is loaded."}


def _running(timeout: float) -> dict[str, dict]:
    return {_name(row["name"]): row for row in ollama.ps(timeout=timeout, include_metadata=True) if row.get("name")}


def _configured_slots(running: dict[str, dict], roles: tuple[str, ...] = _ROLES) -> dict:
    settings = Settings.load()
    configured_slots = {}
    for role in roles:
        choice = _settings_selection(role, settings)
        try:
            name = choice.name() if choice else models.saved_chat_name(settings) if role == "llm" else ""
        except (OSError, ValueError):
            name = ""
        runner = running.get(_name(name))
        if runner and (choice is None or runner.get("context_length") == choice.context):
            configured_slots[role] = _slot(choice or _Selection(role, Path(name), runner.get("context_length") or 0), runner)
        else:
            configured_slots[role] = _slot()
    return configured_slots


def _snapshot(running: dict[str, dict], *, marker_wait: float = 3.0, roles: tuple[str, ...] = _ROLES) -> dict:
    matched = _matching_slots(running, roles)
    slots = {role: next(iter(matches.values()), _slot()) for role, matches in matched.items()}
    sources = sorted({slot["model"] for matches in matched.values() for slot in matches.values()})
    artifacts = [{"role": role, "path": str(slot[field])}
                 for role, matches in matched.items() for slot in matches.values()
                 for field in ("model", "adapter") if slot.get(field)]
    return {"slots": slots, "configured_slots": _configured_slots(running, roles), "exclusive": exclusive.current(wait=marker_wait), "running_models": list(running.values()), "running_sources": sources,
            "running_artifacts": artifacts, "free_vram_bytes": None, "consumers": [], "queue": []}


def _offline_snapshot(*, roles: tuple[str, ...] = _ROLES, marker_wait: float = 3.0) -> dict:
    """What is known when Ollama does not answer: nothing is loaded, and the reservation still holds."""
    return {"slots": {role: _slot() for role in roles}, "configured_slots": {role: _slot() for role in roles},
            "exclusive": exclusive.current(wait=marker_wait), "running_models": [], "running_sources": [],
            "running_artifacts": [], "free_vram_bytes": None, "consumers": [], "queue": [], "reachable": False}


def status(timeout: float = 3.0, *, roles: tuple[str, ...] = _ROLES) -> dict | None:
    """The gateway snapshot; when Ollama does not answer, a snapshot with ``reachable`` False and no running models.
    None only when the reservation marker itself cannot be read."""
    try:
        return _snapshot(_running(timeout), roles=roles)
    except ollama.OllamaError as error:
        if error.code != "unreachable":
            return None  # Ollama answers (slowly or with an error): what it runs is unknown
    except oslock.Busy:
        return None
    try:
        return _offline_snapshot(roles=roles)
    except oslock.Busy:
        return None


def running_model(name: str, timeout: float = 3.0) -> dict | None:
    try:
        return _running(timeout).get(_name(name))
    except ollama.OllamaError:
        return None


def running_source_paths(timeout: float = 1.0) -> list[str]:
    snapshot = status(timeout=timeout) or {}
    paths = snapshot.get("running_sources")
    if paths is not None:
        return paths
    return [slot["model"] for slot in (snapshot.get("slots") or {}).values()
            if slot.get("state") in ("loading", "ready", "draining") and slot.get("model")]


@contextmanager
def _gateway_errors():
    try:
        yield
    except ollama.OllamaError as error:
        if error.code == "timeout":
            raise GatewayError("load_timeout", "Ollama did not answer in time.") from error
        retry_after = 5.0 if error.code == "unreachable" else None
        raise GatewayError("load_failed", str(error), retry_after=retry_after) from error
    except exclusive.AlreadyMarked as error:
        raise GatewayError("gpu_exclusive", f"The graphics card is already held by {error.owner}.") from error
    except exclusive.ReservationLost as error:
        raise GatewayError("cancelled", str(error)) from error
    except oslock.Busy as error:
        raise GatewayError("busy", _busy_message(error), retry_after=1.0) from error
    except FileNotFoundError as error:
        raise GatewayError("model_file_missing", f"A selected model file is missing: {error.filename or error}.") from error
    except ValueError as error:
        raise GatewayError("bad_request", str(error)) from error
    except OSError as error:
        raise GatewayError("load_failed", str(error)) from error


def _preparation_admission(token: int | None = None) -> None:
    from app.backend import lifecycle

    state = lifecycle.status()
    if state["running"] or state["done"] or (token is not None and lifecycle.admission_token() != token):
        raise GatewayError("cancelled", "The application is shutting down. Model preparation was cancelled.")


def _check_exclusive(*, wait: float = 3.0) -> None:
    held = exclusive.current(wait=wait)
    if held:
        raise GatewayError("busy", f"The graphics card is in use by {held['owner']}.", retry_after=1.0)


def check_inference() -> None:
    """Refuse new application inference when shutdown or a GPU reservation is active."""
    _preparation_admission()
    if not ollama.local_endpoint():
        raise GatewayError("not_local", "Local inference requires Ollama running on this computer.")
    with _gateway_errors():
        _check_exclusive()


# -- shared turn leases -------------------------------------------------------------------------------------------
# A turn (a Project chat, a scan, an embedding request) holds a shared lease for as long as it talks to a loaded model.
# Leases do not exclude each other or model loads. The exclusive drain marks the card first, so no new lease is
# admitted, then waits until every lease has ended before it unloads. A lease is a locked file per holder: the
# operating system drops the lock when the process ends, so a crashed holder never keeps the card.

_thread = threading.local()


def _lease_folder() -> Path:
    return paths.interim_dir() / "ollama_leases"


class _Lease:
    def __init__(self, owner: str):
        folder = _lease_folder()
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{os.getpid()}-{uuid.uuid4().hex}"
        self.lock = oslock.FileLock(folder / f"{stem}.lock", note=False).acquire(wait=3.0)
        self.info = folder / f"{stem}.json"
        try:
            fsio.write_json(self.info, {"owner": owner, "pid": os.getpid()})
        except OSError:
            pass  # ignored: the owner only improves the busy message

    def release(self) -> None:
        self.info.unlink(missing_ok=True)
        path = self.lock.path
        self.lock.release()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass  # ignored: a stale lease file is cleaned up by the next drain


def _live_leases(*, clean: bool = False) -> list[dict]:
    """Leases whose holder is alive. ``clean`` removes the files of ended holders (only under the operation lock)."""
    folder = _lease_folder()
    if not folder.is_dir():
        return []
    live = []
    for path in sorted(folder.glob("*.lock")):
        try:
            probe = oslock.FileLock(path, note=False).acquire(wait=0.0)
        except oslock.Busy:
            try:
                info = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                info = {}
            info = info if isinstance(info, dict) else {}
            live.append({"owner": str(info.get("owner") or "a local model request"), "pid": info.get("pid")})
            continue
        except OSError:
            continue
        probe.release()
        if clean:
            for stale in (path, path.with_suffix(".json")):
                try:
                    stale.unlink(missing_ok=True)
                except OSError:
                    pass  # ignored: removed by a later drain
    return live


def _lease_words(leases: list[dict]) -> str:
    names = list(dict.fromkeys(lease["owner"] for lease in leases))
    return ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")


class InferenceSession:
    """One holder's shared lease. ``paused()`` gives the lease up while the holder waits for a person."""

    def __init__(self, owner: str):
        self.owner = owner
        self._lease: _Lease | None = None

    def _admit(self) -> None:
        check_inference()
        with _gateway_errors(), _operation(_STARTUP_TIMEOUT, f"admitting {self.owner}"):
            check_inference()
            self._lease = _Lease(self.owner)

    def _leave(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()

    @contextmanager
    def paused(self):
        """Release the lease (an exclusive drain may run meanwhile); re-admit with the same checks afterwards."""
        self._leave()
        yield
        self._admit()


@contextmanager
def inference_session(owner: str = "a local model request"):
    """Hold a shared lease while talking to a loaded model, so the exclusive drain waits for this turn.
    A nested session in the same thread reuses the outer lease."""
    outer = getattr(_thread, "session", None)
    if outer is not None and outer._lease is not None:
        yield outer
        return
    session = InferenceSession(owner)
    session._admit()
    _thread.session = session
    try:
        yield session
    finally:
        _thread.session = outer
        session._leave()


def inference_model(role: str, *, timeout: float = 30.0, settings: Settings | None = None,
                    require_gpu: bool = False) -> str:
    """Register the configured role and return the native model name for Ollama requests."""
    settings = settings if settings is not None else Settings.load()
    choice = _settings_selection(role, settings)
    name = models.saved_chat_name(settings) if role == "llm" and choice is None else ""
    if choice is None and not name:
        raise GatewayError("not_configured", f"Choose a local {'language model' if role == 'llm' else 'embedding model'} in Settings.")
    deadline = time.monotonic() + timeout
    check_inference()
    with _gateway_errors():
        if choice is not None and require_gpu:
            _, status = _preload(role, choice, _remaining(deadline))
            slot = status["slots"][role]
            if not slot.get("gpu_bytes"):
                raise GatewayError("gpu_required", "Ollama loaded the selected model on the CPU. Local context generation requires a GPU.")
            return slot["ollama_name"]
        with _operation(min(_remaining(deadline), _STARTUP_TIMEOUT), f"preparing the {role} model"):
            check_inference()
            if choice is not None:
                return choice.register(_remaining(deadline))
            server.ensure_serving(timeout=min(_remaining(deadline), _STARTUP_TIMEOUT))
            row = next((row for row in ollama.tags(timeout=_remaining(deadline), include_metadata=True)
                        if _name(row.get("name") or "") == _name(name)), None)
            if not row:
                raise GatewayError("not_configured", f"Ollama does not have {name}. Choose an installed language model in Settings.")
            if catalog.cloud_tag(name) or row.get("remote_host") or row.get("remote_model"):
                raise GatewayError("not_local", "That Ollama model uses cloud inference. Select it in Projects to opt in.")
            details = ollama.show(row["name"], timeout=min(10.0, _remaining(deadline)))
            if details.get("remote_host") or details.get("remote_model"):
                raise GatewayError("not_local", "That Ollama model uses cloud inference. Select it in Projects to opt in.")
            if require_gpu:
                ollama.load(name, timeout=_remaining(deadline))
                runner = _running(min(_remaining(deadline), 3.0)).get(_name(name))
                if not runner or not runner.get("size_vram"):
                    raise GatewayError("gpu_required", "Ollama loaded the selected model on the CPU. Local context generation requires a GPU.")
            return name


def ensure_running(*, admission_token: int | None = None) -> None:
    _preparation_admission(admission_token)
    with _gateway_errors():
        _check_exclusive()
        if not healthy():
            deadline = time.monotonic() + _STARTUP_TIMEOUT
            with _operation(_remaining(deadline), "starting Ollama"):
                _preparation_admission(admission_token)
                _check_exclusive()
                server.ensure_serving(timeout=_remaining(deadline))
    _preparation_admission(admission_token)


def _remaining(deadline: float, *, code: str = "load_timeout") -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GatewayError(code, "The local model operation timed out.")
    return remaining


def _operation_path() -> Path:
    return paths.interim_dir() / "ollama_operation.lock"


def _busy_message(error: oslock.Busy) -> str:
    if error.path.name == _operation_path().name:
        try:
            holder = json.loads(error.path.with_name(error.path.name + ".owner.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            holder = {}
        what = holder.get("what") if isinstance(holder, dict) else None
        if what:
            pid = holder.get("pid")
            return f"Ollama is busy {what}" + (f" (process {pid})" if pid else "") + ". Try again shortly."
    return "Another local model operation is still in progress. Try again shortly."


@contextmanager
def _operation(wait: float, what: str = "with another local model operation"):
    """Serialize model loads, registrations, unloads and the exclusive drain. ``what`` names the holder in busy messages."""
    # Separate from the marker lock so status and busy checks can read it during a preload or drain.
    path = _operation_path()
    lock = oslock.FileLock(path, note=False).acquire(wait=wait)
    owner = path.with_name(path.name + ".owner.json")
    try:
        try:
            owner.write_text(json.dumps({"what": what, "pid": os.getpid()}), encoding="utf-8")
        except OSError:
            pass  # ignored: the holder only improves the busy message
        yield
    finally:
        try:
            owner.unlink(missing_ok=True)
        except OSError:
            pass  # ignored: the next holder overwrites it
        lock.release()


def _preload(role: str, choice: _Selection, timeout: float, token: int | None = None, *, expected_name: str | None = None) -> tuple[bool, dict]:
    deadline = time.monotonic() + timeout
    with _operation(min(_remaining(deadline), _STARTUP_TIMEOUT), f"loading {choice.path.name}"):
        _preparation_admission(token)
        _check_exclusive(wait=min(3.0, _remaining(deadline)))
        if expected_name and choice.name() != expected_name:
            raise GatewayError("bad_request", "The selected GGUF changed. Register the current file and select it again.")
        name = choice.register(_remaining(deadline))
        if expected_name and name != expected_name:
            raise GatewayError("bad_request", "The selected GGUF changed. Register the current file and select it again.")
        running = _running(min(_remaining(deadline), 3.0))
        runner = running.get(_name(name))
        started = runner is None or runner.get("context_length") != choice.context
        if started:
            _preparation_admission(token)
            _check_exclusive(wait=min(3.0, _remaining(deadline)))
            if role == "embed":
                # An empty list schedules the embedding runner without computing a vector.
                ollama.embed(name, [], keep_alive="30m", timeout=_remaining(deadline))
            else:
                ollama.load(name, timeout=_remaining(deadline))
            running = _running(min(_remaining(deadline), 3.0))
            runner = running.get(_name(name))
        if runner is None:
            raise GatewayError("load_failed", f"Ollama did not report {name} as loaded.")
        actual_context = runner.get("context_length")
        if actual_context is None:
            raise GatewayError("load_failed", f"Ollama did not report the context for {name}.")
        if actual_context != choice.context:
            raise GatewayError("does_not_fit", f"Ollama loaded {actual_context} tokens of context for the requested {choice.context}.")
        _preparation_admission(token)
        _selected[role] = (*_scope(), name, choice)
        roles = ("llm",) if role == "llm" else _ROLES
        return started, _snapshot(running, marker_wait=min(3.0, _remaining(deadline)), roles=roles)


def prepare(model: str, *, timeout: float = 240, admission_token: int | None = None) -> dict:
    """Preload an enabled local language-model selection, preserving the requested alias."""
    deadline = time.monotonic() + timeout
    _preparation_admission(admission_token)
    with _gateway_errors():
        _check_exclusive(wait=min(3.0, _remaining(deadline)))
        selected = model_access.default().spec(model) if isinstance(model, str) else None
        if selected is None or selected.get("embedding"):
            raise GatewayError("bad_request", "Choose an enabled language model ID.")
        expected_name = model if selected.get("ollama_name") == model else None
        _, snapshot = _preload("llm", _registry_selection(model, selected), _remaining(deadline), admission_token, expected_name=expected_name)
        slot = snapshot["slots"]["llm"]
        return {field: slot[field] for field in ("alias", "context", "state", "message", "ollama_name")}


def prepare_tag(model: str, *, digest: str, context: int, timeout: float = _LOAD_TIMEOUT, require_tools: bool = True,
                admission_token: int | None = None) -> dict:
    """Prepare an installed tag, using a private context variant when needed."""
    deadline = time.monotonic() + timeout
    check_inference()
    with _gateway_errors(), _operation(min(_remaining(deadline), _STARTUP_TIMEOUT), f"loading {model}"):
        _preparation_admission(admission_token)
        _check_exclusive(wait=min(3.0, _remaining(deadline)))
        server.ensure_serving(timeout=min(_remaining(deadline), _STARTUP_TIMEOUT))
        current = next((row for row in catalog.installed() if row["tag"] == model), None)
        required = {"completion", "tools"} if require_tools else {"completion"}
        if (not current or current["digest"] != digest or current["location"] != "local" or not current["available"]
                or not required.issubset(current["capabilities"])):
            raise GatewayError("not_configured", "The selected Ollama tag changed or is unavailable. Select it again.")
        name = catalog.runtime_tag(model, digest, current["configured_context"], context)
        if name != model:
            try:
                variant = ollama.show(name, timeout=min(3.0, _remaining(deadline)))
            except ollama.OllamaError as error:
                if error.code != "not_found":
                    raise
                ollama.create_context_model(model, name, context, timeout=_remaining(deadline))
            else:
                if variant.get("remote_host") or variant.get("remote_model"):
                    raise GatewayError("bad_request", "The local context variant resolves to a remote model.")
        runner = _running(min(3.0, _remaining(deadline))).get(_name(name))
        if not runner or runner.get("context_length") != context:
            _check_exclusive(wait=min(3.0, _remaining(deadline)))
            ollama.load(name, context=context, timeout=_remaining(deadline))
            runner = _running(min(3.0, _remaining(deadline))).get(_name(name))
        if not runner or runner.get("context_length") != context:
            raise GatewayError("does_not_fit", f"Ollama did not load {model} with the requested {context} tokens of context.")
        _preparation_admission(admission_token)
        return {"alias": model, "context": context, "state": "ready", "message": "The selected Ollama model is loaded.", "ollama_name": name}


def load(role: str, *, timeout: float | None = None) -> dict:
    if role not in _ROLES:
        raise GatewayError("bad_request", "role must be llm or embed.")
    deadline = time.monotonic() + (_LOAD_TIMEOUT if timeout is None else timeout)
    _preparation_admission()
    with _gateway_errors():
        _check_exclusive(wait=min(3.0, _remaining(deadline)))
        chosen = _settings_selection(role, Settings.load())
        if chosen is None:
            if role == "llm" and models.saved_chat_name(Settings.load()):
                name = inference_model(role, timeout=_remaining(deadline))
                with _operation(_remaining(deadline), f"loading {name}"):
                    check_inference()
                    before = _running(min(3.0, _remaining(deadline)))
                    if _name(name) not in before:
                        ollama.load(name, timeout=_remaining(deadline))
                    after = _running(min(3.0, _remaining(deadline)))
                    if _name(name) not in after:
                        raise GatewayError("load_failed", f"Ollama did not report {name} as loaded.")
                    return {"started": _name(name) not in before, "status": _snapshot(after, roles=("llm",))}
            label = "language model" if role == "llm" else "embedding model"
            raise GatewayError("not_configured", f"Choose a local {label} in Settings.")
        started, snapshot = _preload(role, chosen, _remaining(deadline))
        return {"started": started, "status": snapshot}


def _unload_budget(wait: float | None, timeout: float | None) -> float:
    budget = float("inf") if timeout is None else timeout
    if wait is not None and wait > 0:
        budget = min(budget, wait) if timeout is not None else wait
    return budget


def _unload(role: str | None, *, wait: float | None, timeout: float | None, model: str | None = None,
            configured: bool = False) -> dict:
    deadline = time.monotonic() + _unload_budget(wait, timeout)
    lock_wait = 0.0 if wait == 0 else _remaining(deadline)
    with _operation(lock_wait, "unloading models"):
        return _unload_locked(role, deadline, model=model, configured=configured)


def _unload_locked(role: str | None, deadline: float, *, model: str | None = None, configured: bool = False) -> dict:
    running = _running(min(_remaining(deadline), 3.0))
    roles = (role,) if role else ("llm",) if model is not None else _ROLES
    slots = _matching_slots(running, roles)
    unloaded = [name for name, matches in slots.items() if matches and role in (None, name)]
    if model is not None:
        targets = [model] if model in running else []
    elif configured:
        selected = {key: slot["ollama_name"] for key, slot in _configured_slots(running, roles).items()
                    if slot["state"] == "ready"}
        targets = list(dict.fromkeys(selected.values()))
        unloaded = list(selected)
    elif role is None:
        targets = list(running)
        unloaded = [running[name]["name"] for name in targets]
    else:
        targets = list(slots[role])
        if role == "llm":
            saved = _name(models.saved_chat_name(Settings.load()))
            if saved in running and saved not in targets:
                targets.append(saved)
    for name in targets:
        ollama.unload(running[name]["name"], timeout=min(30.0, _remaining(deadline)))
    if targets:
        remaining = _running(min(_remaining(deadline), 3.0))
        while any(name in remaining for name in targets):
            time.sleep(min(0.05, _remaining(deadline, code="busy")))
            remaining = _running(min(_remaining(deadline, code="busy"), 3.0))
        running = remaining
    return {"unloaded": targets if model is not None else unloaded, "status": _snapshot(running, roles=roles)}


def unload(role: str | None = None, *, wait: float | None = None, timeout: float | None = None) -> dict:
    if role not in (None, *_ROLES):
        raise GatewayError("bad_request", "role must be llm or embed.")
    with _gateway_errors():
        return _unload(role, wait=wait, timeout=timeout)


def unload_model(name: str, *, timeout: float = 30.0) -> dict:
    """Unload an exact name returned by Ollama's running models list."""
    with _gateway_errors():
        return _unload(None, wait=None, timeout=timeout, model=_name(name))


def unload_configured(role: str | None = None) -> dict:
    """Release the app's configured chat and embedding runners."""
    with _gateway_errors():
        return _unload(role, wait=None, timeout=30.0, configured=True)


def reload(*, wait: float | None = None, timeout: float | None = None) -> dict:
    return unload(wait=wait, timeout=timeout)


def begin_exclusive(owner: str, pid: int | None = None, *, wait: float | None = None, timeout: float | None = None) -> dict:
    """Mark the card for ``owner``, wait until every turn lease has ended, then unload every model.
    ``wait=0`` refuses at once (naming the turns) instead of waiting. When Ollama does not answer, nothing is
    loaded and the mark is kept."""
    with _gateway_errors(), exclusive.reservation(owner, pid):
        deadline = time.monotonic() + _unload_budget(wait, timeout)
        while True:
            leases = _live_leases()
            if leases:
                if wait == 0 or deadline - time.monotonic() <= 0:
                    raise GatewayError("busy", f"The graphics card is in use by {_lease_words(leases)}.", retry_after=1.0)
                time.sleep(max(0.0, min(0.1, deadline - time.monotonic())))
                continue
            with _operation(0.0 if wait == 0 else _remaining(deadline, code="busy"), f"freeing the graphics card for {owner.strip()}"):
                if _live_leases(clean=True):
                    continue
                try:
                    result = _unload_locked(None, deadline)
                except ollama.OllamaError as error:
                    if error.code != "unreachable":
                        raise
                    result = {"unloaded": [], "status": _offline_snapshot()}
                break
        if result["status"]["running_models"]:
            raise GatewayError("busy", "Another Ollama client started a model while the graphics card was being freed.", retry_after=1.0)
        return {"owner": owner.strip(), "free_vram_bytes": None, "status": result["status"]}


def end_exclusive(owner: str | None = None, *, timeout: float = 10.0) -> bool:
    with _gateway_errors():
        return exclusive.clear(owner)


def shutdown(timeout: float = 10.0) -> bool:
    server.shutdown()
    return True


def consumer(owner, *, active=True, pid=None, ttl=45) -> dict:
    return {"consumers": [], "unloaded": []}


@contextmanager
def hold_consumer(owner):
    yield
