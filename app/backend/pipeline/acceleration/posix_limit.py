"""Aggregate memory guard of a campaign worker on Linux and macOS.

On Windows the worker is placed in a job object whose memory limit the kernel enforces (``windows_job.install_campaign_limit``). Other systems have no per-tree equivalent that works without
a privileged cgroup, so this module keeps a watchdog thread in the worker: every few seconds it sums the proportional memory (PSS, else RSS) of the worker and all its descendants and reads
the machine's available memory. The cap is the campaign's budget (the adaptive tiers of ``memory_profile`` size the workers to it, so staying under it is the normal case). The guard does
not kill at the cap; it stops the worker when the tree stays above ``OVER`` times the cap, or the machine runs out of room, for several checks in a row. It terminates the descendants and
exits with ``EXIT_CODE``; the campaign then shows as interrupted and can be resumed, which is the same outcome as the Windows kill and better than the kernel OOM killer picking a victim.
"""
from __future__ import annotations

import os
import sys
import threading
import time

OVER = 1.5                    # stop when the tree holds more than this many times the cap ...
STRIKES = 3                   # ... for this many checks in a row
MIN_AVAILABLE = 800 * 1024 * 1024
EXIT_CODE = 75

_state: dict = {'cap': None, 'thread': None, 'stop': None}


def tree_bytes(root_pid: int | None = None) -> int:
    """Proportional memory of a process and its descendants (PSS where the system reports it, else RSS)."""
    import psutil
    root = psutil.Process(root_pid or os.getpid())
    total = 0
    for proc in [root] + root.children(recursive=True):
        try:
            try:
                total += proc.memory_full_info().pss
            except (AttributeError, psutil.AccessDenied):
                total += proc.memory_info().rss
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return total


def verdict(used: int, cap: int, available: int, strikes: int) -> tuple[str, int]:
    """('ok' | 'stop', strikes after this check): pure, so the policy is testable without any process."""
    bad = used > OVER * cap or available < MIN_AVAILABLE
    strikes = strikes + 1 if bad else 0
    return ('stop' if strikes >= STRIKES else 'ok'), strikes


def _stop_tree(log) -> None:
    import psutil
    me = psutil.Process()
    kids = me.children(recursive=True)
    for kid in kids:
        try:
            kid.terminate()
        except psutil.Error:
            pass
    psutil.wait_procs(kids, timeout=5)
    for kid in kids:
        try:
            if kid.is_running():
                kid.kill()
        except psutil.Error:
            pass


def install(cap: int, *, interval: float = 5.0, probe=None, available=None, on_stop=None, log=None) -> None:
    """Start the watchdog once; a second call with the same cap is a no-op, a different cap is refused (the cap of a campaign does not change while it runs)."""
    if _state['cap'] is not None:
        if _state['cap'] != cap:
            raise RuntimeError('Campaign memory cap changed')
        return
    import psutil
    probe = probe or tree_bytes
    available = available or (lambda: psutil.virtual_memory().available)
    log = log or (lambda text: (print(text, flush=True)))
    on_stop = on_stop or (lambda: (_stop_tree(log), sys.stdout.flush(), os._exit(EXIT_CODE)))
    stop = threading.Event()
    _state.update(cap=cap, stop=stop)

    def watch():
        strikes = 0
        while not stop.wait(interval):
            try:
                used, free = probe(), available()
            except Exception:                       # ignored: a failed reading (a process that just exited) skips this tick; the next interval reads again
                continue
            state, strikes = verdict(used, cap, free, strikes)
            if state == 'stop':
                log(f'campaign memory guard: the worker tree holds {used // 1048576} MB (cap {cap // 1048576} MB, stop above {int(OVER * cap) // 1048576} MB) and the machine has '
                    f'{free // 1048576} MB available; stopping so the campaign can be resumed')
                on_stop()
                return
    thread = threading.Thread(target=watch, name='campaign-memory-guard', daemon=True)
    _state['thread'] = thread
    thread.start()


def current_limit() -> int | None:
    return _state['cap']


def reset_for_tests() -> None:
    if _state.get('stop') is not None:
        _state['stop'].set()
    _state.update(cap=None, thread=None, stop=None)
