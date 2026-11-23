"""The read-only commands of the Plan terminal that run in Python, over the project's own files.

``cat head tail wc sort uniq ls find tree diff echo pwd`` are written here instead of being started as programs. A Windows computer does not have them, a program started from the project's
folder would see everything (a link that leads out of it, a file of credentials), and running them here means every path goes through ``Workspace.resolve`` (inside the project, no link
followed, nothing kept from the assistant) and every flag that is not listed is refused, whatever the program would have done with it. What each one does is the usual meaning of the flags
that are listed; anything else is an error that says what is supported.
"""
from __future__ import annotations

import difflib
import fnmatch
import os
import posixpath
import re
from dataclasses import dataclass

from app.backend.pipeline import chat_tools
from app.backend.pipeline.credentials import is_kept

MAX_OUT = 400_000
MAX_FILE = 2_000_000
LINES_DEFAULT = 10


class Usage(chat_tools.ToolError):
    """A command was used in a way this terminal does not support; the message says what is."""


_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def is_device(path: str) -> bool:
    """Whether a path names a Windows device (``CON``, ``NUL``, ``aux.txt``...): opened from any folder it is the device, not a file, and reading ``CON`` waits for a keyboard."""
    return os.name == "nt" and any(part.split(".")[0].rstrip(" ").upper() in _DEVICES for part in str(path).replace("\\", "/").split("/") if part)


@dataclass
class Out:
    text: str = ""
    err: str = ""
    code: int = 0
    notes: list[str] | None = None


def _flags(cmd: str, args: list[str], spec: dict[str, tuple[str, bool]], *, numeric: str | None = None) -> tuple[dict, list[str]]:
    """The flags of ``args`` as ``{name: value}`` and the words that are not flags. ``spec`` maps a flag (``-n``, ``--lines``) to ``(name, takes a value)``. Short flags may be joined
    (``-la``), a value may be joined (``-n5``, ``--lines=5``), ``--`` ends the flags, and with ``numeric`` set ``-5`` means that flag with the value 5."""
    got: dict = {}
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            rest += args[i + 1:]
            break
        if a.startswith("--") and len(a) > 2:
            key, eq, val = a.partition("=")
            if key not in spec:
                raise Usage(f"{cmd}: {key} is not supported here. Supported: {', '.join(sorted(spec))}.")
            name, takes = spec[key]
            if takes and not eq:
                i += 1
                if i >= len(args):
                    raise Usage(f"{cmd}: {key} needs a value.")
                val = args[i]
            got[name] = val if takes else True
        elif a.startswith("-") and len(a) > 1:
            if numeric and re.fullmatch(r"-\d+", a):
                got[numeric] = a[1:]
            else:
                j = 1
                while j < len(a):
                    flag = "-" + a[j]
                    if flag not in spec:
                        raise Usage(f"{cmd}: {flag} is not supported here. Supported: {', '.join(sorted(spec))}.")
                    name, takes = spec[flag]
                    if takes:
                        val = a[j + 1:]
                        if not val:
                            i += 1
                            if i >= len(args):
                                raise Usage(f"{cmd}: {flag} needs a value.")
                            val = args[i]
                        got[name] = val
                        break
                    got[name] = True
                    j += 1
        else:
            rest.append(a)
        i += 1
    return got, rest


def _int(cmd: str, flag: str, value) -> int:
    try:
        n = int(str(value))
    except ValueError:
        raise Usage(f"{cmd}: {flag} needs a whole number, not {value!r}.") from None
    if n < 0:
        raise Usage(f"{cmd}: {flag} cannot be negative.")
    return n


