"""Checks, attempts and the gate on the final answer.

* ``detect_check`` says whether a shell command is a test, lint, type or scan run, from its text.
* ``tree_fingerprint`` says what the project's copy looks like right now (git's own hash of the whole working tree; when git cannot say, a hash of the files this run has changed). A check is
  RELEVANT to a claim only if it ran on the tree as it is now.
* ``CheckRecorder`` turns the finished run of a check into a record: its kind, its class (passed, failed on the project's own code, could not run here), the key of its failure, and the tree it ran on.
* ``AttemptLedger`` watches what happens between checks: an attempt is a check that follows a change; it ends fixed, the same failure, a new failure, a regression, or an environment problem. Two
  attempts that end the same way are told to the model with the table of attempts; three stop the run (``no_progress``) with what was done.
* ``CompletionGate`` looks at the final answer. An answer that CLAIMS success (tests pass, verified, fixed) needs a passing check that is relevant to the files as they are; without one the answer is
  sent back once, and if the claim is made again the answer is kept but the engine's record says unverified, and says why. The record is the engine's, not the model's: the page's badge comes from it.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

from app.backend.pipeline.agentic.engine import failures as fl

CHECK_KINDS = ("test", "lint", "type", "scan")
_RULES = (
    ("test", re.compile(r"^(?:pytest|py\.test|tox|nosetests?|jest|vitest|mocha|phpunit|rspec)\b|^(?:python3?|py)\s+-m\s+(?:pytest|unittest|nose2?)\b|^npm\s+(?:run\s+)?test\b|^yarn\s+test\b|^pnpm\s+(?:run\s+)?test\b|"
                       r"^go\s+test\b|^cargo\s+test\b|^make\s+(?:test|check)\b|^mvn\s+(?:-\S+\s+)*test\b|^gradle\s+test\b|^dotnet\s+test\b|^ctest\b")),
    ("type", re.compile(r"^(?:mypy|pyright|tsc|pytype)\b|^npx\s+tsc\b|^python3?\s+-m\s+(?:mypy|pyright)\b")),
    ("lint", re.compile(r"^(?:ruff|flake8|pylint|eslint|golangci-lint|clippy|shellcheck|black\s+--check|isort\s+--check)\b|^cargo\s+clippy\b|^npx\s+eslint\b|^python3?\s+-m\s+(?:ruff|flake8|pylint)\b")),
    ("scan", re.compile(r"^(?:semgrep|bandit|gitleaks|trivy|pip-audit|safety\s+check|npm\s+audit|osv-scanner)\b|^python3?\s+-m\s+bandit\b|^node\s+\S*eslint\S*\s")),
)
_STAGE_SPLIT = re.compile(r"(&&|\|\||;|\||\n)")
_NOT_A_RUN = re.compile(r"(?:^|\s)(?:--version|-V|--help|-h|--collect-only|--co|--fixtures|--markers|--setup-plan|--setup-only|--trace-config|--list-tests)(?:\s|=|$)")
_ENV_PREFIX = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")


def detect_check(command: str) -> str | None:
    """``"test"``, ``"lint"``, ``"type"`` or ``"scan"`` when ``command`` is a check whose exit status IS the check's: the stage that runs it is the last one and everything before it is joined by ``&&``
    (``cd src && pytest`` is a check; ``pytest; true``, ``pytest || true`` and ``pytest | tail`` end in the status of something else, and ``pytest --version`` or ``--collect-only`` runs no tests). The first
    word of the stage is the tool: ``echo pytest`` is not a test run."""
    parts = _STAGE_SPLIT.split((command or "").strip())
    stages, seps = parts[0::2], parts[1::2]
    while stages and not stages[-1].strip():
        stages.pop()
        if seps:
            seps.pop()
    for i, stage in enumerate(stages):
        text = _ENV_PREFIX.sub("", stage.strip())
        text = re.sub(r"^(?:cd\s+\S+\s*)", "", text).strip()
        if not text:
            continue
        for kind, pattern in _RULES:
            if pattern.search(text):
                if _NOT_A_RUN.search(text) or i != len(stages) - 1 or any(s != "&&" for s in seps[:i]):
                    return None
                return kind
    return None


# -- the tree ------------------------------------------------------------------------------------------------------------------------------------------------------------------

_TREE_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def virtual_tree(files_changed: list[dict]) -> str:
    """What the tree looks like, as far as this run knows: the files it changed and their hashes."""
    h = hashlib.sha256()
    for e in sorted(files_changed, key=lambda e: e["path"]):
        h.update(f"{e['path']}\0{e.get('sha_after')}\n".encode("utf-8"))
    return "v:" + h.hexdigest()[:40]


def tree_fingerprint(exec_run, container_id: str, roots: list[str], files_changed: list[dict]) -> str:
    """Git's hash of the working tree of each root (``git add -A`` then ``git write-tree``: the same index the review of the work already updates), joined; the virtual tree when git cannot say."""
    parts = []
    for root in roots:
        res, line = {}, ""
        try:
            res = exec_run(container_id, "git add -A >/dev/null 2>&1; git write-tree", workdir=root, timeout=60)
            line = (res.get("output") or "").strip().splitlines()[-1].strip() if (res.get("output") or "").strip() else ""
        except Exception:  # ignored: a tree that cannot be hashed is described by what this run knows of it, below
            pass
        if res.get("exit_code") != 0 or not _TREE_ID.match(line):
            return virtual_tree(files_changed)
        parts.append(line)
    return "g:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:40]


