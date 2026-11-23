"""Stream the items of a top-level JSON array without loading the whole file.

``json.loads(path.read_text())`` on a multi-GB NVD harvest needs many times the
file size in memory and parses in one long call; this reads it in chunks and
yields one item at a time.
"""
from __future__ import annotations
import json
import pathlib
from typing import Iterator

_WS = " \t\r\n,"


def iter_json_array(path: str | pathlib.Path, chunk: int = 1 << 20) -> Iterator:
    dec = json.JSONDecoder()
    with open(path, encoding="utf-8") as fh:
        buf, pos, started, eof = "", 0, False, False

        def fill() -> bool:
            nonlocal buf, pos, eof
            more = fh.read(chunk)
            if not more:
                eof = True
                return False
            buf, pos = buf[pos:] + more, 0
            return True

        while True:
            while pos < len(buf) and buf[pos] in _WS:
                pos += 1
            if pos >= len(buf):
                if not fill():
                    return
                continue
            if not started:
                if buf[pos] != "[":
                    raise ValueError(f"{path}: expected a JSON array")
                started, pos = True, pos + 1
                continue
            if buf[pos] == "]":
                return
            try:
                obj, end = dec.raw_decode(buf, pos)
            except json.JSONDecodeError:
                if eof or not fill():
                    raise
                continue
            yield obj
            pos = end
            if pos > chunk:                       # drop consumed text
                buf, pos = buf[pos:], 0
