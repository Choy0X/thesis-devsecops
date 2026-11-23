"""The terminal of Manual and Auto: commands that run in the project's own copy, inside its sandbox container, and print as they run.

The container is the closed one of ``scan/agent_sandbox.py`` (no network, every capability dropped, no new privileges, a read-only root, a process and memory limit); only ``/work`` is written. This
module is what runs a command in it:

* **A shell that remembers.** There is no terminal, so each command is one program run, but it starts where the last one ended: its folder and its exported variables are saved to
  ``/work/.agent/state/<session>`` when it finishes and restored when the next starts (``cd src`` in one call and ``pytest`` in the next run in ``src``). ``/work/.agent`` is beside the copies of the
  project, not inside them, so none of it ever shows in a diff.
* **rtk in front of it.** The command goes to ``rtk rewrite`` inside the container; what comes back (``rtk git status``) is what runs, and what a filter left out is named by a handle that
  ``recall`` reads. A pipeline, ``cd``, a redirect or a command rtk has no filter for is run as it was written. ``raw`` skips rtk.
* **Output as it arrives.** ``run`` takes a function that is given the output in pieces (masked for credentials and stripped of terminal codes, a line at a time so a secret split across two
  pieces is still masked); the final result is the whole of it, cut to what the model is sent with the full text kept beside the session.
* **Jobs.** ``background`` starts the command detached (its own session, a log, a file for its exit status) and returns at once; ``job_output`` reads the log from a line and says whether it is still
  running; ``job_kill`` ends it and everything it started.
* **A run that is stopped.** A run is its own process group: when the owner stops the session (or the time is up) the whole group is ended, not only the shell.
"""
from __future__ import annotations

import posixpath
import re
import shlex
import threading
import time
import uuid
from typing import Callable

from app.backend import sandbox, sandbox_files, termtext
from app.backend.pipeline import chat_tools
from app.backend.pipeline.agentic import clip, outputs
from app.backend.pipeline.agentic.exec import rtk
from app.backend.pipeline.scan import code_rules

STATE = "/work/.agent"
HOME = "/work/.home"
UID = 10001
OUTPUT_CHARS = 12_000
FLOOD_CHARS = 4_000_000               # what one run may print before it is stopped (a loop that prints forever)
MAX_COMMAND = 100_000
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 900
PART = 4_000                          # an unfinished line is shown after this many characters
MAX_JOBS = 8
ENVIRONMENT = {"HOME": HOME, "XDG_CONFIG_HOME": "/etc/xdg", "XDG_DATA_HOME": f"{STATE}/data", "XDG_CACHE_HOME": f"{STATE}/cache", "RTK_TELEMETRY_DISABLED": "1", "NO_COLOR": "1", "TERM": "dumb",
               "LANG": "C.UTF-8", "PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1"}
_READY: set[str] = set()
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_NOT_SAVED = r"^declare -x (_|HOME|PWD|OLDPWD|SHLVL|BASH[A-Z_]*|PATH|TERM|UID|EUID|PPID|XDG_[A-Z_]*|RTK_[A-Z_]*|LANG|PAGER|GIT_PAGER|GIT_TERMINAL_PROMPT|PYTHONDONTWRITEBYTECODE|HOSTNAME)="


class SandboxExecError(chat_tools.ToolError):
    """The command could not be run in the sandbox; the message says why."""


def state_dir(session_id: str) -> str:
    return f"{STATE}/state/{session_id}"


def build_script(command: str, root: str, session_id: str, *, workdir: str | None = None, run_id: str | None = None) -> str:
    """The shell script of one run: start where the last run ended (or in ``workdir``), with the variables it exported, run ``command``, and save the folder and the variables for the next run."""
    st = state_dir(session_id)
    lines = [f"mkdir -p {st}", f"cd {shlex.quote(root)}"]
    if run_id:
        lines.insert(0, f"echo $(awk '{{print $5}}' /proc/$$/stat) > {STATE}/run/{run_id}.pgid")      # the process group of this run, so that it can be ended as a whole
    lines += [f'[ -f {st}/cwd ] && cd "$(cat {st}/cwd)" 2>/dev/null', f"[ -f {st}/env ] && . {st}/env 2>/dev/null"]
    if workdir:
        lines.append(f"cd {shlex.quote(workdir)} || exit 2")
    lines += [command, "__rc=$?", f"pwd -P > {st}/cwd 2>/dev/null",
              f"export -p | grep -E '^declare -x [A-Za-z_][A-Za-z0-9_]*=' | grep -v -E '{_NOT_SAVED}' > {st}/env 2>/dev/null", "exit $__rc"]
    return "\n".join(lines) + "\n"


