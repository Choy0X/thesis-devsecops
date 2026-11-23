"""What a run may spend, and the reasons a run ends.

A run is the work on one instruction, from the owner's words to a stop, across any number of pauses for an approval. Its budget belongs to the run, not to one ``/run`` request: the number of
rounds, the active seconds (time spent waiting for the owner does not count), the tool calls and the tokens it has used so far are stored with the run, so an approval does not give it a fresh
start. When any of them is used up the run does not just stop: it asks the model for one last answer with no tools, so that what was found is written down, and it says why it stopped.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

# why a run ended (``agent_runs.stop_reason`` and the ``stop_reason`` of the ``done`` event)
ANSWERED = "answered"
MAX_ROUNDS = "max_rounds"
MAX_SECONDS = "max_seconds"
MAX_TOOL_CALLS = "max_tool_calls"
MAX_TOKENS = "max_tokens"
NO_PROGRESS = "no_progress"
LOOP = "loop"
CANCELLED = "cancelled"
GATE_UNMET = "gate_unmet"
REFUSED = "refused"                      # Plan mode was asked three times for what it cannot do
ERROR = "error"                           # followed by a code: "error:session_too_large"
PROCESS_LOST = "error:process_lost"       # the process that ran it ended without ending the run
BUDGET_STOPS = (MAX_ROUNDS, MAX_SECONDS, MAX_TOOL_CALLS, MAX_TOKENS)

STATES = ("running", "awaiting_approval", "completed", "failed", "cancelled", "exhausted", "interrupted")
OPEN_STATES = ("running", "awaiting_approval", "interrupted")           # a run in one of these can be continued by the next ``/run`` of its instruction
AUTO_SECONDS = 1800.0                    # what the Auto mode's run may work, not counting the time it waits for the owner
AUTO_TOOL_CALLS = 150


@dataclass(frozen=True)
class Budget:
    rounds: int
    seconds: float | None = None
    tool_calls: int | None = None
    tokens: int | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, raw: dict | None) -> "Budget":
        raw = raw or {}
        return cls(rounds=int(raw.get("rounds", 0)), seconds=raw.get("seconds"), tool_calls=raw.get("tool_calls"), tokens=raw.get("tokens"))


def default_budget(mode: str, rounds: int, params: dict | None = None) -> Budget:
    """The budget of a run in ``mode`` with ``rounds`` rounds. Auto also has a clock and a count of tool calls; a session can set its own (``params["budget"]``: any of ``seconds``, ``tool_calls``,
    ``tokens``, and ``rounds``, where ``0`` or ``None`` means no limit for the three that can be unlimited), so that a benchmark can run without the clock."""
    given = (params or {}).get("budget") or {}

    def pick(name, default):
        if name in given:
            return given[name] or None
        return default
    return Budget(rounds=int(given.get("rounds") or rounds), seconds=pick("seconds", AUTO_SECONDS if mode == "auto" else None), tool_calls=pick("tool_calls", AUTO_TOOL_CALLS if mode == "auto" else None),
                  tokens=pick("tokens", None))


def new_spent() -> dict:
    return {"rounds": 0, "tool_calls": 0, "tokens": 0, "active_seconds": 0.0, "failures": {"model": 0, "env": 0, "policy": 0}}


def over(budget: Budget, spent: dict, active_seconds: float | None = None) -> str | None:
    """The reason the run may not go on, or None. ``active_seconds`` is the time of this request on top of what is stored. The order is the order of what is cheapest to explain."""
    if budget.tool_calls is not None and spent.get("tool_calls", 0) >= budget.tool_calls:
        return MAX_TOOL_CALLS
    if budget.tokens is not None and spent.get("tokens", 0) >= budget.tokens:
        return MAX_TOKENS
    if budget.seconds is not None and spent.get("active_seconds", 0.0) + (active_seconds or 0.0) >= budget.seconds:
        return MAX_SECONDS
    if spent.get("rounds", 0) >= budget.rounds:
        return MAX_ROUNDS
    return None


def remaining_rounds(budget: Budget, spent: dict) -> int:
    return max(0, budget.rounds - int(spent.get("rounds", 0)))


def state_for(stop_reason: str | None, *, paused: bool = False, cancelled: bool = False, failed: bool = False) -> str:
    """The state a run is left in: paused for an approval, cancelled, failed, ended on a budget (``exhausted``) or finished."""
    if paused:
        return "awaiting_approval"
    if cancelled or stop_reason == CANCELLED:
        return "cancelled"
    if failed or (stop_reason or "").startswith(ERROR):
        return "failed"
    if stop_reason in BUDGET_STOPS or stop_reason in (NO_PROGRESS, LOOP, GATE_UNMET, REFUSED):
        return "exhausted"
    return "completed"
