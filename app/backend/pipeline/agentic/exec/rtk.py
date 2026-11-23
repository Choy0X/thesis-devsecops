"""rtk as the command proxy: how the app runs it, asks it to rewrite a command, and gets back what it left out.

rtk is a program that sits between a command and whoever reads its output: ``rtk rewrite "git status"`` says what the same command is as rtk runs it (``rtk git status``), and rtk then
runs the real command and prints a condensed version of what it printed. What it left out is kept by a short hash (``[+5 hidden: rtk recall 999d0481d375]``) and comes back with
``rtk recall``. The agent's terminal puts every command it can through this, in every mode: the model writes the command as usual, this module turns it into the rtk form, and the model
reads the condensed output (and can ask for the rest).

Facts that were measured on rtk 1.0.1 (the ``rtk-oss`` edition, the one that is pinned) and that the code relies on:

* ``rtk rewrite`` exits 0 or 3 when it printed a rewrite (3: it is the host's job to decide whether to allow it, and there are no rules here, so 3 is the usual), 1 when there is nothing to
  rewrite (an unsupported command, ``$(...)``, a pipeline, a redirect), 2 when a rule denied the command. A command that is already in rtk form comes back as itself.
* ``rtk git``, ``rtk rg``, ``rtk find`` and ``rtk read`` do their work themselves. ``rtk ls``, ``rtk wc``, ``rtk grep`` and ``rtk tree`` run the system's own program, which a Windows
  computer does not have, so those are not used on Windows (``rtk_can_run``).
* A copy that is given a different ``USERPROFILE``, ``HOME``, ``APPDATA`` and ``LOCALAPPDATA`` keeps its configuration, history and recall store inside them and nowhere else, with the
  other variables of the environment kept to the few the system needs. Run with the owner's own environment it writes into the owner's own ``rtk`` folders and, with a wrong cwd, into the
  folder it runs in: ``environment`` is how it is always run, and its cwd is never a project's folder when anything is written.
* ``rtk config --create`` rewrites an existing configuration file with the defaults. Nothing here calls it with an environment that is not the isolated one.
"""
from __future__ import annotations

import os
import pathlib
import re
import tempfile

from app.backend.pipeline.agentic.exec import toolchain

RTK_HOME = pathlib.Path(os.environ.get("ZTSA_RTK_HOME") or "data/tools/rtk-home")
HINT = re.compile(r"\[\+(\d+) hidden: rtk recall ([0-9a-f]{6,64})\]")
HANDLE = re.compile(r"^[0-9a-f]{6,64}$")
CONFIG = """[tracking]
enabled = false
history_days = 1

[display]
colors = false
emoji = false
max_width = 200

[retriever]
mode = "sqlite"
max_entry_bytes = 10485760
max_entries = 500
"""
# the variables of the system a program needs to start; everything else of the owner's environment (keys, tokens, paths) is left out
SYSTEM_VARS = ("SystemRoot", "SystemDrive", "windir", "COMSPEC", "PATHEXT", "OS", "USERNAME", "USERDOMAIN", "COMPUTERNAME", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
               "ALLUSERSPROFILE", "PUBLIC", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "LANG", "LC_ALL", "TZ")


def rtk_can_run(subcommand: str, system: str | None = None) -> bool:
    """Whether ``rtk <subcommand>`` does its own work on this system (see the facts above). On Windows ``ls``, ``wc``, ``grep`` and ``tree`` need a program the system does not have."""
    import platform
    return not ((system or platform.system()).lower() == "windows" and subcommand in ("ls", "wc", "grep", "tree"))


def profile(home: pathlib.Path | None = None) -> pathlib.Path:
    """The private home of rtk (created if needed, with its configuration written once): the only place it keeps anything."""
    h = pathlib.Path(home or RTK_HOME).resolve()
    for sub in ("AppData/Roaming", "AppData/Local", "tmp", ".config", ".local/share", ".cache", "Library/Application Support"):
        (h / sub).mkdir(parents=True, exist_ok=True)
    for where in ("AppData/Roaming/rtk", ".config/rtk", "Library/Application Support/rtk"):
        config = h / where / "config.toml"
        if not config.exists():
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(CONFIG, encoding="utf-8")
    return h


