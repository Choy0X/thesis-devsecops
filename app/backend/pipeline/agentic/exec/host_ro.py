"""The terminal of Plan mode: commands that read the owner's real project and cannot change it.

Plan mode looks at the project where it is (no copy, no sandbox, no Docker), so the terminal it gets must be unable to write, to leave the project, to read what is kept from the assistant
(files of credentials), or to start anything it was not told it may. That is a boundary, and it is kept by construction rather than by asking a shell to behave:

* a command is read by ``permissions.parse`` (quote-aware, no shell is ever started); a redirect, a background job, a subshell, a here-document, ``eval``, ``bash -c`` and a command with
  nothing on a side of ``&&`` or ``|`` are refused;
* every command is one of a short list. ``cat head tail wc sort uniq ls find tree diff echo pwd cd`` run in Python over ``Workspace`` (``builtins.py``); ``rg`` and ``grep`` run ripgrep with a list
  of flags that are allowed and the credential files excluded after the model's own globs, so they win; ``git`` runs with a list of sub-commands and flags that are allowed, no option that
  names a program or an output file, no repository configuration that could start a program, and the credential files excluded; anything else is refused with what is allowed;
* every path goes through ``Workspace.resolve`` (inside the project, no ``..``, no drive or ``/``, no link followed, nothing kept), and a program that is started has the project folder as its
  working folder and an environment of its own (``rtk.environment``: no keys, tokens or other variable of the owner's, and a home of its own so nothing is written into the project or the owner's folders);
* output is stripped of terminal codes, masked for credentials, and cut to what the model is sent, with the whole of it kept for ``recall``.

``rtk`` is the command proxy where it works on every system: ``git`` and ``rg`` go through it so the model reads condensed output and can ask for what was left out. A pipeline's commands run
on the raw output of the one before (condensing it would change what ``wc`` or ``head`` count); only a command that stands alone is condensed.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import time

from app.backend import termtext
from app.backend.pipeline import chat_tools, credentials
from app.backend.pipeline.agentic import clip, outputs, permissions
from app.backend.pipeline.agentic.exec import builtins, rtk, toolchain
from app.backend.pipeline.git_runtime import find_git
from app.backend.pipeline.scan import code_rules

MAX_COMMAND = 4_000
MAX_STAGES = 8
MAX_ARGS = 100
MAX_ARG = 2_000
TIMEOUT = 30
OUTPUT_CHARS = 12_000
ALLOWED_TEXT = ("rg (and grep) to search; git status, diff, log, show, blame, ls-files, ls-tree, grep, branch, tag, remote, describe, shortlog, rev-parse and rev-list; "
                "cat, head, tail, wc, sort, uniq, ls, find, tree, diff, echo, pwd and cd. Join them with | && || or ;.")
ECHO_HINT = ("echo only prints into this terminal: the owner does not see it and nothing was done. To tell the owner something, write it in your answer (the exact command to run, and that Manual or Auto "
             "can run it in a sandbox copy). Do not echo it again.")


class Refused(chat_tools.ToolError):
    """The command is not one this read-only terminal runs; the message says why and what is allowed."""


class NotInPlan(Refused):
    """The command would change something or run a program this terminal does not have: an install, a file written, a background job, a program that is not on its list. This is a refusal because of what
    Plan mode is, not because of how the command was written, so the loop counts these, tells the model what to do instead, and ends a run that keeps trying."""


# the files and folders kept from the assistant, as globs for the programs that walk the project themselves
SECRET_NAMES = [".env", ".env.*", ".envrc", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx", "*.ppk", "*.p8", "*.asc", "*.gpg", "*.pgp", "*.tfvars", "*.tfstate", "*.secret", "*.vault",
                "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".npmrc", ".pypirc", ".netrc", ".git-credentials", ".pgpass", ".my.cnf", ".htpasswd", ".dockercfg", ".vault-pass", ".vault_pass",
                "credential", "credential.*", "credentials", "credentials.*", "secret.json", "secret.yml", "secret.yaml", "secrets.json", "secrets.yml", "secrets.yaml", "secrets.toml", "secrets.txt",
                "secrets.env", "secrets.ini", "kubeconfig", "terraform.tfstate*", "service*account*.json", "auth.json", "auth.txt", "auth.yml", "auth.yaml", "token.json", "token.txt", "tokens.json",
                "tokens.txt", "tokens.yml", "tokens.yaml"]
SECRET_DIRS = sorted(credentials.SECRET_DIRS)


def rg_excludes() -> list[str]:
    """``--iglob`` arguments that leave the kept files and folders out of a search, matched without regard to case (``Secrets.YML`` is kept as ``secrets.yml`` is: the app's own rule is not
    case-sensitive and neither are the file systems of Windows and macOS). They come after the model's own globs, and the last glob wins."""
    out: list[str] = []
    for g in SECRET_NAMES:
        out += ["--iglob", f"!**/{g}"]
    for d in SECRET_DIRS:
        out += ["--iglob", f"!**/{d}/**"]
    return out


def git_excludes() -> list[str]:
    """The same as pathspecs (for after ``--``), also without regard to case."""
    return [f":(exclude,glob,icase)**/{g}" for g in SECRET_NAMES] + [f":(exclude,glob,icase)**/{d}/**" for d in SECRET_DIRS]


