"""Application-owned chat workspaces, turns and approvals."""
from __future__ import annotations

import asyncio
import hashlib
import copy
from contextlib import ExitStack, contextmanager, nullcontext
import json
import shutil
from pathlib import Path
import threading
import time
import uuid

from . import workspace
from .agent_progress import guidance, observed_tools, plan_for_checkpoint
from .resources import Cancelled, ResourceQueues
from .runtime import OpenCodeRuntime, PINNED_VERSION
from .store import Store, StoreError, encode, stamp

TERMINAL = {"completed", "failed", "interrupted"}
SCAN_ONLY_MODEL = {"kind": "scan_only", "id": "project-scans", "model": "project-scans",
                   "name": "Local project scans", "available": False}
SCAN_ONLY_MESSAGE = "Choose an installed agent model to start a conversation. Code and dependency checks are available."
PHASE_MESSAGES = {
    "queued": "Waiting for this chat's earlier work.",
    "waiting_resource": "Waiting for the selected model's execution capacity.",
    "preparing_model": "Checking the selected model and project configuration.",
    "starting_runtime": "Starting this chat's coding runtime.",
    "initializing_runtime": "Initializing the coding runtime and its dependencies.",
    "connecting_tools": "Connecting the configured project tools.",
    "preparing_workspace": "Preparing an isolated project workspace.",
    "preparing_context": "Preparing the saved conversation and attached sources.",
    "submitting_prompt": "Sending the message to the coding runtime.",
    "generating": "Waiting for the selected model's response.",
    "receiving_response": "Receiving the model's response.",
    "executing_tool": "The coding runtime is executing a tool.",
    "waiting_approval": "Waiting for your approval.",
    "waiting_input": "Waiting for your answer.",
    "scanning": "Analyzing the selected project sources.",
}


def _model_free_scan(payload: dict) -> bool:
    return payload.get("scope") == "project_code" or (
        payload.get("scope") == "dependencies" and payload.get("dependency_mode") == "versions")