# -- the record of one check ------------------------------------------------------------------------------------------------------------------------------------

def check_record(call: str, command: str, kind: str, payload: dict, tree_before: str, tree_after: str, local_modules=None) -> dict:
    """The record of a check that has finished: ``class`` is ``passed``, ``validation_failed`` or ``validation_env``."""
    output = str(payload.get("output") or payload.get("text") or "")
    code = payload.get("exit_code")
    failure = fl.classify_check(output, code if isinstance(code, int) else None, bool(payload.get("timed_out")), kind, local_modules)
    key, readable = fl.signature(output) if failure and failure.cls == "validation_failed" else ("", failure.reason if failure else "")
    return {"id": "v_" + hashlib.sha1(f"{call}{time.time()}".encode()).hexdigest()[:8], "call": call, "cmd": command[:300], "kind": kind, "exit": code if isinstance(code, int) else None,
            "class": failure.cls if failure else "passed", "signature": key, "readable": readable[:240], "tree_before": tree_before, "tree_after": tree_after,
            "env_reason": failure.reason if failure and failure.cls == "validation_env" else ""}


def relevant(record: dict, current_tree: str) -> bool:
    """Whether a check ran on the tree as it is now: the tree before it or after it is the current one (running a check may itself leave files behind)."""
    return bool(current_tree) and current_tree in (record["tree_before"], record["tree_after"])


# -- the attempts -------------------------------------------------------------------------------------------------------------------------------------------------------------

class AttemptLedger:
    """What happened between one check and the next. ``add`` takes a check record and the files changed since the previous check, and returns the attempt it makes."""

    NUDGE_AFTER = 2          # attempts that end the same way: the model is told, with the table
    STOP_AFTER = 3           # and the run is ended

    def __init__(self, task):
        self.task = task

    @property
    def attempts(self) -> list[dict]:
        return self.task.doc["attempts"]

    def add(self, record: dict, changed_since: list[str]) -> dict:
        prev = self.attempts[-1] if self.attempts else None
        passed = record["class"] == "passed"
        if record["class"] == "validation_env":
            outcome = "env"
        elif passed:
            outcome = "fixed" if prev and prev["outcome"] in ("same_failure", "new_failure", "regressed", "failed") else "passed"
        elif prev is None or prev["outcome"] in ("passed", "fixed"):
            outcome = "regressed" if prev else "failed"
        elif prev["outcome"] == "env":
            outcome = "failed"
        else:
            outcome = "same_failure" if prev["signature"] == record["signature"] else "new_failure"
        seen_trees = [a["tree"] for a in self.attempts]
        attempt = {"n": len(self.attempts) + 1, "check": record["id"], "signature": record["signature"], "readable": record["readable"], "outcome": outcome, "files": changed_since[:12], "tree": record["tree_after"],
                   "oscillating": record["tree_after"] in seen_trees and not passed}
        self.task.add_attempt(attempt)
        return attempt

    def streak(self) -> int:
        """How many of the latest attempts in a row ended in the same failure as the one before (so three identical failures of a check are a streak of two)."""
        n = 0
        for a in reversed(self.attempts):
            if a["outcome"] != "same_failure":
                break
            n += 1
        return n

    def table(self) -> str:
        rows = [f"{a['n']}. changed {', '.join(a['files']) or 'nothing'} -> {a['outcome'].replace('_', ' ')}: {a['readable'][:100]}" for a in self.attempts[-6:]]
        return "\n".join(rows)

    def nudge(self) -> str | None:
        s = self.streak()
        if s >= self.NUDGE_AFTER:
            return ("(A note from the system, not from the owner: the same check has failed in the same way after your last attempts. Do not make another edit of the same kind. Attempts so far:\n"
                    f"{self.table()}\nRead the failing code and the failing check again, say what the cause is, and change a different thing. If you cannot find it, stop and write what you know.)")
        return None

    def stuck(self) -> bool:
        return self.streak() >= self.STOP_AFTER