def _words(cmd: str, args: list[str], table: dict[str, bool]) -> tuple[list[tuple[str, str | None]], list[str]]:
    """``[(flag, value)]`` and the words that are not flags, for a program whose flags are in ``table`` (flag to whether it takes a value). ``-nA3``, ``-A 3``, ``--context=3`` and ``--`` work as
    usual; ``-5`` is ``-C 5``. A flag that is not in the table is refused."""
    flags: list[tuple[str, str | None]] = []
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            rest += args[i + 1:]
            break
        if a.startswith("--") and len(a) > 2:
            key, eq, val = a.partition("=")
            if key not in table:
                raise Refused(f"{cmd}: {key} is not allowed in this read-only terminal.")
            if table[key]:
                if not eq:
                    i += 1
                    if i >= len(args):
                        raise Refused(f"{cmd}: {key} needs a value.")
                    val = args[i]
                flags.append((key, val))
            elif eq:
                raise Refused(f"{cmd}: {key} takes no value.")
            else:
                flags.append((key, None))
        elif re.fullmatch(r"-\d+", a):
            flags.append(("-C", a[1:]))
        elif a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                flag = "-" + a[j]
                if flag not in table:
                    raise Refused(f"{cmd}: {flag} is not allowed in this read-only terminal.")
                if table[flag]:
                    val = a[j + 1:]
                    if not val:
                        i += 1
                        if i >= len(args):
                            raise Refused(f"{cmd}: {flag} needs a value.")
                        val = args[i]
                    flags.append((flag, val))
                    break
                flags.append((flag, None))
                j += 1
        else:
            rest.append(a)
        i += 1
    return flags, rest


# -- ripgrep -------------------------------------------------------------------------------------------------------------------------------------------------

_RG_FLAGS: dict[str, bool] = {}                                  # flag -> takes a value
for _f in ("-n", "--line-number", "-N", "--no-line-number", "-i", "--ignore-case", "-s", "--case-sensitive", "-S", "--smart-case", "-w", "--word-regexp", "-x", "--line-regexp", "-F", "--fixed-strings",
           "-v", "--invert-match", "-l", "--files-with-matches", "--files-without-match", "-c", "--count", "--count-matches", "-o", "--only-matching", "-U", "--multiline", "--multiline-dotall",
           "-H", "--with-filename", "-I", "--no-filename", "--column", "--trim", "--heading", "--no-heading", "--hidden", "-.", "--no-ignore", "--no-ignore-vcs", "-u", "--no-messages", "--files",
           "-a", "--text", "--json", "--stats", "--crlf", "-P", "--pcre2", "--vimgrep", "--no-binary", "--binary", "--passthru", "-q", "--quiet"):
    _RG_FLAGS[_f] = False
for _f in ("-e", "--regexp", "-g", "--glob", "--iglob", "-t", "--type", "-T", "--type-not", "-A", "--after-context", "-B", "--before-context", "-C", "--context", "-m", "--max-count",
           "--max-depth", "-d", "--max-filesize", "-j", "--threads", "--sort", "--sortr", "-r", "--replace", "--max-columns", "-M", "--context-separator"):
    _RG_FLAGS[_f] = True
_RG_NUMBERS = {"-A", "-B", "-C", "-m", "--max-depth", "-d", "-j", "--threads", "--after-context", "--before-context", "--context", "--max-count", "--max-columns", "-M"}
_RG_PLAIN_ONLY = {"-c", "--count", "--count-matches", "-l", "--files-with-matches", "--files-without-match", "--files", "--json", "--stats", "-o", "--only-matching", "-q", "--quiet", "--vimgrep",
                  "--passthru", "-r", "--replace", "--column"}     # forms whose output is not search lines, so rtk's filter would not read them

# GNU grep's flags and what each is in ripgrep (None: nothing, ripgrep already does it)
_GREP_FLAGS = {"-r": None, "-R": None, "--recursive": None, "-n": "-n", "--line-number": "-n", "-i": "-i", "--ignore-case": "-i", "-w": "-w", "--word-regexp": "-w", "-x": "-x", "--line-regexp": "-x",
               "-F": "-F", "--fixed-strings": "-F", "-E": None, "--extended-regexp": None, "-v": "-v", "--invert-match": "-v", "-l": "-l", "--files-with-matches": "-l",
               "-L": "--files-without-match", "--files-without-match": "--files-without-match", "-c": "-c", "--count": "-c", "-o": "-o", "--only-matching": "-o", "-H": "-H", "--with-filename": "-H",
               "-h": "-I", "--no-filename": "-I", "-I": None, "-s": None, "--no-messages": None, "-a": "-a", "--text": "-a", "-q": "-q", "--quiet": "-q", "--line-buffered": None}
_GREP_VALUE = {"-A": "-A", "--after-context": "-A", "-B": "-B", "--before-context": "-B", "-C": "-C", "--context": "-C", "-m": "-m", "--max-count": "-m", "-e": "-e", "--regexp": "-e",
               "--include": "include", "--exclude": "exclude", "--exclude-dir": "exclude-dir"}
GREP_HELP = "Use rg, or one of the common grep flags (-rnilwvcoF -A -B -C -m -e --include --exclude --exclude-dir)."


def translate_grep(args: list[str]) -> list[str]:
    """The ripgrep arguments for the GNU ``grep`` the model wrote. The common flags are translated; anything else is refused with a pointer to ``rg``."""
    out: list[str] = []
    rest: list[str] = []
    i = 0

    def value_of(flag: str, val: str) -> list[str]:
        kind = _GREP_VALUE[flag]
        if kind == "include":
            return ["-g", val]
        if kind == "exclude":
            return ["-g", "!" + val]
        if kind == "exclude-dir":
            return ["-g", f"!**/{val.strip('/')}/**"]
        return [kind, val]

    while i < len(args):
        a = args[i]
        if a == "--":
            rest += args[i + 1:]
            break
        if a.startswith("--") and len(a) > 2:
            key, eq, val = a.partition("=")
            if key in _GREP_FLAGS:
                if _GREP_FLAGS[key]:
                    out.append(_GREP_FLAGS[key])
            elif key in _GREP_VALUE:
                if not eq:
                    i += 1
                    if i >= len(args):
                        raise Refused(f"grep: {key} needs a value.")
                    val = args[i]
                out += value_of(key, val)
            elif key.startswith("--color"):
                pass
            else:
                raise Refused(f"grep: {key} is not supported here. {GREP_HELP}")
        elif re.fullmatch(r"-\d+", a):
            out += ["-C", a[1:]]
        elif a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                flag = "-" + a[j]
                if flag in _GREP_FLAGS:
                    if _GREP_FLAGS[flag]:
                        out.append(_GREP_FLAGS[flag])
                    j += 1
                elif flag in _GREP_VALUE:
                    val = a[j + 1:]
                    if not val:
                        i += 1
                        if i >= len(args):
                            raise Refused(f"grep: {flag} needs a value.")
                        val = args[i]
                    out += value_of(flag, val)
                    break
                else:
                    raise Refused(f"grep: {flag} is not supported here. {GREP_HELP}")
        else:
            rest.append(a)
        i += 1
    return out + rest


