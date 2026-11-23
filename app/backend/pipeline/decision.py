"""The one decision a model makes every round -- call a tool, or answer -- built as a JSON Schema instead of asked for through ``tools``/``tool_choice``.

Why: llama.cpp's OpenAI-compatible ``tools``/``tool_choice`` support watches for the model to *spontaneously* emit a trigger token sequence (literally
``<tool_call>`` for this family of chat templates) before it ever constrains anything; a model that drifts to a different wrapper, or none at all, is never
caught, no matter what ``tool_choice`` says -- confirmed live, repeatedly, across two llama.cpp releases and every ``tool_choice`` value this server accepts.
``response_format``'s ``json_schema``, by contrast, constrains the sampler itself from the first token: the model cannot produce anything that does not
parse as the schema below, verified live (streaming, the exact call shape this app uses) across dozens of samples with zero failures, correctly
disambiguating between tools and matching each one's own argument shape.

``build_schema`` turns an OpenAI-style tool list (``chat_tools.TOOLS``, ``agent_tools.TOOLS`` or ``agent_tools.PLAN_TOOLS`` -- whatever the caller already
offers) into that schema: a free-text ``thought`` (a scratchpad that is never required to be shown) and exactly one ``action`` -- a plain answer, or a call
to one of the tools, its ``arguments`` constrained to that tool's own ``parameters`` schema, not just "any object". ``parse_decision`` turns the model's
complete decision text back into the exact ``{index: {"id", "name", "arguments"}}`` shape the round machinery already builds from real ``tool_calls``
deltas, so nothing downstream (dispatch, the transcript, the UI's event shapes) needs to know this protocol exists."""
from __future__ import annotations

import json
import re


def build_schema(tools: list[dict]) -> dict:
    """The JSON Schema one round's decision must match, from ``tools`` (each ``{"type": "function", "function": {"name", "parameters", ...}}``)."""
    answer_branch = {"type": "object", "required": ["kind", "text"], "properties": {"kind": {"const": "answer"}, "text": {"type": "string"}}}
    call_branches = [{"type": "object", "required": ["kind", "name", "arguments"],
                      "properties": {"kind": {"const": "call"}, "name": {"const": t["function"]["name"]}, "arguments": t["function"]["parameters"]}}
                     for t in tools]
    return {"type": "object", "required": ["thought", "action"],
            "properties": {"thought": {"type": "string", "description": "One or two sentences in the first person, read by the owner: what you are about to do and why (for a final answer, your reasoning)."},
                           "action": {"oneOf": [answer_branch, *call_branches]}}}


def _type_of(schema: dict) -> str:
    kind = schema.get("type")
    if kind == "array":
        return f"list of {_type_of(schema.get('items') or {})}" if schema.get("items") else "list"
    return str(kind) if kind else "any"


def catalogue(tools: list[dict]) -> str:
    """The tool list as prose for a system prompt, one line per tool (name, arguments with their types, what it does). A grammar can force a valid tool name but cannot tell the model
    what a tool is for; native ``tools`` get this from the chat template, a schema-constrained round has to be told it in words or the model chooses blind."""
    lines = []
    for t in tools:
        f = t["function"]
        params = f.get("parameters") or {}
        required = set(params.get("required") or [])
        args = ", ".join(f"{k}{'' if k in required else '?'}: {_type_of(v)}" for k, v in (params.get("properties") or {}).items())
        lines.append(f"- {f['name']}({args}): {f.get('description', '')}".rstrip())
    return ("Your tools (arguments marked ? are optional). Each turn you either call exactly one tool or give your final answer:\n" + "\n".join(lines))


def parse_decision(text: str, tools: list[dict]) -> dict:
    """``text`` (the model's complete, schema-constrained answer) read as ``{"thought", "calls", "answer"}``: ``calls`` is ``{0: {"id", "name",
    "arguments"}}`` when the decision was a call, else ``{}``; ``answer`` is the text to show when it was a plain answer, else ``None``. Both empty when
    ``text`` does not parse as the schema at all (a token budget can cut generation off mid-object even though the grammar never allows invalid syntax for
    however much was produced) -- the caller falls back to showing the raw text rather than losing the turn."""
    names = {t["function"]["name"] for t in tools}
    try:
        obj = json.loads(text)
    except ValueError:
        return {"thought": None, "calls": {}, "answer": None}
    if not isinstance(obj, dict):
        return {"thought": None, "calls": {}, "answer": None}
    thought = obj.get("thought") if isinstance(obj.get("thought"), str) else None
    action = obj.get("action")
    if not isinstance(action, dict):
        return {"thought": thought, "calls": {}, "answer": None}
    if action.get("kind") == "answer" and isinstance(action.get("text"), str):
        return {"thought": thought, "calls": {}, "answer": action["text"]}
    if action.get("kind") == "call" and action.get("name") in names and isinstance(action.get("arguments"), dict):
        return {"thought": thought, "calls": {0: {"id": "call_0", "name": action["name"], "arguments": json.dumps(action["arguments"], ensure_ascii=False)}}, "answer": None}
    return {"thought": thought, "calls": {}, "answer": None}


