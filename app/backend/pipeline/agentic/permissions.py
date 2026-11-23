"""What a call may do without asking: rules, how a command is read, and the floor nothing overrides.

A rule is ``{tool, pattern, action}``: ``tool`` is a permission key (``read``, ``edit``, ``bash``, ``grep``, ``glob``, ``scan``, ``todo``, ``question``, ``task``, ``websearch``, ``webfetch``,
``checkpoint``, ``external_directory``, or ``*``), ``pattern`` a glob (``*`` matches any run of characters, ``git status *`` also matches ``git status``), ``action`` one of ``allow``, ``ask``, ``deny``.
Which rules apply, from the weakest layer to the strongest, with the last match winning inside a layer set:

1. the mode's own defaults (Plan reads and stops at the owner's decisions, Manual asks before it writes or runs, Auto does not ask);
2. the owner's global rules (``data/agent/permissions.json``);
3. the project's rules (``data/projects/<id>/agent_permissions.json``);

and then the session's grants ("Allow always" for this session), which can only turn an ``ask`` into an ``allow``: a ``deny`` stays a ``deny``. Rules found inside a project folder itself could only narrow
(``narrowed``): a project cannot give its own agent more than the owner did.

A command is read the way a shell reads it before it is matched (``parse``): quote-aware, split at ``&&``, ``||``, ``;``, ``|``, ``&`` and line ends, every segment passing on its own. A command
that hides another (``$(...)``, backticks, ``eval``, ``bash -c``, ``xargs``, ``find -exec``) cannot be allowed by a pattern; it asks. The ``FLOOR`` is what no rule, grant or mode allows.
"""
from __future__ import annotations

import fnmatch
import json
import pathlib
import posixpath
import re
from dataclasses import dataclass, field

from app.backend.pipeline import credentials, fsio
from app.backend.pipeline.agentic import sources
from app.backend.pipeline.agentic.tools import spec

ACTIONS = ("allow", "ask", "deny")
ROOT = pathlib.Path("data/agent")
GLOBAL_FILE = "permissions.json"
PROJECT_FILE = "agent_permissions.json"
MAX_RULES = 200
MAX_PATTERN = 300
KEYS = ("read", "glob", "grep", "scan", "edit", "bash", "verify", "todo", "question", "task", "websearch", "webfetch", "checkpoint", "mcp", "depscan", "external_directory", "*")
STRENGTH = {"allow": 0, "ask": 1, "deny": 2}


@dataclass(frozen=True)
class Rule:
    tool: str
    pattern: str = "*"
    action: str = "ask"
    source: str = ""                                       # which layer it came from, for the reason shown to the owner

    def to_dict(self) -> dict:
        return {"tool": self.tool, "pattern": self.pattern, "action": self.action}


@dataclass
class Decision:
    action: str                                            # allow | ask | deny
    permission: str
    value: str                                             # what the pattern was matched against
    reason: str = ""
    source: str = ""                                       # mode | global | project | session | floor | engine
    always: list[str] = field(default_factory=list)       # the patterns "Allow always" would add (empty: this call can only be allowed once)

    def to_dict(self) -> dict:
        return {"action": self.action, "permission": self.permission, "value": self.value, "reason": self.reason, "source": self.source, "always": list(self.always)}


# -- rules ------------------------------------------------------------------------------------------------------------------------------------------------