def plan_rg(args: list[str]) -> tuple[list[tuple[str, str | None]], str | None, list[str]]:
    """``(flags, pattern, paths)`` of a ripgrep command, every flag checked. The pattern is the first word that is not a flag unless ``-e`` or ``--files`` is used."""
    flags, rest = _words("rg", args, _RG_FLAGS)
    names = {f for f, _ in flags}
    for f, v in flags:
        if f in _RG_NUMBERS and not str(v).isdigit():
            raise Refused(f"rg: {f} needs a whole number.")
        if f in ("--sort", "--sortr") and v not in ("path", "modified", "accessed", "created", "none"):
            raise Refused("rg: --sort is path, modified, accessed, created or none.")
    if "--files" in names or names & {"-e", "--regexp"}:
        return flags, None, rest
    if not rest:
        raise Refused('rg: give the text to search for, for example rg -n "def main" src')
    return flags, rest[0], rest[1:]


# -- git --------------------------------------------------------------------------------------------------------------------------------------------------------

_GIT_SUBS = {"status", "diff", "log", "show", "blame", "ls-files", "ls-tree", "grep", "branch", "tag", "remote", "describe", "shortlog", "rev-parse", "rev-list"}
# an option that names a program, a file to write, a file to read from outside the repository, or the network (matched by its start, before any =)
_GIT_NEVER = {"--output", "--ext-diff", "--textconv", "--no-index", "--open-files-in-pager", "--exec", "--exec-path", "--git-dir", "--work-tree", "--namespace", "--paginate", "--upload-pack",
              "--receive-pack", "--show-signature", "--pager", "--contents", "--ignore-revs-file", "--file", "--config", "--config-env", "--edit", "--exclude-from", "--exclude-per-directory",
              "--super-prefix", "--literally", "--with-tree", "--ssh-command",
              "--objects", "--objects-edge", "--objects-edge-aggressive", "--indexed-objects",          # these list the objects of the history, blobs of credential files among them
              "--ignore-submodules", "--recurse-submodules", "--submodule"}          # a submodule is another repository with settings of its own (a program git would start while reading it); git takes the LAST of two
                                                                                     # --ignore-submodules, so the one this terminal puts in front of the model's arguments must not be one it can undo
# ``git ls-tree`` prints the id of every file of a commit, so it takes only the flags that keep the name beside the id (the line of a credential file is then left out of what it printed)
_LS_TREE_LONG = {"--name-only", "--name-status", "--long", "--full-name", "--full-tree"}
_LS_TREE_SHORT = set("rtdl")
_GIT_SHORT_NEVER = {"grep": ("-f",), "ls-files": ("-X",), "blame": ("-S",)}           # a short option that reads its patterns or revisions from a file
_GIT_LIST_FLAGS = {"branch": {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--list", "-l", "--show-current", "--merged", "--no-merged", "--contains", "--no-contains", "--sort",
                              "--points-at", "--format", "--abbrev", "--no-abbrev", "--color", "--no-color", "-i", "--ignore-case"},
                   "tag": {"-l", "--list", "-n", "--contains", "--no-contains", "--merged", "--no-merged", "--sort", "--points-at", "--format", "--column", "--no-column", "-i", "--ignore-case"},
                   "remote": {"-v", "--verbose"}}
_GIT_LIST_VALUES = {"--merged", "--no-merged", "--contains", "--no-contains", "--sort", "--points-at", "--format", "--abbrev"}
_GIT_LISTING = {"--list", "-l", "--contains", "--merged", "--no-merged", "--points-at", "--no-contains"}
_GIT_VALUE_WORDS = {"-S", "-G", "-L", "-U", "--since", "--until", "--after", "--before", "--author", "--committer", "--grep", "--max-count", "--skip", "--diff-filter", "--date"}
_GIT_VALUE_BY_SUB = {"log": {"-n"}, "shortlog": {"-n"}, "rev-list": {"-n"}, "grep": {"-A", "-B", "-C", "-m", "--max-depth", "-e"}}
# settings of a repository that name a program git would start while reading (the ones that only matter when it writes or reaches the network are overridden instead, see _git)
_RISKY = re.compile(r"^(filter\..+\.(clean|smudge|process)|diff\..+\.(textconv|command)|diff\.external|include\.path|includeif\..+\.path)$")
# git-lfs, which the owner installed, is the one program a filter may name, and only as git-lfs itself writes it: the whole value (git runs a filter through a shell, so anything after it would run)
_LFS = re.compile(r"git-lfs (clean|smudge)( --skip)? -- %f|git-lfs filter-process( --skip)?")
_CONFIG_SCOPES_OF_A_REPOSITORY = {"local", "worktree"}       # the settings a repository's own folder brings with it; the system's and the owner's own are theirs to trust
_ELIDED = re.compile(r"\[\+(\d+) lines omitted\]|\(more changes truncated\)|^\+(\d+) more\b", re.M)
_REV_PATH = re.compile(r"^[^:\s]*:(.+)$")
# beside a ``REV:path`` only options that change how that one file or diff is drawn are allowed: any option that selects revisions (--all, --branches, -g, --stdin ...) would print commits too
_BLOB_FLAG = re.compile(r"--stat|--shortstat|--numstat|--no-color|--color|-w|-b|--ignore-all-space|--ignore-space-change|-p|--patch|--minimal|--word-diff|--abbrev|-U\d{1,3}|--unified=\d{1,3}")
_PATHSPEC_SUBS = {"status", "log", "diff", "ls-files", "grep", "show"}
# flags of ``git log`` that cannot put the content of a file in its output: a log with only these lists every commit, and the credential files are left out of the others (a commit that touches
# only those files is then not listed, which is what leaving them out means)
_LOG_PLAIN = {"--oneline", "--graph", "--decorate", "--no-decorate", "--all", "--branches", "--tags", "--remotes", "--since", "--until", "--after", "--before", "--author", "--committer", "--grep",
              "--max-count", "--skip", "--format", "--pretty", "--abbrev-commit", "--no-abbrev-commit", "--abbrev", "--date", "--stat", "--shortstat", "--numstat", "--name-only", "--name-status",
              "--no-merges", "--merges", "--first-parent", "--reverse", "--follow", "--topo-order", "--date-order", "--author-date-order", "--relative-date", "--no-color", "-n", "-i",
              "--regexp-ignore-case", "--invert-grep", "--all-match", "--children", "--parents", "--boundary", "--left-right", "--cherry-pick", "--simplify-by-decoration", "--source",
              "--full-history", "--ancestry-path", "--no-walk"}