# -- reading the decision while it streams ---------------------------------------------------------------------------------------------------------------------------------
#
# A schema-constrained round is one JSON object and it arrives in pieces. Waiting for all of it would leave the page empty for as long as the model takes to write a long answer, which a
# native round never does. The grammar fixes the order of the keys (``thought`` first, then ``action`` with ``kind`` first), so the two strings a person reads, the thought and an
# answer's ``text``, can be decoded and handed on as they grow. Everything else (a call's name and arguments) waits for the whole object, which ``parse_decision`` reads.

_THOUGHT = re.compile(r'\s*\{\s*"thought"\s*:\s*"')
_ANSWER = re.compile(r'\s*,\s*"action"\s*:\s*\{\s*"kind"\s*:\s*"answer"\s*,\s*"text"\s*:\s*"')
_PARTIAL_ESCAPE = re.compile(r"(\\+)(u[0-9a-fA-F]{0,3})?$")


def _string_at(buf: str, start: int) -> tuple[str, bool, int]:
    """``(decoded, closed, end)``: the JSON string whose first character is ``buf[start]`` (just past its opening quote), as far as it has arrived. ``closed`` once its closing quote is
    there, and ``end`` is then the index after it. An escape that is only half there is left for the next piece."""
    i, n = start, len(buf)
    closed, end = False, n
    while i < n:
        if buf[i] == "\\":
            i += 2
            continue
        if buf[i] == '"':
            closed, end = True, i + 1
            break
        i += 1
    raw = buf[start:end - 1] if closed else buf[start:]
    if not closed:
        m = _PARTIAL_ESCAPE.search(raw)
        if m and len(m.group(1)) % 2:                       # an odd run of backslashes ends in one that starts an escape which has not finished arriving
            raw = raw[:m.start()] + m.group(1)[:-1]
    try:
        text = json.loads('"' + raw + '"')
    except ValueError:
        text = raw.replace("\\n", "\n").replace('\\"', '"')
    if not closed and text and "\ud800" <= text[-1] <= "\udbff":          # the first half of an escaped pair: held until its second half arrives, so that what was sent stays a prefix
        text = text[:-1]
    return text, closed, end


class DecisionStream:
    """Feed it the pieces of a decision as they arrive; ``feed`` returns the ``(kind, text)`` pairs that can be shown now: ``("thought", ...)`` for the thought and ``("answer", ...)`` for an
    answer's text, each as the new characters since the last call. A decision whose keys come in another order is not shown early: ``parse_decision`` on the whole text is what decides, and
    the caller shows whatever it was not given here (``thought_sent`` and ``answer_sent`` say how much that was)."""

    def __init__(self):
        self.buf = ""
        self.thought_sent = self.answer_sent = 0
        self._thought_at = self._answer_at = self._thought_end = None
        self._thought_done = self._answer_done = False

    def feed(self, piece: str) -> list[tuple[str, str]]:
        self.buf += piece
        out: list[tuple[str, str]] = []
        if self._thought_at is None:
            m = _THOUGHT.match(self.buf)
            if m:
                self._thought_at = m.end()
        if self._thought_at is not None and not self._thought_done:
            text, closed, end = _string_at(self.buf, self._thought_at)
            if len(text) > self.thought_sent:
                out.append(("thought", text[self.thought_sent:]))
                self.thought_sent = len(text)
            if closed:
                self._thought_done, self._thought_end = True, end
        if self._thought_done and self._answer_at is None:
            m = _ANSWER.match(self.buf, self._thought_end)
            if m:
                self._answer_at = m.end()
        if self._answer_at is not None and not self._answer_done:
            text, closed, _ = _string_at(self.buf, self._answer_at)
            if len(text) > self.answer_sent:
                out.append(("answer", text[self.answer_sent:]))
                self.answer_sent = len(text)
            self._answer_done = closed
        return out
