"""Bounded full subtree recounts. No cached sizes or filesystem assumptions."""
from concurrent.futures import FIRST_COMPLETED, wait
import os
from pathlib import Path

from .disk_budget import size_of


def _units(root, depth=3):
    if not depth:
        yield root
        return
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        yield from _units(entry.path, depth - 1)
                    else:
                        yield entry.stat(follow_symlinks=False).st_size
                except FileNotFoundError:
                    pass
    except (FileNotFoundError, NotADirectoryError):
        pass


def _size(root, executor):
    total, pending = 0, set()
    units = _units(root)
    try:
        for unit in units:
            if isinstance(unit, int):
                total += unit
                continue
            pending.add(executor.submit(size_of, unit))
            if len(pending) >= 8:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                total += sum(future.result() for future in done)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            total += sum(future.result() for future in done)
        return total
    finally:
        units.close()
        for future in pending:
            future.cancel()
        if pending:
            wait(pending)


def count_used(root, baselines, executor):
    # Clamp once per complete additional root, exactly as the serial reference.
    # Shrinkage in one shard offsets growth in another shard of the same root.
    return _size(root, executor) + sum(
        max(0, _size(Path(path), executor) - baseline)
        for path, baseline in baselines.items())