def _without_kept_entries(text: str) -> str:
    """The lines of a ``git ls-tree`` listing that do not name a file or folder of credentials (``mode type id TAB path``, or only the path with --name-only). The id of such a file is not shown either."""
    out = []
    for line in text.split("\n"):
        path = line.rpartition("\t")[2] if "\t" in line else line
        path = path.strip()
        if len(path) > 1 and path[0] == path[-1] == '"':
            path = path[1:-1]
        if path and credentials.is_kept(path):
            continue
        out.append(line)
    return "\n".join(out)


def plan_git(args: list[str]) -> tuple[str, list[str]]:
    """``(sub-command, the rest)`` of a git command, checked. Nothing before the sub-command (no ``-c``, ``-C``, ``--git-dir``), only sub-commands that read, and no option that names a
    program, a file to write or a file to read from outside the repository."""
    if not args:
        raise Refused(f"git: give a sub-command: {', '.join(sorted(_GIT_SUBS))}.")
    sub, rest = args[0], args[1:]
    if sub.startswith("-"):
        raise Refused(f"git: options before the sub-command ({sub}) are not allowed here.")
    if sub not in _GIT_SUBS:
        raise NotInPlan(f"git {sub} is not allowed in this read-only terminal. Allowed: {', '.join(sorted(_GIT_SUBS))}.")
    for a in rest:
        if a == "--":
            break
        key = a.split("=", 1)[0]
        abbreviation = key.startswith("--") and len(key) > 2 and any(n.startswith(key) for n in _GIT_NEVER)       # git takes any unique start of a long option: --ext-dif is --ext-diff
        short = a.startswith("-") and not a.startswith("--") and (a.startswith("-O") or any(a.startswith(f) for f in _GIT_SHORT_NEVER.get(sub, ())))
        if key in _GIT_NEVER or key.startswith("--output") or abbreviation or short:
            raise Refused(f"git {sub} {key} is not allowed in this read-only terminal.")
    if sub == "ls-tree":
        for a in rest:
            if a == "--":
                break
            if a.startswith("--") and a not in _LS_TREE_LONG or a.startswith("-") and not a.startswith("--") and not set(a[1:]) <= _LS_TREE_SHORT:
                raise Refused(f"git ls-tree {a} is not allowed in this read-only terminal. Allowed: -r, -t, -d, -l, {', '.join(sorted(_LS_TREE_LONG))}.")
    if sub == "remote" and any(not a.startswith("-") for a in rest):
        raise NotInPlan("git remote: only `git remote` and `git remote -v` are allowed (the other forms reach the network or change the repository).")
    if sub in _GIT_LIST_FLAGS:
        allowed = _GIT_LIST_FLAGS[sub]
        listing = sub == "remote" or bool(_GIT_LISTING & {a.split("=", 1)[0] for a in rest})
        i = 0
        while i < len(rest):
            a = rest[i]
            key = a.split("=", 1)[0]
            if a.startswith("-"):
                if key not in allowed and not re.fullmatch(r"-n\d*", a):
                    raise NotInPlan(f"git {sub} {a} would change the repository; only the list forms are allowed ({', '.join(sorted(allowed))}).")
                if key in _GIT_LIST_VALUES and "=" not in a:
                    i += 1
            elif not listing:
                raise NotInPlan(f"git {sub} {a} would make a {sub}; only listing is allowed here.")
            i += 1
    return sub, rest


# -- the terminal -----------------------------------------------------------------------------------------------------------------------------------------------