def environment(path_dirs: list[pathlib.Path | str] | None = None, home: pathlib.Path | None = None, extra: dict | None = None, base: dict | None = None) -> dict:
    """The environment every program of the terminal runs in: the few system variables, a PATH of ``path_dirs`` and the system's own folders, and rtk's private home. No key, token or other
    variable of the owner's environment gets through."""
    h = profile(home)
    src = os.environ if base is None else base
    env = {k: src[k] for k in SYSTEM_VARS if k in src}
    system = [src.get("SystemRoot", "") and str(pathlib.Path(src["SystemRoot"]) / "System32"), src.get("SystemRoot", "")] if os.name == "nt" else ["/usr/local/bin", "/usr/bin", "/bin"]
    env["PATH"] = os.pathsep.join(str(p) for p in [*(path_dirs or []), *system] if p)
    env.update({"USERPROFILE": str(h), "HOME": str(h), "APPDATA": str(h / "AppData/Roaming"), "LOCALAPPDATA": str(h / "AppData/Local"), "XDG_CONFIG_HOME": str(h / ".config"),
                "XDG_DATA_HOME": str(h / ".local/share"), "XDG_CACHE_HOME": str(h / ".cache"), "TEMP": str(h / "tmp"), "TMP": str(h / "tmp"), "TMPDIR": str(h / "tmp"),
                "RTK_TELEMETRY_DISABLED": "1", "NO_COLOR": "1", "TERM": "dumb", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_PAGER": "cat", "PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    env.update(extra or {})
    return env


class Rewrite:
    """What ``rtk rewrite`` said: ``code`` (0 or 3: ``text`` is the command as rtk runs it; 1: nothing to rewrite; 2: refused, ``text`` may say why)."""

    def __init__(self, code: int, text: str):
        self.code, self.text = code, text.strip()

    @property
    def rewritten(self) -> bool:
        return self.code in (0, 3) and bool(self.text)

    @property
    def refused(self) -> bool:
        return self.code == 2


def rewrite(exe: pathlib.Path, command: str, env: dict, timeout: float = 10) -> Rewrite:
    """Ask rtk what ``command`` is as it runs it. The command is given as one argument and is never run by this."""
    r = toolchain.run([str(exe), "rewrite", command], timeout=timeout, env=env, cwd=env.get("TEMP"))
    return Rewrite(r.returncode, r.stdout or "")


def recall(exe: pathlib.Path, handle: str, env: dict, *, full: bool = False, start: int | None = None, lines: int | None = None, grep: str | None = None, timeout: float = 20) -> str:
    """What a filter left out, by the handle it printed: the part that was missed, or with ``full`` the whole output (from line ``start``, at most ``lines`` lines, only those matching ``grep``)."""
    if not HANDLE.fullmatch(handle or ""):
        raise toolchain.ToolchainError("A recall handle is the short hex code printed after 'rtk recall' in a hint.")
    argv = [str(exe), "recall", handle]
    if full:
        argv.append("--full")
    if start:
        argv += ["--from", str(int(start))]
    if lines:
        argv += ["--lines", str(int(lines))]
    if grep:
        argv += ["--grep", grep]
    r = toolchain.run(argv, timeout=timeout, env=env, cwd=env.get("TEMP"))
    if r.returncode != 0:
        raise toolchain.ToolchainError((r.stderr or r.stdout or "rtk could not recall that").strip()[:300])
    return r.stdout


def hint_of(text: str) -> tuple[int, str] | None:
    """``(how many lines were left out, the handle)`` from the hint at the end of condensed output, or None."""
    found = HINT.findall(text or "")
    return (int(found[-1][0]), found[-1][1]) if found else None


def self_test(exe: pathlib.Path, want: str) -> tuple[bool, list[str]]:
    """Run the copy the way the app does and check what the app relies on: its version, the rewrite of a supported and an unsupported command, and that what a filter left out comes back
    whole. ``(passed, what was seen)``."""
    notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="rtk-test-") as scratch:
        env = environment(home=pathlib.Path(scratch) / "home")
        version = toolchain.run([str(exe), "--version"], env=env, cwd=scratch)
        if want not in version.stdout:
            return False, [f"rtk --version said {version.stdout.strip()[:60]!r}, not {want}"]
        supported = rewrite(exe, "git status", env)
        if not supported.rewritten or not supported.text.startswith("rtk git status"):
            return False, [f"rtk rewrite 'git status' gave exit {supported.code} and {supported.text[:60]!r}, not 'rtk git status'"]
        if rewrite(exe, "echo hi", env).code != 1:
            return False, ["rtk rewrite 'echo hi' did not say there was nothing to rewrite"]
        tree = pathlib.Path(scratch) / "tree"
        names = [f"file_{i:03d}.txt" for i in range(120)]
        for i, n in enumerate(names):
            d = tree / f"dir_{i % 12}"
            d.mkdir(parents=True, exist_ok=True)
            (d / n).write_text("x\n", encoding="utf-8")
        listing = toolchain.run([str(exe), "find", str(tree), "-name", "*.txt"], env=env, cwd=scratch)
        hint = hint_of(listing.stdout)
        if hint is None:
            return False, ["a long listing through rtk find left something out but printed no recall hint"]
        back = recall(exe, hint[1], env, full=True)
        missing = [n for n in names if n not in back]
        if missing:
            return False, [f"rtk recall --full did not bring back {len(missing)} of {len(names)} file names"]
        notes.append(f"rtk {want} (rtk-oss): rewrite as expected; a listing of {len(names)} files left {hint[0]} lines out and recall brought them all back")
    return True, notes