def clean_rules(rules, source: str = "") -> list[Rule]:
    """Rules from JSON, checked: a list of ``{tool, pattern, action}``. A malformed entry is an error naming it, never silently dropped (a dropped ``deny`` would loosen the agent)."""
    if not isinstance(rules, list):
        raise ValueError("The rules must be a list.")
    if len(rules) > MAX_RULES:
        raise ValueError(f"At most {MAX_RULES} rules are kept.")
    out = []
    for i, r in enumerate(rules, 1):
        if not isinstance(r, dict):
            raise ValueError(f"Rule {i} must be an object with tool, pattern and action.")
        tool, pattern, action = str(r.get("tool") or "").strip(), str(r.get("pattern") if r.get("pattern") is not None else "*").strip() or "*", str(r.get("action") or "").strip()
        if tool not in KEYS:
            raise ValueError(f"Rule {i}: the tool must be one of {', '.join(KEYS)}.")
        if action not in ACTIONS:
            raise ValueError(f"Rule {i}: the action must be one of {', '.join(ACTIONS)}.")
        if len(pattern) > MAX_PATTERN:
            raise ValueError(f"Rule {i}: the pattern is longer than {MAX_PATTERN} characters.")
        out.append(Rule(tool, pattern, action, source))
    return out


def narrowed(rules: list[Rule]) -> list[Rule]:
    """The rules a project folder may bring: only those that ask for more care. An ``allow`` found there is dropped."""
    return [r for r in rules if r.action != "allow"]


def match(pattern: str, text: str) -> bool:
    """``*`` matches any run of characters (``/`` and spaces too); a trailing `` *`` also matches the bare command. Case matters (the sandbox is Linux)."""
    if pattern == "*":
        return True
    if fnmatch.fnmatchcase(text, pattern):
        return True
    return pattern.endswith(" *") and fnmatch.fnmatchcase(text, pattern[:-2])


def last_match(rules, permission: str, value: str) -> Rule | None:
    hit = None
    for r in rules:
        if r.tool in (permission, "*") and match(r.pattern, value):
            hit = r
    return hit


def defaults(mode: str, params: dict | None = None) -> list[Rule]:
    """The mode's own rules. They reproduce what the agent did before rules existed: Plan pauses only at the owner's questions and the plan, Manual pauses at a write or a command unless the
    session is set to auto-approve, Auto never pauses."""
    p = params or {}
    reading = [Rule(k, "*", "allow", "mode") for k in ("read", "glob", "grep", "scan", "todo", "task", "websearch", "webfetch")]
    if mode == "auto":
        return [Rule("*", "*", "allow", "mode"), Rule("external_directory", "*", "allow", "mode"), Rule("mcp", "*", "ask", "mode"), Rule("depscan", "*", "ask", "mode")]      # a tool of an MCP server may reach something outside the sandbox with a key the owner gave it: asked even here
    if mode == "plan":
        return [Rule("*", "*", "deny", "mode"), *reading, Rule("bash", "*", "allow", "mode"), Rule("question", "*", "ask", "mode"), Rule("depscan", "*", "ask", "mode")]       # the terminal of Plan mode refuses by itself whatever writes
    manual_edit = "allow" if p.get("auto_approve") else "ask"
    return [Rule("*", "*", "ask", "mode"), *reading, Rule("question", "*", "allow", "mode"), Rule("checkpoint", "*", "allow", "mode"),
            Rule("edit", "*", manual_edit, "mode"), Rule("bash", "*", manual_edit, "mode"), Rule("verify", "*", "allow", "mode"), Rule("external_directory", "*", "ask", "mode"), Rule("depscan", "*", "ask", "mode")]


# -- paths ------------------------------------------------------------------------------------------------------------------------------------------------

def normal_path(path: str) -> str:
    """A path as a rule is matched against it: forward slashes, no ``.`` parts, no doubled slashes, every ``..`` that can be resolved resolved (``docs/../.git/x`` is ``.git/x``)."""
    p = str(path or "").replace("\\", "/")
    if not p:
        return ""
    p = posixpath.normpath(p)
    return "" if p == "." else p


def is_outside(path: str, roots: tuple[str, ...] = ("/work",)) -> bool:
    """Whether a path leaves the project: a ``..`` that climbs out, ``~``, or an absolute path not under one of ``roots`` (the sandbox's own work folder)."""
    p = normal_path(path)
    if not p:
        return False
    if p.startswith("~"):
        return True
    absolute = p.startswith("/") or bool(re.match(r"^[A-Za-z]:/", p)) or p.startswith("//")
    if absolute:
        return not any(p == r or p.startswith(r.rstrip("/") + "/") for r in roots)
    return p == ".." or p.startswith("../")                  # normal_path has resolved what it can: a .. that is left climbs out


