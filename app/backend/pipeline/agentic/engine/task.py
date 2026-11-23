"""The task a run works on: what was asked, what the owner ruled out, the plan, what has changed, what was checked, and whether the work is done.

``session.task`` (one document, kept with the session) is written by the ENGINE. The model writes only the text and the done flag of the plan steps, through the task-list tool it always had; the
files that changed, the checks that ran and their outcomes, the observations and the completion status are the engine's own record of what happened, never the model's claim about it.

* ``objective``: the owner's instruction, as written (at most 2,000 characters).
* ``constraints``: the sentences of the instruction that rule something out ("do not change the tests"). Where a constraint names files, ``protect`` holds the path patterns, and the engine REFUSES a
  write to them (a rule of the application, not a sentence in a prompt).
* ``plan``: steps ``{id, text, state: pending|active|done|blocked|skipped, files, evidence}``.
* ``observations``: short facts the engine noted, the newest 50.
* ``files_changed``: a list ``{path, sha_before, sha_after, calls}`` (a list, because a path is not a safe field name).
* ``validations`` and ``attempts``: see ``gates.py``.
* ``completion``: ``{status: open|complete|unverified|partial, criteria, reason}``.
"""
from __future__ import annotations

import fnmatch
import posixpath
import re
import secrets
from typing import Iterable

from app.backend.pipeline.agentic.engine.context import mask_result
from app.backend.pipeline.verify.runner import patch_paths

MAX_OBJECTIVE = 2000
MAX_OBSERVATIONS = 50
MAX_VALIDATIONS = 100
MAX_ATTEMPTS = 50
MAX_READS = 200
MAX_CONSTRAINTS = 8
STEP_STATES = ("pending", "active", "done", "blocked", "skipped")
TASK_STATES = ("planning", "executing", "validating", "replanning", "awaiting_approval", "completed", "failed", "cancelled")

TEST_PATTERNS = ["tests/*", "*/tests/*", "test/*", "*/test/*", "test_*.py", "*/test_*.py", "*_test.py", "*/*_test.py", "*.test.*", "*.spec.*", "conftest.py", "*/conftest.py"]
_NEG = re.compile(r"\b(?:do not|don't|dont|never|must not|should not|cannot|can't|without|avoid)\b", re.I)
_VERB = r"(?:change|changing|edit|editing|modify|modifying|touch|touching|alter|altering|rewrite|rewriting|delete|deleting|remove|removing|update|updating|break|breaking|replace|replacing|skip|skipping|disable|disabling)"
_ANY_FILE = re.compile(rf"(?:\bnot\b|n't|\bnever\b|\bwithout\b|\bavoid\b)\W+(?:\w+\W+){{0,3}}?(?:{_VERB}|write|writing|create|creating)\W+(?:any|a|every|all)\W+(?:of\W+the\W+)?(?:project\W+|source\W+|code\W+)?files?\b", re.I)
_TESTS_OF = re.compile(rf"(?:\bnot\b|n't|\bnever\b|\bwithout\b|\bavoid\b)\W+(?:\w+\W+){{0,3}}?{_VERB}\W+(?:\w+\W+){{0,2}}?(?:tests?|test files?|test suite|test folder|tests folder|specs?)\b", re.I)
_PATH_OF = re.compile(rf"(?:\bnot\b|n't|\bnever\b|\bwithout\b|\bavoid\b)\W+(?:\w+\W+){{0,3}}?{_VERB}\W+(?:the\s+|any\s+|existing\s+)*(?:file\s+|folder\s+|directory\s+)?[`'\"]?([\w./-]*[\w-]\.\w{{1,6}}|[\w./-]+/)[`'\"]?", re.I)
_FOLDER_OF = re.compile(rf"(?:\bnot\b|n't|\bnever\b|\bwithout\b|\bavoid\b)\W+(?:\w+\W+){{0,3}}?{_VERB}\W+(?:the\s+|any\s+)*[`'\"]?([\w.-]+)[`'\"]?\s+(?:folder|directory|dir)\b", re.I)


