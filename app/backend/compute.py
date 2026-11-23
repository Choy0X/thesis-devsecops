"""Worker pools sized from the machine: CPU processes for pure-Python work, a probe of the GPU for the local GPU job.

Two kinds of parallelism are kept apart because they are limited by different things:

* **CPU workers** (processes): for work that is pure Python and holds the GIL (parsing code, building contexts, MinHash). The number follows the
  physical cores (one is left for the server and the window) and is reduced when memory is short; a value typed in Settings or set in
  ``ZTSA_CPU_WORKERS`` is used as given.
* **I/O workers** (threads): for waiting on a network, a registry or a model server. Their number is bounded by latency and rate limits, not
  by cores, so each stage keeps its own default (``io_workers``).

``pmap`` runs a top-level function over a list in a pool of spawned processes and returns the results **in input order**, so a parallel build
is byte-identical to a serial one. It falls back to a plain loop for one worker or a short list. Workers print nothing (a job's standard
output carries its progress lines) and set the tokenizers' own threading off (the pool already uses the cores).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import multiprocessing as mp
import os
import pathlib
import subprocess
import sys
import threading
import time

ENV_CPU = "ZTSA_CPU_WORKERS"
ENV_GPU_PYTHON = "ZTSA_GPU_PYTHON"
MAX_CPU_WORKERS = 16
RAM_GB_PER_WORKER = 0.5          # measured: a dataset-build worker peaks at about 110 MB on real files; this leaves room for a very large one
ROOT = pathlib.Path(__file__).resolve().parents[2]


# -- the machine -------------------------------------------------------------------------------------------------------------------------------

def physical_cores() -> int:
    try:
        import psutil
        n = psutil.cpu_count(logical=False)
        if n:
            return n
    except Exception:  # ignored: psutil missing or failing falls back to the os.cpu_count estimate below
        pass
    return max(1, (os.cpu_count() or 2) // 2)


def logical_cores() -> int:
    return os.cpu_count() or 1


def memory_gb() -> tuple[float | None, float | None]:
    """(total, available) in GB, or (None, None) when it cannot be read."""
    try:
        import psutil
        m = psutil.virtual_memory()
        return round(m.total / 1024 ** 3, 1), round(m.available / 1024 ** 3, 1)
    except Exception:  # ignored: memory figures are optional; None means unknown
        return None, None


def auto_cpu_workers() -> int:
    """Physical cores minus one, never more than the free memory can feed."""
    n = max(1, physical_cores() - 1)
    _total, free = memory_gb()
    if free is not None:
        n = min(n, max(1, int(free // RAM_GB_PER_WORKER)))
    try:
        from app.backend.pipeline.acceleration.memory_profile import current as memory_profile
        n = memory_profile().capped(n, 'ingest_workers')    # a smaller memory tier feeds fewer worker processes (each one imports pandas)
    except Exception:  # ignored: a profile problem must never stop the pool from sizing itself
        pass
    return min(n, MAX_CPU_WORKERS)


def memory_adaptive_workers(ceiling: int, per_worker_gb: float = 0.1) -> int:
    """How many of up to ``ceiling`` workers the memory free right now can feed. Meant to be re-read on every pass of a loop that is already
    running (not only once when it starts), so a long batch can grow back into memory that frees up while it runs, not only shrink into memory
    that tightens. Memory that cannot be read never limits it (the ceiling itself already came from somewhere that decided it was safe)."""
    ceiling = max(1, int(ceiling))
    _total, free = memory_gb()
    if free is None:
        return ceiling
    return max(1, min(ceiling, int(free // max(per_worker_gb, 1e-6))))


def cpu_workers(requested=None) -> int:
    """How many CPU worker processes to use: ``requested`` (a stage's own parameter) if given, else ``ZTSA_CPU_WORKERS`` (a number, or
    ``auto``), else the automatic choice. A number given by a person is used as given (1 to 64): they may know the machine is idle."""
    for value in (requested, os.environ.get(ENV_CPU)):
        if value is None or str(value).strip().lower() in ("", "auto", "0"):
            continue
        try:
            return max(1, min(int(value), 64))
        except ValueError:
            continue
    return auto_cpu_workers()


def io_workers(default: int, requested=None, cap: int = 64, kind: str | None = None) -> int:
    """Threads for work that waits on the network. A stage's default is kept unless a number is given. ``kind`` (``mine_threads``, ``recover_threads``, ``deps_threads``) lets a
    smaller memory tier lower the count; the normal tier changes nothing."""
    try:
        n = max(1, min(int(requested), cap)) if requested not in (None, "", 0) else default
    except (TypeError, ValueError):
        n = default
    if kind:
        try:
            from app.backend.pipeline.acceleration.memory_profile import current as memory_profile
            n = memory_profile().capped(n, kind)
        except Exception:  # ignored: the stage default stands
            pass
    return n


# -- pools -------------------------------------------------------------------------------------------------------------------------------------

def lower_priority() -> None:
    """Run this process below normal priority, so heavy work cannot starve the window (a job's own priority is inherited by its workers
    on Windows; this makes it explicit, and it is the only way on other systems). Failing to lower it is not an error."""
    try:
        if os.name == "nt":
            import psutil
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            os.nice(10)
    except Exception:  # ignored: priority is a courtesy to the rest of the machine
        pass


def _init_worker(initializer, initargs):
    lower_priority()
    os.environ["TOKENIZERS_PARALLELISM"] = "false"           # the pool already uses the cores
    sys.stdout = open(os.devnull, "w")                       # a job's standard output carries its progress lines
    if initializer is not None:
        initializer(*initargs)


class Pool:
    """Worker processes that live for a ``with`` block, for work done in several rounds (starting a worker costs a second or two: each
    imports the pipeline). ``map`` has the semantics of ``pmap``. With one worker no process is started and ``map`` is a loop."""

    def __init__(self, workers=None, *, initializer=None, initargs=(), force_process=False):
        self.workers = cpu_workers(workers)
        self._init = (initializer, initargs)
        self._pool = None
        self.force_process = force_process
        self._abort_lock = threading.Lock()
        self._aborted = False
        self._owned_processes = {}

    def __enter__(self):
        if self.workers > 1 or self.force_process:
            self._pool = cf.ProcessPoolExecutor(max_workers=self.workers, mp_context=mp.get_context("spawn"), initializer=_init_worker,
                                                initargs=self._init)       # processes start when the first item is submitted
        return self

    def __exit__(self, *exc):
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None

    def submit(self, fn, *args):
        """Runs ``fn(*args)`` in a worker process and returns its result, blocking the calling thread (a blocking wait on another process's result releases the GIL
        the same as any other blocking wait, so several threads can each have one of these in flight at once, each on its own core). With one worker (no process
        pool) ``fn`` runs directly, in this thread."""
        from app.backend.pipeline.acceleration import operations
        from app.backend.pipeline import stopping
        def aborted(error=None):
            paused = stopping.Paused('The shared parser was stopped; resume to retry unfinished commits')
            paused.reason = {'code':'mining_parser_stopped','detail':str(paused),'retryable':True}
            raise paused from error
        with self._abort_lock:
            if self._aborted: aborted()
            pool = self._pool
            if pool is None: return fn(*args)
            future = pool.submit(fn, *args)
        if not operations.current():
            return future.result()
        try:
            with operations.resource(self.abort):
                while True:
                    operations.check()
                    try: return future.result(timeout=operations.timeout(.2))
                    except cf.TimeoutError: continue
        except operations.DeadlineExpired as error:
            paused = stopping.Paused('Parser deadline exceeded; resume to retry unfinished commits')
            paused.reason = {'code':'mining_parser_deadline','detail':str(paused)}
            raise paused from error
        except MemoryError:
            raise
        except Exception as error:
            if self._aborted: aborted(error)
            raise

    def abort(self):
        """Stop our parser processes, including children, before releasing resources."""
        from app.backend.pipeline.acceleration.processes import terminate
        with self._abort_lock:
            self._aborted = True
            if self._pool is None: return
            pool = self._pool
            for process in list((pool._processes or {}).values()):
                # multiprocessing.Process has no poll API. Keep the same owned handle.
                class Owned:
                    def __init__(self, process): self.process, self.pid = process, process.pid
                    def poll(self): return self.process.exitcode
                    def wait(self, timeout):
                        self.process.join(timeout)
                        if self.process.is_alive(): raise OSError('Parser process did not exit')
                handle = self._owned_processes.setdefault(process.pid, Owned(process))
                terminate(handle)
            pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def map(self, fn, items, *, chunksize: int = 1, min_items: int = 1, progress=None) -> list:
        if self._aborted:
            from app.backend.pipeline import stopping
            raise stopping.Paused('The shared parser was stopped; resume to retry unfinished commits')
        items = list(items)
        total = len(items)
        out = []
        if self._pool is None or total < min_items:
            for i, x in enumerate(items, 1):
                out.append(fn(x))
                if progress:
                    progress(i, total)
            return out
        for i, result in enumerate(self._pool.map(fn, items, chunksize=max(1, chunksize)), 1):
            out.append(result)
            if progress:
                progress(i, total)
        return out


def pmap(fn, items, *, workers=None, chunksize: int = 1, min_items: int = 8, initializer=None, initargs=(), progress=None) -> list:
    """``[fn(x) for x in items]`` computed by worker processes, results in input order. ``fn`` and the items must be picklable (``fn`` a
    top-level function). One worker, or fewer than ``min_items`` items, runs in this process. ``progress(done, total)`` is called as results
    arrive in order. An exception in ``fn`` is raised here, as in the serial loop."""
    items = list(items)
    n = cpu_workers(workers)
    if n <= 1 or len(items) < min_items:
        return Pool(1).map(fn, items, progress=progress)
    with Pool(min(n, len(items)), initializer=initializer, initargs=initargs) as pool:
        return pool.map(fn, items, chunksize=chunksize, progress=progress)


def tmap(fn, items, *, workers: int = 1, progress=None) -> list:
    """``[fn(x) for x in items]`` computed by ``workers`` threads, results in input order, for work that waits on a network or a model server
    (the GIL is released while it waits, so threads are enough and ``fn`` need not be picklable). One worker is a plain loop. ``progress(done,
    total)`` is called as results arrive in order; an exception in ``fn`` is raised here."""
    items = list(items)
    total = len(items)
    out = []
    if workers <= 1 or total <= 1:
        for i, x in enumerate(items, 1):
            out.append(fn(x))
            if progress:
                progress(i, total)
        return out
    with cf.ThreadPoolExecutor(max_workers=min(workers, total), thread_name_prefix="tmap") as pool:
        for i, result in enumerate(pool.map(fn, items), 1):
            out.append(result)
            if progress:
                progress(i, total)
    return out


def interleave(items, key) -> list:
    """The items one group at a time in turn (the first of each group, then the second of each, ...), groups in order of first appearance and each
    group's items in their own order. Work queued by repository or by registry then spreads over many hosts instead of hitting one at a time."""
    groups: dict = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    out, rounds = [], max((len(g) for g in groups.values()), default=0)
    for i in range(rounds):
        out.extend(g[i] for g in groups.values() if i < len(g))
    return out


