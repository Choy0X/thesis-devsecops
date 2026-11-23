"""How much a model thinks before it acts: one choice for the session (off, low, medium, high, max), and what each level does for the model that is in use.

A model thinks in one of four ways (``Reasoning.kind`` of its profile). One whose chat template takes ``enable_thinking`` (``toggle``) is told to think or not, with a budget of tokens for it; one that
takes ``reasoning_effort`` (``effort``, the gpt-oss way) is told low, medium or high; one that opens ``<think>`` by itself (``always``) cannot be told not to, only how long (a budget); and one that does
not think at all (``none``: Qwen2.5-Coder, the model on this computer) is given a short planning pass of the app's own, a separate request of a bounded length before an action, whose text is shown as its
thinking. A model asked for a decision through a JSON schema cannot think inside that request (the grammar makes the first token the start of the decision), so it too gets the planning pass.

Nothing here talks to a model: it decides what to put in a request and whether a planning pass is due, from the profile and the level, so it can be tested without one.
"""
from __future__ import annotations

EFFORTS = ("off", "low", "medium", "high", "max")
DEFAULT_EFFORT = "off"
BUDGETS = {"low": 1024, "medium": 4096, "high": 12288, "max": None}            # tokens of thinking for a model that is told how long (None: no limit)
PLANNING_TOKENS = {"low": 128, "medium": 384, "high": 1024, "max": 2048}          # tokens of the planning pass for a model that does not think by itself
EFFORT_WORD = {"low": "low", "medium": "medium", "high": "high", "max": "high"}   # the three levels a template of the effort kind knows
MAX_PLANNING_PASSES = 6                                                           # in one run: the cost of a pass is paid on a small card, and a run that keeps failing should stop and not plan for ever


def clean(value) -> str:
    """A level as it is kept: one of ``EFFORTS``, anything else is the default."""
    v = str(value or "").strip().lower()
    return v if v in EFFORTS else DEFAULT_EFFORT


def levels(kind: str) -> list[str]:
    """The levels worth offering for a way of thinking: a model that is always thinking has no off, and one that only knows low, medium and high has no separate max."""
    if kind == "always":
        return ["low", "medium", "high", "max"]
    if kind == "effort":
        return ["off", "low", "medium", "high"]
    return list(EFFORTS)


def native(kind: str, protocol: str) -> bool:
    """Whether the model itself thinks at the level asked (its template is told), as against the planning pass of the app's own. Not for a model that does not think, nor for one asked for a
    decision through a schema."""
    return kind in ("toggle", "effort", "always") and protocol != "schema"


def request_extra(kind: str, protocol: str, effort: str) -> dict:
    """The fields to add to a request so that the model thinks at ``effort``; empty when it does not (or when the level is the model's own way)."""
    effort = clean(effort)
    if not native(kind, protocol):
        return {}
    if kind == "toggle":
        extra: dict = {"chat_template_kwargs": {"enable_thinking": effort != "off"}}
        if effort != "off" and BUDGETS[effort] is not None:
            extra["thinking_budget_tokens"] = BUDGETS[effort]
        return extra
    if kind == "effort":
        return {"reasoning_effort": EFFORT_WORD.get(effort, "low")}               # off is the lowest: such a model cannot be told not to think
    if kind == "always" and BUDGETS.get(effort) is not None:
        return {"thinking_budget_tokens": BUDGETS[effort]}
    return {}


def planning_tokens(kind: str, protocol: str, effort: str) -> int | None:
    """How long a planning pass of the app's own may be, or None when there is none: the model thinks by itself at this level, or the level is off."""
    effort = clean(effort)
    if effort == "off" or native(kind, protocol):
        return None
    return PLANNING_TOKENS[effort]


def planning_due(history: list[dict], passes: int) -> str | None:
    """Why a planning pass is due before the next round, or None. When the owner has just written (the start of a run), after a call that failed, and after a note about a call made again and
    again. A run has only so many."""
    if passes >= MAX_PLANNING_PASSES or not history:
        return None
    if history[-1].get("role") == "user":
        return "to begin with"
    last = history[-1]
    if last.get("role") != "tool":
        return None
    if last.get("ok") is False:
        return "after a call that failed"
    if '"note"' in (last.get("content") or "") and "this exact call" in (last.get("content") or ""):
        return "after a call that was made again and again"
    return None


PLANNING_PROMPT = ("Before you act, think about the next step in a short paragraph of plain words. What do you know so far, what is still missing, what will you do next (which tool, with what, or the "
                   "answer) and why? If the last result was an error, say what went wrong and what to change. Do not call a tool and do not write the answer now. Be brief.")
PLANNING_NOTE = "Your own thinking just before this step (use it, do not repeat it):\n{text}"
