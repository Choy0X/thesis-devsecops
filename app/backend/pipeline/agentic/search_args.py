"""What the arguments of a search or a file lookup mean, whichever tool or mode they come to.

A model writes ``app/**/*`` where a folder belongs, writes a word boundary as ``\\b`` inside JSON (which decodes to a backspace character), and writes ``*.{ts,tsx}`` expecting braces to work.
Each of those used to end in a search of nothing that was reported as a success. Here a path with glob characters is split into the folder it starts in and the pattern that follows,
a backspace in a pattern becomes the word boundary it was meant to be, and a brace group becomes the alternatives it lists.
"""
from __future__ import annotations

GLOB_CHARS = "*?[{"
MAX_ALTERNATIVES = 32


def has_glob(text: str) -> bool:
    return any(c in str(text or "") for c in GLOB_CHARS)


def split_path(path: str) -> tuple[str, str]:
    """``(folder or file, glob)`` for a ``path`` argument: ``app/**/*`` is ``("app", "**/*")``, ``*.py`` is ``("", "*.py")``, ``src/app.py`` is ``("src/app.py", "")``."""
    text = str(path or "").replace("\\", "/").strip()
    if not has_glob(text):
        return text, ""
    parts = text.split("/")
    for i, part in enumerate(parts):
        if has_glob(part):
            return "/".join(parts[:i]), "/".join(parts[i:])
    return text, ""


def fix_regex(pattern: str) -> str:
    """A pattern as the model meant it: a backspace character (what ``\\b`` becomes after JSON decoding) is the word boundary it stood for."""
    return str(pattern or "").replace("\x08", "\\b")


def expand_braces(pattern: str) -> list[str]:
    """``*.{ts,tsx}`` as ``["*.ts", "*.tsx"]``; nested and several groups are expanded too, up to ``MAX_ALTERNATIVES`` alternatives, and a brace that does not close is a plain character."""
    out = [str(pattern or "")]
    changed = True
    while changed and len(out) < MAX_ALTERNATIVES:
        changed = False
        nxt: list[str] = []
        for p in out:
            start = p.find("{")
            end = p.find("}", start + 1) if start >= 0 else -1
            if start >= 0 and end > start:
                inner = p[start + 1:end]
                if "{" in inner:                           # the innermost group first
                    start = p.rfind("{", 0, end)
                    inner = p[start + 1:end]
                nxt.extend(p[:start] + alt + p[end + 1:] for alt in inner.split(","))
                changed = True
            else:
                nxt.append(p)
        out = nxt[:MAX_ALTERNATIVES]
    return out
