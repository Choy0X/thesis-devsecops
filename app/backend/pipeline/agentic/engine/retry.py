"""Trying a model request again when the server, not the request, is at fault.

A hosted model answers 429 when it is asked too fast and 503 when it is busy; a local server answers 503 while it loads the model; a network drops a connection. None of that is the model's
answer, and none of it should end a run that may have worked for an hour. The rules, taken from how the OpenCode retry module behaves and tested here:

* retry on 408, 409, 425, 429, every 5xx, and a connection or timeout error; never on 400, 401, 403, 404, 422 (a request that is wrong stays wrong) and never on a context overflow (the loop
  makes the request smaller instead; ``agent_loop._is_context_error``);
* at most ``tries`` attempts, the wait before the next one ``base * factor ** (attempt - 1)`` seconds up to ``cap``, plus up to ``jitter`` of itself at random (so that many runs do not all come
  back at the same second), and never shorter than what the server asked for in ``retry-after-ms`` or ``retry-after``;
* a wait ends at once when the run is stopped.

It wraps the opening of a request (where the HTTP call is made). A stream that breaks after it started is not retried: what it had said is already shown.
"""
from __future__ import annotations

import email.utils
import random
import re
import time
from dataclasses import dataclass
from typing import Callable

RETRY_STATUS = frozenset({408, 409, 425, 429})
NEVER_RETRY = frozenset({400, 401, 403, 404, 405, 413, 422})
MAX_HINT = 120.0


@dataclass(frozen=True)
class RetryPolicy:
    tries: int = 5
    base: float = 2.0
    factor: float = 2.0
    cap: float = 30.0
    jitter: float = 0.25


def status_of(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    return int(code) if isinstance(code, (int, float)) else None


def retry_after(exc: BaseException) -> float | None:
    """What the server asked for, in seconds: ``retry-after-ms``, else ``retry-after`` (a number of seconds or a date)."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    get = headers.get if hasattr(headers, "get") else (lambda k: None)
    ms = get("retry-after-ms")
    try:
        if ms is not None:
            return min(MAX_HINT, max(0.0, float(ms) / 1000.0))
    except (TypeError, ValueError):
        pass
    value = get("retry-after")
    if value is None:
        return None
    try:
        return min(MAX_HINT, max(0.0, float(value)))
    except (TypeError, ValueError):
        pass
    try:
        when = email.utils.parsedate_to_datetime(str(value))
        return min(MAX_HINT, max(0.0, when.timestamp() - time.time()))
    except (TypeError, ValueError):
        return None


_OVERFLOW = re.compile(r"context[_ ](?:length|window|size)|maximum context|exceeds? the available context|too many tokens|prompt is too long", re.I)


def is_retryable(exc: BaseException) -> bool:
    """Whether the failure is the server's or the network's and may pass. A request that is too long for the model's context is the request's fault whatever status the server answered it with."""
    body = getattr(exc, "body", None)
    if _OVERFLOW.search(str(exc) + " " + (str(body) if body else "")):
        return False
    status = status_of(exc)
    if status is not None:
        if status in NEVER_RETRY:
            return False
        return status in RETRY_STATUS or 500 <= status <= 599
    name = type(exc).__name__.lower()
    return any(w in name for w in ("connection", "timeout", "timedout", "connecterror", "readerror", "remoteprotocol", "networkerror"))


def delay(attempt: int, policy: RetryPolicy, hint: float | None = None, rng: random.Random | None = None) -> float:
    """Seconds to wait before attempt ``attempt + 1`` (``attempt`` starts at 1)."""
    wait = min(policy.cap, policy.base * policy.factor ** (attempt - 1))
    wait *= 1.0 + policy.jitter * (rng or random).random()
    return max(wait, hint) if hint is not None else wait


def call(fn: Callable, policy: RetryPolicy = RetryPolicy(), *, sleep: Callable[[float], bool] | None = None, on_retry: Callable[[int, float, BaseException], None] | None = None,
         rng: random.Random | None = None):
    """``fn()``, tried again while it fails with something ``is_retryable``. ``sleep(seconds)`` returns True when the wait was cut short because the run was stopped (the failure is then raised).
    ``on_retry(attempt, seconds, error)`` is told before each wait."""
    attempt = 1
    wait_for = sleep or (lambda s: (time.sleep(s), False)[1])
    while True:
        try:
            return fn()
        except Exception as e:
            if attempt >= policy.tries or not is_retryable(e):
                raise
            seconds = delay(attempt, policy, retry_after(e), rng)
            if on_retry:
                on_retry(attempt, seconds, e)
            if wait_for(seconds):
                raise
            attempt += 1
