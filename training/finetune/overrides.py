"""Per-run overrides of the training contract. No torch.

The contract (``training/configs/dataset_finetuning.yaml``) holds the defaults and the rules the experiments depend on. A run may change a *setting* (the
LoRA rank, the learning rate, how long to train) but never a *rule*: a value the configuration pins, or a field with a single implemented choice, cannot be
overridden, and neither can the identity of the run (the commits and the dataset are filled in by ``resolve``). An override is a dotted key and a value
(``{"lora.r": 64}``). Every problem is collected and reported in plain words; nothing is applied half way. The result is re-validated by the same models
as the contract, so the cross-field rules (the batch sizes multiply out, the evaluation points are ascending) still hold.
"""
from __future__ import annotations

import copy
import re
import typing

from pydantic import AfterValidator, BaseModel

from training.finetune import config as cfgmod

# Filled in by resolve() from the hub and the dataset, so they are never set by hand.
IDENTITY = {"schema_version", "model.revision", "model.tokenizer_revision", "data.dataset_version", "data.manifest_sha256"}


class OverrideError(ValueError):
    """One or more overrides cannot be applied; ``problems`` lists each with its key."""

    def __init__(self, problems: list[dict]):
        self.problems = problems
        super().__init__("; ".join(f"{p['key']}: {p['reason']}" for p in problems))


def pinned_value(field) -> tuple[bool, object]:
    """``(True, value)`` when a field is pinned by ``_only(...)`` in the config models."""
    for m in field.metadata:
        if isinstance(m, AfterValidator) and getattr(m.func, "__qualname__", "").startswith("_equal."):
            return True, m.func.__closure__[0].cell_contents
    return False, None


def _single_choice(annotation) -> bool:
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _single_choice(typing.get_args(annotation)[0])
    return origin is typing.Literal and len(typing.get_args(annotation)) == 1


def _walk(model_cls, prefix: str, out: dict[str, str]) -> None:
    """Every settable leaf of the config models as dotted key -> "" (changeable) or the reason it is fixed."""
    for name, field in model_cls.model_fields.items():
        key = f"{prefix}{name}"
        ann = field.annotation
        inner = typing.get_args(ann)[0] if typing.get_origin(ann) is typing.Annotated else ann
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            _walk(inner, key + ".", out)
            continue
        pinned, _ = pinned_value(field)
        if key in IDENTITY:
            out[key] = "It is filled in when the run is prepared."
        elif pinned:
            out[key] = "A rule the experiments depend on, or a method that has no implementation yet."
        elif _single_choice(ann):
            out[key] = "Only one choice is implemented."
        else:
            out[key] = ""


def settable() -> dict[str, str]:
    out: dict[str, str] = {}
    _walk(cfgmod.ProjectConfig, "", out)
    return out


def apply(data: dict, overrides: dict[str, object] | None) -> dict:
    """A copy of the contract's data with the overrides set. Raises ``OverrideError`` naming every key that is unknown or fixed, or whose value the config
    models refuse (their own messages, with the key)."""
    if not overrides:
        return copy.deepcopy(data)
    keys = settable()
    problems: list[dict] = []
    out = copy.deepcopy(data)
    for key, value in overrides.items():
        if key not in keys:
            problems.append({"key": key, "reason": "There is no such setting."})
        elif keys[key]:
            problems.append({"key": key, "reason": f"It cannot be changed. {keys[key]}"})
        else:
            cur = out
            *parents, leaf = key.split(".")
            for part in parents:
                cur = cur[part]
            cur[leaf] = value
    if problems:
        raise OverrideError(problems)
    try:
        cfgmod.parse(out, "contract")
    except cfgmod.ConfigError as e:
        raise OverrideError([{"key": _key_of(m, overrides), "reason": m.split(": ", 1)[-1]} for m in str(e).split("; ")]) from e
    return out


def _key_of(message: str, overrides: dict) -> str:
    """The dotted key a validation message starts with, else the override whose setting the message names (a rule across fields, such as two settings that exclude each
    other, names them by their short names), else the first override."""
    head = message.split(": ", 1)[0]
    if "." in head or head in overrides:
        return head
    named = next((k for k in overrides if re.search(rf"\b{re.escape(k.rsplit('.', 1)[-1])}\b", message)), None)
    return named or next(iter(overrides), head)


def changed(base: dict, effective: dict) -> list[dict]:
    """The settings that differ between two config dicts, as ``{key, default, value}`` rows in key order."""
    rows: list[dict] = []

    def walk(a, b, prefix: str) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a) | set(b)):
                walk(a.get(k), b.get(k), f"{prefix}{k}.")
        elif a != b:
            rows.append({"key": prefix[:-1], "default": a, "value": b})
    walk(base, effective, "")
    return rows