# A child started from the windowed app must not open a console window of its own (it flashes on Windows). 0 elsewhere.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_text(cmd, **kw) -> subprocess.CompletedProcess:
    """``subprocess.run`` capturing the output as text. The child writes UTF-8 (the callers set PYTHONIOENCODING), but ``text=True`` decodes with
    the console's code page on Windows (cp1252), where a byte such as 0x8f raised inside the reader thread and left ``stdout`` as None."""
    kw.setdefault("creationflags", NO_WINDOW)
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", **kw)


def chunks(items, size: int) -> list[list]:
    """The items in consecutive slices of ``size`` (the last may be shorter), for work whose unit is a slice."""
    items = list(items)
    return [items[i:i + size] for i in range(0, len(items), size)]


# -- the GPU (through the CUDA interpreter; this process stays on the CPU build of torch) ----------------------------------------------------------

ENV_COMPUTE_GPU = "ZTSA_COMPUTE_GPU"
OFF_REASON = "Hardware acceleration for compute is turned off in Settings, so the graphics card is not used."


def gpu_enabled() -> bool:
    """False when the person turned hardware acceleration for compute off (Settings, Performance): nothing then probes, plans for or starts on the graphics card."""
    return os.environ.get(ENV_COMPUTE_GPU, "").strip().lower() not in ("off", "0", "false", "no")