# -- the final answer -----------------------------------------------------------------------------------------------------------------------------------------------------

_NEGATION = re.compile(r"(?:\bnot\b|n't\b|\bno\b|\bnever\b|\bunable\b|\bcannot\b|\bcould not\b|\bfail(?:s|ed)?\b|\bwithout\b|\bif\b|\bonce\b|\bshould\b|\bwould\b|\bto\b|\bnot yet\b)\s*(?:\w+\s+){0,3}$", re.I)
_CLAIMS = (
    ("tests", re.compile(r"\b(?:all\s+)?(?:the\s+)?(?:unit\s+|existing\s+)?tests?\s+(?:now\s+|all\s+)?(?:pass(?:es|ed|ing)?|succeed(?:s|ed)?|are\s+(?:passing|green))\b|\btests?\s+(?:are\s+)?green\b|\ball\s+(?:the\s+)?checks\s+pass", re.I)),
    ("verified", re.compile(r"\b(?:i\s+(?:have\s+|'ve\s+)?)?(?:verified|confirmed)\b|\bis\s+verified\b|\bhas\s+been\s+verified\b", re.I)),
    ("fixed", re.compile(r"\b(?:is|are|has\s+been|have\s+been|was|were)\s+(?:now\s+)?(?:fixed|patched|resolved|remediated|mitigated)\b|\bI(?:\s+have|'ve)?\s+(?:now\s+)?(?:fixed|patched|resolved|remediated|mitigated)\b|\bno\s+longer\s+(?:vulnerable|reproduc\w*|fails?)\b|\bthe\s+(?:fix|patch)\s+works\b|\bsuccessfully\s+(?:fixed|patched)\b", re.I)),
    ("build", re.compile(r"\bbuild\s+(?:now\s+)?(?:succeeds|passes|is\s+green|works)\b", re.I)),
)


def find_claims(answer: str) -> list[str]:
    """The kinds of success an answer claims (``tests``, ``verified``, ``fixed``, ``build``): a phrase counts unless a word just before it denies or conditions it ('could not verify', 'once the tests pass')."""
    text = answer or ""
    found = []
    for kind, pattern in _CLAIMS:
        for m in pattern.finditer(text):
            before = text[max(0, m.start() - 40):m.start()]
            if _NEGATION.search(before):
                continue
            found.append(kind)
            break
    return found


KIND_FOR = {"tests": ("test",), "verified": ("test", "scan"), "fixed": ("test", "scan"), "build": ("test", "type", "lint")}

# A task about a weakness (an injection, a traversal, a secret in the code, a weak hash...): the project's own tests passing does not show that the weakness is gone, because they were written
# before anyone knew of it. What does is a regression test for the weakness that fails on the original code and passes on the fix, which ``verify_fix`` checks. The evaluation saw a run say
# "all tests passed" about the visible tests while the hidden test for the weakness still failed.
_WEAKNESS = re.compile(r"vulnerab|injection|traversal|\bxss\b|\bssrf\b|\bcsrf\b|\bcve-?\d|\bsecrets?\b|hard-?coded|weak\s+(?:hash|crypto|cipher)|\bmd5\b|\bsha-?1\b|deserializ|\binsecure|\bsecurity\b|\bexploit", re.I)
WEAKNESS_CLAIMS = ("fixed", "verified", "tests")


def is_weakness_task(doc: dict) -> bool:
    """Whether the task is about a weakness: the instruction says so, or a fix check (``verify_fix``) was already made in it."""
    return bool(doc.get("remediations")) or bool(_WEAKNESS.search(doc.get("objective") or ""))


def fresh_verified_fix(doc: dict) -> bool:
    """Whether a ``verify_fix`` verified a fix and nothing has changed since (a verified fix is marked stale by a later change)."""
    return any(r.get("status") == "verified" and not r.get("stale") for r in doc.get("remediations") or [])


@dataclass
class Verdict:
    action: str                  # "ok" | "send_back" | "unverified"
    claims: list[str]
    reason: str = ""
    nudge: str = ""