# -- commands ---------------------------------------------------------------------------------------------------------------------------------------------

@dataclass
class Parsed:
    segments: list[list[str]] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)           # hidden: a command that runs another (subshell, backticks, eval, shell -c, xargs, find -exec); write: a redirect to a file;
    #                                                         background: a trailing &; heredoc: << ; syntax: a separator with no command on a side of it
    redirects: list[str] = field(default_factory=list)
    ops: list[str] = field(default_factory=list)          # what follows each segment but the last: && || ; | (a newline counts as ;)

    def texts(self) -> list[str]:
        return [" ".join(s) for s in self.segments if s]


_SEPARATORS = ("&&", "||", "|&", ";", "|", "&", "\n")
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "cmd", "csh", "tcsh"}
_WRAPPERS = {"sudo", "env", "command", "nohup", "time", "nice", "stdbuf", "doas", "exec", "builtin"}
_QUIET_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr"}


def parse(command: str) -> Parsed:
    """The command as a shell would split it, without running or expanding anything. Anything this cannot be sure about is flagged ``hidden`` so it asks."""
    out, words, word, has = Parsed(), [], [], False
    pending: tuple[str, bool] | None = None                    # a redirect operator waiting for its target: the next word, read like any other (quotes, $( and backticks included)
    i, n = 0, len(command)

    def end_word():
        nonlocal word, has, pending
        if not has:
            return
        text = "".join(word)
        word, has = [], False
        if pending is None:
            words.append(text)
            return
        op, copies = pending
        pending = None
        if ">" in op and text not in _QUIET_TARGETS and not (copies and (text.isdigit() or text == "-")):
            out.flags.add("write")
            out.redirects.append(text)
            if re.search(r"[$`()*?\[]", text):                 # a target that is worked out by the shell is a place this cannot name
                out.flags.add("hidden")

    def end_segment():
        nonlocal words, pending
        end_word()
        if pending is not None:                                # an operator with no target
            out.flags.add("syntax")
            pending = None
        if words:
            out.segments.append(words)
        words = []

    while i < n:
        c = command[i]
        if c == "\\" and i + 1 < n:
            if command[i + 1] == "\n":
                i += 2
                continue
            word.append(command[i + 1]); has = True; i += 2
            continue
        if c == "'":
            j = command.find("'", i + 1)
            if j < 0:
                out.flags.add("hidden")                    # an unclosed quote: what follows is whatever the shell would wait for
                word.append(command[i + 1:]); has = True; i = n
                continue
            word.append(command[i + 1:j]); has = True; i = j + 1
            continue
        if c == '"':
            j, buf = i + 1, []
            while j < n and command[j] != '"':
                if command[j] == "\\" and j + 1 < n:
                    buf.append(command[j + 1]); j += 2
                    continue
                if command[j] == "`" or command.startswith("$(", j):
                    out.flags.add("hidden")
                buf.append(command[j]); j += 1
            if j >= n:
                out.flags.add("hidden")
            word.append("".join(buf)); has = True; i = j + 1
            continue
        if c == "`" or command.startswith("$(", i) or command.startswith("<(", i) or command.startswith(">(", i):
            out.flags.add("hidden")
            word.append(c); has = True; i += 1
            continue
        if c in "<>":
            j = i
            while j < n and command[j] in "<>":
                j += 1
            op = command[i:j]
            if op.startswith("<<"):
                out.flags.add("heredoc")
            if j < n and command[j] == "&":                  # >&2 and 2>&1 copy a descriptor
                op, j = op + "&", j + 1
            if has and "".join(word).isdigit():             # the 2 of 2>&1 belongs to the redirect, not to the command
                word, has = [], False
            else:
                end_word()                                   # a > ends the word before it: a>b is a, then the redirect to b
            pending = (op, op.endswith("&"))
            i = j
            continue
        sep = next((s for s in _SEPARATORS if command.startswith(s, i)), None)
        if sep:
            had = bool(words) or has
            end_segment()
            i += len(sep)
            if sep == "&":
                out.flags.add("background")
            if not had:
                out.flags.add("syntax")
            else:
                out.ops.append(";" if sep in ("\n", "&") else "|" if sep == "|&" else sep)
            continue
        if c in " \t":
            end_word(); i += 1
            continue
        word.append(c); has = True; i += 1
    end_segment()
    if len(out.ops) >= len(out.segments) and out.ops:       # a separator at the very end: fine for ; and a newline, a missing command for && || |
        if out.ops.pop() in ("&&", "||", "|"):
            out.flags.add("syntax")
    for words in out.segments:
        core = _core(words)
        if not core:
            continue
        first = pathlib.PurePosixPath(core[0]).name.lower()
        rest = core[1:]
        if first in _SHELLS and any(a in ("-c", "-lc", "-ic", "-Command", "-command", "/c", "/C", "-EncodedCommand") for a in rest):
            out.flags.add("hidden")
        elif first in ("eval", "source", ".", "xargs"):
            out.flags.add("hidden")
        elif first == "find" and any(a in ("-exec", "-execdir", "-ok", "-okdir") for a in rest):
            out.flags.add("hidden")
    return out


