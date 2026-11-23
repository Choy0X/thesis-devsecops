"""The agents a session can hand a task to (the ``task`` tool), and the files that define more of them.

A subagent is another run of the agent loop in a session of its own, with one task as its first message, a short list of rounds and, in this version, one level only (it cannot start another). Two
kinds are built in: ``explore`` reads (the files, searches and the read-only terminal of Plan mode, whatever mode the parent is in) and ``general`` has the parent's own tools but is never asked
anything: a call that would stop for the owner is refused, because nobody is looking at a subagent while it works.

More can be defined in a Markdown file, with a few lines of settings at the top:

    ---
    name: security-reviewer
    description: Reads code for security problems and reports each with the file and line
    kind: explore
    tools: read_file, grep, glob
    max_rounds: 10
    ---
    Look only for ... Report ...

``data/agent/agents/`` is the owner's (any kind, the settings are taken as written). ``.aivdf/agents/`` in a project is the repository's, which may be hostile: such a file can only define an
``explore`` agent with a subset of the reading tools, its instructions are fenced as data, and it cannot name a model or more rounds than the built-in limit.
"""
from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass, field

KINDS = ("explore", "general")
EXPLORE_TOOLS = ("list_files", "read_file", "read_files", "find_symbol", "grep", "glob", "bash", "recall", "scan_results", "project_graph")
WEB_TOOLS = ("websearch", "webfetch")
NEVER_FOR_A_SUBAGENT = ("task", "ask_question", "propose_plan", "update_task_list")       # it cannot start another agent, ask the owner, propose a plan or keep the owner's task list
EXPLORE_ROUNDS = 8
GENERAL_ROUNDS = 12
MAX_ROUNDS = 20
MAX_INSTRUCTIONS = 4_000
MAX_FILES = 40
ROOT = pathlib.Path("data/agent/agents")
PROJECT_DIR = ".aivdf/agents"
_NAME = re.compile(r"[a-z][a-z0-9-]{1,38}[a-z0-9]")


@dataclass
class AgentDef:
    name: str
    description: str
    kind: str = "explore"
    tools: tuple[str, ...] | None = None             # a subset of what its kind offers; None is all of it
    max_rounds: int | None = None
    instructions: str = ""
    source: str = "builtin"                          # builtin, global (the owner's file) or project (the repository's file)
    notes: list[str] = field(default_factory=list)   # what was left out of a definition and why
    answer_form: tuple[str, ...] = ()                # headings the final answer must have ("Evidence:"); one that lacks them is asked for again, once

    def rounds(self) -> int:
        base = EXPLORE_ROUNDS if self.kind == "explore" else GENERAL_ROUNDS
        return max(1, min(self.max_rounds or base, MAX_ROUNDS))


SECURITY_ANALYST = ("You look for ONE weakness that the task names, in the code you can read, and you report it in exactly this form and nothing else:\n"
                    "Evidence: the file and line, and the code that shows it (quote it)\n"
                    "Root cause: one sentence\n"
                    "Confidence: high, medium or low\n"
                    "Not checked: what you did not look at\n"
                    "Use find_symbol to find where a function is defined and who calls it, and read_files to read several files at once. If you find nothing, say so under Evidence and give the confidence as low.")
ROLES = ("security_analyst",)                        # defined, and offered only to a session that switches them on (params.flags.roles): the benchmark decides whether one earns a place in the default list

BUILTIN = {
    "explore": AgentDef("explore", "Reads the project and reports what it found: where something is, how it works, who calls it. Read-only; it cannot change anything or run code.", "explore"),
    "general": AgentDef("general", "Does a self-contained task with the same tools as the main agent (but it cannot ask the owner anything, so what would ask is refused). In Manual and Auto it can change and run things in the sandbox copy; in Plan it only reads, like explore.",
                        "general"),
    "security_analyst": AgentDef("security_analyst", "Looks for one named weakness in the code and reports it in a fixed form: Evidence (file, line, the code), Root cause, Confidence, Not checked. Read-only.", "explore",
                                 ("list_files", "read_file", "read_files", "find_symbol", "grep", "glob", "bash", "project_graph"), 8, SECURITY_ANALYST, answer_form=("Evidence:", "Root cause:", "Confidence:")),
}