class CompletionGate:
    MAX_SEND_BACKS = 1

    def __init__(self, task):
        self.task = task
        self.send_backs = 0
        self.weakness_rule = False       # set by the flow when the model has a way to meet it (verify_fix is offered): the claim of a fixed weakness needs a verified fix, not only the project's tests

    def judge(self, answer: str, current_tree: str) -> Verdict:
        """What to do with a final answer (``current_tree`` is the fingerprint of the files now)."""
        d = self.task.doc
        claims = find_claims(answer)
        changed = bool(d["files_changed"])
        good = [v for v in d["validations"] if v["class"] == "passed" and relevant(v, current_tree)]
        env_blocked = [v for v in d["validations"] if v["class"] == "validation_env"]
        missing = []
        for claim in claims:
            if not any(v["kind"] in KIND_FOR[claim] for v in good):
                missing.append(claim)
        if not missing and self.weakness_rule and changed and is_weakness_task(d) and any(c in WEAKNESS_CLAIMS for c in claims) and not fresh_verified_fix(d):
            why = "the project's own tests passing does not show that the weakness is gone: no regression test for it was verified (verify_fix), or the files changed after it was."
            if self.send_backs < self.MAX_SEND_BACKS:
                self.send_backs += 1
                return Verdict("send_back", claims, why, "(A note from the system, not from the owner: your answer says the weakness is fixed, but " + why + " Write a regression test that fails on the original code and passes on "
                               "your fix (for example tests/security/test_<name>.py), run verify_fix on it, and answer from its result. Or take the claim out and say plainly what you changed and what you could not check.)")
            return Verdict("unverified", claims, why)
        if missing:
            why = self._why(missing, d["validations"], good, env_blocked, changed)
            if self.send_backs < self.MAX_SEND_BACKS:
                self.send_backs += 1
                return Verdict("send_back", claims, why, "(A note from the system, not from the owner: your answer says " + ", ".join(self._words(missing)) + f", but {why} Run the project's own check on the files as they are now, "
                               "or take the claim out and say plainly what you did and what you could not check.)")
            return Verdict("unverified", claims, why)
        if changed and not good:
            return Verdict("ok", claims, "files were changed and no check passed on them as they are now")
        return Verdict("ok", claims, "")

    @staticmethod
    def _words(kinds: list[str]) -> list[str]:
        return [{"tests": "that the tests pass", "verified": "that something is verified", "fixed": "that something is fixed", "build": "that the build works"}[k] for k in kinds]

    @staticmethod
    def _why(missing, validations, good, env_blocked, changed: bool = True) -> str:
        if not changed and not validations:
            return "no file was changed in this run (an edit that fails changes nothing), so there is nothing a check could have confirmed."
        if env_blocked and not good:
            return "the only checks that were tried could not run here (" + (env_blocked[-1].get("env_reason") or env_blocked[-1]["readable"] or "the environment") + ")."
        if validations and not good:
            last = validations[-1]
            return f"the last check ({last['cmd'][:60]}) {'failed' if last['class'] != 'passed' else 'ran on files that have changed since'}."
        return "no check was run on the files as they are now."

    def finish(self, answer: str, verdict: Verdict, current_tree: str) -> tuple[str, str, str]:
        """``(the answer to keep, the completion status, the reason)``. An unverified claim stays in the answer, and the engine's record is appended to it."""
        d = self.task.doc
        changed = bool(d["files_changed"])
        good = [v for v in d["validations"] if v["class"] == "passed" and relevant(v, current_tree)]
        if verdict.action == "unverified":
            return (answer.rstrip() + f"\n\nEngine record: unverified. The answer claims success, but {verdict.reason}", "unverified", verdict.reason)
        if changed and not good:
            return answer, "unverified", "files were changed in this run and no check passed on them as they are now"
        return answer, "complete", ""

    def criteria(self, current_tree: str) -> list[dict]:
        d = self.task.doc
        if not d["files_changed"]:
            return []
        good = [v for v in d["validations"] if v["class"] == "passed" and relevant(v, current_tree)]
        out = [{"id": "checked", "text": "a check passed on the files as they are now", "met": bool(good), "by": good[-1]["id"] if good else None}]
        if is_weakness_task(d):
            out.append({"id": "fix_verified", "text": "a regression test for the weakness was verified (verify_fix) on the files as they are now", "met": fresh_verified_fix(d), "by": None})
        return out