def _core(words: list[str]) -> list[str]:
    """The command itself: a leading ``VAR=value`` and wrappers like ``sudo`` or ``env`` set aside."""
    w = list(words)
    while w:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]):
            w = w[1:]
        elif pathlib.PurePosixPath(w[0]).name.lower() in _WRAPPERS:
            w = w[1:]
            while w and w[0].startswith("-"):
                w = w[1:]
        else:
            break
    return w


# the number of words that name what a command does (git commit, npm run test): "Allow always" covers that, not every use of the program
_ARITY = {"git": 2, "npm": 2, "pnpm": 2, "yarn": 2, "bun": 2, "pip": 2, "pip3": 2, "cargo": 2, "go": 2, "docker": 2, "kubectl": 2, "dotnet": 2, "gh": 2, "poetry": 2, "uv": 2,
          "npm run": 3, "pnpm run": 3, "yarn run": 3, "bun run": 3, "docker compose": 3, "uv run": 3, "uv pip": 3, "git config": 3, "python -m": 3, "python3 -m": 3, "cargo run": 3}
NEVER_ALWAYS = {"rm", "rmdir", "sudo", "doas", "dd", "chmod", "chown", "kill", "pkill", "killall", "curl", "wget", "ssh", "scp", "sftp", "nc", "ncat", "eval", "bash", "sh", "zsh", "su", "mv", "cp",
                "ln", "tee", "truncate", "shred", "mkfs", "mount", "umount", "crontab", "systemctl", "service", "docker", "kubectl", "xargs", "find",
                "python", "python3", "node", "nodejs", "deno", "perl", "ruby", "php", "java", "lua", "powershell", "pwsh", "cmd", "env", "awk", "gawk", "sed", "npx", "pnpx", "bunx", "uvx", "pipx",
                "exec", "source", "nohup", "time", "watch", "timeout", "install", "patch", "tar", "unzip", "zip", "rsync"}          # a program that runs whatever code it is given, or changes what it is aimed at


def _lead(words: list[str]) -> int:
    """How many words come before the command itself: ``VAR=value`` and wrappers like ``env`` with their flags."""
    i = 0
    while i < len(words):
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i]):
            i += 1
        elif pathlib.PurePosixPath(words[i]).name.lower() in _WRAPPERS:
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
        else:
            break
    return i


