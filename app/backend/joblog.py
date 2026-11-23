"""The output of one job: the latest lines in memory, every line on disk.

A job that prints a line per record can print millions of lines. Keeping them all in a Python list grew the API process without bound and
made every status request carry the whole log. Here the last ``KEEP`` lines stay in memory, every line is also appended to a file
(``experiments/logs/<job id>.log``) when the job has one, and a reader asks by absolute position: "from line N on". Line numbers never
change, so a client that lost its connection can resume where it stopped.
"""
from __future__ import annotations

import collections
import itertools
import pathlib
import threading
from typing import Iterable

KEEP = 5000              # lines kept in memory
FLUSH_EVERY = 100        # appends between flushes of the file


class JobLog:
    def __init__(self, initial: Iterable[str] = (), spill: pathlib.Path | None = None, keep: int = KEEP):
        self._window: collections.deque[str] = collections.deque()
        self._first = 0                       # absolute number of the first line in memory
        self._keep = keep
        self._spill = pathlib.Path(spill) if spill else None
        self._file = None
        self._unflushed = 0
        self._lock = threading.RLock()
        for line in initial:
            self.append(line)

    # -- writing -------------------------------------------------------------------------------------------------------------------------
    def append(self, line: str) -> None:
        with self._lock:
            self._window.append(line)
            if len(self._window) > self._keep:
                self._window.popleft()
                self._first += 1
            if self._spill is not None:
                try:
                    if self._file is None:
                        self._spill.parent.mkdir(parents=True, exist_ok=True)
                        self._file = open(self._spill, "a", encoding="utf-8", newline="\n")
                    self._file.write(line.replace("\n", " ") + "\n")
                    self._unflushed += 1
                    if self._unflushed >= FLUSH_EVERY:
                        self._file.flush()
                        self._unflushed = 0
                except OSError:
                    self._spill = None        # ignored: a log that cannot be spilled stays in memory only; the job goes on

    def extend(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.append(line)

    def close(self) -> None:
        """Flush and release the file (the job ended)."""
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass                      # ignored: closing a file that is already gone
                self._file = None

    # -- reading -------------------------------------------------------------------------------------------------------------------------
    def __len__(self) -> int:
        """How many lines the job has printed so far, the ones no longer in memory included."""
        with self._lock:
            return self._first + len(self._window)

    @property
    def first_in_memory(self) -> int:
        return self._first

    def lines(self, start: int = 0, stop: int | None = None) -> list[str]:
        """The lines numbered ``start`` up to (not including) ``stop``; lines that are no longer in memory come from the file."""
        with self._lock:
            total = self._first + len(self._window)
            stop = total if stop is None else min(stop, total)
            start = max(0, start)
            if start >= stop:
                return []
            out: list[str] = []
            if start < self._first:
                out = self._from_file(start, min(stop, self._first))
                start = min(stop, self._first)
                if start >= stop:
                    return out
            offset = start - self._first
            return out + list(itertools.islice(self._window, offset, stop - self._first))

    def _from_file(self, start: int, stop: int) -> list[str]:
        if self._spill is None:
            return []                         # not kept anywhere
        try:
            if self._file is not None:
                self._file.flush()
            with open(self._spill, encoding="utf-8", newline="\n") as fh:          # only "\n" ends a line here: a carriage return belongs to the entry (a redraw marker) and is kept
                return [line.rstrip("\n") for line in itertools.islice(fh, start, stop)]
        except OSError:
            return []

    def tail(self, count: int) -> list[str]:
        with self._lock:
            return self.lines(max(0, len(self) - count))

    def __getitem__(self, key):
        """``log[i]`` and ``log[a:b]`` with the numbering of a list that holds every line (negative numbers count from the end)."""
        total = len(self)
        if isinstance(key, slice):
            start, stop, step = key.indices(total)
            if step != 1:
                raise ValueError("a job log is read in steps of one line")
            return self.lines(start, stop)
        index = key + total if key < 0 else key
        if not 0 <= index < total:
            raise IndexError(key)
        return self.lines(index, index + 1)[0]

    def __iter__(self):
        return iter(self.lines())

    def __bool__(self) -> bool:
        return len(self) > 0
