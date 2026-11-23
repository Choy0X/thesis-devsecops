"""Cooperative stopping: how a campaign asks a stage to pause.

A stage that can be paused takes ``should_stop``, a function that answers whether the campaign asked it to pause (it reads the campaign's control flag, at most once a second). The stage checks it
at safe points (between batches, records or chunks, never in the middle of a write) and, when it says yes, finishes what is in progress, leaves its queue and cursors consistent, and ends. Most
stages then return their usual result with ``"paused": True``; a stage that has no result to return (the dump download and read, whose progress is kept on disk) raises ``Paused``. A stage
that is run again picks up from what is stored, so a pause costs at most the work of the unit that was in progress.
"""
from __future__ import annotations

import time
from typing import Callable

ShouldStop = Callable[[], bool]


class Paused(Exception):
    """A stage reached a safe point and stopped because the campaign asked it to pause. What it did so far is kept."""


def stop(should_stop: ShouldStop | None) -> bool:
    """Did the campaign ask to pause? (False when there is nobody to ask.)"""
    return bool(should_stop is not None and should_stop())


def check(should_stop: ShouldStop | None) -> None:
    """Raise ``Paused`` when the campaign asked to pause."""
    if stop(should_stop):
        raise Paused()


def sleep(seconds: float, should_stop: ShouldStop | None = None, *, step: float = 1.0, nap: Callable[[float], None] = time.sleep) -> bool:
    """Sleep ``seconds`` in slices of ``step`` and give up early when the campaign asks to pause; returns whether it was cut short. Without ``should_stop`` this is one plain sleep."""
    if should_stop is None:
        nap(seconds)
        return False
    left = float(seconds)
    while left > 0:
        nap(min(step, left))
        left -= step
        if should_stop():
            return True
    return False
