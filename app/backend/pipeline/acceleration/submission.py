"""Fence work queued by an executor whose submit fails during thread startup."""
from concurrent.futures import CancelledError
import threading


def submit_owned(executor, function, *args, register=lambda future:None):
    # CPython may queue a work item before _adjust_thread_count raises. The
    # wrapper cannot enter user work until submit has returned successfully.
    # RLock also supports deterministic inline executors used by callers' tests.
    gate = threading.RLock()
    accepted = True
    def invoke():
        with gate:
            if not accepted: raise CancelledError('Submission did not acquire ownership')
        return function(*args)
    with gate:
        future=None
        try:
            future=executor.submit(invoke)
            # Registration must be one atomic ownership-table insertion. Work
            # remains fenced if allocation of that table entry fails as well.
            register(future)
            return future
        except BaseException:
            accepted = False
            if future is not None:future.cancel()
            raise
