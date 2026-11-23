"""What went wrong when a call failed, and whose fault it was.

The loop used to see only ``ok: true`` or ``ok: false``, and a failing test is ``ok: true`` with an exit code of 1. A model that is told "that failed" does not know whether to change its
arguments, read the file again, give up on a command, or tell the owner that the environment cannot do this. A run that counts every failure against the model stops it for a missing network.
So each failure gets a CLASS, from the call and what came back, by a table (no model is asked):

| class | meaning | counts against the model |
|---|---|---|
| ``model_invalid_args`` | the call did not fit the tool (bad JSON, missing or unknown argument, no such tool) | yes |
| ``model_wrong_target`` | the file, text or place the call named is not there, or is not unique | yes |
| ``policy_denied`` | a rule or the owner's instruction forbids it | no (a rule is not a mistake, but repeating it is: the loop guard counts that) |
| ``conflict`` | the file changed since the model read it | no |
| ``tool_env`` | the sandbox, the daemon, the journal or a timeout: not the model's doing | no |
| ``command_failed`` | an ordinary command that exited with an error | yes |
| ``validation_failed`` | a test, lint, type or scan command ran and failed on the project's own code | yes |
| ``validation_env`` | a check could not run here: a dependency is missing, there is no network, the program is not there | no, and any claim of success is blocked |

``signature`` turns the output of a failed check into a short key (the failing test ids and the first error line, with numbers and addresses taken out), so that the same failure on the next run
is recognised as the same.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Callable

MODEL_CLASSES = ("model_invalid_args", "model_wrong_target", "command_failed", "validation_failed")
ENV_CLASSES = ("tool_env", "validation_env")
CLASSES = (*MODEL_CLASSES, "policy_denied", "conflict", *ENV_CLASSES)

_INVALID = re.compile(r"not valid JSON|do not fit the tool|must be an object|There is no tool called|takes:|is required|missing .*argument|unexpected keyword|Unknown argument|Give the |Name a file|arguments of", re.I)
_DENIED = re.compile(r"^Not allowed|Not allowed:|never allowed|is forbidden|owner's instruction|protected by", re.I)
_CONFLICT = re.compile(r"changed since you read it|has changed since", re.I)
_WRONG_TARGET = re.compile(r"No such file|not found|does not exist|is not a file|is not a folder|old text|not unique|more than once|appears \d+ times|could not find|cannot find|is a folder|outside the project|no match", re.I)
_ENV = re.compile(r"sandbox|docker|container|daemon|timed out|time limit|temporar(?:y|ily)|could not be started|journal could not|unexpectedly|database|network is|connection (?:refused|reset)", re.I)

# a check that cannot run here (as opposed to one that ran and failed)
_VALIDATION_ENV = re.compile(
    r"command not found|: not found|is not recognized|No such file or directory: ['\"]?(?:pytest|python|npm|node|go|cargo|make)\b|Cannot find module|npm ERR!|ENOTFOUND|EAI_AGAIN|Could not resolve host|"
    r"Temporary failure in name resolution|Network is unreachable|externally-managed-environment|no tests ran|collected 0 items|no test files|no tests to run|Connection refused|Max retries exceeded|"
    r"verification could not run", re.I)
_TEST_SUMMARY = re.compile(r"\b\d+\s+(?:passed|tests?\s+passed)\b|\bpassed\b|\bRan\s+\d+\s+tests?\b|\btest result:\s*ok\b|^ok\s+\S+|\bTests?:\s+\d+|\b\d+\s+(?:specs?|examples?|tests?)\b.*\b0 failures\b|\bPASS\b|^OK\b",
                           re.I | re.M)
_MISSING_MODULE = re.compile(r"(?:ModuleNotFoundError|ImportError): No module named ['\"]?([A-Za-z0-9_.]+)")
_PYTEST_ENV_EXITS = (2, 4, 5, 126, 127)          # interrupted/collection error, usage error, no tests, not executable, not found


@dataclass
class Failure:
    cls: str
    reason: str

    @property
    def counts_against_model(self) -> bool:
        return self.cls in MODEL_CLASSES

    @property
    def bucket(self) -> str:
        """The key in ``spent.failures``: model, env or policy."""
        return "model" if self.cls in MODEL_CLASSES else ("policy" if self.cls in ("policy_denied", "conflict") else "env")


def payload_of(payload_text: str) -> dict:
    try:
        data = json.loads(payload_text or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def error_text(summary: str, payload: dict) -> str:
    return " ".join(str(x) for x in (summary, payload.get("error"), payload.get("note")) if x)


def classify_check(command_output: str, exit_code: int | None, timed_out: bool, kind: str, local_modules: Callable[[], set[str]] | None = None) -> Failure | None:
    """A test, lint, type or scan command that exited with ``exit_code``: ``None`` when it passed, else why it did not. ``local_modules`` (lazy) names the project's own top-level modules, so that a
    missing import of the project's own code is the model's mistake while a missing third-party package is the environment's."""
    if timed_out:
        return Failure("validation_env", "the check did not finish in the time it was given")
    if exit_code is None:
        return Failure("validation_env", "the check did not report how it ended (a job left running in the background?)")
    if exit_code == 0:
        if kind == "test" and not _TEST_SUMMARY.search(command_output or ""):
            return Failure("validation_env", "the command exited 0 but its output reports no test run, so nothing shows that tests ran")
        return None
    text = command_output or ""
    miss = _MISSING_MODULE.search(text)
    if miss:
        root = miss.group(1).split(".")[0]
        if local_modules is not None and root in local_modules():
            return Failure("validation_failed", f"the project's own module {root} cannot be imported")
        return Failure("validation_env", f"the module {root} is not installed in this sandbox (it has no network)")
    if _VALIDATION_ENV.search(text):
        return Failure("validation_env", "the check could not run here: " + (_VALIDATION_ENV.search(text).group(0)[:80]))
    if exit_code in (126, 127):
        return Failure("validation_env", "the program of the check is not available here")
    if "pytest" in text.lower() and exit_code in (4, 5):
        return Failure("validation_env", "pytest did not collect any test")
    return Failure("validation_failed", f"the check exited with {exit_code}")