def prefix(words: list[str]) -> str:
    """The part of a command an "Allow always" covers, as a pattern that matches the command as written: ``git commit -m x`` gives ``git commit *``, ``FOO=1 make all`` gives ``FOO=1 make *``."""
    lead = _lead(words)
    core = words[lead:]
    if not core:
        return ""
    n, key = _ARITY.get(core[0], 1), 1
    if len(core) >= 2 and " ".join(core[:2]) in _ARITY:
        n, key = _ARITY[" ".join(core[:2])], 2
    n = min(n, len(core))
    if any(w.startswith("-") for w in core[key:n]) or any(c in w for w in words[:lead + n] for c in "*?["):
        return ""                                            # git -c x=y log is not "git log"; a * in a word would widen the pattern
    head = " ".join(words[:lead + n])
    return head if len(core) <= n else head + " *"


# -- the floor ----------------------------------------------------------------------------------------------------------------------------------------------

_FLOOR_COMMANDS = [
    (re.compile(r":\s*\(\s*\)\s*\{.*\|.*&.*\}\s*;\s*:"), "is a fork bomb"),
    (re.compile(r"(^|\s)mkfs(\.\w+)?(\s|$)"), "formats a disk"),
    (re.compile(r"(^|\s)dd\s.*\bof=/dev/"), "writes to a device"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|vd|mmcblk)"), "writes to a device"),
    (re.compile(r"(^|\s)(shutdown|reboot|halt|poweroff|init\s+[06])(\s|$)"), "stops the machine"),
    (re.compile(r"docker\.sock"), "reaches the Docker socket"),
    (re.compile(r"(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b"), "runs a download as a program"),
    (re.compile(r"(^|\s)chmod\s+(-[a-zA-Z]+\s+)*[0-7]*777\s+/(\s|$)"), "opens the whole file system"),
]
_STARTS_PROGRAMS = ("core.hookspath", "core.fsmonitor", "core.sshcommand", "core.pager", "core.editor", "core.askpass", "diff.external", "credential.helper", "gpg.program", "sequence.editor",
                    "alias.", ".textconv", "include.path", "includeif", "--exec-path", "--upload-pack", "--receive-pack", "--config-env")
_GIT_READ_CONFIG = {"--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin", "--show-scope", "--name-only", "--null", "-z", "--type", "--default"}


def _git_floor(words: list[str]) -> str | None:
    """Why a segment is never allowed because of what it does with ``.git`` or with git's configuration, or None. The agent reads history with git itself; it does not open the repository's
    own files (hooks, config, the index), and no command it runs may set what makes git start a program."""
    core = _core(words)
    if not core:
        return None
    program, args = pathlib.PurePosixPath(core[0]).name.lower(), core[1:]
    if program == "git":
        low = " ".join(args).lower()
        if any(k in low for k in _STARTS_PROGRAMS):
            return "This sets something that makes git start a program (a hook, a pager, an editor, an alias, a filter). That is never allowed."
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub == "config":
            flags = {a.split("=", 1)[0] for a in args}
            values = [a for a in args[args.index("config") + 1:] if not a.startswith("-")]
            writes = flags & {"--unset", "--unset-all", "--add", "--replace-all", "--edit", "-e", "--rename-section", "--remove-section", "--file", "-f", "--system", "--global"}
            if writes or (len(values) >= 2 and not flags & _GIT_READ_CONFIG):
                return "This changes git's configuration, which can make git start programs. That is never allowed."
        return None
    for a in args:
        for piece in {a, a.split("=", 1)[-1]}:
            if ".git" in normal_path(piece).split("/"):
                return "This command works on a path inside .git (hooks, config, the index). The agent reads history with git itself and never opens the repository's own files."
    return None


_WIDE = {"/", "/*", "~", "~/", "~/*", "$HOME", "$HOME/", "$HOME/*", "${HOME}", "${HOME}/", "${HOME}/*", "*", ".", ".."}


