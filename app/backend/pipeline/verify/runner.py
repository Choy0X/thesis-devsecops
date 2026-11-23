"""Inspect filenames and modes in proposed project patches."""
from __future__ import annotations

import pathlib
import re
import sys

_C_ESCAPES = {"n": 10, "t": 9, "r": 13, '"': 34, "\\": 92, "a": 7, "b": 8, "f": 12, "v": 11}


def unquote_path(name: str) -> str:
    """A path as git writes it in a patch: a name with a non-ASCII or special character is in double quotes with C escapes (octal bytes, backslash-n and so on)."""
    name = name.strip()
    if not (len(name) >= 2 and name.startswith('"') and name.endswith('"')):
        return name
    body, out, i = name[1:-1], bytearray(), 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            n = body[i + 1]
            if n in "01234567":
                j, digits = i + 1, ""
                while j < len(body) and len(digits) < 3 and body[j] in "01234567":
                    digits += body[j]
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
                continue
            if n in _C_ESCAPES:
                out.append(_C_ESCAPES[n])
                i += 2
                continue
        out += c.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


_GIT_HEADER = re.compile(r'^diff --git (?P<a>"(?:[^"\\]|\\.)*"|\S+) (?P<b>"(?:[^"\\]|\\.)*"|\S+)\s*$')


def _header_paths(rest: str) -> list[str]:
    """The two paths of a ``diff --git a/X b/Y`` line (the names may hold spaces when git does not quote them: then the two halves are the same name)."""
    m = _GIT_HEADER.match("diff --git " + rest)
    if m:
        return [re.sub(r"^[ab]/", "", unquote_path(m.group("a"))), re.sub(r"^[ab]/", "", unquote_path(m.group("b")))]
    if len(rest) % 2 == 1:
        mid = len(rest) // 2
        a, b = rest[:mid], rest[mid + 1:]
        if a[2:] == b[2:]:
            return [a[2:]]
    return []


def patch_paths(text: str) -> list[str]:
    """The files a patch touches: the ``diff --git`` header (both names), the ``---`` and ``+++`` lines, and ``rename`` and ``copy`` lines, so that a rename, a copy, a change of mode only and a name
    git had to quote are seen too. ``/dev/null`` is not a file."""
    out: list[str] = []

    def add(path: str) -> None:
        if path and path != "/dev/null" and path not in out:
            out.append(path)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("diff --git "):
            for p in _header_paths(line[len("diff --git "):]):
                add(p)
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            add(unquote_path(line.split(" ", 2)[2]))
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            for header in (line[4:], lines[i + 1][4:]):
                add(re.sub(r"^[ab]/", "", unquote_path(header.split("\t")[0])))
    return out


# What the patch must not be able to put on Python's path: a module that shadows the standard library, or a file Python runs by itself at start-up. The verifier runs ``python -m unittest`` with
# the project's folders first on the path, so a patch that adds ``unittest/__main__.py`` would be the verifier.
STARTUP_FILES = ("sitecustomize", "usercustomize")
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))


def shadowed_paths(paths) -> list[str]:
    out = []
    for p in paths:
        parts = pathlib.PurePosixPath(str(p).replace("\\", "/")).parts
        if not parts:
            continue
        name = parts[-1]
        stem = name[:-3] if name.endswith(".py") else name
        if name.endswith(".pth") or (name.endswith(".py") and stem in STARTUP_FILES):
            out.append(p)
            continue
        hit = False
        for depth in (0, 1) if (len(parts) > 1 and parts[0] == "src") else (0,):          # the first name below the project folder, or below src/
            if depth >= len(parts):
                continue
            first = parts[depth]
            module = first[:-3] if first.endswith(".py") else first
            if module not in _STDLIB:
                continue
            is_file = depth == len(parts) - 1 and first.endswith(".py")
            is_package_file = depth == len(parts) - 2 and name in ("__init__.py", "__main__.py")
            hit = hit or is_file or is_package_file
        if hit:
            out.append(p)
    return out


LINK_MODE = re.compile(r"^(?:(?:new\s+file|deleted\s+file|old|new)\s+mode\s+0*120000|index\s+[0-9a-f]+\.\.[0-9a-f]+\s+0*120000)\s*$", re.M)


def makes_links(text: str) -> bool:
    """True when a git patch creates, changes or removes a symbolic link (mode 120000). A link in the copy of the project could send a later
    write of the acceptance files, made by this process on the host, outside the project."""
    return bool(LINK_MODE.search(text))


def _unsafe(path: str) -> bool:
    parts = pathlib.PurePosixPath(path.replace("\\", "/")).parts
    return path.startswith(("/", "\\")) or ".." in parts or (len(path) > 1 and path[1] == ":")
