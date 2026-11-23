"""The front of the tool layer: what every call passes through before a tool runs (``ToolFront.prepare``), and what a tool is (``kind_of``).

``prepare`` takes the name and the argument text a model wrote and returns the call as it will be run: the tool's real name (a prefix, the case, an old name or a close match put right), the
arguments as an object that fits the tool's schema, and a list of what was changed, or the one sentence that tells the model what is wrong. A call that was put right is stored put right (the
text the model wrote is kept beside it), so what is replayed to the model next round is a valid call and not its own mistake.

``KINDS`` says what each tool does to the world (read, write, exec, web, scan, meta), which permission rule applies to it and which argument a rule's pattern is matched against.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.backend.pipeline.agentic import repair
from app.backend.pipeline.agentic.tools import validate

# name -> (kind, the permission a rule names, the argument a rule's pattern is matched against)
KINDS = {
    "read_file": ("read", "read", "path"), "read_files": ("read", "read", ""), "find_symbol": ("read", "read", "path"), "list_files": ("read", "read", "path"), "project_graph": ("read", "read", "path"), "diff_so_far": ("read", "read", ""),
    "git_status": ("read", "read", ""), "git_log": ("read", "read", ""), "find_files": ("read", "glob", "pattern"), "glob": ("read", "glob", "pattern"),
    "search_code": ("read", "grep", "pattern"), "grep": ("read", "grep", "pattern"), "recall": ("read", "read", "handle"), "scan_results": ("read", "scan", ""),
    "rescan_code": ("scan", "scan", ""), "rescan_dependencies": ("scan", "depscan", ""),
    "write_file": ("write", "edit", "path"), "edit_file": ("write", "edit", "path"), "apply_patch": ("write", "edit", ""), "checkpoint": ("write", "checkpoint", ""),
    "run_command": ("exec", "bash", "command"), "bash": ("exec", "bash", "command"), "verify_fix": ("exec", "verify", "test"),
    "bash_output": ("read", "read", "job"), "bash_kill": ("meta", "todo", "job"),
    "update_task_list": ("meta", "todo", ""), "propose_plan": ("meta", "question", ""), "ask_question": ("meta", "question", ""), "task": ("meta", "task", "description"),
    "websearch": ("web", "websearch", "query"), "webfetch": ("web", "webfetch", "url"),
}
WRITE_KINDS = ("write", "exec")                        # what changes something: never run from arguments that may be cut short
# Names the app has used or models tend to use, by the tool they mean.
ALIASES = {"run_command": "bash", "shell": "bash", "execute": "bash", "run": "bash", "search": "grep", "search_code": "grep", "find_files": "glob", "find": "glob", "ls": "list_files",
           "cat": "read_file", "read": "read_file", "edit": "edit_file", "write": "write_file", "todo": "update_task_list", "todowrite": "update_task_list"}


def kind_of(name: str) -> tuple[str, str, str]:
    """``(kind, permission, pattern argument)`` of a tool; a tool this table does not know is treated as one that executes, the most careful reading."""
    if str(name).startswith("mcp__"):                  # a tool of one of the owner's MCP servers: it runs a program, and a rule for it names ``server/tool``
        return ("exec", "mcp", "")
    return KINDS.get(name, ("exec", name, ""))


@dataclass
class Prepared:
    name: str | None
    arguments: dict | None
    text: str = ""                                     # the arguments as JSON text, as they will be stored and replayed
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    changed: bool = False


class ToolFront:
    def __init__(self, tools: list[dict]):
        self.schemas = {t["function"]["name"]: t["function"].get("parameters") or {"type": "object", "properties": {}} for t in tools}

    def names(self) -> list[str]:
        return list(self.schemas)

    def prepare(self, name: str, arguments: str | None) -> Prepared:
        known = self.names()
        fixed_name = repair.repair_name(name, known, {a: t for a, t in ALIASES.items() if t in self.schemas})
        if fixed_name is None:
            return Prepared(None, None, error=f"There is no tool called {name!r}. Use one of: {', '.join(known)}.")
        notes = [f"the tool {name} was read as {fixed_name}"] if fixed_name != name else []
        args, fixes = repair.repair_arguments(arguments)
        if args is None:
            example = json.dumps({k: "..." for k in (self.schemas[fixed_name].get("required") or [])})
            return Prepared(fixed_name, None, notes=notes, error=f"The arguments of {fixed_name} were not valid JSON. Write them as one JSON object, for example {example}. "
                            f"It takes: {validate.describe_parameters(self.schemas[fixed_name])}.")
        notes += fixes
        args, coerced, problem = validate.coerce(fixed_name, args, self.schemas[fixed_name])
        notes += coerced
        text = json.dumps(args, ensure_ascii=False)
        changed = fixed_name != name or bool(fixes) or bool(coerced)
        return Prepared(fixed_name, args, text, notes, problem, changed)