def _wide_rm(words: list[str]) -> bool:
    """``rm`` told to remove the root, the home folder, everything here or the folder above (with -r, or with --no-preserve-root whatever it removes)."""
    core = _core(words)
    if not core or pathlib.PurePosixPath(core[0]).name.lower() != "rm":
        return False
    args = core[1:]
    if "--no-preserve-root" in args:
        return True
    recursive = any(a == "--recursive" or (a.startswith("-") and not a.startswith("--") and "r" in a.lower()) for a in args)
    return recursive and any(a in _WIDE for a in args if not a.startswith("-"))


def floor(permission: str, value: str, parsed: Parsed | None = None) -> str | None:
    """Why a call is refused no matter what any rule or grant says, or None. Short on purpose: only what is never right."""
    if permission == "bash":
        if parsed and any(_wide_rm(w) for w in parsed.segments):
            return "This command removes the root, the home folder or everything around the project. That is never allowed."
        texts = [value] + (parsed.texts() if parsed else [])
        for text in texts:
            for rx, why in _FLOOR_COMMANDS:
                if rx.search(text):
                    return f"This command {why}. That is never allowed."
        if parsed:
            if any(".git" in normal_path(t).split("/") for t in parsed.redirects):
                return "This command writes inside .git. That is never allowed."
            for words in parsed.segments:
                why = _git_floor(words)
                if why:
                    return why
        return None
    if permission == "edit":
        p = normal_path(value)
        parts = [x for x in p.split("/") if x and x != "."]
        if ".git" in parts:
            return "Files inside .git are never written by the agent."
        if parts and credentials.SECRET_FILE.search(credentials.plain(parts[-1])):
            return "A file that holds credentials is never written by the agent."
    return None


# -- the decision -----------------------------------------------------------------------------------------------------------------------------------------------

def value_of(name: str, arguments: dict | None) -> tuple[str, str]:
    """``(permission key, the text a pattern is matched against)`` for a call."""
    _, permission, key = spec.kind_of(name)
    if permission == "mcp":
        server, _, tool = str(name)[len("mcp__"):].partition("__")
        return "mcp", f"{server}/{tool}"
    args = arguments or {}
    raw = args.get(key) if key else ""
    return permission, ("" if raw is None else str(raw))


def _merge(a: Decision, b: Decision) -> Decision:
    return b if STRENGTH[b.action] > STRENGTH[a.action] else a


def decide(mode: str, name: str, arguments: dict | None, *, params: dict | None = None, global_rules=(), project_rules=(), grants=(), roots: tuple[str, ...] | None = ("/work",),
           known_urls: frozenset[str] | None = None, tainted: bool = False) -> Decision:
    """What happens to a call: allowed, asked or denied, and why. ``roots`` is where the call may reach (the sandbox's work folder), or None where the tool confines itself (Plan, on the real project). Pure: the layers come in as arguments (``for_session`` fetches them)."""
    if name == "read_files":                           # several files in one call are decided one by one, as if each were its own read_file
        return _each_path(mode, arguments, params=params, global_rules=global_rules, project_rules=project_rules, grants=grants, roots=roots)
    permission, value = value_of(name, arguments)
    base = [*defaults(mode, params), *global_rules, *project_rules]
    grant_rules = list(grants)
    if permission in ("edit", "read"):                 # a rule is matched against the path the tool will open: ``./a``, ``a//b`` and ``a/../b`` are the same file as ``a`` and ``a/b``
        value = normal_path(value)
    parsed = parse(value) if permission == "bash" else None

    why = floor(permission, value, parsed)
    if why:
        return Decision("deny", permission, value, why, "floor")

    if roots is not None and permission in ("edit", "read", "glob", "grep") and value and is_outside(value, roots):
        outside = last_match(base, "external_directory", value)
        if outside and outside.action != "allow":
            if outside.action == "deny" or not any(g.tool == "external_directory" and match(g.pattern, value) for g in grant_rules):
                return Decision(outside.action, "external_directory", value, f"{value} is outside the project.", outside.source, [value] if outside.action == "ask" else [])

    if parsed is not None:
        return _bash(mode, parsed, value, base, grant_rules)

    rule = last_match(base, permission, value)
    action, source = (rule.action, rule.source) if rule else ("ask", "engine")
    reason = f"{permission} {value}".strip() + (f" is denied by a {source} rule." if action == "deny" else "")
    if permission == "websearch" and tainted and action == "allow" and source == "mode":
        action, source, reason = "ask", "engine", ("Web content is already in this session, and a page or a search result can carry instructions. A search now could be a way to send something "
                                                  "out in its words, so look at the query before it is sent. Allow always (for this session) if you trust what was read.")
    if permission == "webfetch" and known_urls is not None and action == "allow" and source == "mode" and sources.normal(value) not in known_urls:
        action, source, reason = "ask", "engine", "This address did not come from a search result or from your message, so it is asked about before it is fetched."
    if action == "ask" and any(g.tool in (permission, "*") and match(g.pattern, value) for g in grant_rules):
        return Decision("allow", permission, value, "Allowed for this session.", "session")
    always = [] if action != "ask" or permission == "question" else [_always(permission, value)]
    return Decision(action, permission, value, reason if action != "allow" else "", source, always)