CONTROL_FILES = {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", ".coveragerc", "noxfile.py", "unittest.cfg", "nose2.cfg"}      # files that change how tests are found or run

FIX_WORDS = re.compile(r"\b(?:fix|fixes|resolve|repair|patch|remediate|correct)\b|\bfailing tests?\b|\bmake\b.{0,40}\b(?:pass|work)\b", re.I)          # the owner asked for something to be put right, not for an explanation

# What a fix does not need to touch: the project's documents and the files of its tooling. A run that was asked to fix something and also wrote a README, a changelog or a CI file (the evaluation saw
# eleven such files in one run) changed what nobody asked it to change. Each group lists the words with which an instruction asks for those files (then the group is left out of the rule) and the patterns.
SCOPE_GROUPS: list[tuple[tuple[str, ...], list[str]]] = [
    (("readme", "document"), ["README*", "*/README*"]),
    (("changelog", "change log", "release notes", "document"), ["CHANGELOG*", "*/CHANGELOG*"]),
    (("contributing",), ["CONTRIBUTING*", "*/CONTRIBUTING*"]),
    (("license", "licence"), ["LICENSE*", "*/LICENSE*"]),
    ((".md", "markdown", "document", "readme", "changelog", "contributing", "license", "licence", "docs"), ["*.md"]),
    ((".rst", "document", "readme", "changelog", "docs"), ["*.rst"]),
    (("docs", "documentation", "document"), ["docs/*", "*/docs/*"]),
    ((".github", "workflow", "github actions", "pipeline", " ci "), [".github/*", "*/.github/*"]),
    (("dockerfile", "docker"), ["Dockerfile*", "*/Dockerfile*"]),
    (("makefile",), ["Makefile", "*/Makefile"]),
    ((".ini",), ["*.ini"]),
    ((".cfg",), ["*.cfg"]),
    ((".yml", ".yaml", "yaml", "workflow", ".github", " ci "), ["*.yml", "*.yaml"]),
]
SCOPE_TEXT = ("A fix changes the code that has the fault, not the project's documents or the files of its tooling (README, CHANGELOG, docs, CI, Docker, Make and configuration files). "
              "The owner can ask for them by name.")


def scope_constraint(instruction: str) -> dict | None:
    """The constraint the engine adds to a task that asks for a fix: do not write the documents and tooling files the instruction does not name. None for an instruction that is not a fix, and when it
    names every group. It is an ordinary constraint (``protect`` patterns, enforced by the same refusal and the same check of the tree), marked ``implicit`` so that nobody reads it as the owner's words."""
    text = f" {(instruction or '').lower()} "
    if not FIX_WORDS.search(instruction or ""):
        return None
    patterns = [pat for words, pats in SCOPE_GROUPS if not any(w in text for w in words) for pat in pats]
    return {"text": SCOPE_TEXT, "protect": patterns, "implicit": True} if patterns else None


def is_control_file(path: str) -> bool:
    return normal_path(path).rsplit("/", 1)[-1].lower() in CONTROL_FILES


def new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(4)


def protect_paths(sentence: str) -> list[str]:
    """The path patterns a sentence of the instruction rules out for writing, or none."""
    out: list[str] = []
    if _ANY_FILE.search(sentence):                                  # "do not change any file": no write at all, a new file included
        return ["*"]
    if _TESTS_OF.search(sentence):
        out += TEST_PATTERNS
    for m in _PATH_OF.finditer(sentence):
        path = m.group(1).strip("./") if m.group(1).startswith("./") else m.group(1)
        if path.lower() in ("it", "them", "this", "that", "anything", "e.g", "i.e"):
            continue
        out += [path + "*"] if path.endswith("/") else [path, "*/" + path]
    for m in _FOLDER_OF.finditer(sentence):
        folder = m.group(1)
        out += [folder + "/*", "*/" + folder + "/*"]
    seen, unique = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique[:24]


def constraints_of(instruction: str) -> list[dict]:
    """The sentences of ``instruction`` that rule something out, each with the paths it protects (if it names any)."""
    out = []
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", instruction or ""):
        sentence = sentence.strip()
        if sentence and len(sentence) <= 400 and _NEG.search(sentence):
            only_tests = bool(_TESTS_OF.search(sentence)) and not _PATH_OF.search(sentence) and not _FOLDER_OF.search(sentence)
            out.append({"text": sentence[:300], "protect": protect_paths(sentence), **({"allow_new": True} if only_tests else {})})        # "do not change the tests" does not forbid adding a test
        if len(out) >= MAX_CONSTRAINTS:
            break
    return out


def normal_path(path: str) -> str:
    """``path`` the way the tools will open it: forward slashes, ``.`` and ``//`` and ``a/../`` collapsed, no leading ``./`` or ``/``."""
    p = str(path or "").replace("\\", "/").strip()
    if not p:
        return ""
    p = posixpath.normpath(p)
    return "" if p == "." else p.lstrip("/")


def protected_by(constraints: Iterable[dict], path: str, is_new=None) -> dict | None:
    """The constraint that forbids writing ``path``, or None. A rule about the tests (``allow_new``) forbids changing the tests that exist, not adding one: ``is_new`` is a function that says whether the
    path is a file the project did not have, and a new file passes unless it changes how tests are found or run (``conftest.py``, ``pytest.ini`` ...). Without ``is_new`` nothing is let through."""
    p = normal_path(path)
    for c in constraints or []:
        if any(fnmatch.fnmatch(p, pat) for pat in c.get("protect") or []):
            if c.get("allow_new") and is_new is not None and not is_control_file(p) and is_new(p):
                continue
            return c
    return None


def diff_paths(patch_text: str) -> list[str]:
    """The files a patch touches: new, changed, deleted, renamed, copied, or only given another mode, with the names git had to quote read back (the verifier reads patches the same way)."""
    return patch_paths(patch_text or "")


def steps_from_text(text: str) -> list[str]:
    """The steps of a plan written as prose: numbered or bulleted lines (or, with none, the sentences), at most 30."""
    lines = [re.sub(r"^\s*(?:\d+[.)]|[-*+])\s+", "", l).strip() for l in (text or "").splitlines() if re.match(r"^\s*(?:\d+[.)]|[-*+])\s+\S", l)]
    if not lines:
        lines = [s.strip() for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s.strip()]
    return [l[:300] for l in lines][:30]


class TaskState:
    """The task document of a session and the operations on it. ``save`` writes it to the session; every method changes it in memory only."""

    def __init__(self, store, project_id: str, session_id: str, doc: dict):
        self.store, self.project_id, self.session_id, self.doc = store, project_id, session_id, doc

    # -- creating -----------------------------------------------------------------------------------------------------------------------------------------------------

    @classmethod
    def for_instruction(cls, store, project_id: str, session_id: str, instruction_id: str | None, instruction: str, mode: str, scope: bool = True) -> tuple["TaskState", bool]:
        """``(the task, whether it continues the task of the same instruction)``. A new instruction starts a new task; the plan of a Plan-mode task is carried into the Build task that follows it.
        A Build task that asks for a fix also gets the implicit scope constraint (``scope_constraint``) unless ``scope`` is off."""
        meta = store.meta(project_id, session_id) or {}
        old = meta.get("task") or {}
        kind = "plan" if mode == "plan" else "build"
        if old and old.get("instruction_id") == instruction_id and instruction_id is not None:
            old["mode"] = kind
            return cls(store, project_id, session_id, old), True
        constraints = constraints_of(instruction)
        implicit = scope_constraint(instruction) if scope and kind == "build" else None
        if implicit:
            constraints.append(implicit)
        doc = {"id": new_id("t_"), "instruction_id": instruction_id, "objective": (instruction or "")[:MAX_OBJECTIVE], "constraints": constraints, "mode": kind, "state": "planning" if kind == "plan" else "executing",
               "plan": [], "observations": [], "files_changed": [], "reads": [], "validations": [], "attempts": [], "remediations": [],
               "completion": {"status": "open", "criteria": [], "reason": ""}}
        if old.get("mode") == "plan" and kind == "build" and old.get("plan"):
            doc["plan"] = [{**s, "state": "pending", "evidence": []} for s in old["plan"] if s.get("state") != "skipped"]
            doc["carried_plan_from"] = old.get("id")
        return cls(store, project_id, session_id, doc), False

    def save(self) -> None:
        self.store.commit(self.project_id, self.session_id, set_={"task": self.doc})

    # -- the plan ---------------------------------------------------------------------------------------------------------------------------------------------------------

    def set_plan(self, texts: list[str], done: list[bool] | None = None) -> bool:
        """Replace the plan's steps by ``texts`` (keeping the state and the evidence of a step whose text is the same). Returns whether the plan changed."""
        old = {s["text"]: s for s in self.doc["plan"]}
        steps = []
        for i, text in enumerate(texts):
            kept = old.get(text)
            state = "done" if (done and i < len(done) and done[i]) else (kept["state"] if kept and kept["state"] != "done" else "pending")
            steps.append({"id": kept["id"] if kept else f"s{i + 1}", "text": text, "state": state, "files": kept["files"] if kept else [], "evidence": kept["evidence"] if kept else []})
        changed = [(s["text"], s["state"]) for s in steps] != [(s["text"], s["state"]) for s in self.doc["plan"]]
        self.doc["plan"] = steps
        if steps and not any(s["state"] == "active" for s in steps):
            first = next((s for s in steps if s["state"] == "pending"), None)
            if first is not None:
                first["state"] = "active"
        return changed

    def block_active_step(self) -> None:
        for s in self.doc["plan"]:
            if s["state"] == "active":
                s["state"] = "blocked"

    def unresolved_steps(self) -> list[str]:
        return [s["text"] for s in self.doc["plan"] if s["state"] in ("pending", "active", "blocked")]

    # -- what the engine saw ------------------------------------------------------------------------------------------------------------------------------------------

    def observe(self, kind: str, text: str, call: str | None = None) -> None:
        self.doc["observations"].append({"id": new_id("o_"), "kind": kind, "text": str(mask_result(str(text)))[:300], **({"call": call} if call else {})})
        del self.doc["observations"][:-MAX_OBSERVATIONS]

    def file_changed(self, path: str, sha_before: str | None, sha_after: str | None, call: str) -> None:
        for entry in self.doc["files_changed"]:
            if entry["path"] == path:
                entry["sha_after"] = sha_after
                if call not in entry["calls"]:
                    entry["calls"].append(call)
                break
        else:
            self.doc["files_changed"].append({"path": path, "sha_before": sha_before, "sha_after": sha_after, "calls": [call]})
        for s in self.doc["plan"]:
            if s["state"] == "active" and path not in s["files"]:
                s["files"].append(path)
        self.note_read(path, sha_after)

    def changed_paths(self) -> list[str]:
        return [e["path"] for e in self.doc["files_changed"]]

    def note_read(self, path: str, sha: str | None) -> None:
        """The hash of a file as the model last saw it (what it read, or what it wrote): an edit of a file that has changed since is refused once."""
        reads = self.doc.setdefault("reads", [])
        for r in reads:
            if r["path"] == path:
                r["sha"] = sha
                return
        reads.append({"path": path, "sha": sha})
        del reads[:-MAX_READS]

    def read_hash(self, path: str) -> str | None:
        return next((r["sha"] for r in self.doc.get("reads", []) if r["path"] == path), None)

    def add_validation(self, record: dict) -> None:
        self.doc["validations"].append(mask_result(record))                  # a command line, a failure's first line and the condensed output can all carry a secret out of a container
        del self.doc["validations"][:-MAX_VALIDATIONS]

    def add_attempt(self, record: dict) -> None:
        self.doc["attempts"].append(record)
        del self.doc["attempts"][:-MAX_ATTEMPTS]

    # -- the end ------------------------------------------------------------------------------------------------------------------------------------------------------------

    def set_state(self, state: str) -> None:
        if state in TASK_STATES:
            self.doc["state"] = state

    def complete(self, status: str, reason: str, criteria: list[dict] | None = None) -> None:
        """Record how the work ended. The states of the plan's steps are the model's own report and are left as it wrote them."""
        self.doc["completion"] = {"status": status, "reason": reason[:300], "criteria": criteria if criteria is not None else self.doc["completion"].get("criteria", [])}

    def digest(self) -> str:
        """A few plain lines of where the task stands, for the last round of a run that ran out of budget and for the summary that replaces old rounds."""
        d = self.doc
        lines = [f"Objective: {d['objective'][:400]}"]
        if d["constraints"]:
            lines.append("The owner ruled out: " + " | ".join(c["text"] for c in d["constraints"]))
        if d["files_changed"]:
            lines.append("Files changed: " + ", ".join(self.changed_paths()[:20]))
        last = d["validations"][-1] if d["validations"] else None
        if last:
            lines.append(f"Last check: {last['cmd'][:80]} -> {last['class']}" + (f" ({last['readable'][:120]})" if last.get("readable") else ""))
        open_steps = self.unresolved_steps()
        if open_steps:
            lines.append("Still to do: " + "; ".join(open_steps[:6]))
        return "\n".join(lines)
