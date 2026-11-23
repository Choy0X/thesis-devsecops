"""Parse Python source that is not ours (mined fix commits, model predictions).

Since Python 3.12 compiling source with an invalid escape sequence (``"\\d"``,
``"\\|"``, ``"\\."`` - typically regexes written without ``r"..."``) emits a
``SyntaxWarning``, as do a few other compile-time checks (``x is 1``, ``0x1for``,
``return`` in ``finally``). For third-party code these warnings say nothing about
this project and flooded the mining log ("<unknown>:180: SyntaxWarning: ...").
They are silenced here; real syntax errors still raise.
"""
from __future__ import annotations
import ast
import threading
import warnings

# catch_warnings() swaps the process-wide filter list, and the miner parses from
# worker threads, so parses are serialised (they hold the GIL anyway).
_LOCK = threading.Lock()


def parse_python(src: str) -> ast.Module:
    with _LOCK, warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.filterwarnings("ignore", category=DeprecationWarning,
                                message="invalid escape sequence")   # Python <= 3.11
        return ast.parse(src)