class Terminal:
    """The files a command may touch (``ws``) and what flows between the commands of a pipeline (``stdin``)."""

    def __init__(self, ws: chat_tools.Workspace):
        self.ws = ws
        self.cwd = ""                                  # the folder ``cd`` moved to, below the project's top; paths are relative to it
        self.skipped: dict[str, int] = {}

    # -- paths and files ----------------------------------------------------------------------------------------------------------------------------------

    def join(self, arg: str) -> str:
        """A path as the project knows it (from its top) for one the model wrote relative to the folder it is in. A ``..`` that climbs out stays a ``..`` and is refused by ``resolve``."""
        text = str(arg or "").replace("\\", "/")
        joined = posixpath.normpath(posixpath.join(self.cwd or ".", text)) if (text or self.cwd) else ""
        return "" if joined == "." else joined

    def path(self, arg: str) -> tuple[str, str, str]:
        if is_device(arg):
            raise Usage(f"{arg}: that is a name of the system, not a file.")
        try:
            return self.ws.resolve(self.join(arg))
        except chat_tools.ToolError as e:
            raise Usage(f"{arg}: {e}") from None

    def read(self, arg: str) -> str:
        label, full, rel = self.path(arg)
        if not full:
            raise Usage(f"{arg}: that is the top of a project with several folders; name a file inside one.")
        if os.path.isdir(full):
            raise Usage(f"{arg}: is a folder.")
        try:
            size = os.path.getsize(full)
        except OSError:
            raise Usage(f"{arg}: no such file.") from None
        if size > MAX_FILE:
            raise Usage(f"{arg}: {size:,} bytes is too large for this terminal; read a range of it with read_file, or search it with rg.")
        with open(full, "rb") as f:
            data = f.read()
        if b"\x00" in data[:8192]:
            raise Usage(f"{arg}: a binary file.")
        return data.decode("utf-8", errors="replace")

    def inputs(self, cmd: str, files: list[str], stdin: str | None) -> list[tuple[str, str]]:
        """``[(name, text)]`` of the named files, or of the pipeline's input when none is named."""
        if files:
            return [(f, self.read(f)) for f in files]
        if stdin is None:
            raise Usage(f"{cmd}: give a file to read, or pipe something into it.")
        return [("-", stdin)]

    # -- the commands ---------------------------------------------------------------------------------------------------------------------------------------

    def cat(self, args, stdin):
        f, files = _flags("cat", args, {"-n": ("n", False), "--number": ("n", False), "-A": ("ignored", False)})
        out = []
        for _, text in self.inputs("cat", files, stdin):
            lines = text.split("\n")
            if f.get("n"):
                if lines and lines[-1] == "":
                    lines.pop()
                out.append("\n".join(f"{i:6d}\t{line}" for i, line in enumerate(lines, 1)) + "\n")
            else:
                out.append(text)
        return Out("".join(out))

    def _head_tail(self, cmd, args, stdin, tail):
        f, files = _flags(cmd, args, {"-n": ("n", True), "--lines": ("n", True), "-c": ("c", True), "--bytes": ("c", True), "-q": ("q", False), "--quiet": ("q", False)}, numeric="n")
        many = len(files) > 1 and not f.get("q")
        out = []
        for name, text in self.inputs(cmd, files, stdin):
            if "c" in f:
                n = _int(cmd, "-c", f["c"])
                piece = text.encode("utf-8")[-n:] if tail and n else b"" if tail else text.encode("utf-8")[:n]
                body = piece.decode("utf-8", errors="replace")
            else:
                raw = str(f.get("n", LINES_DEFAULT))
                lines = text.splitlines(keepends=True)
                if not tail and raw.startswith("-"):                                  # head -n -3: all but the last 3
                    body = "".join(lines[:max(0, len(lines) - _int(cmd, "-n", raw[1:]))])
                elif tail and raw.startswith("+"):                                    # tail -n +3: from the 3rd line on
                    body = "".join(lines[max(0, _int(cmd, "-n", raw[1:]) - 1):])
                else:
                    n = _int(cmd, "-n", raw)
                    body = "".join(lines[-n:] if tail and n else [] if tail else lines[:n])
            out.append((f"==> {name} <==\n" if many else "") + body + ("" if body.endswith("\n") or not body else "\n"))
        return Out("\n".join(out) if many else "".join(out))

    def head(self, args, stdin):
        return self._head_tail("head", args, stdin, False)

    def tail(self, args, stdin):
        return self._head_tail("tail", args, stdin, True)

    def wc(self, args, stdin):
        f, files = _flags("wc", args, {"-l": ("l", False), "-w": ("w", False), "-c": ("c", False), "-m": ("m", False), "--lines": ("l", False), "--words": ("w", False), "--bytes": ("c", False),
                                       "--chars": ("m", False)})
        want = [k for k in "lwcm" if f.get(k)] or ["l", "w", "c"]
        rows, totals = [], [0] * len(want)
        for name, text in self.inputs("wc", files, stdin):
            counts = {"l": text.count("\n"), "w": len(text.split()), "c": len(text.encode("utf-8")), "m": len(text)}
            nums = [counts[k] for k in want]
            totals = [a + b for a, b in zip(totals, nums)]
            rows.append(" ".join(f"{n:7d}" for n in nums) + ("" if name == "-" else f" {name}"))
        if len(rows) > 1:
            rows.append(" ".join(f"{n:7d}" for n in totals) + " total")
        return Out("\n".join(rows) + "\n")

    def sort(self, args, stdin):
        f, files = _flags("sort", args, {"-r": ("r", False), "--reverse": ("r", False), "-n": ("n", False), "--numeric-sort": ("n", False), "-u": ("u", False), "--unique": ("u", False),
                                         "-f": ("f", False), "--ignore-case": ("f", False)})
        lines = [line for _, text in self.inputs("sort", files, stdin) for line in text.splitlines()]

        def key(line):
            if f.get("n"):
                m = re.match(r"\s*(-?\d+(?:\.\d+)?)", line)
                return (float(m.group(1)) if m else 0.0, line)
            return line.lower() if f.get("f") else line
        lines.sort(key=key, reverse=bool(f.get("r")))
        if f.get("u"):
            seen, unique = set(), []
            for line in lines:
                k = line.lower() if f.get("f") else line
                if k not in seen:
                    seen.add(k)
                    unique.append(line)
            lines = unique
        return Out("".join(line + "\n" for line in lines))

    def uniq(self, args, stdin):
        f, files = _flags("uniq", args, {"-c": ("c", False), "--count": ("c", False), "-d": ("d", False), "-u": ("u", False), "-i": ("i", False), "--ignore-case": ("i", False)})
        lines = [line for _, text in self.inputs("uniq", files, stdin) for line in text.splitlines()]
        groups: list[list] = []
        for line in lines:
            same = groups and (groups[-1][0].lower() == line.lower() if f.get("i") else groups[-1][0] == line)
            if same:
                groups[-1][1] += 1
            else:
                groups.append([line, 1])
        rows = [(line, n) for line, n in groups if not (f.get("d") and n < 2) and not (f.get("u") and n > 1)]
        return Out("".join((f"{n:7d} {line}" if f.get("c") else line) + "\n" for line, n in rows))

    def echo(self, args, stdin):
        newline = True
        if args and args[0] == "-n":
            newline, args = False, args[1:]
        return Out(" ".join(args) + ("\n" if newline else ""))

    def pwd(self, args, stdin):
        return Out(".\n")

    def ls(self, args, stdin):
        f, paths = _flags("ls", args, {"-l": ("l", False), "-a": ("a", False), "-A": ("A", False), "-R": ("R", False), "-1": ("one", False), "-h": ("h", False), "-d": ("d", False),
                                       "-S": ("S", False), "-t": ("t", False), "-r": ("r", False), "-F": ("F", False), "--all": ("a", False), "--almost-all": ("A", False),
                                       "--recursive": ("R", False), "--human-readable": ("h", False)})
        out, many = [], len(paths) > 1 or bool(f.get("R"))
        for p in paths or [""]:
            label, full, rel = self.path(p)
            if not full:                                                          # the top of a project of several folders: its folders
                out.append("".join(f"{name}/\n" for name, _ in self.ws.folders))
                continue
            if os.path.isfile(full) or f.get("d"):
                out.append(self._row(full, os.path.basename(full) if os.path.isfile(full) else (rel or "."), f))
                continue
            self._list(full, self.ws.shown(label, rel) or ".", f, out, many)
        return Out("".join(out))

    def _row(self, full: str, name: str, f: dict) -> str:
        try:
            st = os.stat(full)
        except OSError:
            return ""
        suffix = "/" if os.path.isdir(full) and (f.get("F") or f.get("l")) else ""
        if f.get("l"):
            size = _human(st.st_size) if f.get("h") else str(st.st_size)
            return f"{'d' if os.path.isdir(full) else '-'} {size:>9} {name}{suffix}\n"
        return f"{name}{'/' if os.path.isdir(full) and f.get('F') else ''}\n"

    def _list(self, folder: str, shown: str, f: dict, out: list, header: bool, depth: int = 0) -> None:
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            raise Usage(f"{shown}: cannot be read.") from None
        show_dot = bool(f.get("a") or f.get("A"))
        keep = []
        for e in entries:
            if is_kept(e.name):
                self.skipped["kept"] = self.skipped.get("kept", 0) + 1
            elif e.name.startswith(".") and not show_dot:
                continue
            elif chat_tools._is_link(e):
                self.skipped["links"] = self.skipped.get("links", 0) + 1
            else:
                keep.append(e)
        if f.get("S"):
            keep.sort(key=lambda e: -(e.stat().st_size if e.is_file() else 0))
        elif f.get("t"):
            keep.sort(key=lambda e: -e.stat().st_mtime)
        else:
            keep.sort(key=lambda e: e.name.lower())
        if f.get("r"):
            keep.reverse()
        if header:
            out.append(f"{shown}:\n")
        for e in keep:
            out.append(self._row(e.path, e.name, f) if (f.get("l") or f.get("F") or f.get("h")) else f"{e.name}{'/' if e.is_dir(follow_symlinks=False) and f.get('F') else ''}\n")
        if header:
            out.append("\n")
        if f.get("R") and depth < 8:
            for e in keep:
                if e.is_dir(follow_symlinks=False) and not chat_tools._skip_dir(e.name):
                    self._list(e.path, f"{shown}/{e.name}" if shown != "." else e.name, f, out, True, depth + 1)

    def find(self, args, stdin):
        starts: list[str] = []
        i = 0
        while i < len(args) and not args[i].startswith("-") and args[i] not in ("(", ")", "!"):
            starts.append(args[i])
            i += 1
        tests: list[tuple[str, str]] = []
        maxdepth, mindepth = 64, 0
        simple = {"-name", "-iname", "-path", "-ipath", "-type", "-maxdepth", "-mindepth", "-size"}
        while i < len(args):
            a = args[i]
            if a == "-print":
                i += 1
            elif a == "-a" or a == "-and":
                i += 1
            elif a in simple:
                if i + 1 >= len(args):
                    raise Usage(f"find: {a} needs a value.")
                v = args[i + 1]
                if a == "-maxdepth":
                    maxdepth = _int("find", a, v)
                elif a == "-mindepth":
                    mindepth = _int("find", a, v)
                else:
                    tests.append((a, v))
                i += 2
            else:
                raise Usage(f"find: {a} is not supported here. Supported: a folder or file to start from, -name, -iname, -path, -ipath, -type f|d, -size, -maxdepth, -mindepth, -print.")
        for kind in [v for a, v in tests if a == "-type"]:
            if kind not in ("f", "d"):
                raise Usage("find: -type is f (files) or d (folders).")
        out: list[str] = []
        for start in starts or [""]:
            label, full, rel = self.path(start)
            if not full:
                for name, root in self.ws.folders:
                    self._find_in(root, name, tests, maxdepth, mindepth, out)
                continue
            self._find_in(full, self.ws.shown(label, rel) or ".", tests, maxdepth, mindepth, out, single=os.path.isfile(full))
            if len(out) >= 20_000:
                break
        return Out("".join(f"{line}\n" for line in out[:20_000]) + ("(more than 20,000 results; narrow it)\n" if len(out) > 20_000 else ""))

    def _find_in(self, full: str, shown: str, tests: list, maxdepth: int, mindepth: int, out: list, single: bool = False) -> None:
        def passes(path: str, rel: str, is_dir: bool) -> bool:
            for a, v in tests:
                name = os.path.basename(path)
                if a == "-name" and not fnmatch.fnmatchcase(name, v):
                    return False
                if a == "-iname" and not fnmatch.fnmatchcase(name.lower(), v.lower()):
                    return False
                if a == "-path" and not fnmatch.fnmatchcase(rel, v):
                    return False
                if a == "-ipath" and not fnmatch.fnmatchcase(rel.lower(), v.lower()):
                    return False
                if a == "-type" and (v == "d") != is_dir:
                    return False
                if a == "-size" and (is_dir or not _size_ok(os.path.getsize(path), v)):
                    return False
            return True
        if single:
            if passes(full, shown, False) and mindepth <= 0:
                out.append(shown)
            return
        stack = [(full, shown, 0)]
        if mindepth <= 0 and passes(full, shown, True):
            out.append(shown)
        while stack and len(out) < 20_001:
            folder, name, depth = stack.pop()
            if depth >= maxdepth:
                continue
            try:
                with os.scandir(folder) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            sub = []
            for e in entries:
                try:
                    is_dir, link = e.is_dir(follow_symlinks=False), chat_tools._is_link(e)
                except OSError:
                    continue
                if link:
                    self.skipped["links"] = self.skipped.get("links", 0) + 1
                    continue
                if is_dir and chat_tools._skip_dir(e.name):
                    self.skipped["folders"] = self.skipped.get("folders", 0) + 1
                    continue
                if is_kept(e.name):
                    self.skipped["kept"] = self.skipped.get("kept", 0) + 1
                    continue
                shown_path = f"{name}/{e.name}" if name != "." else f"./{e.name}"
                if depth + 1 >= mindepth and passes(e.path, shown_path, is_dir):
                    out.append(shown_path)
                if is_dir:
                    sub.append((e.path, shown_path, depth + 1))
            stack.extend(reversed(sub))

    def tree(self, args, stdin):
        f, paths = _flags("tree", args, {"-L": ("L", True), "-a": ("a", False), "-d": ("d", False), "-I": ("I", True)})
        levels = _int("tree", "-L", f["L"]) if "L" in f else 3
        ignore = str(f.get("I", "")).split("|") if f.get("I") else []
        out: list[str] = []
        counts = [0, 0]
        for p in paths or [""]:
            label, full, rel = self.path(p)
            if not full:
                for name, root in self.ws.folders:
                    out.append(name)
                    self._tree(root, "", 1, levels, f, ignore, out, counts)
                continue
            out.append(self.ws.shown(label, rel) or ".")
            self._tree(full, "", 1, levels, f, ignore, out, counts)
        out.append("")
        out.append(f"{counts[0]} directories" + ("" if f.get("d") else f", {counts[1]} files"))
        return Out("\n".join(out) + "\n")

    def _tree(self, folder, prefix, depth, levels, f, ignore, out, counts):
        if depth > levels or len(out) > 5000:
            return
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except OSError:
            return
        keep = []
        for e in entries:
            if is_kept(e.name) or chat_tools._is_link(e) or (e.name.startswith(".") and not f.get("a")) or any(fnmatch.fnmatch(e.name, g) for g in ignore):
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            if is_dir and chat_tools._skip_dir(e.name) and not f.get("a"):
                continue
            if f.get("d") and not is_dir:
                continue
            keep.append((e, is_dir))
        for i, (e, is_dir) in enumerate(keep):
            last = i == len(keep) - 1
            out.append(f"{prefix}{'`-- ' if last else '|-- '}{e.name}{'/' if is_dir else ''}")
            counts[0 if is_dir else 1] += 1
            if is_dir:
                self._tree(e.path, prefix + ("    " if last else "|   "), depth + 1, levels, f, ignore, out, counts)

    def diff(self, args, stdin):
        f, files = _flags("diff", args, {"-u": ("u", False), "--unified": ("u", False), "-U": ("U", True), "-q": ("q", False), "--brief": ("q", False)})
        if len(files) != 2:
            raise Usage("diff: give exactly two files.")
        a, b = (self.read(x) for x in files)
        if a == b:
            return Out("")
        if f.get("q"):
            return Out(f"Files {files[0]} and {files[1]} differ\n", code=1)
        context = _int("diff", "-U", f["U"]) if "U" in f else 3
        lines = difflib.unified_diff(a.splitlines(keepends=True), b.splitlines(keepends=True), fromfile=files[0], tofile=files[1], n=context)
        return Out("".join(line if line.endswith("\n") else line + "\n" for line in lines), code=1)


def _human(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def _size_ok(size: int, spec: str) -> bool:
    m = re.fullmatch(r"([+-]?)(\d+)([ckMG]?)", spec)
    if not m:
        raise Usage("find: -size is like +10k, -1M or 200c.")
    sign, n, unit = m.groups()
    limit = int(n) * {"": 512, "c": 1, "k": 1024, "M": 1 << 20, "G": 1 << 30}[unit]
    return size > limit if sign == "+" else size < limit if sign == "-" else size == limit


BUILTINS = {"cat": Terminal.cat, "head": Terminal.head, "tail": Terminal.tail, "wc": Terminal.wc, "sort": Terminal.sort, "uniq": Terminal.uniq, "ls": Terminal.ls, "find": Terminal.find,
            "tree": Terminal.tree, "diff": Terminal.diff, "echo": Terminal.echo, "pwd": Terminal.pwd}