def default_gpu_python() -> str | None:
    """The interpreter "Automatic" means: a ``.venv-gpu`` next to the code."""
    for rel in (".venv-gpu/Scripts/python.exe", ".venv-gpu/bin/python"):
        p = ROOT / rel
        if p.exists():
            return str(p)
    return None


def gpu_python() -> str | None:
    """The Python that has CUDA torch: ``ZTSA_GPU_PYTHON``, else a ``.venv-gpu`` next to the code."""
    given = os.environ.get(ENV_GPU_PYTHON, "").strip()
    if given:
        return given if pathlib.Path(given).exists() else None
    return default_gpu_python()


_PROBE = ("import json, torch\n"
          "ok = torch.cuda.is_available()\n"
          "out = {'torch': torch.__version__, 'cuda': ok, 'cuda_build': torch.version.cuda}\n"
          "if ok:\n"
          "    free, total = torch.cuda.mem_get_info()\n"
          "    p = torch.cuda.get_device_properties(0)\n"
          "    out.update(name=p.name, total_mb=round(total / 2**20), free_mb=round(free / 2**20), capability=list(torch.cuda.get_device_capability(0)))\n"
          "print(json.dumps(out))\n")
_GPU_CACHE: dict = {"at": 0.0, "value": None}
_GPU_LOCK = threading.Lock()