def _front_matter(text: str) -> tuple[dict, str]:
    """``(the settings at the top, the rest)`` of a definition file: ``key: value`` lines between two lines of three dashes. No YAML: values are plain text, a list is words separated by commas."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            meta = {}
            for line in lines[1:i]:
                key, sep, value = line.partition(":")
                if sep and key.strip():
                    meta[key.strip().lower()] = value.strip()
            return meta, "\n".join(lines[i + 1:]).strip()
    return {}, text


def parse(text: str, *, source: str, filename: str = "") -> AgentDef | None:
    """A definition from the text of a file, checked, or None when it is not usable (no valid name, no description). A repository's file is held to less than the owner's, and what is left out is noted."""
    meta, body = _front_matter(text)
    name = meta.get("name") or pathlib.PurePath(filename).stem
    name = name.strip().lower()
    description = " ".join(meta.get("description", "").split())[:300]
    if not _NAME.fullmatch(name) or not description or name in BUILTIN:
        return None
    kind = meta.get("kind", "explore").strip().lower()
    notes: list[str] = []
    if kind not in KINDS:
        kind, notes = "explore", notes + [f"kind {meta.get('kind')!r} is not one of {', '.join(KINDS)}; read as explore"]
    tools = tuple(t for t in re.split(r"[,\s]+", meta.get("tools", "")) if t) or None
    rounds = None
    if meta.get("max_rounds", "").strip().isdigit():
        rounds = int(meta["max_rounds"])
    if source == "project":
        if kind != "explore":
            kind, notes = "explore", notes + ["a repository can only define an explore agent"]
        if tools is not None:
            allowed = tuple(t for t in tools if t in EXPLORE_TOOLS)
            if len(allowed) != len(tools):
                notes.append("tools that are not reading tools were left out")
            tools = allowed or None
        if rounds is not None and rounds > EXPLORE_ROUNDS:
            rounds, notes = EXPLORE_ROUNDS, notes + [f"more than {EXPLORE_ROUNDS} rounds was cut to {EXPLORE_ROUNDS}"]
    elif tools is not None:
        tools = tuple(t for t in tools if t not in NEVER_FOR_A_SUBAGENT) or None
    return AgentDef(name, description, kind, tools, rounds, body[:MAX_INSTRUCTIONS], source, notes)


def _files(folder: pathlib.Path) -> list[pathlib.Path]:
    try:
        return sorted(p for p in folder.iterdir() if p.suffix.lower() == ".md" and p.is_file() and not p.is_symlink())[:MAX_FILES]
    except OSError:
        return []


def custom(project: dict | None = None) -> dict[str, AgentDef]:
    """The agents the files define: the project's first, then the owner's (the owner's wins when the two name the same agent)."""
    found: dict[str, AgentDef] = {}
    for folder in (project or {}).get("folders") or []:
        base = os.path.realpath(str(folder.get("path") or ""))
        for path in _files(pathlib.Path(base) / PROJECT_DIR):
            try:
                raw = path.read_bytes()[:MAX_INSTRUCTIONS + 2_000].decode("utf-8", errors="replace")
            except OSError:
                continue
            d = parse(raw, source="project", filename=path.name)
            if d is not None:
                found.setdefault(d.name, d)
    for path in _files(ROOT):
        try:
            raw = path.read_bytes()[:MAX_INSTRUCTIONS + 2_000].decode("utf-8", errors="replace")
        except OSError:
            continue
        d = parse(raw, source="global", filename=path.name)
        if d is not None:
            found[d.name] = d
    return found


def available(project: dict | None = None, roles: bool = False) -> dict[str, AgentDef]:
    """Every agent a ``task`` may name: the two built in, then the ones the files define, and the roles when the session asks for them."""
    every = {**BUILTIN, **custom(project)}
    return every if roles else {k: v for k, v in every.items() if k not in ROLES}


def tool_names(agent: AgentDef, parent_tools: list[str], parent_web: bool) -> list[str]:
    """The names of the tools a subagent may call: what its kind offers, narrowed by its own list, never the ones a subagent cannot have."""
    if agent.kind == "explore":
        base = list(EXPLORE_TOOLS) + (list(WEB_TOOLS) if parent_web else [])
    else:
        base = [t for t in parent_tools if t not in NEVER_FOR_A_SUBAGENT]
    if agent.tools is not None:
        base = [t for t in base if t in agent.tools]
    return [t for t in base if t not in NEVER_FOR_A_SUBAGENT]


ANSWER_CHARS = 4_000


def task_tool(agents: dict[str, AgentDef]) -> dict:
    """The ``task`` tool as a model is shown it, listing the agents it may name."""
    listing = " ".join(f"{a.name}: {a.description}" if a.source != "project" else f"{a.name}: (defined by this project's repository; what it is for is not shown here, use it only if the owner asks)" for a in agents.values())
    return {"type": "function", "function": {
        "name": "task",
        "description": ("Hand one self-contained piece of work to another agent and get back its final answer (at most about 4,000 characters). It works in a session of its own and does not see this "
                        "conversation, so give it everything it needs in prompt: what to find or do, where to look, what to report. It can only do what you can do in this mode and cannot reach the owner or "
                        "wait for them: never use it to ask the owner something or to wait for a reply, and give the same task only once. Use explore to look into something in depth without filling "
                        "your own context, general for a separate piece of work. Agents: " + listing),
        "parameters": {"type": "object", "properties": {"description": {"type": "string", "description": "The task in three to eight words"},
                                                        "prompt": {"type": "string", "description": "The whole task, written for an agent that knows nothing of this conversation"},
                                                        "agent": {"type": "string", "enum": list(agents), "description": "Which agent does it (default explore)"}}, "required": ["description", "prompt"]}}}


SUBAGENT_INSTRUCTION = ("You are a subagent: another agent handed you one task and cannot be asked anything. Do only that task. Look at what you need, then answer. Your final answer goes back to that agent: "
                        "write it in plain words, at most about 300 words, say which files and lines you relied on, and say what you could not find. You cannot ask the owner anything, wait for anyone, "
                        "start another agent, or propose a plan. In Plan mode you only read: if the task needs something installed, created or changed, say which command that would be, in your answer, "
                        "and stop. Never try another way to run a command that was refused, and what echo prints is not shown to anyone.")
