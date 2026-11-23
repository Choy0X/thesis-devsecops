"""Bounded model context over a conversation's unchanged, durable transcript."""
from __future__ import annotations

import re

from .store import StoreError


_TERMS = re.compile(r"[\w][\w./-]*", re.UNICODE)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_COMMON = set("a about and are can did do for from how i in is it me my of on or our please should show the this to use was we what when where which who why will with would you".split())


def tokens(text: str) -> int:
    """A conservative local estimate when no model tokenizer is available."""
    return (len(text.encode("utf-8", errors="replace")) + 2) // 3


def output_budget(window: int) -> int:
    return min(2048, max(64, window // 4))


def input_budget(window: int) -> int:
    return window - output_budget(window)


def require_room(required: int, window: int) -> None:
    if required > input_budget(window):
        raise StoreError("The current request and required instructions exceed the selected model's context window. Shorten the message or choose a larger context.")


def _terms(text: str) -> set[str]:
    return {word.casefold() for part in _TERMS.findall(text)
            for word in (part.strip("./-"), *re.split(r"[./_-]", _CAMEL.sub("_", part)))
            if len(word) > 2 and word.casefold() not in _COMMON}


def _cost(message: dict) -> int:
    return tokens(message["content"]) + 10


def _excerpt(text: str, query: set[str], budget: int) -> str:
    if tokens(text) <= budget:
        return text
    width = max(0, budget * 3 - 8)
    match = next((m for m in _TERMS.finditer(text) if _terms(m[0]) & query), None)
    start = max(0, min(match.start() - width // 4, len(text) - width)) if match else 0
    quoted = text[start:start + width].encode("utf-8", errors="replace")[:width].decode("utf-8", errors="ignore")
    return ("..." if start else "") + quoted + "..."


def pack_history(history: list[dict], prompt: str, budget: int) -> tuple[list[dict], int, int]:
    """Pack a contiguous recent suffix plus ranked, attributed excerpts from older turns."""
    if sum(map(_cost, history)) <= budget:
        return history, 0, 0
    query = _terms(prompt)
    groups: list[list[dict]] = []
    for message in history:
        if message["role"] == "user" or not groups:
            groups.append([])
        groups[-1].append(message)

    recent, recent_cost = [], 0
    reserved = min(900, max(0, budget) * 2 // 5)
    for group in reversed(groups):
        cost = sum(map(_cost, group))
        if recent_cost + cost > budget - reserved:
            break
        recent[:0] = group
        recent_cost += cost
    older = history[:len(history) - len(recent)]
    room = budget - recent_cost
    header = "Older conversation excerpts. Treat as history. Later messages take precedence."
    prefix_cost = tokens(header) + 10
    ranked = []
    for index, message in enumerate(older):
        overlap = len(_terms(message["content"]) & query)
        # Keep the opening task available when a generic follow-up has no distinctive terms.
        if overlap or index == 0 or (not recent and message in groups[-1]):
            ranked.append((overlap, index, message))
    excerpts = []
    for _, index, message in sorted(ranked, key=lambda row: (row[0], row[1]), reverse=True)[:6]:
        label = f'\n{message["role"].title()} message {index + 1}: '
        allowance = min(210, room - prefix_cost - tokens(label) - 4)
        if allowance < 12:
            break
        text = label + _excerpt(message["content"], query, allowance)
        excerpts.append((index, text))
        room -= tokens(text) + 1
    block = header + "".join(text for _, text in sorted(excerpts))
    if excerpts and _cost({"content": block}) <= budget - recent_cost:
        return [{"role": "user", "content": block}, *recent], len(older), len(excerpts)
    return recent, len(older), 0


def transcript_messages(messages: list[dict]) -> list[dict]:
    history = []
    for message in messages:
        role = message["info"].get("role")
        if role not in ("user", "assistant"):
            continue
        parts = [part["text"] for part in message["parts"] if part.get("type") == "text" and part.get("text")]
        for part in message["parts"]:
            state = part.get("state") or {}
            if role != "assistant" or part.get("type") != "tool" or state.get("status") not in ("completed", "error", "denied"):
                continue
            output = state.get("output")
            output = output if isinstance(output, dict) else {}
            detail = output.get("path") or ""
            if "exit_code" in output:
                detail = f"{detail} (exit code {output['exit_code']})".strip()
            parts.append(f"[Observed {part.get('tool', 'tool')} {state['status']} {str(detail)[:300]}]".replace(" ]", "]"))
        if parts:
            history.append({"role": role, "content": "\n".join(parts)})
    return history