class Pieces:
    """Output as it arrives, cleaned a line at a time: terminal codes removed, credentials masked. A line that is not finished stays until it is (or until it is ``PART`` characters long, when it
    is let go as it is), so that a secret cannot be split across two pieces and escape the masking."""

    def __init__(self, emit: Callable[[str, str], None] | None = None):
        self.emit, self.whole, self.held = emit, [], {"stdout": "", "stderr": ""}
        self.size = 0

    def add(self, stream: str, data: bytes) -> bool:
        """Take a piece; False when the run has printed more than it may."""
        text = (self.held[stream] + data.decode("utf-8", errors="replace")).replace("\r\n", "\n")
        if "\n" in text:
            body, _, rest = text.rpartition("\n")
            self._give(stream, body + "\n")
            text = rest
        if len(text) >= PART:
            self._give(stream, text)
            text = ""
        self.held[stream] = text
        return self.size < FLOOD_CHARS

    def _give(self, stream: str, text: str) -> None:
        clean = code_rules.mask(clip.shorten_runs(termtext.strip_escapes(text)), config=True)
        self.size += len(clean)
        self.whole.append(clean)
        if self.emit and clean:
            self.emit(stream, clean)

    def finish(self) -> str:
        for stream in ("stdout", "stderr"):
            if self.held[stream]:
                self._give(stream, self.held[stream])
                self.held[stream] = ""
        return "".join(self.whole)


