"""What the agent knows about one model, in one record.

The agent used to ask one question of a model: is its server on this computer? That chose how it was asked for a decision, and nothing else was known about it: not whether its chat template
shows tools, not whether it thinks, not how much it may write. A ``ModelProfile`` holds those answers, read from the model's own file and template, confirmed by a short live test, and kept
until the model changes (``store.py``). ``protocol`` is how a round asks the model for its decision:

* ``native``: the server is sent ``tools`` and the chat template shows them to the model (a hosted model's usual way, and a local model's when it really calls tools);
* ``schema``: no tools are sent; the reply is constrained to a decision object by a JSON Schema and the tools are listed in the system prompt (one call a round, the model cannot think aloud
  before it, but it can never write a call as prose: measured on the 7B this app runs, where native tools did not work at all);
* ``text``: no tools are sent and the reply is free text in which a call is written in a known format that the agent reads back (needs the repair layer; not offered yet).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field

PROTOCOLS = ("native", "text", "schema")
SUITE_VERSION = 1                       # the version of the live test (``probe.py``): a change of it makes every saved profile be confirmed again
REASONING_KINDS = ("none", "toggle", "effort", "always")


@dataclass
class Reasoning:
    """How a model thinks. ``toggle``: the template takes ``enable_thinking``; ``effort``: it takes ``reasoning_effort`` (low, medium, high); ``always``: it opens ``<think>`` itself and
    cannot be told not to; ``none``: it does not think before it answers."""

    kind: str = "none"
    tags: tuple[str, str] | None = None             # what wraps the thinking in the text of a reply, when it is not sent apart
    param: str = ""                                 # how it is switched, for the request: "chat_template_kwargs.enable_thinking" or "reasoning_effort"


@dataclass
class ModelProfile:
    ref: str                                        # "local/<file name>" for a model on this computer, else "<provider>/<model>"
    family: str = "unknown"                         # qwen2, qwen3, llama, mistral, gemma, gpt-oss, deepseek, phi, unknown
    context: int | None = None                      # tokens the model can be given (what it was trained for, or what the server was started with)
    max_output: int = 2048                          # what one reply may be, by default
    tool_format: str = "none"                       # hermes_xml, llama3_json, mistral, harmony, deepseek, xml_function, native_json (the template takes tools, in a way not named here), none
    native_tools: bool = False                      # whether the chat template takes ``tools`` at all
    protocol: str = "schema"                        # how a round asks it for its decision; see the module's note
    parallel_calls: bool = False                    # whether it may be sent several calls in one reply
    system_role: bool = True                        # whether the template accepts a system message
    vision: bool = False
    reasoning: Reasoning = field(default_factory=Reasoning)
    sampling: dict = field(default_factory=dict)    # temperature, top_p, top_k its authors recommend for what it does, used when the session names none
    source: str = "derived"                         # derived (from the file and template), probed (and confirmed by a live test), manual (set by the owner)
    fingerprint: str = ""
    probe: dict | None = None                       # what the live test found
    notes: list[str] = field(default_factory=list)  # things worth telling the owner, in words

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["reasoning"]["tags"] = list(self.reasoning.tags) if self.reasoning.tags else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ModelProfile":
        known = {f.name for f in dataclasses.fields(cls)}
        r = d.get("reasoning") or {}
        tags = r.get("tags")
        reasoning = Reasoning(kind=r.get("kind") if r.get("kind") in REASONING_KINDS else "none", tags=(tags[0], tags[1]) if isinstance(tags, (list, tuple)) and len(tags) == 2 else None,
                              param=str(r.get("param") or ""))
        values = {k: v for k, v in d.items() if k in known and k != "reasoning"}
        if values.get("protocol") not in PROTOCOLS:
            values["protocol"] = "schema"
        return cls(**values, reasoning=reasoning)


def default_max_output(context: int | None) -> int:
    """How much one reply may be when nothing says: a quarter of the context, at least 1,024 and at most 4,096 tokens (the 1,024 that every session was given cut a file written in one call short)."""
    return 2048 if not context else max(1024, min(4096, int(context) // 4))


def fingerprint(**parts) -> str:
    """The identity of what a profile was made from: the file (size and modification time), the template, the server's build, the version of the live test. Any change is a new model."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]
