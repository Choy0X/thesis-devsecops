"""The training configuration as a list of settings a person can read: each with its value, its limits, and whether it can be changed in this phase.

Everything is read from the typed configuration (``training/finetune/config.py``) by looking at its fields, so a new field, a new limit or a newly pinned
value shows up here without a second list to keep in step. A field is *fixed* when the configuration pins it to one value (a method that has no
implementation yet, or a rule the experiments depend on) or offers one choice only.
"""
from __future__ import annotations

import typing

from pydantic import BaseModel

from training.finetune import config as cfgmod, overrides as ov

TITLES = {
    "rag": "Retrieval", "model": "Base model", "data": "Data", "leakage": "Leakage control", "mining": "Mining", "context": "Context",
    "sampling": "Sampling", "precision": "Precision", "lora": "LoRA adapters", "training": "Training", "runtime": "Runtime", "loss": "Loss",
    "calibration": "Calibration", "student_correction": "Student correction", "preference": "Preference training", "advanced": "Advanced methods",
    "verification": "Verification", "evaluation": "Evaluation",
}
_BOUNDS = (("ge", "minimum"), ("gt", "above"), ("le", "maximum"), ("lt", "below"), ("min_length", "min_length"))


_pinned = ov.pinned_value                      # one definition of what is pinned, shared with the override rules


def _bounds(field) -> dict:
    out = {}
    for m in field.metadata:
        for attr, name in _BOUNDS:
            v = getattr(m, attr, None)
            if v is not None:
                out[name] = v
    return out


def _kind(annotation) -> str:
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _kind(typing.get_args(annotation)[0])
    if origin is typing.Literal:
        return "choice"
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)):
        inner = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _kind(inner[0]) if len(inner) == 1 else "mixed"
    if origin in (list, tuple):
        return "list"
    if origin is dict:
        return "dict"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "group"
    return {bool: "bool", int: "int", float: "float", str: "text"}.get(annotation, "text")


def _choices(annotation) -> list | None:
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return list(typing.get_args(annotation))
    if origin is typing.Annotated:
        return _choices(typing.get_args(annotation)[0])
    return None


def _walk(model_cls, value, prefix: str, out: list[dict]) -> None:
    for name, field in model_cls.model_fields.items():
        v = getattr(value, name)
        key = f"{prefix}{name}"
        annotation = field.annotation
        if isinstance(v, BaseModel):
            _walk(type(v), v, key + ".", out)
            continue
        pinned, pinned_value = _pinned(field)
        choices = _choices(annotation)
        one_choice = bool(choices) and len(choices) == 1
        item = {"key": key, "name": name, "kind": _kind(annotation), "value": v if not hasattr(v, "model_dump") else v.model_dump(),
                "fixed": pinned or one_choice, **_bounds(field)}
        if choices:
            item["choices"] = choices
        if pinned:
            item["fixed_reason"] = "A rule the experiments depend on, or a method that has no implementation yet."
        elif one_choice:
            item["fixed_reason"] = "Only one choice is implemented."
        out.append(item)


def describe(cfg: "cfgmod.ProjectConfig", file_hash: str | None = None) -> dict:
    sections = []
    for name, field in cfgmod.ProjectConfig.model_fields.items():
        v = getattr(cfg, name)
        if not isinstance(v, BaseModel):
            continue
        fields: list[dict] = []
        _walk(type(v), v, f"{name}.", fields)
        sections.append({"key": name, "title": TITLES.get(name, name.replace("_", " ").capitalize()), "fields": fields})
    total = sum(len(s["fields"]) for s in sections)
    fixed = sum(1 for s in sections for f in s["fields"] if f["fixed"])
    return {"sections": sections, "counts": {"settings": total, "fixed": fixed, "changeable": total - fixed}, "file_sha256": file_hash,
            "unresolved": cfgmod.unresolved(cfg)}
