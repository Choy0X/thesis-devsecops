"""The benchmark registry (``training/configs/benchmarks.yaml``). No torch.

Every requested benchmark has an entry. An entry is ``unresolved`` until its primary source has been read: the owner, version, revision,
task, metric and its direction, language, access requirements, harness and budget are then filled together with ``source_checked`` and
the source, and only then may the status be ``resolved``. An entry never carries a score (unknown fields are rejected), and the registry
rules say that a leaderboard is not a matched local experiment. ``python -m training.finetune.benchmarks --write`` regenerates
``notes/benchmark_registry.md`` from the YAML.
"""
from __future__ import annotations

import pathlib
import sys
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

DEFAULT_PATH = pathlib.Path(__file__).resolve().parents[1] / "configs" / "benchmarks.yaml"
NOTES_PATH = pathlib.Path(__file__).resolve().parents[2] / "notes" / "benchmark_registry.md"
REQUESTED = ("CyberGym", "CWE-Bench", "CWE-bench v1", "Gray Swan IPI", "ExploitGym", "ExploitBench", "SEC-Bench Pro", "The Last Ones",
             "FrontierCyber")
FACTS = ("owner", "version", "revision", "task", "metric", "metric_direction", "language", "access", "harness", "budget")


class Benchmark(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    status: Literal["unresolved", "deferred", "unavailable", "resolved"]
    owner: str | None = None
    version: str | None = None
    revision: str | None = None
    task: str | None = None
    metric: str | None = None
    metric_direction: Literal["higher_is_better", "lower_is_better"] | None = None
    language: list[str] | None = None
    access: str | None = None
    harness: str | None = None
    budget: str | None = None
    ambiguity: str | None = None
    notes: list[str] = []
    source_checked: bool = False
    source: str | None = None

    @model_validator(mode="after")
    def _resolved_needs_everything(self):
        if self.status == "resolved":
            missing = [f for f in (*FACTS, "source") if not getattr(self, f)]
            if missing or not self.source_checked:
                raise ValueError(f"{self.name}: a resolved entry needs source_checked and every fact; missing "
                                 f"{missing + ([] if self.source_checked else ['source_checked'])}")
        return self


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_: str
    phase: str
    rules: list[str]
    benchmarks: list[Benchmark]

    def __init__(self, **data):
        if "schema" in data:
            data["schema_"] = data.pop("schema")
        super().__init__(**data)

    @model_validator(mode="after")
    def _names(self):
        names = [b.name for b in self.benchmarks]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"duplicate benchmark entries: {dup}")
        missing = [n for n in REQUESTED if n not in names]
        if missing:
            raise ValueError(f"requested benchmarks without an entry: {missing}")
        return self


def load(path=DEFAULT_PATH) -> Registry:
    return Registry(**yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")))


def blocking_reason(b: Benchmark) -> str:
    """Why nothing can be run for this entry yet (empty for a resolved one)."""
    if b.status == "resolved":
        return ""
    missing = [f for f in FACTS if not getattr(b, f)]
    return (f"{b.status}: the primary source {'has been checked' if b.source_checked else 'has not been checked'}; "
            f"still to resolve from a primary source: {', '.join(missing) if missing else 'nothing, mark it resolved'}")


def render_markdown(reg: Registry) -> str:
    """The notes page: generated from the YAML, plain ASCII, no scores."""
    lines = ["# Benchmark registry", "",
             "Generated from `training/configs/benchmarks.yaml` by `python -m training.finetune.benchmarks --write`. Do not edit by hand.",
             "", "## Rules", ""]
    lines += [f"- {r}" for r in reg.rules]
    lines += ["", "## Entries", "", "| Benchmark | Status | Source checked | Still to resolve |", "|---|---|---|---|"]
    for b in reg.benchmarks:
        missing = ", ".join(f for f in FACTS if not getattr(b, f)) or "nothing"
        lines.append(f"| {b.name} | {b.status} | {'yes' if b.source_checked else 'no'} | {missing} |")
    lines += ["", "## Notes", ""]
    for b in reg.benchmarks:
        extra = ([f"Ambiguity: {b.ambiguity}"] if b.ambiguity else []) + list(b.notes)
        if extra:
            lines += [f"### {b.name}", ""] + [f"- {e}" for e in extra] + [""]
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    text = render_markdown(load())
    if "--write" in argv:
        NOTES_PATH.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {NOTES_PATH}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
