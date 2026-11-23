"""Which shell commands may be run a second time without asking the owner: a POSITIVE grammar.

After the app stops while a command is running, the next run does not know whether it ran. A command whose second run cannot do harm is simply run again; every other command goes back to the
owner as one that may already have run. "Cannot do harm" is decided here, and only by what the text of the command SAYS, never by what it might mean to a shell: a command is accepted only if
every character, every program, every flag and every operand is on a list. Anything not listed is refused, so a way of writing a command that nobody thought of is refused too.

* No shell syntax at all except ``|``, ``&&`` and ``||`` between stages: no newline, no ``;``, no lone ``&``, no redirection, no substitution (backtick, ``$``), no globbing, no braces, no
  backslash, no leading ``~``. Quotes are allowed (``grep "two words" file``), and a ``|`` or ``&`` inside quotes is simply refused.
* The first word of a stage is the program itself: no ``VAR=value`` before it (``GIT_EXTERNAL_DIFF``, ``PATH``, ``LD_PRELOAD`` and ``GIT_PAGER`` all make a harmless-looking command run a program),
  no path to a program.
* A program has a fixed set of flags, and a flag that takes a value is told so; a word that starts with ``-`` and is not on the list is refused. That is what keeps ``--output=``, ``--pre``,
  ``--exec-path``, ``-c``, ``-O``, ``--exec`` and every plugin, wrapper or config option out. git accepts no option before its subcommand.
* Test runners are accepted with a short list of flags. They run the project's own code, and the project is the sandbox's copy with no network, so running the tests again is what the agent does
  in its fix-and-retest loop anyway; what is refused is every option that names a program or a plugin to run with them (``-p``, ``-exec``, ``--config``, ``--script-shell``).
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

BAD_CHARS = set("`$\\(){}<>;*?[]!#\x00")
MAX_LENGTH = 1000
MAX_STAGES = 6
_VALUE = re.compile(r"[A-Za-z0-9_.,:+/@%=-]{1,100}")
_PROGRAM = re.compile(r"[a-z][a-z0-9-]*")


@dataclass(frozen=True)
class Spec:
    short: str = ""                                    # single-letter flags that take no value; they may be written together (-lah)
    short_values: frozenset = frozenset()              # single-letter flags that take the next word as a value (-n 5)
    long: frozenset = frozenset()                      # --flags that take no value
    long_values: frozenset = frozenset()               # --flags that take a value (--maxfail=3 or --maxfail 3)
    single_dash: frozenset = frozenset()               # -word flags with no value (go's -v, -short)
    single_dash_values: frozenset = frozenset()        # -word=value flags (go's -count=1)
    operands: bool = True                              # whether words that are not flags (paths, patterns, refs) are accepted
    number: bool = False                               # whether a bare count such as -5 is a flag (head -5, git log -5)


def S(**kw) -> Spec:
    return Spec(**{k: (frozenset(v.split()) if k in ("short_values", "long", "long_values", "single_dash", "single_dash_values") else v) for k, v in kw.items()})


READERS = {
    "ls": S(short="alhRt1SrdFc", long="all almost-all"),
    "cat": S(short="nbsAE"),
    "head": S(short="qv", short_values="n c", number=True),
    "tail": S(short="qv", short_values="n c", number=True),                           # no -f and no -F: they never end
    "wc": S(short="lwcmL"),
    "grep": S(short="nilrRwFEvHhcosqI", short_values="A B C m", long="line-number ignore-case recursive files-with-matches count word-regexp fixed-strings invert-match no-filename"),
    "rg": S(short="nilwFvHhcsuS", short_values="A B C m t", long="hidden no-heading files files-with-matches count ignore-case fixed-strings line-number word-regexp invert-match smart-case "
                                                             "no-line-number with-filename"),
    "stat": S(),
    "pwd": S(operands=False),
    "true": S(operands=False),
    "echo": S(short="ne"),
    "diff": S(short="uqrN", long="brief recursive unified"),
}

# Only the git subcommands that read the repository's objects and never consult a setting that can name a program. `status` (core.fsmonitor), `diff`, `log -p`, `show` and `blame` (diff drivers and
# text conversion filters, set in the repository's own config or attributes, which the agent can write) are NOT here: after a restart they go back to the owner like any other command.
GIT = {
    "ls-files": S(short="cmos", long="cached others modified deleted stage"),
    "rev-parse": S(long="short abbrev-ref show-toplevel is-inside-work-tree verify"),
    "describe": S(long="tags always"),
    "grep": S(short="nilcwFEvh", short_values="m", long="line-number ignore-case files-with-matches count word-regexp fixed-strings"),
    "cat-file": S(short="ptse"),
    "shortlog": S(short="sne", long="summary numbered email"),
}

TESTS = {
    "pytest": S(short="qxvl", short_values="k m", long="lf ff collect-only co no-header showlocals strict-markers last-failed failed-first", long_values="tb maxfail durations"),
    "unittest": S(short="vfbq", short_values="k s t p"),
    "go": S(single_dash="v short failfast race cover", single_dash_values="count run timeout p"),
    "cargo": S(short="q", long="quiet lib no-fail-fast workspace release", long_values="package"),
}


def _safe_word(word: str) -> bool:
    return bool(word) and not word.startswith("~") and all(32 <= ord(c) < 127 for c in word)


def _args_ok(words: list[str], spec: Spec) -> bool:
    i = 0
    while i < len(words):
        w = words[i]
        if w == "--":
            rest = words[i + 1:]                       # after --, every word is an operand: still checked, and none may look like a flag (a runner would not read it as one, but nothing here relies on that)
            return all(_safe_word(r) and not r.startswith("-") for r in rest) and (spec.operands or not rest)
        if w.startswith("--"):
            name, eq, value = w.partition("=")
            if name[2:] in spec.long and not eq:
                i += 1
                continue
            if name[2:] in spec.long_values:
                if eq:
                    if not _VALUE.fullmatch(value):
                        return False
                    i += 1
                else:
                    if i + 1 >= len(words) or not _VALUE.fullmatch(words[i + 1]):
                        return False
                    i += 2
                continue
            return False
        if w.startswith("-") and len(w) > 1:
            if spec.number and w[1:].isdigit() and len(w) <= 7:
                i += 1
                continue
            name, eq, value = w[1:].partition("=")
            if name in spec.single_dash and not eq:
                i += 1
                continue
            if name in spec.single_dash_values and eq and _VALUE.fullmatch(value):
                i += 1
                continue
            if len(w) == 2 and w[1] in {n for n in spec.short_values if len(n) == 1}:
                if i + 1 >= len(words) or not _VALUE.fullmatch(words[i + 1]):
                    return False
                i += 2
                continue
            if len(w) > 2 and w[1] in {n for n in spec.short_values if len(n) == 1} and w[2:].isdigit():          # -n5
                i += 1
                continue
            if all(ch in spec.short for ch in w[1:]):
                i += 1
                continue
            return False
        if not spec.operands or not _safe_word(w):
            return False
        i += 1
    return True


def _stage_ok(words: list[str]) -> bool:
    if not words or not _PROGRAM.fullmatch(words[0]):
        return False                                    # empty, an assignment (VAR=value), a path, or something that is not a program name
    program, rest = words[0], words[1:]
    if program == "git":
        return bool(rest) and rest[0] in GIT and _args_ok(rest[1:], GIT[rest[0]])
    if program in ("python", "python3"):
        if rest[:2] == ["-m", "pytest"]:
            return _args_ok(rest[2:], TESTS["pytest"])
        if rest[:2] == ["-m", "unittest"]:
            return _args_ok(rest[2:], TESTS["unittest"])
        return False
    if program == "pytest":
        return _args_ok(rest, TESTS["pytest"])
    if program == "npm":
        return rest in (["test"], ["run", "test"])      # whatever the project's own test script is, with nothing added to it
    if program == "go":
        return rest[:1] == ["test"] and _args_ok(rest[1:], TESTS["go"])
    if program == "cargo":
        return rest[:1] == ["test"] and _args_ok(rest[1:], TESTS["cargo"])
    return program in READERS and _args_ok(rest, READERS[program])


def rerunnable(command: str) -> bool:
    """Whether running ``command`` a second time cannot do harm (see the module's note). False for anything not on the lists."""
    text = (command or "").strip()
    if not text or len(text) > MAX_LENGTH or any(c in BAD_CHARS or ord(c) < 32 or ord(c) > 126 for c in text):
        return False
    probe = text.replace("&&", " ")
    if "&" in probe:
        return False                                    # a lone & starts a second command in the background
    stages = re.split(r"\|\||&&|\|", text)
    if len(stages) > MAX_STAGES:
        return False
    for stage in stages:
        try:
            words = shlex.split(stage, posix=True)
        except ValueError:
            return False
        if not _stage_ok(words):
            return False
    return True
