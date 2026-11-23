"""The record of a run: its state, its budget and what it has spent, stored in ``agent_runs`` (one document per run) so that it survives the request, the pause and the restart.

A run belongs to one instruction (``instruction_id``, the owner's message that started it). The first ``/run`` for an instruction opens it; every later ``/run`` for the same instruction (after an
approval, after a restart, after the owner pressed Stop and then continued) continues it, with the budget it has left. A new instruction opens a new run, and a run of an earlier instruction that
was left open is closed as interrupted.
"""
from __future__ import annotations

import datetime
import secrets
import threading

from app.backend.pipeline.agentic.engine import budget as bud


def now_iso() -> str:
    n = datetime.datetime.now(datetime.timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


class RunRecord:
    def __init__(self, store, doc: dict):
        self.store, self.doc = store, doc
        self._lock = threading.Lock()                    # the lease's thread and the loop both change the counters

    # -- opening -----------------------------------------------------------------------------------------------------------------------------------------------------

    @classmethod
    def open(cls, store, project_id: str, session_id: str, instruction_id: str | None, engine: str, budget: bud.Budget) -> tuple["RunRecord", bool]:
        """``(the run, whether it continues an earlier request of the same instruction)``. Older open runs of the session are closed as interrupted."""
        runs = store.runs_of(session_id)
        current = None
        for run in runs:
            if run["state"] in bud.OPEN_STATES:
                if current is None and run.get("instruction_id") == instruction_id and instruction_id is not None:
                    current = run
                else:
                    store.run_update(run["id"], set_={"state": "interrupted", "stop_reason": run.get("stop_reason") or bud.PROCESS_LOST, "updated_at": now_iso()})
        if current is not None:
            doc = store.run_update(current["id"], set_={"state": "running", "updated_at": now_iso()}, unset=["stop_reason"])
            return cls(store, doc), True
        doc = {"id": "r_" + secrets.token_hex(6), "session_id": session_id, "project_id": project_id, "instruction_id": instruction_id, "engine": engine, "state": "running",
               "n": len(runs) + 1, "started_at": now_iso(), "updated_at": now_iso(), "budget": budget.to_dict(), "spent": bud.new_spent(), "jseq": 0}
        store.run_insert(doc)
        return cls(store, doc), False

    # -- reading ------------------------------------------------------------------------------------------------------------------------------------------------------

    @property
    def id(self) -> str:
        return self.doc["id"]

    @property
    def budget(self) -> bud.Budget:
        return bud.Budget.from_dict(self.doc.get("budget"))

    @property
    def spent(self) -> dict:
        return self.doc.get("spent") or bud.new_spent()

    def remaining_rounds(self) -> int:
        return bud.remaining_rounds(self.budget, self.spent)

    def over(self, active_seconds: float = 0.0) -> str | None:
        return bud.over(self.budget, self.spent, active_seconds)

    # -- changing -----------------------------------------------------------------------------------------------------------------------------------------------------

    def _update(self, **kw) -> None:
        with self._lock:
            self.doc = self.store.run_update(self.id, **kw) or self.doc

    def add(self, *, rounds: int = 0, tool_calls: int = 0, tokens: int = 0, seconds: float = 0.0, failure: str | None = None) -> None:
        inc = {f"spent.{k}": v for k, v in (("rounds", rounds), ("tool_calls", tool_calls), ("tokens", tokens), ("active_seconds", round(seconds, 3))) if v}
        if failure:
            inc[f"spent.failures.{failure}"] = 1
        if inc:
            self._update(inc=inc, set_={"updated_at": now_iso()})

    def set_state(self, state: str, stop_reason: str | None = None, partial: dict | None = None) -> None:
        changes: dict = {"state": state, "updated_at": now_iso()}
        if stop_reason is not None:
            changes["stop_reason"] = stop_reason
        if partial is not None:
            changes["partial"] = partial
        self._update(set_=changes)

    def view(self) -> dict:
        """The run as the page and the API show it."""
        d = dict(self.doc)
        d.pop("jseq", None)
        return d