class Service:
    def __init__(self, root: Path, *, runtime_factory=OpenCodeRuntime, configure=None, scan_handler=None,
                 describe_config=None, admit_turn=None, text_turn=None, coding_turn=None, coding_ready=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root / "projects.sqlite3")
        self.store.reconcile()
        self.runtime_factory, self.configure, self.scan_handler = runtime_factory, configure, scan_handler
        self.describe_config = describe_config
        self.admit_turn = admit_turn
        self.text_turn = text_turn
        self.coding_turn, self.coding_ready = coding_turn, coding_ready
        self.resources = ResourceQueues()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads, self._cancel, self._runtimes, self._pending = {}, {}, {}, {}
        self._native_replies: dict[str, list] = {}  # answers waiting for their turn to take the model back
        self._starting = {}
        self._holds = {}
        self._runtime_jobs, self._runtime_threads = {}, set()
        self._live, self._stream_generation = {}, {}

    def _configuration(self, chat):
        if chat["model"].get("kind") == "scan_only":
            raise StoreError(SCAN_ONLY_MESSAGE)
        if self.configure is None:
            raise StoreError("An explicit evaluated model configuration is required.")
        item = self.ensure_workspace(chat["id"])
        project = self.store.get("projects", chat["project_id"])
        return item, self.configure(chat["model"], item, {**project["settings"], "project_id": project["id"]})

    def runtime_status(self, chat_id):
        chat = self.store.get("chats", chat_id)
        if chat["model"].get("kind") == "scan_only":
            return {"state": "failed", "phase": "unavailable", "message": SCAN_ONLY_MESSAGE,
                    "chat_id": chat_id, "model_id": chat["model"]["id"], "ready": False, "elapsed_seconds": 0}
        if chat["model"].get("tool_protocol") == "text":
            return {"state": "ready", "phase": "ready", "message": "This model answers text prompts.",
                    "chat_id": chat_id, "model_id": chat["model"]["id"], "ready": True, "elapsed_seconds": 0}
        if self.coding_ready:
            try:
                if self._stop.is_set() or chat["archived"]:
                    raise StoreError("This chat cannot start new work.")
                self.coding_ready(chat)
                state, message = "ready", "The embedded coding runtime is ready."
            except (ImportError, OSError, ValueError) as error:
                state, message = "failed", str(error)
            return {"state": state, "phase": state, "message": message, "chat_id": chat_id,
                    "model_id": chat["model"]["id"], "ready": state == "ready", "elapsed_seconds": 0}
        with self._lock:
            job = self._runtime_jobs.get(chat_id)
            binding = self._runtimes.get(chat_id)
            current = job is not None and job["model"] == chat["model"]
            result = copy.deepcopy(job["status"]) if current else {
                "state": "queued", "phase": "queued", "message": "Prepare this chat's coding runtime before sending a message."}
            started = job["started"] if current else time.monotonic()
            finished = job.get("finished", time.monotonic()) if current else started
        ready = False
        if binding and not self._stop.is_set() and not chat["archived"]:
            try:
                _, config = self._configuration(chat)
                process = getattr(binding[0], "process", None)
                ready = binding[2] == config and (process is None or process.poll() is None)
            except Exception as exc:
                result.update(state="failed", phase="failed", message=str(exc), error={"code": "configuration_failed", "message": str(exc)})
        with self._lock:
            ready = ready and self._runtimes.get(chat_id) is binding and not self._stop.is_set() and self.store.get("chats", chat_id)["model"] == chat["model"]
        if ready:
            result.update(state="ready", phase="ready", message="This chat's coding runtime and tools are ready.")
            result.pop("error", None)
        elif result["state"] == "ready":
            result.update(state="queued", phase="queued", message="Prepare this chat's current coding runtime before sending a message.")
        if self._stop.is_set():
            result.update(state="failed", phase="failed", message="The application is shutting down.")
        return {**result, "chat_id": chat_id, "model_id": chat["model"].get("id"), "ready": bool(ready),
                "elapsed_seconds": round(max(0, finished - started), 1)}

    def prepare_runtime(self, chat_id):
        status = self.runtime_status(chat_id)
        with self._lock:
            if self._stop.is_set():
                raise StoreError("The application is shutting down.")
            chat = self.store.get("chats", chat_id)
            if chat["archived"]:
                raise StoreError("Archived chats cannot prepare a coding runtime.")
            if chat["model"].get("kind") == "scan_only":
                raise StoreError(SCAN_ONLY_MESSAGE)
            if self.coding_turn or chat["model"].get("tool_protocol") == "text":
                return status
            job = self._runtime_jobs.get(chat_id)
            if (status["ready"] and status["model_id"] == chat["model"].get("id") and chat_id in self._runtimes) or (job and job["model"] == chat["model"] and job["status"]["state"] in ("queued", "loading") and not job["cancelled"].is_set()):
                return status
            if job:
                job["cancelled"].set()
            job = {"model": copy.deepcopy(chat["model"]), "cancelled": threading.Event(), "started": time.monotonic(),
                   "status": {"state": "queued", "phase": "queued", "message": PHASE_MESSAGES["queued"]}}
            self._runtime_jobs[chat_id] = job
            worker = threading.Thread(target=self._prepare_runtime, args=(chat_id, job), name="projects-runtime-" + chat_id[:8], daemon=True)
            self._runtime_threads.add(worker)
            worker.start()
            return {**job["status"], "chat_id": chat_id, "model_id": chat["model"].get("id"), "ready": False, "elapsed_seconds": 0}

    def _prepare_runtime(self, chat_id, job):
        def phase(value):
            with self._lock:
                if self._stop.is_set() or job["cancelled"].is_set() or self._runtime_jobs.get(chat_id) is not job:
                    raise Cancelled("Runtime preparation was cancelled.")
                if self.store.get("chats", chat_id)["model"] != job["model"]:
                    raise Cancelled("The chat model changed during runtime preparation.")
                job["status"].update(state="queued" if value == "waiting_resource" else "loading", phase=value, message=PHASE_MESSAGES[value])
        try:
            chat = self.store.get("chats", chat_id)
            with self.resources.acquire("chat:" + chat_id, job["cancelled"], on_wait=lambda: phase("waiting_resource")):
                key = "local-gateway" if chat["model"].get("kind") == "local" else "hosted:" + chat["model"].get("endpoint", "")
                with self.resources.acquire(key, job["cancelled"], on_wait=lambda: phase("waiting_resource")):
                    phase("preparing_model")
                    self._runtime(chat, cancelled=job["cancelled"], on_phase=phase)
                    with self._lock:
                        phase("connecting_tools")
                        job["status"].update(state="ready", phase="ready", message="This chat's coding runtime and tools are ready.")
        except Exception as exc:
            with self._lock:
                job["status"].update(state="failed", phase="failed", message=str(exc),
                                     error={"code": "cancelled" if isinstance(exc, Cancelled) else "runtime_failed", "message": str(exc)})
        finally:
            with self._lock:
                job["finished"] = time.monotonic()
                self._runtime_threads.discard(threading.current_thread())

    def delete_chat(self, chat_id: str, *, discard: bool = False) -> None:
        """Delete a conversation with its workspace copy, Git worktree registration and search cache.
        Refused while the workspace holds changes that were not integrated, unless ``discard`` is set."""
        from .project_grounding import conversation_search_scope

        chat = self.store.get("chats", chat_id)
        item = scope = None
        if chat["workspace"]:
            directory = Path(chat["workspace"]).resolve()
            if not directory.is_relative_to((self.root / "workspaces").resolve()):
                raise StoreError("The conversation workspace is outside Projects storage.")
            item = workspace.load(directory)
            pending = workspace.changes(item)
            if pending and not discard:
                raise workspace.Conflict(f"This conversation's workspace has {len(pending)} change(s) that were not "
                                         "integrated into the project. Integrate them first, or delete the conversation "
                                         "and discard them.")
            scope = conversation_search_scope(self, chat)
        self.store.delete_chat(chat_id)
        self.invalidate_runtime(chat_id)
        if item is not None:
            workspace.remove(item)
        if scope is not None:
            shutil.rmtree(scope.cache_dir, ignore_errors=True)

    def invalidate_runtime(self, chat_id):
        """Invalidate preparation while the caller holds the model-change lock."""
        with self._lock:
            job = self._runtime_jobs.get(chat_id)
            if job:
                job["cancelled"].set()
            binding = self._runtimes.pop(chat_id, None)
            starting = self._starting.get(chat_id)
        if binding:
            binding[0].stop()
        if starting:
            starting.stop()

    def create_project(self, name: str, source: str, *, instructions: str = "") -> dict:
        from app.backend.pipeline.projects import validate_folder
        folder = validate_folder(source)
        if len(instructions) > 32000 or folder.is_relative_to(self.root):
            raise StoreError("Choose a source folder outside managed IDE metadata and bounded project instructions.")
        return self.store.project(name, str(folder), {"instructions": instructions, "memory": [], "extensions": []})

    def create_chat(self, project_id: str, title: str, model: dict, *, defer_workspace=False) -> dict:
        chat = self.store.chat(project_id, title, model)
        if defer_workspace:
            return chat
        self.ensure_workspace(chat["id"])
        return self.store.get("chats", chat["id"])

    def ensure_workspace(self, chat_id: str, cancelled=None) -> dict:
        chat = self.store.get("chats", chat_id)
        if chat["workspace"]:
            return workspace.load(Path(chat["workspace"]))
        token = cancelled or self._stop
        with self.resources.acquire("workspace:" + chat_id, token):
            chat = self.store.get("chats", chat_id)
            if chat["workspace"]:
                return workspace.load(Path(chat["workspace"]))
            if chat["archived"]:
                raise StoreError("An archived conversation cannot create a new workspace.")
            project = self.store.get("projects", chat["project_id"])
            directory = workspace.bound(self.root, "workspaces/" + chat_id)
            # A process may have stopped before a prior attempt could clean up.
            # Keep those files separate from the next snapshot and its baseline.
            if directory.exists():
                directory /= "snapshot-" + uuid.uuid4().hex
            try:
                item = workspace.provision(Path(project["source"]), directory, cancelled=token)
                provenance = {key: item[key] for key in ("kind", "revision", "exclusions")}
                self.store.attach_workspace(chat_id, str(item["directory"]), provenance)
            except Exception as exc:
                self.store.event(chat_id, "workspace.failed", {"error": str(exc)})
                raise
            return item

    def create_scan_chat(self, project_id: str) -> dict:
        with self._lock:
            for chat in self.store.chats(project_id):
                if not chat["archived"] and chat["model"].get("kind") == "scan_only":
                    return chat
            return self.create_chat(project_id, "Security review", SCAN_ONLY_MODEL, defer_workspace=True)

    def submit(self, chat_id: str, request_key: str, text: str, *, mode: str = "build") -> dict:
        if mode not in ("plan", "build") or not text.strip() or len(text) > 100000:
            raise StoreError("Choose Plan or Build and a message of 1 to 100000 characters.")
        model = self.store.get("chats", chat_id)["model"]
        if model.get("kind") == "scan_only":
            raise StoreError(SCAN_ONLY_MESSAGE)
        if model.get("tool_protocol") == "text":
            mode = "plan"
        result = self.store.submit(chat_id, request_key, "turn", {"text": text, "mode": mode})
        self._wake(chat_id)
        return result

    def submit_scan(self, chat_id: str, request_key: str, payload: dict) -> dict:
        if self.scan_handler is None:
            raise StoreError("Scan services are not available in this runtime configuration.")
        standalone = payload.get("scope") == "model_review" or _model_free_scan(payload)
        model = self.store.get("chats", chat_id)["model"]
        if not standalone:
            if model.get("kind") == "scan_only":
                raise StoreError(SCAN_ONLY_MESSAGE)
            if model.get("tool_protocol") == "text":
                raise StoreError("Choose a model with project tools before reviewing dependency source.")
        result = self.store.submit(chat_id, request_key, "scan", payload)
        self._wake(chat_id)
        return result

    def resume(self, chat_id, run_id):
        with self._lock:
            if self._stop.is_set() or not self.coding_turn:
                raise StoreError("The embedded coding runtime is unavailable.")
            run = self.store.resume(chat_id, run_id)
            self._cancel.pop(run_id, None)
            self._wake(chat_id)
        return run

    def _wake(self, chat_id: str):
        with self._lock:
            if self._stop.is_set():
                raise StoreError("The application is shutting down.")
            thread = self._threads.get(chat_id)
            if thread is None or not thread.is_alive():
                thread = threading.Thread(target=self._work, args=(chat_id,), name="projects-chat-" + chat_id[:8], daemon=True)
                self._threads[chat_id] = thread
                thread.start()

    def _phase(self, chat_id: str, run_id: str, phase: str):
        self.store.event(chat_id, "run.phase", {"phase": phase, "message": PHASE_MESSAGES[phase]}, run_id)

    def _runtime(self, chat: dict, run: dict | None = None, cancelled=None, on_phase=None):
        def phase(value):
            if self._stop.is_set() or (cancelled is not None and cancelled.is_set()):
                raise Cancelled("The work was cancelled before the message was sent.")
            if self.store.get("chats", chat["id"])["model"] != chat["model"]:
                raise Cancelled("The chat model changed during runtime preparation.")
            if on_phase:
                on_phase(value)
            if run:
                self._phase(chat["id"], run["id"], value)
        phase("preparing_model")
        item, configuration = self._configuration(chat)
        with self._lock:
            prior = self._runtimes.get(chat["id"])
        process = getattr(prior[0], "process", None) if prior else None
        if prior and prior[2] == configuration and (process is None or process.poll() is None):
            return prior
        if prior:
            prior[0].stop()
            with self._lock:
                self._runtimes.pop(chat["id"], None)
        native = self.runtime_factory(item["root"], item["directory"] / "runtime", configuration=configuration)
        native.dependency_cache = self.root / "native-dependencies" / PINNED_VERSION
        native.dependency_sources = tuple(path for path in (self.root / "workspaces").glob("*/runtime/config/opencode")
                                          if path.resolve().is_relative_to(self.root))
        native.on_phase = phase
        phase("starting_runtime")
        with self._lock:
            if self._stop.is_set() or (cancelled is not None and cancelled.is_set()):
                raise Cancelled("The work was cancelled before runtime startup.")
            self._starting[chat["id"]] = native
        try:
            native.start()
            if run and getattr(native, "dependency_status", None):
                self.store.event(chat["id"], "native.dependencies", native.dependency_status, run["id"])
            phase("connecting_tools")
            if hasattr(native, "connect_tools") and any(value.get("enabled", True) for value in configuration.get("mcp", {}).values()):
                tools = native.connect_tools()
                if run:
                    self.store.event(chat["id"], "native.tools", {"servers": tools}, run["id"])
                unavailable = [name for name, value in configuration["mcp"].items() if value.get("enabled", True)
                               and (not isinstance(tools, dict) or tools.get(name, {}).get("status") != "connected")]
                if unavailable:
                    raise StoreError("Configured project tools could not connect: " + ", ".join(unavailable) + ". Check their configuration and retry preparation.")
            session = chat["native_session"]
            if not session:
                session = native.request("POST", "/session", {"title": "Projects chat " + chat["id"]})["id"]
                self.store.update_chat(chat["id"], native_session=session)
            if self._configuration(self.store.get("chats", chat["id"]))[1] != configuration:
                raise Cancelled("The project configuration changed during runtime preparation.")
            with self._lock:
                phase("connecting_tools")
                self._runtimes[chat["id"]] = (native, session, configuration)
            return native, session, configuration
        except BaseException:
            native.stop()
            raise
        finally:
            with self._lock:
                self._starting.pop(chat["id"], None)

    def _work(self, chat_id: str):
        try:
            while not self._stop.is_set():
                queued = next((row for row in self.store.runs(chat_id) if row["state"] == "queued"), None)
                if queued is None:
                    return
                with self._lock:
                    cancelled = self._cancel.setdefault(queued["id"], threading.Event())
                chat = self.store.get("chats", chat_id)
                local_review = queued["kind"] == "scan" and queued["payload"].get("scope") == "model_review"
                key = "local-gateway" if local_review or chat["model"].get("kind") == "local" else "hosted:" + chat["model"].get("endpoint", "")
                try:
                    capacity = (nullcontext() if queued["kind"] == "scan" and _model_free_scan(queued["payload"]) else
                                self.resources.acquire(key, cancelled, on_wait=lambda: self.store.event(chat_id, "run.waiting_resource", {}, queued["id"])))
                    with self.resources.acquire("chat:" + chat_id, cancelled), capacity as hold:
                        with self._lock:
                            self._holds[chat_id] = {"capacity": hold}
                            current = self.store.get("chats", chat_id)
                            if current["model"] != chat["model"]:
                                continue
                            run = self.store.claim(chat_id, queued["id"])
                        if run is None:
                            continue
                        if cancelled.is_set() or self._stop.is_set():
                            raise Cancelled("The work was cancelled before execution.")
                        chat = current
                        if run["kind"] == "turn" and self.coding_turn and chat["model"].get("tool_protocol") != "text":
                            self.store.save_checkpoint(run["id"], model=chat["model"])
                        if not chat["workspace"] and (run["kind"] == "scan" or chat["model"].get("tool_protocol") != "text"):
                            self._phase(chat_id, run["id"], "preparing_workspace")
                            item = self.ensure_workspace(chat_id, cancelled)
                            chat = {**chat, "workspace": str(item["directory"])}
                            if cancelled.is_set() or self._stop.is_set():
                                raise Cancelled("The work was cancelled while preparing the workspace.")
                        if run["kind"] == "scan":
                            self._phase(chat_id, run["id"], "scanning")
                            result = self.scan_handler(chat, run, cancelled)
                        elif chat["model"].get("tool_protocol") == "text":
                            with self.admitted(chat_id, chat["model"]):
                                result = self._text_turn(chat, run, cancelled)
                        elif self.coding_turn:
                            with self.admitted(chat_id, chat["model"]):
                                result = self._embedded_turn(chat, run, cancelled)
                        else:
                            binding = self._runtime(chat, run, cancelled)
                            with self.admitted(chat_id, chat["model"]):
                                result = self._turn(chat, run, cancelled, *binding)
                        state = "interrupted" if cancelled.is_set() or self._stop.is_set() else "completed"
                        self.store.transition(run["id"], state, result)
                except Cancelled as exc:
                    self._finish_error(queued["id"], "interrupted", str(exc))
                except Exception as exc:
                    self._finish_error(queued["id"], "interrupted" if cancelled.is_set() else "failed", str(exc))
                finally:
                    with self._lock:
                        self._holds.pop(chat_id, None)
                        live = self._live.get(chat_id)
                        if live and live[0] == queued["id"]:
                            self._live.pop(chat_id)
                            self._bump_stream(chat_id)
        finally:
            # A submit can arrive as an idle worker exits. Recheck under the same
            # lock used by _wake so queued work cannot become stranded.
            with self._lock:
                self._threads.pop(chat_id, None)
                if not self._stop.is_set() and any(row["state"] == "queued" for row in self.store.runs(chat_id)):
                    self._wake(chat_id)

    def _finish_error(self, run_id: str, state: str, message: str, *, result=None):
        run = self.store.get("runs", run_id)
        if run["state"] not in TERMINAL:
            if result is None:
                result = {"error": message, "automatic_retry": False}
                with self._lock:
                    live = self._live.get(run["chat_id"])
                    if live and live[0] == run_id:
                        result["text_messages"] = live[1]
                if self._stop.is_set() and state == "interrupted" and self.store.checkpoint(run_id):
                    result["reason"] = "application_restart"
            try:
                self.store.transition(run_id, state, result)
            except StoreError:
                # Cancellation/shutdown may settle the run after the read above.
                # Preserve that durable outcome without retrying the work.
                if self.store.get("runs", run_id)["state"] not in TERMINAL:
                    raise

    def _bump_stream(self, chat_id):
        self._stream_generation[chat_id] = self._stream_generation.get(chat_id, 0) + 1

    @property
    def stopping(self):
        return self._stop.is_set()

    def stream_revision(self, chat_id):
        with self._lock:
            live = self._stream_generation.get(chat_id, 0)
        return self.store.latest_sequence(chat_id), live

    def _live_turn(self, chat, run, saved=None):
        started = time.time_ns() // 1_000_000
        messages = [{"info": {"id": run["id"] + ":" + role, "role": role, "time": {"created": started}}, "parts": []}
                    for role in ("user", "assistant")]
        messages[0]["parts"] = [{"type": "text", "text": run["payload"]["text"]}]
        if saved:
            messages = saved
        elif run["payload"].get("mode") == "plan":
            messages[1]["info"]["agent"] = "plan"

        def publish(parts):
            messages[1]["parts"] = parts
            with self._lock:
                self._live[chat["id"]] = (run["id"], copy.deepcopy(messages))
                self._bump_stream(chat["id"])

        publish(messages[1]["parts"])
        return messages, publish

    def _embedded_turn(self, chat, run, cancelled):
        chat_id, run_id = chat["id"], run["id"]
        self._phase(chat_id, run_id, "preparing_context")
        checkpoint = self.store.checkpoint(run_id) or {}
        history = [m for m in self.detail(chat_id)["messages"] if not m["info"]["id"].startswith(run_id + ":")]
        saved = copy.deepcopy(checkpoint.get("messages"))
        if saved:
            for part in saved[1]["parts"]:
                if part["type"] == "tool" and part["state"]["status"] == "running":
                    part["state"]["status"] = "interrupted"
        messages, publish = self._live_turn(chat, run, saved)
        self.store.save_checkpoint(run_id, model=chat["model"], messages=messages)
        receipted = {part["callID"] for part in messages[1]["parts"] if part["type"] == "tool"}
        effects = dict(checkpoint.get("effects", {}))
        last_save, signature = 0.0, None

        def record_effect(key, item):
            effects[key] = {**item, "recorded_at": stamp()}
            self.store.save_checkpoint(run_id, effects=effects)

        def publish_and_record(parts):
            nonlocal last_save, signature
            publish(parts)
            observations = observed_tools(parts)
            now = time.monotonic()
            marker = [(part.get("callID"), part["state"]["status"]) for part in parts if part["type"] == "tool"]
            if marker != signature or now - last_save >= 0.75:
                self.store.save_checkpoint(run_id, messages=messages, observations=observations)
                last_save, signature = now, marker
            for part in parts:
                if part.get("type") != "tool":
                    continue
                call_id = part["callID"]
                state = part["state"]
                if call_id not in receipted and state["status"] in ("completed", "error", "denied"):
                    validation = {"completed": "execution_observed", "error": "execution_failed", "denied": "execution_denied"}
                    self.store.event(chat_id, "tool.execution", {"tool": part["tool"], "call_id": call_id, "state": state,
                                     "runtime": "pydantic_ai", "validation": validation[state["status"]]}, run_id)
                    receipted.add(call_id)

        phase = lambda value: self._phase(chat_id, run_id, value)
        def checkpoint_context():
            data = self.store.checkpoint(run_id) or {}
            data["decisions"] = self.store.recent_decisions(chat_id)
            return plan_for_checkpoint(self.store, chat_id), data

        def read_progress(section):
            plan, data = checkpoint_context()
            if section == "tasks":
                return {"todo": plan}
            if section == "decisions":
                return {"decisions": [{"question": q["question"], "answer": a[0]}
                                      for row in data["decisions"] for q, a in zip(row["request"]["questions"], row["answers"])]}
            return {"effects": sorted(data.get("effects", {}).values(), key=lambda item: item["recorded_at"])[-8:],
                    "recent_tools": (data.get("observations") or [])[-4:]}

        phase("generating")
        parts = self.coding_turn(chat, run, history, publish_and_record, cancelled,
                                 lambda label: self._approve(chat_id, run_id, label, cancelled), phase,
                                 ask=lambda question: self._ask(chat_id, run_id, question, cancelled),
                                 brief=lambda: guidance(*checkpoint_context()), read_progress=read_progress,
                                 model_checkpoint=lambda entries: self.store.save_checkpoint(run_id, model_history=entries),
                                 record_effect=record_effect, saved=checkpoint)
        publish(parts)
        result = {"text_messages": messages, "runtime": "pydantic_ai", "model": chat["model"]}
        self.store.save_checkpoint(run_id, finished=result, messages=messages, model_history=None)
        return result

    @contextmanager
    def admitted(self, chat_id, model):
        """Admit a turn to its model (the local model lease) and remember the admission for waits on the user."""
        with self.admit_turn(model) if self.admit_turn else nullcontext() as admission:
            with self._lock:
                self._holds.setdefault(chat_id, {})["admission"] = admission
            try:
                yield admission
            finally:
                with self._lock:
                    (self._holds.get(chat_id) or {}).pop("admission", None)

    @contextmanager
    def _user_wait(self, chat_id):
        """While a turn waits for an approval or a planning answer, give up the turn lease and the local model
        capacity so other chats and an exclusive GPU job can proceed; take both back (and re-check the model) after."""
        with self._lock:
            held = dict(self._holds.get(chat_id) or {})
        admission, capacity = held.get("admission"), held.get("capacity")
        with ExitStack() as stack:
            if admission is not None and hasattr(admission, "paused"):
                stack.enter_context(admission.paused())
            if capacity is not None and hasattr(capacity, "released"):
                stack.enter_context(capacity.released())
            yield

    async def _ask(self, chat_id, run_id, question, cancelled):
        loop = asyncio.get_running_loop()
        response = loop.create_future()
        request = {"id": uuid.uuid4().hex, "questions": [question]}
        pending = {"permissions": [], "questions": [request], "run_id": run_id, "answer": (loop, response)}
        with self._lock:
            if cancelled.is_set() or self._stop.is_set():
                raise Cancelled("The planning turn was cancelled.")
            self.store.save_question(run_id, request)
            self._pending[chat_id] = pending
        try:
            with self._user_wait(chat_id):
                answer = await response
            return answer[0][0]
        finally:
            with self._lock:
                if self._pending.get(chat_id) is pending:
                    self._pending.pop(chat_id)

    async def _approve(self, chat_id, run_id, label, cancelled):
        loop = asyncio.get_running_loop()
        response = loop.create_future()
        request = {"id": uuid.uuid4().hex, "permission": "Run Python tests", "patterns": [label]}
        pending = {"permissions": [request], "questions": [], "run_id": run_id, "response": (loop, response)}
        with self._lock:
            if cancelled.is_set() or self._stop.is_set():
                raise Cancelled("The coding turn was cancelled before approval.")
            self._pending[chat_id] = pending
            self.store.transition(run_id, "waiting_approval")
        try:
            with self._user_wait(chat_id):
                return await response
        finally:
            with self._lock:
                if self._pending.get(chat_id) is pending:
                    self._pending.pop(chat_id)
                if not cancelled.is_set() and self.store.get("runs", run_id)["state"] == "waiting_approval":
                    self.store.transition(run_id, "running")

    def _text_turn(self, chat, run, cancelled):
        if self.text_turn is None:
            raise StoreError("Text chat is unavailable.")
        self._phase(chat["id"], run["id"], "generating")
        prompt = run["payload"]["text"]
        from .conversation_context import transcript_messages
        history = transcript_messages(self.detail(chat["id"])["messages"])
        messages, publish = self._live_turn(chat, run)
        chunks = []

        def receive(chunk):
            if not chunks:
                self._phase(chat["id"], run["id"], "receiving_response")
            chunks.append(chunk)
            publish([{"type": "text", "text": "".join(chunks)}])

        answer = self.text_turn(chat, [*history, {"role": "user", "content": prompt}], cancelled, receive)
        if cancelled.is_set():
            raise Cancelled("The text turn was cancelled.")
        publish([{"type": "text", "text": answer}])
        return {"text_messages": messages}

    def _turn(self, chat, run, cancelled, native, session, configuration):
        waiting, paused = ExitStack(), False
        try:
            self._phase(chat["id"], run["id"], "preparing_context")
            prior = native.request("GET", f"/session/{session}/message")
            prior_ids = {m.get("info", {}).get("id") for m in prior}
            self.store.event(chat["id"], "turn.provenance", {"model": chat["model"], "runtime_version": PINNED_VERSION,
                "configuration_sha256": hashlib.sha256(encode(configuration).encode()).hexdigest(),
                "effective_instructions": configuration.get("agent", {}).get(run["payload"]["mode"], {}).get("prompt"),
                "permissions": configuration.get("permission", {}),
                "extensions": self.describe_config(configuration) if self.describe_config else []}, run["id"])
            # The application sends once. An uncertain transport outcome is never retried.
            from .chat_context import context_for_turn
            parts = [{"type": "text", "text": run["payload"]["text"]}]
            source_context = context_for_turn(chat["id"], self.store)
            if source_context:
                parts.append({"type": "text", "text": source_context})
            prompt = {"agent": run["payload"]["mode"], "parts": parts}
            # Native sessions retain their last model. Always select the current
            # chat model explicitly so a switch applies to future turns only.
            selected_model = configuration.get("model", "")
            if "/" in selected_model:
                provider, model_id = selected_model.split("/", 1)
                prompt["model"] = {"providerID": provider, "modelID": model_id}
            if cancelled.is_set() or self._stop.is_set():
                raise Cancelled("The work was cancelled before the message was sent.")
            self._phase(chat["id"], run["id"], "submitting_prompt")
            native.request("POST", f"/session/{session}/prompt_async", prompt)
            self._phase(chat["id"], run["id"], "generating")
            observed_phase = "generating"
            previous, receipted = None, set()
            while True:
                if cancelled.is_set() or self._stop.is_set():
                    native.request("POST", f"/session/{session}/abort")
                    messages = native.request("GET", f"/session/{session}/message")
                    native.stop()
                    with self._lock:
                        self._runtimes.pop(chat["id"], None)
                    return {"messages": messages, "automatic_retry": False}
                process = getattr(native, "process", None)
                if process is not None and process.poll() is not None:
                    raise StoreError("The native runtime stopped. Inspect changes before retrying work.")
                with self._lock:
                    replies = self._native_replies.pop(chat["id"], [])
                if replies:
                    # Take the lease and the model back before the runtime may call the model again: an exclusive
                    # GPU job that started during the wait then refuses the turn instead of seeing a reload (RR-1).
                    if paused:
                        paused = False
                        waiting.close()
                    for path, body in replies:
                        native.request("POST", path, body)
                messages = native.request("GET", f"/session/{session}/message")
                permissions = [p for p in native.request("GET", "/permission") if p.get("sessionID") == session]
                questions = [q for q in native.request("GET", "/question") if q.get("sessionID") == session]
                with self._lock:
                    self._pending[chat["id"]] = {"permissions": permissions, "questions": questions, "run_id": run["id"]}
                state = "waiting_approval" if permissions else "waiting_input" if questions else "running"
                if state != "running" and not paused:
                    waiting.enter_context(self._user_wait(chat["id"]))
                    paused = True
                elif state == "running" and paused:
                    paused = False
                    waiting.close()
                current = self.store.get("runs", run["id"])
                if current["state"] in TERMINAL:
                    cancelled.set()
                    continue
                if current["state"] != state:
                    self.store.transition(run["id"], state)
                snapshot = {"messages": messages, "permissions": permissions, "questions": questions}
                fingerprint = hashlib.sha256(encode(snapshot).encode()).hexdigest()
                if fingerprint != previous:
                    self.store.event(chat["id"], "native.snapshot", snapshot, run["id"])
                    previous = fingerprint
                for message in messages:
                    if message.get("info", {}).get("id") in prior_ids:
                        continue
                    for part in message.get("parts", []):
                        tool_state = part.get("state", {})
                        identity = part.get("id") or part.get("callID")
                        if part.get("type") == "tool" and identity and identity not in receipted and tool_state.get("status") in ("completed", "error"):
                            self.store.event(chat["id"], "tool.execution", {"tool": part.get("tool"), "call_id": identity,
                                "state": tool_state, "runtime_version": PINNED_VERSION,
                                "validation": "execution_observed" if tool_state.get("status") == "completed" else "execution_failed"}, run["id"])
                            receipted.add(identity)
                assistants = [m for m in messages if m.get("info", {}).get("role") == "assistant" and m.get("info", {}).get("id") not in prior_ids]
                parts_now = assistants[-1].get("parts", []) if assistants else []
                phase = state if state != "running" else "executing_tool" if any(
                    part.get("type") == "tool" and part.get("state", {}).get("status") in ("pending", "running")
                    for part in parts_now) else "receiving_response" if any(
                    part.get("type") in ("text", "reasoning") and part.get("text") for part in parts_now) else "generating"
                if phase != observed_phase:
                    self._phase(chat["id"], run["id"], phase)
                    observed_phase = phase
                if any(m.get("info", {}).get("error") for m in assistants):
                    raise StoreError("The selected model/runtime returned an error: " + json.dumps(assistants[-1]["info"].get("error")))
                status = native.request("GET", "/session/status").get(session, {"type": "idle"})
                if assistants and not permissions and not questions and status.get("type") == "idle":
                    final = assistants[-1]["info"]
                    if not final.get("summary") and final.get("time", {}).get("completed") and final.get("finish") == "stop":
                        return {"messages": messages, "runtime_version": PINNED_VERSION, "model": chat["model"]}
                cancelled.wait(0.3)
        except BaseException:
            # Closing this chat's private job ends descendants even after a crash.
            native.stop()
            with self._lock:
                self._runtimes.pop(chat["id"], None)
            raise
        finally:
            # Ending while waiting on the user: unwind without taking the model back.
            if paused:
                waiting.__exit__(Cancelled, Cancelled("The turn ended while waiting for the user."), None)
            with self._lock:
                self._pending.pop(chat["id"], None)
                self._native_replies.pop(chat["id"], None)

    def reply(self, chat_id: str, request_id: str, *, approval: str | None = None, answers=None):
        with self._lock:
            pending = self._pending.get(chat_id)
            binding = self._runtimes.get(chat_id)
            if not pending:
                recovery = self.store.recovery(chat_id) if answers is not None else None
                if recovery and not self._stop.is_set():
                    return self.store.answer_question(recovery["run_id"], request_id, answers)
                raise StoreError("The request is stale or was interrupted. It cannot be approved or replayed.")
            if self._stop.is_set() or self._cancel.get(pending["run_id"], threading.Event()).is_set():
                raise StoreError("This run was cancelled. Its approvals and answers cannot be replayed.")
            if "answer" in pending:
                if answers is None:
                    raise StoreError("This request expects a planning answer.")
                accepted = self.store.answer_question(pending["run_id"], request_id, answers)
                loop, response = pending["answer"]
                def deliver():
                    if not response.done():
                        response.set_result(accepted)
                loop.call_soon_threadsafe(deliver)
                self._pending.pop(chat_id, None)
                return True
            if "response" in pending:
                if pending.get("answered") or approval not in ("once", "reject") or pending["permissions"][0]["id"] != request_id:
                    raise StoreError("This permission does not belong to the active chat run.")
                loop, response = pending["response"]
                pending["answered"] = True

                def settle():
                    if not response.done():
                        response.set_result(approval == "once")

                loop.call_soon_threadsafe(settle)
                return True
            if not binding:
                raise StoreError("The request is stale or was interrupted. It cannot be approved or replayed.")
            queued = self._native_replies.setdefault(chat_id, [])
            if any(path.split("/")[2] == request_id for path, _ in queued):
                raise StoreError("This request was already answered.")
            # The turn's poll loop forwards the answer after it has taken the model back (review RR-1).
            if approval is not None:
                if approval not in ("once", "reject") or not any(p["id"] == request_id for p in pending["permissions"]):
                    raise StoreError("This permission does not belong to the active chat run.")
                queued.append((f"/permission/{request_id}/reply", {"reply": approval}))
                return True
            if not any(q["id"] == request_id for q in pending["questions"]):
                raise StoreError("This question does not belong to the active chat run.")
            queued.append((f"/question/{request_id}/reply", {"answers": answers}))
            return True

    def cancel(self, chat_id: str, run_id: str) -> dict:
        run = self.store.get("runs", run_id)
        if run["chat_id"] != chat_id:
            raise StoreError("That run belongs to a different chat.")
        with self._lock:
            self._cancel.setdefault(run_id, threading.Event()).set()
            pending = self._pending.get(chat_id)
            if pending and pending["run_id"] == run_id:
                self._pending.pop(chat_id, None)
                self._native_replies.pop(chat_id, None)
            binding =self._runtimes.get(chat_id) if run["state"] in ("running", "waiting_approval", "waiting_input") else None
            starting = self._starting.get(chat_id) if run["state"] in ("running", "waiting_approval", "waiting_input") else None
            if binding and hasattr(binding[0], "reject_commands"):
                binding[0].reject_commands(binding[1])
        if starting:
            starting.stop()
        if run["state"] == "queued":
            self._finish_error(run_id, "interrupted", "The queued work was cancelled.", result={"reason": "cancelled_before_execution"})
        return self.store.get("runs", run_id)

    def detail(self, chat_id: str) -> dict:
        chat = self.store.get("chats", chat_id)
        with self.store.connect() as connection:
            row = connection.execute("SELECT payload FROM events WHERE chat_id=? AND type='native.snapshot' ORDER BY sequence DESC LIMIT 1", (chat_id,)).fetchone()
            usage_row = connection.execute("SELECT payload FROM events WHERE chat_id=? AND type='context.updated' ORDER BY sequence DESC LIMIT 1", (chat_id,)).fetchone()
            phases = connection.execute("SELECT run_id,type,timestamp,payload FROM events WHERE chat_id=? AND type IN ('run.phase','run.waiting_resource') ORDER BY sequence", (chat_id,)).fetchall()
        snapshot = json.loads(row["payload"]) if row else {"messages": [], "permissions": [], "questions": []}
        with self._lock:
            active = self._pending.get(chat_id)
            live = self._live.get(chat_id)
        latest = {}
        for event in phases:
            latest[event["run_id"]] = {**json.loads(event["payload"]), "started_at": event["timestamp"],
                **({"phase": "waiting_resource"} if event["type"] == "run.waiting_resource" else {})}
        runs, activity = self.store.runs(chat_id), None
        for run in runs:
            observed = latest.get(run["id"], {})
            phase = run["state"] if run["state"] in TERMINAL | {"waiting_approval", "waiting_input"} else observed.get("phase", run["state"])
            changed_state = run["state"] in TERMINAL | {"waiting_approval", "waiting_input"}
            run.update(phase=phase, phase_started_at=run["updated_at"] if changed_state else observed.get("started_at", run["updated_at"]))
            if activity is None and run["state"] not in TERMINAL:
                activity = {"phase": phase, "message": PHASE_MESSAGES.get(phase, "Running the selected work."),
                            "run_id": run["id"], "started_at": run["phase_started_at"]}
        plain = [message for run in runs if isinstance(run.get("result"), dict)
                 for message in run["result"].get("text_messages", [])]
        latest_turn = next((run for run in reversed(runs) if run["kind"] == "turn"), None)
        checkpoint = self.store.checkpoint(latest_turn["id"]) if latest_turn else None
        recovery = self.store.recovery(chat_id) if latest_turn and latest_turn["state"] == "interrupted" else None
        if live and not any(run["id"] == live[0] and run["state"] in TERMINAL for run in runs):
            plain.extend(live[1])
        elif checkpoint and latest_turn["state"] in {"interrupted", "queued", "running"} and not (latest_turn["result"] or {}).get("text_messages"):
            plain.extend(checkpoint.get("messages", []))
        messages = snapshot["messages"]
        if plain:
            messages = sorted([*messages, *plain], key=lambda message: message["info"].get("time", {}).get("created", 0))
        usage = json.loads(usage_row["payload"]) if usage_row else None
        if usage and (usage["model_id"] != chat["model"].get("id") or usage["window_tokens"] != chat["model"].get("context")):
            usage = None
        plan = plan_for_checkpoint(self.store, chat_id) if checkpoint else []
        progress = ({"saved_at": checkpoint["saved_at"], "mode": latest_turn["payload"]["mode"], "state": latest_turn["state"],
                     "items": plan, "last_tool": (checkpoint.get("observations") or [None])[-1]} if checkpoint else None)
        return {"chat": chat, "runs": runs, "activity": activity, "messages": messages, "context_usage": usage, "progress": progress,
                "recovery": recovery, "permissions": active["permissions"] if active else [],
                "questions": active["questions"] if active else [recovery["question"]] if recovery and recovery["question"] else []}

    def close(self):
        self._stop.set()
        with self._lock:
            for event in self._cancel.values():
                event.set()
            for job in self._runtime_jobs.values():
                job["cancelled"].set()
            bindings = list(self._runtimes.values())
            starting = list(self._starting.values())
            threads = list(self._threads.values()) + list(self._runtime_threads)
        for native, _, _ in bindings:
            native.stop()
        for native in starting:
            native.stop()
        deadline = time.monotonic() + 5
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
        self.store.reconcile()
