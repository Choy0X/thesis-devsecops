"""The adapter for an OpenAI-compatible chat server (``/v1/chat/completions``): llama.cpp's server, Ollama, LM Studio, vLLM, and the hosted services that speak the same dialect.

It builds the request (tools, or a JSON Schema for the reply; the generation settings the session or the profile names; the fields only some servers know in the body's ``extra``), asks for the
token counts, tries again without them for a server that does not know the option, tells a server that refuses tools from any other failure, and turns the stream into the events of
``base.py``: reasoning sent apart (``reasoning_content`` or ``reasoning``) and reasoning written between ``<think>`` tags both become ``thinking``; the fragments of a call are put together and
handed over whole at the end.
"""
from __future__ import annotations

from typing import Iterator

from app.backend.pipeline import project_chat
from app.backend.pipeline.agentic.providers.base import ModelStream, RoundRequest, StreamEvent, ToolsRefused


def _usage(u) -> dict:
    """What a server counted, as a plain dict (a server may send an object or a dict, and may leave a field out)."""
    get = (lambda k: u.get(k)) if isinstance(u, dict) else (lambda k: getattr(u, k, None))
    out = {k: int(get(k)) for k in ("prompt_tokens", "completion_tokens", "total_tokens") if isinstance(get(k), (int, float))}
    details = get("completion_tokens_details")
    reasoning = (details.get("reasoning_tokens") if isinstance(details, dict) else getattr(details, "reasoning_tokens", None)) if details is not None else None
    if isinstance(reasoning, (int, float)) and reasoning:
        out["reasoning_tokens"] = int(reasoning)
    return out


class _Stream:
    def __init__(self, raw, raw_content: bool):
        self.raw, self.raw_content = raw, raw_content

    def close(self) -> None:
        close = getattr(self.raw, "close", None)
        if close:
            close()

    def __iter__(self) -> Iterator[StreamEvent]:
        splitter = project_chat.ThinkSplitter()
        calls: dict[int, dict] = {}
        reason = None
        for chunk in self.raw:
            usage = getattr(chunk, "usage", None)
            if usage:
                yield StreamEvent("usage", usage=_usage(usage))
            if not getattr(chunk, "choices", None):
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            thinking = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if isinstance(thinking, str) and thinking:
                yield StreamEvent("thinking", thinking)
            content = getattr(delta, "content", None)
            if isinstance(content, str) and content:
                if self.raw_content:                       # the content is a JSON document: a "<think>" inside one of its strings is words, not a tag
                    yield StreamEvent("text", content)
                else:
                    for kind, piece in splitter.feed(content):
                        yield StreamEvent("thinking" if kind == "thinking" else "text", piece)
            for tc in getattr(delta, "tool_calls", None) or []:
                slot = calls.setdefault(tc.index if tc.index is not None else len(calls), {"id": "", "name": "", "arguments": ""})
                slot["id"] = tc.id or slot["id"]
                if tc.function is not None:
                    slot["name"] = tc.function.name or slot["name"]
                    slot["arguments"] += tc.function.arguments or ""
            if getattr(choice, "finish_reason", None):
                reason = choice.finish_reason
        if not self.raw_content:
            for kind, piece in splitter.flush():
                yield StreamEvent("thinking" if kind == "thinking" else "text", piece)
        if calls:
            yield StreamEvent("tool_calls", calls=[calls[i] for i in sorted(calls)])
        yield StreamEvent("finish", reason=reason)


class OpenAICompat:
    kind = "openai_compat"

    def __init__(self, client, model: str):
        self.client, self.model = client, model

    def stream(self, req: RoundRequest) -> ModelStream:
        """Opens the request. Raises ``ToolsRefused`` for a server that does not take tools, and whatever else the server raises for anything else."""
        kw: dict = {}
        if req.schema is not None:
            kw["response_format"] = {"type": "json_schema", "json_schema": {"name": "decision", "schema": req.schema, "strict": True}}
        elif req.tools:
            kw["tools"] = req.tools
        for name in ("temperature", "top_p", "max_tokens"):
            if getattr(req, name) is not None:
                kw[name] = getattr(req, name)
        if req.extra:
            kw["extra_body"] = dict(req.extra)
        raw_content = req.schema is not None
        try:
            try:
                return _Stream(self.client.chat.completions.create(model=self.model, messages=req.messages, stream=True, stream_options={"include_usage": True}, **kw), raw_content)
            except Exception as e:
                if not project_chat._rejects_options(e):
                    raise
            return _Stream(self.client.chat.completions.create(model=self.model, messages=req.messages, stream=True, **kw), raw_content)      # a server that does not know stream_options
        except Exception as e:
            if req.tools and project_chat._rejects_tools(e):
                raise ToolsRefused() from None
            raise