def _each_path(mode: str, arguments: dict | None, **layers) -> Decision:
    """The decision for ``read_files``: the strictest of the decisions for each of its paths (an entry may carry a line range, ``path:10-60``, which is not part of the path)."""
    raw = (arguments or {}).get("paths")
    entries = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    best = Decision("allow", "read", "", "", "engine")
    for entry in entries[:50]:
        path = re.sub(r":\d+(?:-\d+)?$", "", str(entry or "").strip().strip("`'\""))             # the same reading as the reader (``codeintel_tools.parse_entry``): quotes, then the range
        best = _merge(best, decide(mode, "read_file", {"path": path}, **layers))
    return best


def _always(permission: str, value: str) -> str:
    return value if permission in ("webfetch", "mcp") else "*"           # an address or an MCP tool is allowed one at a time; a search, once the owner has seen what was read, for the session


def _bash(mode: str, parsed: Parsed, whole: str, base: list[Rule], grants: list[Rule]) -> Decision:
    if not parsed.segments:
        return Decision("allow", "bash", whole, "", "engine")
    best = None
    always: list[str] = []
    for words in parsed.segments:
        text = " ".join(words)
        rule = last_match(base, "bash", text)
        core = _core(words)
        if core:                                             # /usr/bin/git, env git, FOO=1 git and sudo git are git: a rule that forbids or asks about it must see it
            plain = " ".join([pathlib.PurePosixPath(core[0]).name, *core[1:]])
            other = last_match(base, "bash", plain) if plain != text else None
            if other is not None and other.action in ("deny", "ask") and (rule is None or STRENGTH[other.action] > STRENGTH[rule.action]):
                rule = other
        action, source = (rule.action, rule.source) if rule else ("ask", "engine")
        reason = f"The command {text!r} is denied by a {source} rule." if action == "deny" else ""
        if action == "ask" and any(g.tool in ("bash", "*") and match(g.pattern, text) for g in grants):
            action, source, reason = "allow", "session", ""
        d = Decision(action, "bash", whole, reason, source)
        best = d if best is None else _merge(best, d)
        if action == "ask":
            first = next((w for w in words if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)), "")
            core = _core(words)
            two = " ".join(core[:2]) in _ARITY                  # python -m, npm run, uv run: the module or the script is part of what is allowed
            names = {pathlib.PurePosixPath(w).name.lower() for w in (first, core[0] if core else first) if w}
            if first and (two and names <= {pathlib.PurePosixPath(core[0]).name.lower()} or not names & NEVER_ALWAYS):
                p = prefix(words)
                if p and p not in always:
                    always.append(p)
    assert best is not None
    if mode == "plan" and best.action != "deny" and ("hidden" in parsed.flags or "write" in parsed.flags or "background" in parsed.flags):
        return Decision("deny", "bash", whole, "Plan mode only reads: no redirect, background job, subshell, eval, bash -c, xargs or find -exec.", "mode")
    if best.action == "allow" and "hidden" in parsed.flags and not (mode == "auto" and best.source == "mode"):
        return Decision("ask", "bash", whole, "This command runs another command inside itself (a subshell, eval, bash -c, xargs, find -exec), so no pattern can say what it does.", "engine")
    if best.action == "allow" and "write" in parsed.flags and mode == "plan":
        return Decision("deny", "bash", whole, "Plan mode does not write files, and this command redirects output into one.", "mode")
    if best.action == "ask":
        if "hidden" in parsed.flags:
            best.reason, always = "This command runs another command inside itself, so it can only be allowed once.", []
        best.always = always
        best.reason = best.reason or "Running a command changes things; the owner decides."
    return best