def gpu_info(max_age: float = 30.0, timeout: float = 25.0) -> dict:
    """What the GPU interpreter reports: ``{available, name, total_mb, free_mb, ...}`` or ``{available: False, reason}``. Cached for ``max_age`` s
    (importing torch takes seconds). The free memory is what is free now: the card is shared with the desktop."""
    if not gpu_enabled():
        return {"available": False, "reason": OFF_REASON}
    with _GPU_LOCK:
        if _GPU_CACHE["value"] is not None and time.monotonic() - _GPU_CACHE["at"] < max_age:
            return _GPU_CACHE["value"]
        exe = gpu_python()
        if not exe:
            value = {"available": False, "reason": "no GPU interpreter found (.venv-gpu) and none set"}
        else:
            try:
                p = subprocess.run([exe, "-c", _PROBE], capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
                info = json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 and p.stdout.strip() else {}
                value = ({"available": True, "python": exe, **info} if info.get("cuda")
                         else {"available": False, "python": exe, "reason": "torch sees no CUDA device" if info else (p.stderr.strip().splitlines() or ["the probe failed"])[-1][:200]})
            except Exception as e:
                value = {"available": False, "python": exe, "reason": f"{type(e).__name__}: {str(e)[:120]}"}
        _GPU_CACHE.update(at=time.monotonic(), value=value)
        return value


def python_candidates() -> list[dict]:
    """Every interpreter that could drive the local GPU jobs, each once: the virtual environments next to the code (``.venv``, ``.venv-gpu`` ...), the Python the app runs in and the one
    chosen in Settings. ``{path, label}``; nothing is run here."""
    found: dict[str, dict] = {}

    def add(path, label: str) -> None:
        key = os.path.normcase(os.path.abspath(str(path)))
        if key not in found and pathlib.Path(path).is_file():
            found[key] = {"path": str(path), "label": label}
    for folder in sorted(ROOT.glob(".venv*")):
        for rel in ("Scripts/python.exe", "bin/python"):
            add(folder / rel, folder.name)
    add(sys.executable, "the Python the app runs in")
    given = os.environ.get(ENV_GPU_PYTHON, "").strip()
    if given:
        add(given, "chosen in Settings")
    return list(found.values())


def probe_python(exe: str, timeout: float = 60.0) -> dict:
    """What one interpreter says about CUDA: ``{torch, cuda, cuda_build, name, total_mb, reason}``. ``reason`` says in a sentence why it cannot drive the card."""
    try:
        p = subprocess.run([exe, "-c", _PROBE], capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"cuda": False, "reason": f"it did not answer within {int(timeout)} s"}
    except OSError as e:
        return {"cuda": False, "reason": f"it could not be started ({type(e).__name__})"}
    lines = p.stdout.strip().splitlines()
    try:
        info = json.loads(lines[-1]) if p.returncode == 0 and lines else {}
    except ValueError:
        info = {}
    if not info:
        err = (p.stderr.strip().splitlines() or ["the probe failed"])[-1]
        return {"cuda": False, "reason": "torch is not installed in it" if "No module named 'torch'" in err else err[:160]}
    return {"torch": info.get("torch"), "cuda": bool(info.get("cuda")), "cuda_build": info.get("cuda_build"), "name": info.get("name"), "total_mb": info.get("total_mb"),
            "reason": None if info.get("cuda") else "torch is there but sees no CUDA device" + ("" if info.get("cuda_build") else " (a build without CUDA)")}


_PYTHONS_CACHE: dict = {"at": 0.0, "value": None}
_PYTHONS_LOCK = threading.Lock()


def python_choices(max_age: float = 60.0, refresh: bool = False) -> list[dict]:
    """The candidates with what each reports, the ones that can drive the card first. Probed side by side (importing torch takes seconds) and kept for ``max_age`` s."""
    with _PYTHONS_LOCK:
        if not refresh and _PYTHONS_CACHE["value"] is not None and time.monotonic() - _PYTHONS_CACHE["at"] < max_age:
            return _PYTHONS_CACHE["value"]
        candidates = python_candidates()
        with cf.ThreadPoolExecutor(max_workers=max(1, min(4, len(candidates)))) as pool:
            probes = list(pool.map(lambda c: probe_python(c["path"]), candidates))
        value = sorted(({**c, **pr} for c, pr in zip(candidates, probes)), key=lambda r: (not r["cuda"], r["label"]))
        _PYTHONS_CACHE.update(at=time.monotonic(), value=value)
        return value


def machine_info() -> dict:
    """For the Settings page: the cores, the memory and the worker count that would be used, and the GPU."""
    total, free = memory_gb()
    return {"physical_cores": physical_cores(), "logical_cores": logical_cores(), "ram_gb": total, "ram_free_gb": free,
            "cpu_workers": cpu_workers(), "auto_cpu_workers": auto_cpu_workers(), "max_cpu_workers": MAX_CPU_WORKERS}
