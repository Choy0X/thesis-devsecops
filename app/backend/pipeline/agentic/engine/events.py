"""The events of a run, stamped and paired with the structured ones.

The agent loop yields the events the page has always been given (``start``, ``token``, ``tool_start``, ``tool``, ``pending_approval``, ``done`` ...). A run of the v2 engine puts three fields on
every one of them (``run_id``, ``seq`` and ``ts``, so that a client can tell a run from the next and a repeated event from a new one) and adds, next to the ones that mark a step, the
structured event for it: ``agent.started``, ``tool.started``, ``tool.completed`` or ``tool.failed``, ``file.modified``, ``agent.awaiting_approval``, ``agent.completed``, ``agent.failed`` and
``agent.cancelled`` (``task.planned``, ``validation.*`` and ``agent.replanning`` come with the task state). They carry identifiers and short facts, never the content of a result: the content
is in the ``tool`` event and in the session.

What the page relies on is kept: nothing is removed or renamed, an event type the page does not know is ignored by it, ``pending_approval`` is still the last event of its stream (its structured
twin comes BEFORE it), and ``done`` still ends a stream that finished (its twin comes before it too).
"""
from __future__ import annotations

from typing import Iterator

from app.backend.pipeline.agentic.engine.run import now_iso


class Emitter:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.seq = 0
        self.queued: list[dict] = []            # structured events a hook wants sent (``file.modified``): sent before the next event of the loop

    def push(self, event: dict) -> None:
        self.queued.append(event)

    def stamp(self, event: dict) -> dict:
        self.seq += 1
        return {**event, "run_id": self.run_id, "seq": self.seq, "ts": now_iso()}

    def expand(self, event: dict, *, resumed: bool = False) -> Iterator[dict]:
        """``event`` and its structured twin, stamped, in the order the stream must have them."""
        while self.queued:
            yield self.stamp(self.queued.pop(0))
        kind = event.get("type")
        if kind == "pending_approval":
            call = event.get("call") or {}
            yield self.stamp({"type": "agent.awaiting_approval", "id": call.get("id"), "name": call.get("name"), "permission": (event.get("permission") or {}).get("permission"),
                              "in_doubt": bool(event.get("in_doubt"))})
            yield self.stamp(event)
        elif kind == "done":
            reason = event.get("stop_reason")
            if event.get("stopped"):
                yield self.stamp({"type": "agent.cancelled", "stop_reason": reason or "cancelled"})
            else:
                yield self.stamp({"type": "agent.completed", "stop_reason": reason})
            yield self.stamp(event)
        else:
            yield self.stamp(event)
            if kind == "start":
                yield self.stamp({"type": "agent.started", "mode": event.get("mode"), "model": event.get("model_label") or event.get("model"), "resumed": resumed})
            elif kind == "tool_start":
                yield self.stamp({"type": "tool.started", "id": event.get("id"), "name": event.get("name")})
            elif kind == "tool":
                yield self.stamp({"type": "tool.completed" if event.get("ok") else "tool.failed", "id": event.get("id"), "name": event.get("name"), "summary": event.get("summary")})
            elif kind == "error":
                yield self.stamp({"type": "agent.failed", "message": event.get("message"), "code": event.get("code")})
