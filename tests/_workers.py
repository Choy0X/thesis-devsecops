"""Top-level functions for the worker-process tests (a spawned worker imports them by name)."""
import os
import time

FLAG = {"set": None}


def square(x):
    return x * x


def pid(_x):
    return os.getpid()


def slow_identity(pair):
    i, delay = pair
    time.sleep(delay)
    return i


def boom(x):
    if x == 3:
        raise ValueError("bad item 3")
    return x


def set_flag(value):
    FLAG["set"] = value


def read_flag(_x):
    return FLAG["set"]


def shout(x):
    print(f"worker output {x}")
    return x


def tokenizers_env(_x):
    return os.environ.get("TOKENIZERS_PARALLELISM")


def numeric_footprint(_x):
    from app.backend.pipeline.acceleration import runtime
    import psutil
    runtime.overlaps([(['x'],['x'])])
    return {'threads':os.environ.get('OPENBLAS_NUM_THREADS'),
            'private':getattr(psutil.Process().memory_info(),'private',0)}
