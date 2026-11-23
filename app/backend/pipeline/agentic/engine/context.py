"""What goes into the model's context, and what is kept out of it.

* ``mask_result``: one choke point for secrets. Every tool masks what it reads (``code_rules.mask``); a result of a tool that forgot, a result that carries the arguments back, an error that
  quotes a line of a file: all of them pass through here before the model, the page, the journal or the session see them. The result keeps its shape (only text is changed).
* ``parse_context_limit``: a server that refuses a request as too long says how long it may be, in words that differ from server to server; this reads the number, so that the request that
  follows is made to fit that and not a guess.
* ``fence_tool_messages``: an option (off by default, ``params.flags.fence_results``): the text of every tool result put between markers carrying a nonce, so that a line in a file cannot pose as
  an instruction by closing the data. It costs tokens and a small model may react to the markers, so whether it helps is measured by an ablation and not assumed.
"""
from __future__ import annotations

import re
from typing import Any

from app.backend.pipeline.agentic import fence
from app.backend.pipeline.scan import code_rules

DEFAULT_HOSTED_CONTEXT = 32_768          # what a model of unknown size is planned for: smaller than most hosted models have, so that a request is never made too large
_LIMIT_PATTERNS = (
    re.compile(r"maximum context length is (\d{3,8})", re.I),
    re.compile(r"context (?:window|length|size)(?: of| is| limit is|:)?\s*\(?(\d{3,8})", re.I),
    re.compile(r"n_ctx(?:_slot)?\D{0,12}(\d{3,8})", re.I),
    re.compile(r"(?:max(?:imum)?|limit of)\s+(\d{3,8})\s+tokens", re.I),
    re.compile(r"(\d{3,8})\s+tokens?\s+(?:maximum|max|limit)", re.I),
)


def mask_result(value: Any) -> Any:
    """``value`` with every string in it masked (dicts and lists are walked; numbers, booleans and None are left alone)."""
    if isinstance(value, str):
        return code_rules.mask(value, config=True)
    if isinstance(value, dict):
        return {k: mask_result(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_result(v) for v in value]
    return value


def parse_context_limit(text: str) -> int | None:
    """The number of tokens a server says it can take, from the text of its refusal, or None. Only a number a context could plausibly be (1,024 to 2,000,000)."""
    for pattern in _LIMIT_PATTERNS:
        for m in pattern.finditer(text or ""):
            n = int(m.group(1))
            if 1_024 <= n <= 2_000_000:
                return n
    return None


def fence_tool_messages(transcript: list[dict], salt: str) -> list[dict]:
    """``transcript`` with the content of each tool message between markers (the stored session is not changed). A message that is already a note of the system is left as it is."""
    out = []
    for m in transcript:
        if m.get("role") == "tool" and isinstance(m.get("content"), str) and m["content"]:
            nonce = fence.nonce_of(salt, str(m.get("id") or m.get("tool_call_id") or ""))
            out.append({**m, "content": fence.wrap("RESULT", m["content"], nonce)})
        else:
            out.append(m)
    return out