class HostReadOnly:
    """One read-only terminal over one project. ``store`` is ``(project id, session id)`` where the whole of a long output is kept for ``recall``."""

    def __init__(self, ws: chat_tools.Workspace, *, store: tuple[str, str] | None = None, tools_root: pathlib.Path | None = None, home: pathlib.Path | None = None):
        self.ws, self.store = ws, store
        self.rg = toolchain.find("ripgrep", tools_root)
        self.rtk = toolchain.find("rtk", tools_root)
        self.git = find_git(tools_root)
        self.term = builtins.Terminal(ws)
        self.ran: list[str] = []
        self.hidden: tuple[int, str] | None = None
        self.env = rtk.environment(path_dirs=[p.parent for p in (self.rg, self.rtk, self.git) if p], home=home)

    def available(self) -> dict:
        return {"rg": self.rg is not None, "rtk": self.rtk is not None, "git": self.git is not None}

    def _git_recent(self, git: pathlib.Path) -> bool:
        """Whether git understands ``GIT_CONFIG_COUNT`` (2.31 and later): without it the settings of a repository could make it start a program, so an older git is not used."""
        try:
            said = toolchain.run([str(git), "--version"], timeout=10, env=self.env).stdout
            m = re.search(r"(\d+)\.(\d+)", said)
            return bool(m) and (int(m.group(1)), int(m.group(2))) >= (2, 31)
        except (subprocess.SubprocessError, OSError):
            return False

    # -- reading the command --------------------------------------------------------------------------------------------------------------------------------

    def parse(self, command: str):
        text = (command or "").strip()
        if not text:
            raise Refused("Give a command, for example: git status")
        if len(text) > MAX_COMMAND or "\x00" in text:
            raise Refused(f"A command is at most {MAX_COMMAND:,} characters.")
        parsed = permissions.parse(text)
        flags = parsed.flags
        if "hidden" in flags:
            raise NotInPlan("A command that runs another inside itself ($(...), backticks, eval, bash -c, xargs, find -exec) is not allowed in this read-only terminal.")
        if "write" in flags or parsed.redirects:
            raise NotInPlan("This terminal is read-only: a redirect (> or >>) would write a file.")
        if "background" in flags:
            raise NotInPlan("A command cannot be started in the background here.")
        if "heredoc" in flags:
            raise Refused("A here-document (<<) is not supported here.")
        if "syntax" in flags or not parsed.segments:
            raise Refused("There is a separator (&& || | ;) with no command on one side of it.")
        if len(parsed.segments) > MAX_STAGES:
            raise Refused(f"At most {MAX_STAGES} commands in one line.")
        for words in parsed.segments:
            if len(words) > MAX_ARGS or any(len(w) > MAX_ARG or "\x00" in w for w in words):
                raise Refused("A command has too many or too long arguments.")
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
                raise Refused("Setting a variable in front of a command is not allowed here.")
        return parsed

    # -- running it ------------------------------------------------------------------------------------------------------------------------------------------

    def run(self, command: str, *, timeout: float = TIMEOUT, raw: bool = False) -> dict:
        """Run a command line and return the result. ``raw`` runs git and ripgrep as they are, without rtk condensing what they print."""
        started = time.monotonic()
        parsed = self.parse(command)
        groups: list[list[list[str]]] = [[parsed.segments[0]]]
        links: list[str] = []
        for words, op in zip(parsed.segments[1:], parsed.ops):
            if op == "|":
                groups[-1].append(words)
            else:
                links.append(op)
                groups.append([words])
        self.term.cwd, self.ran, self.hidden = "", [], None
        out_parts: list[str] = []
        err_parts: list[str] = []
        code = 0
        for i, group in enumerate(groups):
            if i > 0 and ((links[i - 1] == "&&" and code != 0) or (links[i - 1] == "||" and code == 0)):
                continue
            stdin: str | None = None
            for words in group:
                res = self._stage(words, stdin, len(group) == 1 and len(groups) == 1 and not raw, timeout)
                stdin, code = res.text, res.code
                if res.err.strip():
                    err_parts.append(res.err)
            out_parts.append(stdin or "")
        return self._result(command, "".join(out_parts), "".join(err_parts), code, started)

    def _stage(self, words: list[str], stdin: str | None, condense: bool, timeout: float) -> builtins.Out:
        name = pathlib.PurePosixPath(words[0].replace("\\", "/")).name.lower().removesuffix(".exe")
        args = words[1:]
        try:
            if name == "cd":
                return self._cd(args)
            if name in builtins.BUILTINS:
                self.ran.append(f"(built in) {' '.join(words)}")
                return builtins.BUILTINS[name](self.term, args, stdin)
            if name in ("rg", "grep"):
                return self._rg(name, args, stdin, condense, timeout)
            if name == "git":
                return self._git(args, condense, timeout)
        except builtins.Usage as e:
            return builtins.Out(err=f"{e}\n", code=2)
        raise NotInPlan(f"{name} is not allowed in this read-only terminal. Allowed: {ALLOWED_TEXT}")

    def _cd(self, args: list[str]) -> builtins.Out:
        if len(args) > 1:
            raise Refused("cd: one folder.")
        target = args[0] if args else ""
        joined = self.term.join(target) if target else ""
        if joined:
            label, full, rel = self._resolve(target)
            if full and not os.path.isdir(full):
                return builtins.Out(err=f"cd: {target}: not a folder\n", code=1)
        self.term.cwd = joined
        self.ran.append(f"(built in) cd {target}".strip())
        return builtins.Out()

    def _need(self, tool: pathlib.Path | None, what: str, how: str) -> pathlib.Path:
        if tool is None:
            raise Refused(f"{what} is not installed, so this command cannot run. {how}")
        return tool

    def _resolve(self, arg: str) -> tuple[str, str, str]:
        """``(label, absolute path, path below its root)`` for a path as the model wrote it, or a refusal that says why."""
        if builtins.is_device(arg):
            raise Refused(f"{arg}: that is a name of the system, not a file.")
        try:
            return self.ws.resolve(self.term.join(arg))
        except chat_tools.ToolError as e:
            raise Refused(f"{arg}: {e}") from None

    def _targets(self, paths: list[str]) -> list[tuple[str, str, list[str]]]:
        """``[(label, root, [paths below the root])]`` for the paths a command named (the folder it is in when it named none)."""
        out: dict[str, tuple[str, str, list[str]]] = {}
        for p in paths or [""]:
            label, full, rel = self._resolve(p)
            if not full:                                                        # the top of a project of several folders
                for lab, root in self.ws.folders:
                    out.setdefault(root, (lab, root, []))
                continue
            root = self.ws.root_of(label)
            entry = out.setdefault(root, (label, root, []))
            if rel and rel not in entry[2]:
                entry[2].append(rel)
        return list(out.values())

    # -- ripgrep -------------------------------------------------------------------------------------------------------------------------------------------------

    def rg_json(self, args: list[str], timeout: float = TIMEOUT) -> builtins.Out:
        """Ripgrep for a tool of the app (``grep``, ``glob``) rather than for a command line: the arguments are checked like any others, nothing is condensed, and the output of a project
        of several folders is not headed by their names (the tool labels its results itself)."""
        return self._rg("rg", args, None, False, timeout, labels=False)

    def _rg(self, name: str, args: list[str], stdin: str | None, condense: bool, timeout: float, labels: bool = True) -> builtins.Out:
        rg = self._need(self.rg, "ripgrep", "Install it in Settings (Engines, Command tools).")
        if name == "grep":
            args = translate_grep(args)
        flags, pattern, paths = plan_rg(args)
        names = {f for f, _ in flags}
        flat: list[str] = []
        for f, v in flags:
            flat += [f] if v is None else [f, v]
        if pattern is not None:
            flat += ["-e", pattern]
        condensed = condense and self.rtk is not None and stdin is None and not names & _RG_PLAIN_ONLY
        targets = [("", str(self.ws.folders[0][1]), [])] if (stdin is not None and not paths) else self._targets(paths)
        results: list[builtins.Out] = []
        for label, root, rels in targets:
            argv = [str(rg), "--no-config", "--path-separator", "/", *flat, *rg_excludes()]
            if stdin is None and rels:
                argv += ["--", *rels]
            if condensed:
                argv = [str(self.rtk), "rg", *argv[1:]]
            self.ran.append((("rtk rg " if condensed else "rg ") + " ".join(args)).strip())
            res = self._exec(argv, root, stdin, timeout, condensed)
            if labels and len(targets) > 1 and res.text:
                res.text = f"== {label} ==\n{res.text}"
            results.append(res)
        return _join(results)

    # -- git -------------------------------------------------------------------------------------------------------------------------------------------------------

    def _git(self, args: list[str], condense: bool, timeout: float) -> builtins.Out:
        git = self._need(self.git, "git", "Install git to read the history.")
        if not self._git_recent(git):
            raise Refused("The git on this computer is older than 2.31, which this read-only terminal needs to keep a repository's own settings from starting programs. Update git.")
        sub, rest = plan_git(args)
        kept, paths, revs, blobs = self._git_tokens(sub, rest)
        if blobs and sub != "ls-tree":
            others = [a for a in kept if (a.startswith("-") and not _BLOB_FLAG.fullmatch(a)) or (not a.startswith("-") and a not in blobs)]
            if revs or paths or others:
                raise Refused("git: a file out of the history (REV:path) is read on its own: no other revision, no path and no option that selects revisions beside it (only options such as --stat, -w "
                              "or -U3 that change how it is drawn). The commits and diffs printed for those would not have the files of credentials left out, because the rule that leaves them out "
                              "cannot be applied to a file read by REV:path.")
        roots = self._git_roots(paths)
        inject = ["--no-ext-diff", "--no-textconv"] if sub in ("diff", "log", "show") else []
        if sub in ("status", "diff"):
            inject.append("--ignore-submodules=all")                          # a submodule is another repository with settings of its own
        # settings given to git itself win over the repository's own. gpg.* matter because a placeholder such as %G? in --format checks a signature by starting the program of gpg.program
        overrides = {"core.fsmonitor": "false", "core.hooksPath": os.devnull, "protocol.allow": "never", "core.pager": "cat", "color.ui": "false", "log.showSignature": "false",
                     "gpg.program": os.devnull, "gpg.openpgp.program": os.devnull, "gpg.x509.program": os.devnull, "gpg.ssh.program": os.devnull,
                     # a submodule's own settings are never read: the repository's configuration cannot make a command go into one
                     "diff.ignoreSubmodules": "all", "submodule.recurse": "false", "grep.recurseSubmodules": "false", "status.submoduleSummary": "false", "diff.submodule": "short"}
        env = dict(self.env, GIT_PAGER="cat", GIT_CONFIG_COUNT=str(len(overrides)))
        for i, (key, value) in enumerate(overrides.items()):
            env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
        condensed = condense and self.rtk is not None and sub in ("status", "diff", "log", "show", "branch", "grep", "ls-files", "blame")
        results: list[builtins.Out] = []
        for label, root, mine in roots:
            risky = self._repo_names_a_program(git, root, env)
            if risky:
                raise Refused(f"git is not run in this repository by the read-only terminal: its own settings name a program that git would start while reading ({risky}). "
                              "Read the files with cat and rg instead, or take that setting out of the repository's configuration.")
            self._refuse_blob_ids(git, root, env, revs)
            if sub != "ls-tree":
                self._require_files(git, root, env, blobs)
            leave_out = sub in _PATHSPEC_SUBS and not blobs and not (sub == "log" and self._log_is_plain(kept))
            tail = ["--", *mine, *git_excludes()] if leave_out else (["--", *mine] if mine else [])
            argv = ([str(self.rtk), "git"] if condensed else [str(git)]) + [sub, *inject, *kept, *tail]
            self.ran.append((("rtk " if condensed else "") + "git " + " ".join([sub, *rest])).strip())
            res = self._exec(argv, root, None, timeout, condensed, env)
            if sub == "ls-tree":
                res.text = _without_kept_entries(res.text)
            if len(roots) > 1 and res.text:
                res.text = f"== {label} ==\n{res.text}"
            results.append(res)
        return _join(results)

    @staticmethod
    def _log_is_plain(tokens: list[str]) -> bool:
        """Whether every flag of a ``git log`` is one that cannot print a file's content (a revision or a value is not a flag)."""
        flags = [t.split("=", 1)[0] for t in tokens if t.startswith("-")]
        return all(f in _LOG_PLAIN or re.fullmatch(r"-\d+", f) for f in flags)

    def _require_files(self, git: pathlib.Path, root: str, env: dict, blobs: list[str]) -> None:
        """Each ``REV:path`` must be a file: for a folder, ``git diff A:dir B:dir`` prints the content of everything in it, the files of credentials too."""
        for word in blobs:
            try:
                r = toolchain.run([str(git), "cat-file", "-t", word], timeout=10, cwd=root, env=env)
            except (subprocess.SubprocessError, OSError):
                raise Refused(f"git: could not tell what {word} names, so it was not run.") from None
            if r.returncode != 0 or (r.stdout or "").strip() != "blob":                  # fail closed: what git cannot say is a file is not read
                raise Refused(f"git: {word} is not a file that exists in the history. Name the file itself (REV:path/to/file); to list a folder of the history use git ls-tree.")

    def _repo_names_a_program(self, git: pathlib.Path, root: str, env: dict) -> str | None:
        """The first setting of the repository at ``root`` that names a program git would start while reading (a filter, a text converter, an external diff, an included file that could hold
        one), or None. Reading the configuration starts nothing. Git itself is told to leave the other program-starting settings alone (the environment of ``_git``)."""
        try:
            r = toolchain.run([str(git), "config", "--list", "-z", "--show-scope"], timeout=10, cwd=root, env=env)
        except (subprocess.SubprocessError, OSError):
            return "its settings could not be read"
        if r.returncode != 0:
            return None if "not a git repository" in (r.stderr or "").lower() else "its settings could not be read"
        fields = (r.stdout or "").split("\0")                       # -z --show-scope: "scope NUL key NEWLINE value NUL" for each setting, so a value that holds a newline cannot hide a second line
        for scope, entry in zip(fields[0::2], fields[1::2]):
            if scope not in _CONFIG_SCOPES_OF_A_REPOSITORY:
                continue
            key, _, value = entry.partition("\n")
            if _RISKY.match(key.lower()) and not (key.lower().startswith("filter.lfs.") and _LFS.fullmatch(value)):
                return f"{key} = {value}"[:160]
        return None

    def _refuse_blob_ids(self, git: pathlib.Path, root: str, env: dict, revs: list[str]) -> None:
        """A word that names a file's content by its object id (``git show <id>``, ``git diff <id> <id>``) would read a credential file that was committed once, by a name the rules about paths never
        see. Only a word that git resolves to a commit, a tag of one or a tree is passed on; a file out of the history is read as ``REV:path``, where the path rule applies."""
        for word in revs:
            for part in re.split(r"\.\.\.?", word):
                part = part.lstrip("^")
                if not part or ":" in part:
                    continue
                try:
                    r = toolchain.run([str(git), "cat-file", "-t", f"{part}^{{}}"], timeout=10, cwd=root, env=env)
                except (subprocess.SubprocessError, OSError):
                    raise Refused(f"git: could not tell what {word} names, so it was not run.") from None
                if r.returncode == 0 and (r.stdout or "").strip() == "blob":
                    raise Refused(f"git: {word} is the id of a file's content. Read a file out of the history as REV:path (for example HEAD~1:src/app.py), so that the rule about files of credentials applies.")

    def _git_tokens(self, sub: str, rest: list[str]) -> tuple[list[str], list[tuple[str, str, str]], list[str], list[str]]:
        """``(the arguments that stay where they are, [(label, root, path below the root)] of the paths among them, the words that name a revision)``. Words after ``--`` are paths; before it, a word
        is a path when it names something in the project (git would read it the same way); the pattern of ``git grep`` never is. Every path is checked like any other, and ``REV:path`` (a file out of
        the history) is held to the same rule, because the history may hold a credential file that was committed once. What is left over (not a flag, not the value of one, not a path) is a revision, and the ``REV:path`` words are the fourth item."""
        kept: list[str] = []
        paths: list[tuple[str, str, str]] = []
        revs: list[str] = []
        blobs: list[str] = []
        after = False
        pattern_pending = sub == "grep" and not any(a in ("-e", "--regexp") or a.startswith("-e") and len(a) > 2 for a in rest)
        takes_value = False
        for a in rest:
            if a == "--" and not after:
                after = True
                continue
            if after:
                if a.startswith(":"):
                    raise Refused("git: pathspec magic (a leading colon) is not allowed here.")
                label, full, rel = self._resolve(a)
                paths.append((label, self.ws.root_of(label) if full else self.ws.folders[0][1], rel))
                continue
            if takes_value:
                takes_value = False
                kept.append(a)
                continue
            if a.startswith("-"):
                kept.append(a)
                takes_value = a in _GIT_VALUE_WORDS or a in _GIT_VALUE_BY_SUB.get(sub, ())
                continue
            if pattern_pending:
                pattern_pending = False
                kept.append(a)
                continue
            m = _REV_PATH.match(a)
            if m:                                                       # REV:path, :path and :0:path read a file out of the history or the index, in every sub-command that takes a blob (git diff A:f B:f)
                if ".." in a or a.startswith("^") or "^{" in a:
                    raise Refused(f"git: {a}: a file out of the history is written REV:path, with no range, no leading ^ and no ^{{...}} in it.")
                if sub in ("show", "blame", "ls-tree"):
                    self._resolve(m.group(1).lstrip("/"))
                if any(credentials.is_kept(a[i + 1:]) for i, c in enumerate(a) if c == ":"):
                    raise Refused(f"git: {a} names a file of credentials, which this terminal does not read.")
                kept.append(a)
                blobs.append(a)
                continue
            if sub == "ls-files" or self._is_project_path(a):
                label, full, rel = self._resolve(a)
                paths.append((label, self.ws.root_of(label) if full else self.ws.folders[0][1], rel))
                continue
            kept.append(a)
            revs.append(a)
        if sub == "blame" and not paths:
            raise Refused("git blame: give the file.")
        return kept, paths, revs, blobs

    def _is_project_path(self, token: str) -> bool:
        try:
            label, full, rel = self.ws.resolve(self.term.join(token))
        except chat_tools.ToolError:
            return False
        return bool(full) and os.path.exists(full)

    def _git_roots(self, paths: list[tuple[str, str, str]]) -> list[tuple[str, str, list[str]]]:
        """``[(label, root, [paths])]``: the folders a git command runs in. The one its paths are in; with none, the folder the terminal is in, or every folder of the project that is a repository."""
        by_root: dict[str, tuple[str, str, list[str]]] = {}
        for label, root, rel in paths:
            entry = by_root.setdefault(root, (label, root, []))
            if rel:
                entry[2].append(rel)
        if by_root:
            return list(by_root.values())
        if self.term.cwd:
            label, full, rel = self._resolve("")
            if full:
                root = self.ws.root_of(label)
                return [(label, root, [rel] if rel else [])]
        repos = [(lab, root, []) for lab, root in self.ws.folders if os.path.isdir(os.path.join(root, ".git"))]
        return repos or [(self.ws.folders[0][0], self.ws.folders[0][1], [])]

    # -- starting a program --------------------------------------------------------------------------------------------------------------------------------

    def _exec(self, argv: list[str], cwd: str, stdin: str | None, timeout: float, condensed: bool, env: dict | None = None) -> builtins.Out:
        def run(a: list[str]):
            return toolchain.run(a, timeout=timeout, cwd=cwd, env=env or self.env, stdin=stdin)
        try:
            r = run(argv)
        except subprocess.TimeoutExpired:
            return builtins.Out(err=f"The command did not finish in {timeout:g} seconds and was stopped.\n", code=124)
        except OSError as e:
            return builtins.Out(err=f"The command could not be started: {e}\n", code=127)
        err = "".join(line + "\n" for line in (r.stderr or "").splitlines() if not line.startswith("rtk: Failed to resolve"))
        if condensed and r.returncode >= 2 and not (r.stdout or "").strip():       # rtk could not run it: run the program itself, so a result is never lost to the proxy
            tool = self.rg if argv[1] == "rg" else self.git
            try:
                r2 = run([str(tool), *argv[2:]])
                self.ran.append("(rtk could not run it, so it was run directly)")
                return builtins.Out(r2.stdout or "", r2.stderr or "", r2.returncode)
            except (subprocess.SubprocessError, OSError):
                pass
        hint = rtk.hint_of(r.stdout or "")
        if hint and self.hidden is None:
            self.hidden = hint
        elif condensed and self.hidden is None and _ELIDED.search(r.stdout or ""):
            self.hidden = (sum(int(g) for m in _ELIDED.finditer(r.stdout or "") for g in m.groups() if g), "")          # left out, with no handle to get it back: it is run again as it is
        return builtins.Out(r.stdout or "", err, r.returncode)

    # -- the result ----------------------------------------------------------------------------------------------------------------------------------------------

    def _result(self, command: str, text: str, err: str, code: int, started: float) -> dict:
        out = termtext.strip_escapes(text).replace("\r\n", "\n")
        err = termtext.strip_escapes(err).replace("\r\n", "\n")
        if err.strip() and (code != 0 or not out.strip()):
            out = out + ("" if not out or out.endswith("\n") else "\n") + err
        out = code_rules.mask(clip.shorten_runs(out))
        total = out.count("\n") + (1 if out and not out.endswith("\n") else 0)
        recovery, cut = None, False
        if len(out) > OUTPUT_CHARS:
            cut = True
            if self.store:
                try:
                    recovery = outputs.save(self.store[0], self.store[1], out)
                except (outputs.OutputError, OSError):
                    recovery = None
            head, tail = out[: int(OUTPUT_CHARS * 0.7)], out[-int(OUTPUT_CHARS * 0.3):]
            head = head[: head.rfind("\n")] if "\n" in head else head
            tail = tail[tail.find("\n") + 1:] if "\n" in tail else tail
            left = max(total - head.count("\n") - tail.count("\n") - 2, 0)
            out = f"{head}\n... {left} lines left out" + (f"; recall {recovery} returns them" if recovery else "") + f" ...\n{tail}"
        shown = " ".join(command.split())
        result = {"command": command, "ran_as": list(self.ran), "exit_code": code, "output": out, "lines": total, "duration_ms": int((time.monotonic() - started) * 1000),
                  "summary": f"Ran {shown[:90]}{'...' if len(shown) > 90 else ''}" + (f" (exit {code})" if code else "")}
        if self.ran and all(r.startswith("(built in) echo ") or r == "(built in) echo" for r in self.ran):       # a line that only echoes is the model "telling" the owner something: it is not seen by them
            result["hint"] = ECHO_HINT
        if cut:
            result["truncated"] = True
        if recovery:
            result["recovery"] = recovery
        if self.hidden:
            result["condensed"] = {"by": "rtk", "hidden_lines": self.hidden[0], "recall": self.hidden[1] or None}
            if self.hidden[1]:
                result.setdefault("recovery", self.hidden[1])
            else:
                result["note"] = "rtk left part of this output out. Run the command again with raw set to true to get all of it."
        return result


def _join(parts: list[builtins.Out]) -> builtins.Out:
    return builtins.Out("".join(p.text for p in parts), "".join(p.err for p in parts), next((p.code for p in reversed(parts) if p.code), 0))