class SandboxExecutor:
    """One sandbox terminal for one session. ``roots`` are the copies of the project's folders in the container (the first is where a session starts)."""

    def __init__(self, container_id: str, roots: list[str], *, project_id: str, session_id: str):
        if not _ID.fullmatch(session_id or ""):
            raise SandboxExecError("That is not a session.")
        self.cid, self.roots, self.project_id, self.session_id = container_id, list(roots), project_id, session_id
        self.ran: list[str] = []
        self._ready()

    # -- the container -----------------------------------------------------------------------------------------------------------------------------------------

    def _ready(self) -> None:
        if self.cid in _READY:
            return
        res = sandbox.exec_run(self.cid, f"mkdir -p {STATE}/state {STATE}/jobs {STATE}/run {STATE}/data {STATE}/cache {HOME}", timeout=30)
        if res["exit_code"] != 0:
            raise SandboxExecError("The sandbox's own folders could not be made: " + res["output"].strip()[:200])
        _READY.add(self.cid)

    def has_rtk(self) -> bool:
        return self._probe("rtk")

    def _probe(self, program: str) -> bool:
        key = (self.cid, program)
        if key not in _PROGRAMS:
            res = sandbox.exec_run(self.cid, f"command -v {shlex.quote(program)}", timeout=15)
            _PROGRAMS[key] = res["exit_code"] == 0
        return _PROGRAMS[key]

    def _exec(self, argv: list[str], timeout: int = 30) -> tuple[int | None, str]:
        out, code = [], None
        for piece in sandbox.exec_stream(self.cid, argv, workdir=self.roots[0], timeout=timeout, environment=ENVIRONMENT, user=f"{UID}:{UID}"):
            if "data" in piece:
                out.append(piece["data"].decode("utf-8", errors="replace"))
            else:
                code = piece["exit_code"]
        return code, "".join(out)

    # -- rtk -------------------------------------------------------------------------------------------------------------------------------------------------------

    def rewrite(self, command: str) -> str | None:
        """What rtk makes of ``command`` inside the container (exit 0 or 3: the rewrite; 1: nothing), or None."""
        if not self._probe("rtk"):
            return None
        code, out = self._exec(["rtk", "rewrite", command], timeout=15)
        text = out.strip()
        return text if code in (0, 3) and text and text != command.strip() else None

    def recall_rtk(self, handle: str, *, start: int | None = None, lines: int | None = None, grep: str | None = None) -> str:
        if not rtk.HANDLE.fullmatch(handle or ""):
            raise SandboxExecError("A recall handle is the short hex code printed after 'rtk recall' in a hint.")
        argv = ["rtk", "recall", handle, "--full"] + (["--from", str(int(start))] if start and int(start) > 1 else []) + (["--lines", str(int(lines))] if lines else []) + (["--grep", grep] if grep else [])
        code, out = self._exec(argv, timeout=30)
        if code != 0:
            raise SandboxExecError(out.strip()[:300] or "rtk could not recall that.")
        return code_rules.mask(termtext.strip_escapes(out), config=True)

    # -- running a command -----------------------------------------------------------------------------------------------------------------------------------

    def _where(self, workdir: str | None) -> str | None:
        if not workdir:
            return None
        path = posixpath.normpath(str(workdir).replace("\\", "/"))
        if path.startswith("/"):
            if not any(path == r or path.startswith(r + "/") for r in self.roots):
                raise SandboxExecError("That folder is not inside the project.")
            return path
        if path == ".." or path.startswith("../"):
            raise SandboxExecError("A folder cannot go up out of the project.")
        return posixpath.join(self.roots[0], path) if len(self.roots) == 1 else posixpath.join(self.roots[0].rsplit("/", 1)[0], path)

    def run(self, command: str, *, workdir: str | None = None, timeout: int = DEFAULT_TIMEOUT, raw: bool = False, on_output: Callable[[str, str], None] | None = None,
            cancel: threading.Event | None = None) -> dict:
        command = str(command or "")
        if not command.strip():
            raise SandboxExecError("Give a command to run.")
        if len(command) > MAX_COMMAND or "\x00" in command:
            raise SandboxExecError(f"A command is at most {MAX_COMMAND:,} characters.")
        seconds = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
        started = time.monotonic()
        rewritten = None if raw else self.rewrite(command)
        ran = rewritten or command
        run_id = uuid.uuid4().hex[:12]
        script = build_script(ran, self.roots[0], self.session_id, workdir=self._where(workdir), run_id=run_id)
        pieces, code, timed_out, flooded = Pieces(on_output), None, False, False
        watcher = self._watch(run_id, cancel)
        try:
            for piece in sandbox.exec_stream(self.cid, ["setsid", "-w", "timeout", "-k", "5", str(seconds), "bash", "-c", script], workdir=self.roots[0], environment=ENVIRONMENT, user=f"{UID}:{UID}"):
                if "data" in piece:
                    if not pieces.add(piece["stream"], piece["data"]):
                        flooded = True
                        self._end(run_id)
                        break
                else:
                    code, timed_out = piece["exit_code"], piece["exit_code"] == 124
        finally:
            watcher.set()
        text = pieces.finish()
        cancelled = bool(cancel and cancel.is_set())
        if flooded:
            text += f"\n(the command printed more than {FLOOD_CHARS:,} characters and was stopped)\n"
        if timed_out:
            text += f"\n(the command ran for {seconds} seconds, the most it may, and was stopped)\n"
        if cancelled:
            text += "\n(stopped by the owner)\n"
        from app.backend import sandbox_history
        sandbox_history.record_command(sandbox._ours(self.cid).name, command, code if code is not None else -1, time.monotonic() - started, text, timed_out)
        return self._result(command, ran if rewritten else None, text, code, time.monotonic() - started, timed_out=timed_out, cancelled=cancelled)

    def _watch(self, run_id: str, cancel: threading.Event | None) -> threading.Event:
        """Ends the run's process group when the owner stops the session; ``done`` is set when the run is over."""
        done = threading.Event()
        if cancel is None:
            return done

        def loop():
            while True:
                if cancel.is_set():
                    self._end(run_id)
                    return
                if done.wait(0.2):
                    return
        threading.Thread(target=loop, daemon=True, name="agent-exec-watch").start()
        return done

    def _end(self, run_id: str) -> None:
        pidfile = f"{STATE}/run/{run_id}.pgid"
        try:
            sandbox.exec_run(self.cid, f'g=$(cat {pidfile} 2>/dev/null); [ -n "$g" ] && {{ kill -TERM -- -$g 2>/dev/null; sleep 1; kill -KILL -- -$g 2>/dev/null; }}; true', timeout=15)
        except Exception:  # ignored: the container may be gone already; the run ends with it
            pass

    # -- the result -----------------------------------------------------------------------------------------------------------------------------------------

    def _result(self, command: str, ran_as: str | None, text: str, code: int | None, seconds: float, *, timed_out: bool = False, cancelled: bool = False) -> dict:
        total = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        hint = rtk.hint_of(text)
        recovery, cut = None, False
        shown = text
        if len(text) > OUTPUT_CHARS:
            cut = True
            try:
                recovery = outputs.save(self.project_id, self.session_id, text)
            except (outputs.OutputError, OSError):
                recovery = None
            head, tail = text[: int(OUTPUT_CHARS * 0.7)], text[-int(OUTPUT_CHARS * 0.3):]
            head = head[: head.rfind("\n")] if "\n" in head else head
            tail = tail[tail.find("\n") + 1:] if "\n" in tail else tail
            left = max(total - head.count("\n") - tail.count("\n") - 2, 0)
            shown = f"{head}\n... {left} lines left out" + (f"; recall {recovery} returns them" if recovery else "") + f" ...\n{tail}"
        one = " ".join(command.split())
        out = {"command": command, "ran_as": ([ran_as] if ran_as else []), "exit_code": code, "output": shown, "lines": total, "duration_ms": int(seconds * 1000),
               "summary": f"Ran {one[:90]}{'...' if len(one) > 90 else ''}" + (f" (exit {code})" if code else "") + (" (stopped: time)" if timed_out else "") + (" (stopped)" if cancelled else "")}
        if timed_out:
            out["timed_out"] = True
        if cut:
            out["truncated"] = True
        if recovery:
            out["recovery"] = recovery
        if hint:
            out["condensed"] = {"by": "rtk", "hidden_lines": hint[0], "recall": hint[1]}
            out.setdefault("recovery", hint[1])
        elif ran_as and re.search(r"\[\+\d+ lines omitted\]|\(more changes truncated\)", text):
            out["condensed"] = {"by": "rtk", "hidden_lines": None, "recall": None}
            out["note"] = "rtk left part of this output out. Run the command again with raw set to true to get all of it."
        return out

    # -- jobs ----------------------------------------------------------------------------------------------------------------------------------------------------

    def _job_dir(self, job_id: str) -> str:
        if not _ID.fullmatch(job_id or ""):
            raise SandboxExecError("That is not a job. A job id is what bash returned when it started the command in the background.")
        return f"{STATE}/jobs/{self.session_id}-{job_id}"

    def start_job(self, command: str, *, workdir: str | None = None, raw: bool = False) -> dict:
        """Start ``command`` detached and return at once with the job's id. A trailing ``&`` (and the ``echo $!`` that usually follows it) is left out: the job is already in the background, and
        a command that puts itself in the background again makes the shell end at once, so the job looks finished while its program runs on, out of reach of ``bash_kill``."""
        command = str(command or "")
        trimmed = _TRAILING_AMPERSAND.sub("", command).rstrip()
        if not command.strip():
            raise SandboxExecError("Give a command to run.")
        if trimmed and trimmed != command.rstrip():
            command, dropped = trimmed, True
        else:
            dropped = False
        if len(command) > MAX_COMMAND or "\x00" in command:
            raise SandboxExecError(f"A command is at most {MAX_COMMAND:,} characters.")
        running = [j for j in self.jobs() if j["state"] == "running"]
        if len(running) >= MAX_JOBS:
            raise SandboxExecError(f"{len(running)} jobs are already running; end one with bash_kill first.")
        rewritten = None if raw else self.rewrite(command)
        job_id = uuid.uuid4().hex[:8]
        jd = self._job_dir(job_id)
        script = build_script(rewritten or command, self.roots[0], self.session_id, workdir=self._where(workdir)).replace("exit $__rc", f"echo $__rc > {jd}/rc\nexit $__rc")
        folder, name = posixpath.split(jd + "/run.sh")
        code, out = self._exec(["mkdir", "-p", jd], timeout=15)
        if code != 0:
            raise SandboxExecError("The job's folder could not be made: " + out.strip()[:200])
        try:
            sandbox_files.upload(self.cid, folder, name, script.encode("utf-8"), overwrite=True, uid=UID)
        except sandbox_files.FileError as e:
            raise SandboxExecError(str(e)) from None
        code, out = self._exec(["bash", "-c", f"setsid nohup bash {jd}/run.sh > {jd}/log 2>&1 < /dev/null & echo $! > {jd}/pid; echo $! "], timeout=15)
        pid = out.strip().split()[-1] if out.strip() else ""
        started = {"job": job_id, "command": command, "pid": pid, "summary": f"Started {command[:80]!r} as job {job_id}. It keeps running while you run other commands (to use it, to test it). Read its output with bash_output, end it with bash_kill when you are done."}
        if dropped:
            started["note"] = "background already runs the command in the background, so the trailing & was left out."
        (self.ran.append(f"job {job_id}: {rewritten or command}"))
        return started

    def jobs(self) -> list[dict]:
        code, out = self._exec(["bash", "-c", f"for d in {STATE}/jobs/{self.session_id}-*; do [ -d \"$d\" ] && echo \"$(basename $d)\"; done"], timeout=15)
        rows = []
        for name in out.split():
            job_id = name.removeprefix(f"{self.session_id}-")
            try:
                rows.append({"job": job_id, "state": self._state(job_id)[0]})
            except SandboxExecError:
                continue
        return rows

    def _state(self, job_id: str) -> tuple[str, int | None, int]:
        jd = self._job_dir(job_id)
        # running while anything of the job's process group is alive, even when its own shell has finished (a program that put itself in the background lives on)
        code, out = self._exec(["bash", "-c", f"p=$(cat {jd}/pid 2>/dev/null); if [ -n \"$p\" ] && kill -0 -- -$p 2>/dev/null && ! grep -qx killed {jd}/rc 2>/dev/null; then echo running; "
                                              f"elif [ -f {jd}/rc ]; then echo \"exited $(cat {jd}/rc)\"; elif kill -0 $p 2>/dev/null; then echo running; else echo ended; fi; "
                                              f"wc -l < {jd}/log 2>/dev/null || echo 0"], timeout=15)
        rows = out.strip().splitlines() or ["ended", "0"]
        words = rows[0].split()
        lines = int(rows[-1].strip() or 0) if rows[-1].strip().isdigit() else 0
        if words[0] == "exited" and len(words) > 1 and words[1] == "killed":
            return "killed", None, lines
        if words[0] == "exited":
            return "exited", int(words[1]) if len(words) > 1 and words[1].lstrip("-").isdigit() else None, lines
        return words[0], None, lines

    def job_output(self, job_id: str, start: int = 1, lines: int = 400, grep: str | None = None) -> dict:
        """The log of a job from a line, and whether it is still running."""
        state, code, total = self._state(job_id)
        if state == "ended" and total == 0:
            raise SandboxExecError("There is no such job (or it left no log).")
        jd = self._job_dir(job_id)
        start, lines = max(1, int(start or 1)), max(1, min(int(lines or 400), outputs.MAX_LINES))
        code_, out = self._exec(["bash", "-c", f"sed -n '{start},{start + lines - 1}p' {jd}/log"], timeout=30)
        text = code_rules.mask(termtext.strip_escapes(out), config=True)
        if grep:
            try:
                rx = re.compile(grep)
            except re.error as e:
                raise SandboxExecError(f"The pattern is not a valid regular expression: {e}.") from None
            text = "\n".join(line for line in text.split("\n") if rx.search(line))
        shown = len(text.splitlines())
        more = max(0, total - (start + lines - 1))
        what = "running" if state == "running" else f"finished with exit {code}" if state == "exited" else "ended (no exit status was kept)"
        return {"job": job_id, "state": state, "exit_code": code, "text": text, "from": start, "total_lines": total, "more": more,
                "summary": f"Job {job_id} is {what}; lines {start} to {start + max(shown, 1) - 1} of {total}" + (f", {more} more" if more else "") + (". It keeps running: run other commands meanwhile (reading its output again shows nothing new), and end it with bash_kill when you are done." if state == "running" else "")}

    def job_kill(self, job_id: str) -> dict:
        jd = self._job_dir(job_id)
        state, code, total = self._state(job_id)
        if state != "running":
            return {"job": job_id, "state": state, "summary": f"Job {job_id} was not running ({state})."}
        self._exec(["bash", "-c", f"p=$(cat {jd}/pid); kill -TERM -- -$p 2>/dev/null; sleep 2; kill -KILL -- -$p 2>/dev/null; echo killed > {jd}/rc; true"], timeout=20)
        return {"job": job_id, "state": "killed", "summary": f"Ended job {job_id} and everything it started."}


_PROGRAMS: dict[tuple[str, str], bool] = {}
_TRAILING_AMPERSAND = re.compile(r"(?<![&\\])&[ \t]*(?:echo[ \t]+\$!)?[ \t;]*$")


def reset_caches() -> None:
    """Forget what was learned of containers (their folders made, the programs in them). For a test that makes containers with the same id again; a real container id is never reused."""
    _READY.clear()
    _PROGRAMS.clear()
