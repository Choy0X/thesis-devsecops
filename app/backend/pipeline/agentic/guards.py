"""When a repeated call is a loop.

The first guard was a set of every call of a run: asking for the same thing twice ended the run, even when a file had changed between the two (a test run again after an edit is the whole
point of a fix). A call is a loop when it gives the same result again, so the guard watches results: the same call with the same result three times in a row (since the last edit) gets a
note appended to the result, and the fifth is not run at all. An edit changes what the same call can return, so it clears the count.
"""
from __future__ import annotations

import hashlib
import json
import re

NUDGE_AT = 3                     # the third identical result carries a note to the model
NUDGE_READS_AT = 2               # a read (a file, a folder, a search) that gives the same thing twice is already known: the second carries the note
READS = {"read_file", "list_files", "find_files", "search_code", "grep", "glob", "project_graph", "scan_results"}
STOP_AFTER = 4                   # after this many identical results the same call is no longer run
STALE_ROUNDS = 5                 # this many rounds in a row in which every call only repeated one made before (the same call, the same result, no edit since): the run is going round in a circle
WRITES = {"write_file", "edit_file", "apply_patch"}
VOLATILE = {"duration_ms", "recovery", "commits", "child", "rounds"}   # what differs between two runs of a call that gave the same result: how long a command took, the handle its long output was saved under, the commit a checkpoint made
NOTE = ("You have now made this exact call {n} times and it returned the same thing each time. Repeating it will not find more. Use what it returned, or try a different approach "
        "(another file, another pattern, another command), or write your answer from what you have.")


# the words of a question about dependencies: what the dependency scan is for. "How can we optimize the app" has none of them, and the scan is not run for it.
_DEPENDENCY_WORDS = re.compile(r"dependenc|\bpackages?\b|vulnerab|\bcves?\b|advisor|outdated|out[- ]of[- ]date|\baudit|upgrad|supply[- ]chain|lock ?files?|node_modules|third[- ]party|\blibrar(?:y|ies)\b|"
                               r"\bversions?\b|\b(?:npm|pnpm|yarn|bun|pip|pipx|poetry|cargo|composer|gradle|maven|nuget|sbom|snyk|dependabot)\b|requirements\.txt|package\.json|pyproject|pom\.xml|go\.mod", re.I)


def asks_about_dependencies(text: str) -> bool:
    """Whether what the owner wrote is a question about the project's dependencies (their versions, advisories, being outdated, a lock file, a package manager): the only kind of question the
    dependency scan is run for. A question about how the app runs or how to improve it is not, and a scan started for it is noise that sent the agent off installing packages."""
    return bool(_DEPENDENCY_WORDS.search(str(text or "")))


def digest(payload: str) -> str:
    return hashlib.sha1((payload or "").encode("utf-8", "replace")).hexdigest()


_COMMIT_ID = re.compile(r"\b[0-9a-f]{7,40}\b")
_AGO = re.compile(r"\b\d+ (?:second|minute|hour|day|week|month|year)s? ago\b")


def result_digest(result: dict) -> str:
    """The digest of a tool's result for the loop guard: the result without what changes on every run of the same call: a command's duration, the handle of its saved output, and in the text of a result
    the ids of commits and how long ago something was (``git log -1`` after a checkpoint names a new commit and a new number of seconds each time, which made a run that was cycling through a checkpoint
    and a look at the last commit look as if each round found something new). Without this a command that prints the same thing every time never looked like a loop."""
    return digest(json.dumps(_plain({k: v for k, v in result.items() if k not in VOLATILE}), sort_keys=True, ensure_ascii=False))


def _plain(value):
    """``value`` with the ids of commits and the times ago in every text of it made alike (done on the texts, before they are written as JSON, where a line break is a letter and would hide a word's start)."""
    if isinstance(value, str):
        return _AGO.sub("<ago>", _COMMIT_ID.sub("<id>", value))
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def canonical(arguments: str) -> str:
    try:
        return json.dumps(json.loads(arguments or "{}"), sort_keys=True)
    except ValueError:
        return arguments or ""


class DoomLoop:
    """One per run. ``refuses`` before a call is run, ``record`` after, and ``note`` for the text to add to the result of a call that has become a loop."""

    def __init__(self) -> None:
        self._same: dict[tuple[str, str], tuple[str, int]] = {}          # call -> (digest of its last result, how many times in a row that result came)
        self._known: set[tuple[str, str, str]] = set()                   # every call with the result it gave since the last edit
        self._calls = 0                                                  # calls recorded in the round now going, and whether any of them told something new
        self._new = False
        self.stale = 0                                                   # rounds in a row that told nothing new

    def refuses(self, name: str, arguments: str) -> bool:
        _, count = self._same.get((name, canonical(arguments)), ("", 0))
        return count >= STOP_AFTER

    def record(self, name: str, arguments: str, result_digest: str, *, changed: bool = True) -> int:
        """Notes a finished call and returns how many times in a row it has now given this result. A write that succeeded changes what every other call can return, so it starts the count over; a
        write that failed (``changed=False``: an edit whose text was not found) changed nothing, and is counted like any other call: the same failing edit again and again is a loop."""
        self._calls += 1
        if name in WRITES and changed:
            self._same.clear()
            self._known.clear()
            self._new = True
            return 1
        key = (name, canonical(arguments))
        if (*key, result_digest) not in self._known:
            self._known.add((*key, result_digest))
            self._new = True
        last, count = self._same.get(key, ("", 0))
        count = count + 1 if last == result_digest else 1
        self._same[key] = (result_digest, count)
        return count

    def end_round(self) -> bool:
        """Closes a round of calls. True when the last ``STALE_ROUNDS`` rounds each only repeated calls already made with the same results: a run that cycles through a few calls (a checkpoint, a look at the
        project, the last commit, again) never repeats one call enough for ``refuses``, and goes on until the round limit while saying it is finished. A round with no calls is neither."""
        if self._calls:
            self.stale = 0 if self._new else self.stale + 1
        self._calls, self._new = 0, False
        return self.stale >= STALE_ROUNDS

    @staticmethod
    def note(count: int, name: str = "") -> str | None:
        return NOTE.format(n=count) if count >= (NUDGE_READS_AT if name in READS else NUDGE_AT) else None