def classify_call(name: str, ok: bool, summary: str, payload_text: str, *, is_check: bool = False, check_kind: str = "", local_modules: Callable[[], set[str]] | None = None) -> Failure | None:
    """The failure of one finished call, or ``None`` when it did not fail. A command that exits non-zero is ``ok: true`` for the loop; here it is a failure of the kind it is."""
    data = payload_of(payload_text)
    if not ok:
        text = error_text(summary, data)
        if _CONFLICT.search(text):
            return Failure("conflict", "the file changed since it was read")
        if _DENIED.search(text):
            return Failure("policy_denied", "a rule or the owner's instruction forbids this")
        if _INVALID.search(text):
            return Failure("model_invalid_args", "the call did not fit the tool")
        if _WRONG_TARGET.search(text):
            return Failure("model_wrong_target", "the file or text the call named is not there, or is not unique")
        if _ENV.search(text):
            return Failure("tool_env", "the sandbox or a service failed")
        return Failure("model_wrong_target", "the call failed")
    if name in ("bash", "run_command"):
        code = data.get("exit_code")
        timed_out = bool(data.get("timed_out"))
        if is_check:
            return classify_check(str(data.get("output") or data.get("text") or ""), code if isinstance(code, int) else None, timed_out, check_kind, local_modules)
        if timed_out:
            return Failure("tool_env", "the command did not finish in the time it was given")
        if isinstance(code, int) and code not in (0,):
            return Failure("command_failed", f"the command exited with {code}")
    return None


# -- signatures ------------------------------------------------------------------------------------------------------------------------------------------------------------

_FAILED_ID = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M)
_UNITTEST_ID = re.compile(r"^(?:FAIL|ERROR):\s+(\S+)", re.M)
_ERROR_LINE = re.compile(r"^(?:E\s+)?([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Failure)\b.*|AssertionError.*|assert .*)$", re.M)
_NOISE = [(re.compile(r"0x[0-9a-fA-F]+"), "0xN"), (re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds)\b"), "T"), (re.compile(r"\b\d+\b"), "N"), (re.compile(r"[\\/][^\s'\"]+"), "PATH"),
          (re.compile(r"\s+"), " ")]


def normalise(line: str) -> str:
    for pattern, repl in _NOISE:
        line = pattern.sub(repl, line)
    return line.strip().lower()[:200]


def signature(output: str) -> tuple[str, str]:
    """``(key, readable)`` of a failed check's output: its failing test ids (numbers and paths taken out of the line, ids kept) and its first error line. The same failure on the next run has the same key."""
    text = output or ""
    ids = sorted(set(_FAILED_ID.findall(text)) | set(_UNITTEST_ID.findall(text)))
    first = _ERROR_LINE.search(text)
    line = normalise(first.group(1)) if first else normalise(next((l for l in text.splitlines() if l.strip()), ""))
    readable = (", ".join(ids[:4]) + (" ..." if len(ids) > 4 else "") + (": " if ids and line else "") + line)[:240]
    key = hashlib.sha1(("|".join(ids) + "||" + line).encode("utf-8")).hexdigest()[:12]
    return key, readable
