"""``EngineTurn``: the agent loop with a durable run under it.

It is ``AgentTurn`` (the loop, its prompts, its guards and its tools are not copied) with the hooks the loop offers filled in:

* a RUN is opened for the owner's instruction, or continued when the instruction already has one (after an approval, a restart or a stop), with the budget it has left: rounds, active seconds, tool
  calls, tokens (``budget.py``, ``run.py``). The last model round of a run that has used its budget is asked for with no tools, so that what was found is written down;
* a LEASE says which process works on the session, is renewed while it does, and tells the next run if the last one did not end (``lease.py``);
* every tool call is written to the JOURNAL before it runs and its result before the result is added to the transcript (``journal.py``), so that a restart can tell what was in flight and does not
  run a call that already ran a second time;
* every event is stamped with the run, its place and the time, and paired with its structured event (``events.py``);
* a stop says WHY (``stop_reason``) and what state the run is left in.

Chosen per session with ``params.engine`` (``"v2"``); ``"legacy"`` (the default until the benchmark says otherwise) is the loop alone.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Iterator

import posixpath

from app.backend import sandbox_files
from app.backend.pipeline import agent_sessions
from app.backend.pipeline.agent_loop import AgentTurn
from app.backend.pipeline.agentic import codeintel_tools, diagnostics, permissions
from app.backend.pipeline.agentic.engine import budget as bud, context as ctx_mod, journal as jr, lease, remediation, retry
from app.backend.pipeline.agentic.engine.errors import AgentSessionError
from app.backend.pipeline.agentic.engine.events import Emitter
from app.backend.pipeline.agentic.engine.run import RunRecord
from app.backend.pipeline.agentic.engine.task import steps_from_text
from app.backend.pipeline.agentic.engine.taskflow import TaskFlow
from app.backend.pipeline.agentic.tools import spec


def file_hash(ws, container_id: str | None, path: str | None) -> str | None:
    """The sha-256 of a file of the sandbox as it is now, or None when it is not there (or cannot be read)."""
    if not path or container_id is None:
        return None
    try:
        _label, full, _rel = ws.resolve(path)
        _, data = sandbox_files.download(container_id, full)
    except Exception:  # ignored: a path that does not resolve or a file that is not there is "no file", which is a state too
        return None
    return hashlib.sha256(data).hexdigest()


class Recorder:
    """What one tool call writes to the journal, for the loop (``EngineTurn``) and for the approval of a pending call (``agent_loop.approve``): the intent with the hash of the file a write is about
    to change, and the result with the hash after it."""

    def __init__(self, journal: jr.Journal, ws, container_id: str | None):
        self.journal, self.ws, self.container_id = journal, ws, container_id
        self._pre: dict[str, dict] = {}

    def _paths(self, name: str, arguments: dict | None) -> list[str]:
        if jr.class_of(name) == "file_write":
            path = (arguments or {}).get("path")
            return [path] if path else []
        return []

    def intent(self, call_id: str, name: str, arguments_text: str, arguments: dict | None, cls: str | None = None) -> None:
        pre = {p: file_hash(self.ws, self.container_id, p) for p in self._paths(name, arguments)}
        self._pre[call_id] = pre
        self.journal.intent(call_id, name, arguments_text, pre or None, cls)

    def result(self, call_id: str, name: str, arguments: dict | None, ok: bool, summary: str, payload: str, display: str | None) -> list[dict]:
        """Writes the result record. Returns the ``file.modified`` events for the files this call changed."""
        pre = self._pre.pop(call_id, {})
        post = {p: file_hash(self.ws, self.container_id, p) for p in self._paths(name, arguments)} if ok else {}
        self.journal.result(call_id, name, ok, summary, payload, display, post or None)
        return [{"type": "file.modified", "path": p, "sha_before": pre.get(p), "sha_after": post[p], "call": call_id} for p in post if post[p] != pre.get(p)]


def recorder_for(project_id: str, session_id: str, ws, container_id: str | None) -> Recorder | None:
    """The recorder for the run the session is in, or None when it has none (a session of the legacy engine): the approval of its pending call is then not journaled."""
    meta = agent_sessions.store().meta(project_id, session_id)
    run_id = (meta or {}).get("run_id")
    if not run_id or agent_sessions.store().run_get(run_id) is None:
        return None
    return Recorder(jr.Journal(agent_sessions.store(), run_id), ws, container_id)


def settle_from_journal(project_id: str, session_id: str) -> int:
    """Before a NEW instruction closes the calls the last round left without a result: the ones the journal has a result for are given that result, and the ones it has an intent for (they started and may
    have run) are given a note that says so. Without this the generic note of ``close_dangling_calls`` ("interrupted before it ran, so it did not run") would be written for a call that ran, and the
    model would be told something false. Returns how many calls were settled."""
    journal = journal_for(project_id, session_id)
    if journal is None:
        return 0
    doc = agent_sessions.get(project_id, session_id)
    t = doc["transcript"]
    last = next((i for i in range(len(t) - 1, -1, -1) if t[i]["role"] == "assistant" and t[i].get("tool_calls")), None)
    if last is None:
        return 0
    answered = {m.get("tool_call_id") for m in t[last + 1:] if m["role"] == "tool"}
    waiting = (doc.get("pending_call") or {}).get("id")
    missing = [c for c in t[last]["tool_calls"] if c["id"] not in answered and c["id"] != waiting]
    if not missing:
        return 0
    states = jr.call_states(journal.all_records())
    end = last + 1
    while end < len(t) and t[end]["role"] == "tool":
        end += 1
    fresh = []
    for call in missing:
        state = states.get(call["id"]) or {}
        name = call["function"]["name"]
        if state.get("result"):
            rec = state["result"]
            fresh.append(agent_sessions.new_message("tool", rec.get("payload") or json.dumps({"summary": rec.get("summary", "")}), tool_call_id=call["id"], ok=bool(rec.get("ok", True)), name=name,
                                                    summary=rec.get("summary", "recovered"), **({"display": rec["display"]} if rec.get("display") else {})))
        elif state.get("intent"):
            note = "This call was running when the app stopped, so it may have run, wholly or in part. Check what it was meant to change before relying on it."
            fresh.append(agent_sessions.new_message("tool", json.dumps({"error": note}), tool_call_id=call["id"], ok=False, name=name, summary="Interrupted: it may have run."))
    if fresh:
        agent_sessions.store().insert_at(project_id, session_id, end, fresh, set_={"updated_at": agent_sessions._stamp()})
    return len(fresh)


def journal_for(project_id: str, session_id: str) -> jr.Journal | None:
    """The journal of the run a session is in (or was last in), or None when it has none."""
    store = agent_sessions.store()
    meta = store.meta(project_id, session_id) or {}
    run_id = meta.get("run_id")
    if not run_id or store.run_get(run_id) is None:
        return None
    return jr.Journal(store, run_id)


REMEDIATION_DEFAULT = True                         # params.flags.remediation = false turns verify_fix off (an ablation of the benchmark)
ROLLBACK_AFTER = 3                                 # edits of one file in a row that leave it unable to compile, before it is put back as it last compiled
CODEINTEL_DEFAULT = True                           # params.flags.codeintel = false turns the two tools off (an ablation of the benchmark)


class EngineTurn(AgentTurn):
    ENGINE = "v2"
    RETRY = retry.RetryPolicy()                      # how a request that failed for the server's or the network's reasons is tried again
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._base_rounds = self.max_rounds
        self.store = agent_sessions.store()
        self.run: RunRecord | None = None
        self.journal: jr.Journal | None = None
        self.emitter: Emitter | None = None
        self.recorder: Recorder | None = None
        self.flow: TaskFlow | None = None
        self._resumed = False
        self._seg = time.monotonic()                 # the start of the active time not yet stored
        self._seg_lock = threading.Lock()            # the lease's thread and the loop both store the active time
        self._beat: lease.Heartbeat | None = None
        self._learned_context: int | None = None     # what a server said its context is, in a refusal
        self._good: dict[str, bytes | None] = {}     # path -> the last content of the file that compiled (None: none is known)
        self._broken: dict[str, int] = {}            # path -> edits in a row that left it broken
        if self.agent is None and (self.params.get("flags") or {}).get("codeintel", CODEINTEL_DEFAULT):          # find_symbol and read_files: offered to the owner's session, not to a subagent
            self.tools = [*self.tools, *codeintel_tools.TOOLS]
            self.front = spec.ToolFront(self.tools)
            self._aliases = {a: t for a, t in spec.ALIASES.items() if t in self.front.schemas}
            self.ctx.v2 = True                       # project_graph says which files the run has changed since the index was built; scan_results carries the finding ids
        self._retry_policy = type(self).RETRY
        with_options = getattr(self.client, "with_options", None)
        if callable(with_options):                   # the SDK's own two retries would multiply with these: this engine retries by its own rules
            try:
                self.client = with_options(max_retries=0)
                self.adapter = type(self.adapter)(self.client, self.model)
            except Exception:  # ignored: a client that cannot be reconfigured keeps its own retries
                pass

    # -- the run's life ------------------------------------------------------------------------------------------------------------------------------------------------

    def _last_instruction(self) -> str | None:
        return next((m["id"] for m in reversed(self.session.get("transcript", [])) if m.get("role") == "user" and m.get("origin") != "agent"), None) or \
            next((m["id"] for m in reversed(self.session.get("transcript", [])) if m.get("role") == "user"), None)

    def _last_instruction_text(self) -> str:
        for m in reversed(self.session.get("transcript", [])):
            if m.get("role") == "user" and m.get("origin") != "agent":
                return str(m.get("content") or "")
        return ""

    def _begin(self) -> None:
        pid, sid = self.project["id"], self.session["id"]
        taken = lease.acquire(pid, sid)                                     # raises 409 when another process works on the session
        try:
            self.run, self._resumed = RunRecord.open(self.store, pid, sid, self._last_instruction(), self.ENGINE, bud.default_budget(self.mode, self._base_rounds, self.params))
            self.store.commit(pid, sid, set_={"run_id": self.run.id})
            self.journal = jr.Journal(self.store, self.run.id)
            self.emitter = Emitter(self.run.id)
            self.recorder = Recorder(self.journal, self.ws, self.container_id)
            self.max_rounds = self.run.remaining_rounds()
            self._seg = time.monotonic()
            self.flow = TaskFlow(self, self.store, self._last_instruction(), self._last_instruction_text())
            self.flow.start()
            if taken:
                self.journal.note("took_over", owner=taken.get("owner"), host=taken.get("host"), os_pid=taken.get("pid"))
            self.journal.note("started", resumed=self._resumed, boot=agent_sessions.BOOT, rounds_left=self.max_rounds)
            self._beat = lease.Heartbeat(pid, sid, on_beat=self._flush_time, on_lost=self.cancel).start()
        except BaseException:
            try:
                lease.release(pid, sid)                                     # whatever failed while the run was being set up, the session is not left held
            except Exception:  # ignored: the lease goes stale by itself
                pass
            raise

    def _flush_time(self) -> None:
        """Store the active time since the last time it was stored."""
        with self._seg_lock:
            now = time.monotonic()
            delta, self._seg = now - self._seg, now
            if self.run is not None and delta > 0:
                self.run.add(seconds=delta)

    def _end(self, last: dict | None) -> None:
        pid, sid = self.project["id"], self.session["id"]
        try:
            if self._beat is not None:
                self._beat.stop()
            self._flush_time()
            kind = (last or {}).get("type")
            paused, failed = kind == "pending_approval", kind == "error"
            cancelled = kind == "done" and bool(last.get("stopped"))
            reason = last.get("stop_reason") if kind == "done" else (f"error:{last.get('code') or 'failed'}" if failed else None)
            state = "interrupted" if kind not in ("done", "error", "pending_approval") else bud.state_for(reason, paused=paused, cancelled=cancelled, failed=failed)
            if state == "interrupted":
                reason = bud.PROCESS_LOST if kind is None else (reason or "interrupted")
            partial = self.flow.partial() if (self.flow is not None and state in ("exhausted", "failed", "cancelled", "interrupted")) else None
            self.run.set_state(state, reason, partial)
            if self.flow is not None:
                self.flow.end(state, self.flow.task.doc["completion"]["status"] != "open")
            self.journal.stop(reason or "", state)
            lease.release(pid, sid)
        except Exception:  # ignored: the run's own bookkeeping failing at its end (the store went away) must not hide how the run ended; the next run finds the lease stale and the run open
            pass

    def events(self) -> Iterator[dict]:
        if self.session.get("pending_call"):
            yield from super().events()
            return
        try:
            self._begin()
        except AgentSessionError as e:
            yield {"type": "error", "message": str(e), "code": e.code}
            return
        last = None
        try:
            for ev in super().events():
                last = ev
                yield from self.emitter.expand(ev, resumed=self._resumed)
        finally:
            self._end(last)

    # -- the hooks of the loop ---------------------------------------------------------------------------------------------------------------------------------------

    def _limit_nudge(self) -> str:
        return self.flow.limit_nudge(super()._limit_nudge()) if self.flow is not None else super()._limit_nudge()

    def _call(self, cid, prepared):
        """verify_fix is the engine's own tool (it needs the task); every other tool is the loop's."""
        if prepared.name == "verify_fix":
            return {"error": "Active vulnerability verification is unavailable in this distribution."}
            yield  # noqa: B901 - makes this a generator, as the loop expects
        path = (prepared.arguments or {}).get("path") if prepared.name in ("write_file", "edit_file") else None
        if path and self.container_id is not None and self.mode != "plan":
            self._remember_good(path)                 # before the first edit of a file: what it is now, if it compiles
        result = yield from super()._call(cid, prepared)
        if path and isinstance(result, dict) and self.container_id is not None and self.mode != "plan":
            result = self._after_edit(path, result)
        return result

    def _full(self, path: str) -> str | None:
        try:
            return self.ws.resolve(path)[1]
        except Exception:  # ignored: a path the tools refuse is not one this looks at
            return None

    def _remember_good(self, path: str) -> None:
        if path in self._good:
            return
        full = self._full(path)
        if full is None or diagnostics.command_for(full) is None:
            self._good[path] = None
            return
        try:
            _name, content = sandbox_files.download(self.container_id, full)
        except Exception:  # ignored: a file that is not there yet has no earlier version
            self._good[path] = None
            return
        problems = diagnostics.check(self.container_id, full)
        self._good[path] = content if problems == [] else None            # only a file known to parse is a version to go back to

    def _after_edit(self, path: str, result: dict) -> dict:
        """After a write: a file that parses is the new known-good version; one that does not is counted, and after ROLLBACK_AFTER in a row it is put back as it last parsed, with the model told."""
        full = self._full(path)
        if full is None or diagnostics.command_for(full) is None:
            return result
        if not result.get("problems"):
            self._broken[path] = 0
            try:
                self._good[path] = sandbox_files.download(self.container_id, full)[1]
            except Exception:  # ignored: the version is kept as it was
                pass
            return result
        self._broken[path] = self._broken.get(path, 0) + 1
        good = self._good.get(path)
        if self._broken[path] < ROLLBACK_AFTER or good is None:
            return result
        folder, name = posixpath.split(full)
        try:
            from app.backend.pipeline.scan import agent_sandbox
            sandbox_files.upload(self.container_id, folder, name, good, overwrite=True, uid=agent_sandbox.UID)
        except Exception:  # ignored: if the file cannot be put back, the model is left with the problem it was told of
            return result
        self._broken[path] = 0
        self.flow.task.observe("rollback", f"{path} was put back as it last compiled after {ROLLBACK_AFTER} edits that left it unable to")
        return {**result, "restored": True, "summary": f"{result.get('summary', path)} -- {path} was put back as it last compiled",
                "note": (f"{path} did not compile after {ROLLBACK_AFTER} edits in a row, so it was put back as it was when it last compiled. Make your change again in ONE edit_file call, "
                         "copying the lines you replace exactly as read_file shows them.")}

    # -- requests and context -----------------------------------------------------------------------------------------------------------------------------------------------------

    def _open(self, messages, use_tools, schema=False):
        """The request, tried again on the failures that are the server's or the network's (``retry.py``); a stop ends the waiting."""
        def note(attempt: int, seconds: float, error: BaseException) -> None:
            status = retry.status_of(error)
            self.emitter.push({"type": "notice", "text": f"The model server did not answer ({'status ' + str(status) if status else type(error).__name__}); trying again in {seconds:.0f} s (attempt {attempt + 1} of {self._retry_policy.tries})."})
            self.journal.note("retry", attempt=attempt, wait=round(seconds, 1), status=status, error=type(error).__name__)
        return retry.call(lambda: super(EngineTurn, self)._open(messages, use_tools, schema), self._retry_policy, sleep=self._cancelled.wait, on_retry=note)

    def _context_tokens(self) -> int | None:
        """The context to plan for. The local model's, as the gateway says; else what the model's profile knows; else what a server said when it refused a request; else a size smaller than most hosted
        models have. The legacy loop plans for none of these for a hosted model, and so resends the same too-long request when it is refused."""
        known = super()._context_tokens()
        if known:
            return known
        if self._learned_context:
            return self._learned_context
        if self.profile is not None and getattr(self.profile, "context", None):
            return int(self.profile.context)
        return ctx_mod.DEFAULT_HOSTED_CONTEXT

    def _hook_context_error(self, error: Exception) -> None:
        limit = ctx_mod.parse_context_limit(str(error) + " " + str(getattr(error, "body", "") or ""))
        if limit and (self._learned_context is None or limit < self._learned_context):
            self._learned_context = limit
            self.journal.note("learned_context", tokens=limit)

    def _guard_result(self, result: dict) -> dict:
        return ctx_mod.mask_result(result)

    def _hook_transcript(self, transcript: list[dict]) -> list[dict]:
        if (self.params.get("flags") or {}).get("fence_results"):
            return ctx_mod.fence_tool_messages(transcript, self.run.id if self.run is not None else self.session["id"])
        return transcript

    def _compaction_extras(self) -> dict:
        """The facts of the task go into every summary as the engine knows them (the objective, what the owner ruled out, the files changed, the last check), so that a summary a small model writes badly
        cannot lose them; the summary is kept with the session."""
        pid, sid = self.project["id"], self.session["id"]

        def keep(entry: dict) -> None:
            self.store.commit(pid, sid, set_={"context": entry})
        anchor = self.flow.task.digest() if self.flow is not None else ""
        return {"anchor": anchor, "persisted": (self.store.meta(pid, sid) or {}).get("context"), "persist": keep}

    def _hook_last_round(self) -> str | None:
        return self.flow.limit_nudge("(A note from the system, not from the owner: this run has used its rounds. Write your answer now from what you have: what you did, what you found, and what is still to do.)")

    def _hook_round_begin(self, round_no: int) -> str | None:
        if self.flow.failed_too_often():
            return bud.NO_PROGRESS                                   # round after round of the model's own mistakes: the run ends with what it has
        unflushed = time.monotonic() - self._seg
        why = self.run.over(unflushed)
        return None if why == bud.MAX_ROUNDS else why            # the number of rounds is the loop's own last round (``max_rounds``); the others end the work and ask for the answer

    def _hook_round_end(self, usage: dict | None) -> None:
        self.run.add(rounds=1, tokens=int((usage or {}).get("total_tokens") or 0))
        self.journal.round(int(self.run.spent.get("rounds", 0)))

    def _hook_call_intent(self, cid: str, prepared) -> None:
        cls = "read" if self.mode == "plan" and jr.class_of(prepared.name) == "exec" else None            # Plan's terminal is read-only by construction: a restart runs a command of it again
        self.recorder.intent(cid, prepared.name, prepared.text, prepared.arguments, cls)
        self.flow.on_intent(cid, prepared)

    def _hook_precheck(self, cid: str, prepared) -> str | None:
        return self.flow.precheck(cid, prepared) or self.flow.repeat_check(prepared)

    def _hook_call_result(self, cid: str, prepared, ok: bool, summary: str, payload: str, display: str | None) -> None:
        events = self.recorder.result(cid, prepared.name, prepared.arguments, ok, summary, payload, display)
        for event in events:
            self.emitter.push(event)
        self.run.add(tool_calls=1)
        self.flow.on_result(cid, prepared, ok, summary, payload, events)

    def _hook_answer(self, answer: str, use_tools: bool):
        return self.flow.answer(answer, use_tools)

    def _hook_verdict(self, cid: str, prepared, verdict):
        if cid in self._in_doubt:                                  # a call that may already have run: the owner decides, whatever the rules say
            permission, value = permissions.value_of(prepared.name, prepared.arguments)
            return permissions.Decision("ask", permission, value, self._in_doubt[cid], "recovery", [])
        if verdict is not None and verdict.action == "ask" and self.flow is not None and self.mode != "plan":          # a write that would be put to the owner is first held to the task's own rules
            refused = self.flow.precheck(cid, prepared)
            if refused:
                return permissions.Decision("deny", verdict.permission, verdict.value, refused, "engine", [])
        if prepared.name == "verify_fix" and verdict is not None and verdict.action == "ask":           # it needs the run's task, so it cannot wait for an approval and be run from outside the run
            return permissions.Decision("deny", verdict.permission, verdict.value, "verify_fix cannot wait for a single approval. Set the verify permission to allow or to deny in the rules.", "engine", [])
        if prepared.name == "propose_plan" and verdict is not None and verdict.action == "ask":          # the plan the owner is about to be asked about is the task's plan
            steps = steps_from_text(str((prepared.arguments or {}).get("plan") or ""))
            if steps and self.flow.task.set_plan(steps):
                self.emitter.push({"type": "task.planned", "task": self.flow.task.doc["id"], "steps": len(steps)})
            self.flow.task.set_state("awaiting_approval")
            self.flow.task.save()
        return verdict

    def _hook_resume(self, calls: list[dict] | None) -> list[dict] | None:
        """The calls the transcript left without a result: the ones the journal can finish or explain are written down, the ones that may have run are put to the owner, the rest are run as usual."""
        if not calls:
            return None
        states = jr.call_states(self.journal.all_records())
        remaining = []
        for call in calls:
            d = jr.decide(call, states, lambda p: file_hash(self.ws, self.container_id, p))
            cid, name = call["id"], call["function"]["name"]
            if d.action == "recovered":
                rec = d.record
                payload = rec.get("payload") or json.dumps({"summary": rec.get("summary", "")})
                self._append(agent_sessions.new_message("tool", payload, tool_call_id=cid, ok=bool(rec.get("ok", True)), name=name, summary=rec.get("summary", "recovered"),
                                                        **({"display": rec["display"]} if rec.get("display") else {})))
                self.journal.note("recovered", call=cid)
                self.emitter.push({"type": "tool", "id": cid, "name": name, "arguments": call["function"]["arguments"][:500], "ok": bool(rec.get("ok", True)), "summary": rec.get("summary", ""),
                                   "result": payload, "recovered": True})
            elif d.action == "note":
                payload = json.dumps({"error": d.text})
                self._append(agent_sessions.new_message("tool", payload, tool_call_id=cid, ok=False, name=name, summary="Interrupted: it may have run."))
                self.journal.note("interrupted", call=cid, why=d.text[:200])
                self.emitter.push({"type": "tool", "id": cid, "name": name, "arguments": call["function"]["arguments"][:500], "ok": False, "summary": "Interrupted: it may have run.", "result": payload,
                                   "recovered": True})
            elif d.action == "ask":
                self._in_doubt[cid] = d.text
                self.journal.note("in_doubt", call=cid, why=d.text[:200])
                remaining.append(call)
            else:
                remaining.append(call)
        return remaining or None
