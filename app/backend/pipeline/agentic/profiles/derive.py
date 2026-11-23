"""A profile read from what a model says about itself: its chat template and its file.

The template is the model's own description of how it is talked to. Whether it takes ``tools`` and in what wrapper it expects a call (``<tool_call>``, ``[TOOL_CALLS]``, ``<|python_tag|>``...),
whether it takes ``enable_thinking`` or ``reasoning_effort`` or opens ``<think>`` by itself, whether it accepts a system message: all of that is in the text and is read here without running the
model. It is a first answer, not a measurement: ``probe.py`` asks the model, and the probe's result is what chooses the protocol.
"""
from __future__ import annotations

import jinja2
from jinja2 import meta

from app.backend.pipeline.agentic.profiles.model_profile import ModelProfile, Reasoning, default_max_output

# Wrappers a template mentions when the model is trained to write its calls in them, most specific first.
TOOL_MARKERS = (
    ("<|python_tag|>", "llama3_json"),
    ("[TOOL_CALLS]", "mistral"),
    ("<|channel|>", "harmony"),
    ("<｜tool▁calls▁begin｜>", "deepseek"),
    ("<function=", "xml_function"),
    ("<tool_call>", "hermes_xml"),
)
# What the authors of a family recommend while it thinks (a chat default is not what an agent wants without thinking, so the families that never think keep the agent's own).
THINKING_SAMPLING = {
    "qwen3": {"temperature": 0.6, "top_p": 0.95, "top_k": 20},
    "deepseek": {"temperature": 0.6, "top_p": 0.95},
    "gpt-oss": {"temperature": 1.0, "top_p": 1.0},
}
FAMILIES = (("gpt-oss", "gpt-oss"), ("gptoss", "gpt-oss"), ("qwen3", "qwen3"), ("qwen", "qwen2"), ("llama", "llama"), ("mixtral", "mistral"), ("ministral", "mistral"), ("mistral", "mistral"),
            ("gemma", "gemma"), ("deepseek", "deepseek"), ("phi", "phi"))


def undeclared(template: str) -> set[str]:
    """The names a template reads from what it is given (``tools``, ``enable_thinking``...). A template the parser cannot read (an extension tag it does not know) is searched for the names as text."""
    try:
        return set(meta.find_undeclared_variables(jinja2.Environment().parse(template)))
    except jinja2.TemplateSyntaxError:
        return {name for name in ("tools", "enable_thinking", "reasoning_effort", "messages") if name in template}


def family_of(*names: str | None) -> str:
    text = " ".join(n.lower() for n in names if n)
    for needle, family in FAMILIES:
        if needle in text:
            return family
    return "unknown"


def analyse_template(template: str | None) -> dict:
    """What a template says about how its model is talked to."""
    if not template:
        return {"tools": False, "tool_format": "none", "reasoning": Reasoning(), "system_role": True, "has_template": False}
    names = undeclared(template)
    tools = "tools" in names
    tool_format = next((fmt for marker, fmt in TOOL_MARKERS if marker in template), "native_json" if tools else "none")
    if "enable_thinking" in names:
        reasoning = Reasoning("toggle", ("<think>", "</think>"), "chat_template_kwargs.enable_thinking")
    elif "reasoning_effort" in names:
        reasoning = Reasoning("effort", None, "reasoning_effort")
    elif "<think>" in template:
        reasoning = Reasoning("always", ("<think>", "</think>"), "")
    else:
        reasoning = Reasoning()
    return {"tools": tools, "tool_format": tool_format, "reasoning": reasoning, "system_role": "System role not supported" not in template, "has_template": True}


def derive(ref: str, template: str | None, *, architecture: str | None = None, name: str | None = None, filename: str | None = None, context: int | None = None,
           local: bool = True, fingerprint: str = "") -> ModelProfile:
    """The profile of a model from its template and its file. ``protocol`` is the safe first answer: ``schema`` for a model on this computer (it is the one that worked on the model this app
    runs), ``native`` for a hosted model, until the live test says otherwise."""
    a = analyse_template(template)
    family = family_of(architecture, name, filename)
    notes = []
    if not a["has_template"]:
        notes.append("The model file carries no chat template, so how it is talked to is a guess.")
    elif not a["tools"]:
        notes.append("Its chat template does not take tools, so it is asked for its decision with a schema instead.")
    if a["reasoning"].kind == "always":
        notes.append("It thinks before every answer; thinking cannot be switched off, only shortened.")
    return ModelProfile(ref=ref, family=family, context=context, max_output=default_max_output(context), tool_format=a["tool_format"], native_tools=a["tools"],
                        protocol="schema" if local else "native", system_role=a["system_role"], reasoning=a["reasoning"], source="derived", fingerprint=fingerprint,
                        sampling=dict(THINKING_SAMPLING.get(family, {})) if a["reasoning"].kind != "none" else {}, notes=notes)