# -- storage ----------------------------------------------------------------------------------------------------------------------------------------------------

def _read(path: pathlib.Path, source: str) -> list[Rule]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    try:
        return clean_rules(doc.get("rules") if isinstance(doc, dict) else doc, source)
    except ValueError:
        return [Rule("*", "*", "ask", source)]            # a file that cannot be read asks about everything rather than allowing what it meant to forbid


def global_rules() -> list[Rule]:
    return _read(ROOT / GLOBAL_FILE, "global")


def save_global(rules) -> list[Rule]:
    clean = clean_rules(rules, "global")
    fsio.write_json(ROOT / GLOBAL_FILE, {"rules": [r.to_dict() for r in clean]}, indent=2)
    return clean


def _project_path(project_id: str) -> pathlib.Path:
    from app.backend.pipeline import projects
    projects.get(project_id)
    return projects.ROOT / project_id / PROJECT_FILE


def project_rules(project_id: str) -> list[Rule]:
    return _read(_project_path(project_id), "project")


def save_project(project_id: str, rules) -> list[Rule]:
    clean = clean_rules(rules, "project")
    fsio.write_json(_project_path(project_id), {"rules": [r.to_dict() for r in clean]}, indent=2)
    return clean


def add_project_rules(project_id: str, rules: list[Rule]) -> list[Rule]:
    have = project_rules(project_id)
    keep = {(r.tool, r.pattern, r.action) for r in have}
    merged = have + [Rule(r.tool, r.pattern, r.action, "project") for r in rules if (r.tool, r.pattern, r.action) not in keep]
    return save_project(project_id, [r.to_dict() for r in merged])


def grants_of(session: dict) -> list[Rule]:
    try:
        return [r for r in clean_rules(session.get("grants") or [], "session") if r.action == "allow"]
    except ValueError:
        return []


def for_session(project_id: str, session: dict, name: str, arguments: dict | None, roots: tuple[str, ...] | None = ("/work",)) -> Decision:
    """The decision for one call of a session, with every layer read fresh (a rule saved a moment ago applies to the next call)."""
    return decide(session["mode"], name, arguments, params=session.get("params"), global_rules=global_rules(), project_rules=project_rules(project_id), grants=grants_of(session),
                  roots=None if session["mode"] == "plan" else roots, known_urls=(sources.known_urls(session.get("transcript") or []) | frozenset(session.get("inherited_urls") or [])) if name == "webfetch" else None,
                  tainted=name == "websearch" and bool(session.get("tainted") or sources.web_content_seen(session.get("transcript") or [])))


def grants_for(decision: Decision) -> list[Rule]:
    """The rules "Allow always" adds for a decision that asked."""
    tool = "external_directory" if decision.permission == "external_directory" else decision.permission
    return [Rule(tool, p, "allow") for p in decision.always]
