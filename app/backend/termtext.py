"""Plain text from what a program printed to a terminal.

A job's log keeps its output the way the program wrote it: an entry that ended with a lone carriage return (a redraw: the next entry is drawn over it) keeps that
character, and colour and cursor codes stay in the text, because the page draws them like a terminal does. A sentence for a person (the reason a job failed, the
tail of a log in a letter) must not carry that debris: ``visible`` returns the rows a terminal would end up showing, as plain text.
"""
from __future__ import annotations

import re
from typing import Iterable

# A control sequence (ESC [ ... final byte), an operating-system command (ESC ] ... BEL or ESC \), or a two-character escape.
_ESCAPES = re.compile(r"\x1b\[[0-9;?<=>]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")


def strip_escapes(text: str) -> str:
    """The text without colour, cursor and title sequences."""
    return _ESCAPES.sub("", text)


def visible(entries: Iterable[str]) -> list[str]:
    """The rows a terminal would show for these log entries, as plain text. An entry that ends with a carriage return is replaced by the entry after it (a progress bar
    that redraws itself keeps one row, its last state); everything else is a row of its own. Escape sequences are removed and trailing blanks trimmed."""
    rows: list[str] = []
    replace_last = False
    for entry in entries:
        text = strip_escapes(entry.rstrip("\r")).rstrip()
        if replace_last and rows:
            rows[-1] = text
        else:
            rows.append(text)
        replace_last = entry.endswith("\r")
    return rows
