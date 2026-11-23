"""Bounded commit work. A timed-out future keeps its slot until it has stopped."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import time

from app.backend.pipeline import stopping
from . import operations


def run(queue, mine, workers, counts, tick, *, commit_timeout, heartbeat, should_stop):
    from .mining_policy import io_width
    from app.backend.pipeline.ingest import codemine
    pending, active, remaining = {}, {}, deque(queue)
    ceiling, reset = max(1, min(4, int(workers))), None
    width, stable, last_sample, last_note = ceiling, 0, time.monotonic(), time.monotonic()
    last_beat = time.monotonic()
    executor = ThreadPoolExecutor(ceiling, thread_name_prefix='mining-io')

    def work(row):
        operation = operations.Operation(row['commit_key'], commit_timeout, should_stop or (lambda: False), kind='commit')
        active[row['commit_key']] = operation
        try:
            with operations.use(operation):
                operation.phase('metadata')
                result = mine(row)
                operation.check()
                return result
        except BaseException:
            operation.cancel()
            raise

    try:
        while remaining or pending:
            if should_stop and should_stop():
                remaining.clear()
                for future in pending: future.cancel()
                for operation in list(active.values()): operation.cancel()
            elif reset is None:
                # Reduce immediately. Growth needs three stable five-second samples.
                desired = io_width(ceiling)
                if desired == 0 and not pending:
                    # Work already running gives its memory back when it ends, so only an idle miner with no room pauses (and is resumed by the watchdog).
                    from .memory_profile import current as memory_profile
                    paused = stopping.Paused('System RAM reserve would be exhausted; resume when memory is available')
                    paused.reason = {'code':'mining_memory_pressure','detail':str(paused),'retryable':True,'resume_needs_gb':memory_profile().resume_needs_gb}
                    raise paused
                if desired < width: width, stable = desired, 0
                if time.monotonic()-last_sample >= 5:
                    last_sample = time.monotonic()
                    stable = stable+1 if desired > width else 0
                    if stable >= 3: width, stable = min(desired, width+1), 0
                while remaining and len(pending) < width:
                    row = remaining.popleft()
                    pending[executor.submit(work, row)] = row
            if not pending: break
            done, _ = wait(list(pending), timeout=.2, return_when=FIRST_COMPLETED)
            if time.monotonic()-last_beat >= 1:
                last_beat = time.monotonic()
                tick()  # Writes the heartbeat and publishes active phases even during a long commit.
            for future, row in list(pending.items()):
                operation = active.get(row['commit_key'])
                if operation and time.monotonic() >= operation.deadline:
                    operation.cancel()
                    if time.monotonic()-operation.deadline > 15 and not future.done():
                        error = stopping.Paused('A mining operation did not stop after its deadline')
                        error.reason = {'code':'mining_cleanup_unconfirmed', 'detail':str(error)}
                        raise error
            if not done and time.monotonic()-last_note >= 30:
                last_note = time.monotonic()
                for operation in list(active.values()):
                    state = operation.state()
                    print(f"still working on {state['commit_key']} | {state['phase']} | {state['elapsed_seconds']:.0f}s")
            for future in done:
                row = pending.pop(future)
                operation = active.get(row['commit_key'])
                if future.cancelled(): continue
                tag = f"{row['owner']}/{row['repo']}@{row['sha'][:8]}"
                try:
                    result = future.result()
                    with operations.use(operation):
                        operation.phase('persistence')
                        codemine._store_result(row, result, counts, tag)
                except codemine.RateLimited as error:
                    reset = max(reset or 0, error.reset)
                    operation.finish('rate_limited', str(error))
                    remaining.clear()
                    for other in pending: other.cancel()
                except stopping.Paused as error:
                    operation.finish('cancelled', str(error))
                    raise
                except Exception as error:
                    if operation.failed_resources:
                        paused = stopping.Paused('Owned mining resources could not be stopped')
                        paused.reason = {'code':'mining_cleanup_unconfirmed','detail':str(paused)}
                        raise paused from error
                    codemine.codestore.mark_fix_commit(row['commit_key'], 'failed', error=str(error)[:300])
                    codemine.codestore.refresh_records(codemine.codestore.records_for_commit(row))
                    counts['failed'] += 1
                    operation.finish('failed', str(error))
                    print(f'{tag}: failed - {str(error)[:120]}')
                else:
                    operation.finish(completion='legacy')
                finally:
                    active.pop(row['commit_key'], None)
                tick()
    finally:
        for future in pending: future.cancel()
        for operation in list(active.values()): operation.cancel()
        # Resources have bounded waits; never accept results from a cancelled attempt.
        executor.shutdown(wait=True, cancel_futures=True)
        for operation in list(active.values()): operation.finish('cancelled')
    return reset
