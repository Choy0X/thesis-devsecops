"""The two shapes between the agent's loop and a model server: what a round asks for (``RoundRequest``) and what comes back, whatever the server's own chunk format (``StreamEvent``).

The loop used to read an OpenAI chunk directly: a reasoning field here, a ``<think>`` tag there, tool-call fragments with an index. Anything that spoke another dialect needed its own copy of that
loop. An adapter turns its server's stream into the five events below, and the loop reads only those:

* ``thinking``: words of the model's reasoning (sent apart by the server, or between ``<think>`` tags in its text);
* ``text``: words of its reply;
* ``tool_calls``: the calls of the reply, whole, once the stream has ended (``[{"id", "name", "arguments"}]``, the arguments a JSON string as the model wrote them);
* ``usage``: what the server counted (prompt, completion and, when it says, reasoning tokens);
* ``finish``: why the reply ended (``stop``, ``tool_calls``, ``length``...).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol


@dataclass
class RoundRequest:
    messages: list[dict]
    tools: list[dict] | None = None             # native protocol: the tools the chat template shows the model
    schema: dict | None = None                  # schema protocol: the JSON Schema the reply is constrained to (a decision object)
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    extra: dict = field(default_factory=dict)   # fields only some servers know (top_k, chat_template_kwargs, reasoning_effort): sent in the body as they are


@dataclass
class StreamEvent:
    kind: str                                   # thinking | text | tool_calls | usage | finish
    text: str = ""
    calls: list[dict] | None = None
    usage: dict | None = None
    reason: str | None = None


class ModelStream(Protocol):
    def __iter__(self) -> Iterator[StreamEvent]: ...
    def close(self) -> None: ...


class ToolsRefused(Exception):
    """The server does not take tools (it said so in answer to a request that carried them): the round is asked again without."""
