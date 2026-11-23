"""A short run of the training runner on the local GPU, driven by the CUDA interpreter (``.venv-gpu``), as a software check.

What it is for: the machine has a GTX 1660 (6 GB, Turing, fp16 only). That is enough to see the QLoRA path, the 4-bit loader, the precision
choice and the evaluation harness work on a real GPU with a small public model, before any Colab units are spent. It is **not** a training
run: the model is a dev-only smoke model (never the pinned base ``Qwen/Qwen2.5-Coder-7B``, which does not fit in 6 GB with a useful context),
the run is marked ``smoke_test``, and nothing here changes the state of a prepared run. The outputs stay in the run folder; the Fine-tuning
page can import them like any other, under the same rules (a smoke adapter is never a trained control).

The runner is the repository's current one, applied to the bundle's data (the bundle carries the training code of the day it was prepared, which
does not have the dev-only smoke-model options; a run for the paper uses the bundle's own code, on Colab). The report says which commit ran.

The app's own interpreter keeps the CPU build of torch, so the tests and the pipeline never change device by accident; this module only starts
the other interpreter as a child process. Because the card is shared with the desktop, it checks the memory that is free right now, and a
lock file makes sure that only one GPU job runs at a time, across processes.

Which model may run is decided by a fit gate, not by its name: the memory the model's weights and adapters need at the very least (from its own
configuration) must fit in the free memory of the card, else the job refuses and says by how much. The estimate for the full context is shown as a
warning, because the views of a run are mostly shorter than the longest the context allows. The pinned base model is trained for real when it fits (it
does not on a 6 GB card); any other model is a software check and is marked so.

``train`` is the longer job: a stable folder per (model, bundle) so that a run stopped on its session budget, cancelled or cut by a power failure resumes
from its last checkpoint (the runner writes each one into a temporary folder and renames it, so a kill never leaves a half checkpoint), the runner's
output streamed line by line, and the step and loss reported from ``metrics.jsonl`` while it runs.
"""
from __future__ import annotations
from app.backend.pipeline.finetune_deletion import guarded

import collections
import contextlib
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

from app.backend import compute
from app.backend.pipeline import finetune_prep as fp
from app.backend.pipeline import fit, fsio
from app.backend.pipeline.gateway import client as gateway
from app.backend.pipeline.gateway.errors import GatewayError
from training.finetune import bundle as bd, resolve as rs
from app.backend.pipeline import util

KINDS = ("smoke", "eval")
DEFAULT_MODEL = "Qwen/Qwen2.5-Coder-0.5B"
MIN_FREE_MB = 2000                       # the measured peak of the 0.5B smoke was 1.24 GB; the rest is headroom for the desktop
LOCK = fp.RUNS / ".gpu.lock"
NOTE = ("A software check on a small public model, not a training result and not the pinned base model. It does not change the state of the "
        "run; its outputs can be imported on the Fine-tuning page under the usual rules.")
MB = 2 ** 20
POLL_SECONDS = 2.0                       # how often the step and loss of a running job are read from metrics.jsonl
TAIL_LINES = 40


class GpuBusy(RuntimeError):
    """Another GPU job holds the lock, or the card does not have the memory free."""


class FitRefused(RuntimeError):
    """The model cannot fit on this card even at the very least; the message says how much it needs and how much is free."""


def _pid_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:  # ignored: psutil missing or failing: fall back to os.kill below
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


class Lock:
    """One GPU job at a time, across processes: a file created exclusively and holding the owner's pid. A lock whose owner is gone is taken over."""

    def __init__(self, path=LOCK):
        self.path = pathlib.Path(path)

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    owner = int(self.path.read_text().strip() or 0)
                except (OSError, ValueError):
                    owner = 0
                if owner and _pid_alive(owner):
                    raise GpuBusy(f"another GPU job is running (process {owner})") from None
                try:
                    self.path.unlink()                    # its owner is gone: take it over
                except OSError:
                    pass
        raise GpuBusy("could not take the GPU lock")

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


@contextlib.contextmanager
def card_for_job(what: str):
    """Take the graphics card for a job from the model gateway: the language model and the embedding model are unloaded once the requests they are answering have finished (none is
    aborted, and the job waits for as long as that takes: nobody knows how long a model will think), the gateway refuses new requests meanwhile and until the job is done, and the models
    load again when they are asked for. The card is marked even when Ollama does not answer, so an Ollama started later cannot load a model meanwhile. The lease ends with
    the job, or with the job's process if that dies, and a job that is cancelled while it waits takes nothing."""
    owner = f"{what} (process {os.getpid()})"
    try:
        gateway.begin_exclusive(owner, os.getpid())
    except GatewayError as e:
        raise GpuBusy(f"The model gateway could not free the graphics card: {e.message}") from e
    try:
        yield
    finally:
        gateway.end_exclusive(owner)


# -- the fit gate ----------------------------------------------------------------------------------------------------------------------------------------

def _pinned_model(run_dir) -> str | None:
    """The base model the run's resolved configuration pins (None when the run has none)."""
    try:
        return rs.read_resolved(run_dir, verify_contract=False).model.id
    except Exception:  # ignored: an unreadable resolved config is reported by the callers that need it; here the model is simply not known to be the pinned one
        return None


def _run_numbers(run_dir) -> dict:
    """The context length, micro-batch, adapter rank and whether the 16-bit weights are cast to 32 bits (``prepare_model_for_kbit_training``) of the run's resolved configuration,
    for the estimate (stated defaults, the contract's, when it cannot be read)."""
    try:
        cfg = rs.read_resolved(run_dir, verify_contract=False)
        return {"context": cfg.context.max_total_tokens, "micro_batch": cfg.training.per_device_train_batch_size, "rank": cfg.lora.r,
                "upcast": bool(cfg.precision.prepare_model_for_kbit_training), "from": "the run", "mode": getattr(cfg.precision, 'mode', 'nf4')}
    except Exception:  # ignored: a run folder without a readable resolved config still gets an estimate, from the contract's usual numbers
        return {"context": 4096, "micro_batch": 1, "rank": 32, "upcast": True, "from": "the usual settings"}


def _shape_of(model_id: str):
    """The dimensions of a model from its own configuration: the copy on this computer when there is one, else the Hub's metadata (a few kilobytes).
    None when neither can be read; the gate then cannot judge and says so."""
    from app.backend.pipeline import hub_search, models_local as ml
    try:
        local = next((m for m in ml.inventory()["models"] if m["id"] == model_id and m["source"] == "hub_cache"), None)
        if local:
            shape, _ = ml.read_shape(local)
            if shape.params and shape.n_layers:
                return shape
    except Exception:  # ignored: the cache could not be read; the Hub is asked next
        pass
    try:
        return hub_search.fit_inputs(model_id)[0]
    except Exception:  # ignored: no internet and no local copy; the gate reports that the model could not be judged
        return None


def fit_gate(kind: str, model_id: str, run_dir, free_mb: int, total_mb: int | None = None) -> dict:
    """Whether ``model_id`` can be run as ``kind`` ("smoke" and "train" fine-tune it with QLoRA, "eval" only answers) in ``free_mb`` of video memory.

    ``refuse`` is true only when the least the job can need (the base weights, 4-bit for the layers and 32-bit for the embedding table and head, the adapters with their optimizer for a fine-tune, and the fixed overhead)
    is more than what is free: that cannot work whatever the sequences are. The estimate at the full context length is the worst case; when it is over the
    free memory the verdict is ``tight`` or ``too_big`` and the job is allowed, with the warning in ``message``, because most views are shorter than the
    context. Every number is an estimate (see ``fit.py``). ``unknown`` means the model's dimensions could not be read."""
    nums = _run_numbers(run_dir)
    shape = _shape_of(model_id)
    out = {"model": model_id, "kind": kind, "free_mb": free_mb, "total_mb": total_mb, "context": nums["context"], "micro_batch": nums["micro_batch"],
           "rank": nums["rank"], "refuse": False}
    if shape is None:
        return {**out, "verdict": "unknown", "message": f"The dimensions of {model_id} could not be read (no copy on this computer and the Hub could not be reached), so "
                                                         "whether it fits is not known; the run itself will stop if it does not."}
    try:
        est = (fit.cpu_offload_estimate(shape, nums["context"], nums["micro_batch"], nums["rank"]) if nums.get("mode") == "cpu_weight_offload" else fit.qlora_estimate(shape, nums["context"], nums["micro_batch"], nums["rank"], mlp=shape.intermediate is not None, upcast_16bit=nums["upcast"]))
    except fit.MissingDimension as e:
        return {**out, "verdict": "unknown", "message": f"{e}, so whether {model_id} fits is not known."}
    if nums.get('mode') == 'cpu_weight_offload':
        import psutil
        out.update(experimental=True, host_required_bytes=est['host_required'])
        if psutil.virtual_memory().available < est['host_required']:
            return {**out, 'verdict': 'cannot_fit', 'refuse': True,
                    'message': f"CPU weight offload requires about {est['host_required'] / fit.GB:.1f} GiB of free system RAM. The selected model, context and rank remain unchanged."}
    least = est["weights"] + est["overhead"] + (est["adapters_and_optimizer"] if kind != "eval" else 0)
    free = free_mb * MB
    gb = lambda n: f"{n / 2 ** 30:.1f} GB"
    offload = nums.get('mode') == 'cpu_weight_offload'
    out.update(least_bytes=least, estimate_bytes=est["total"], parts=est,
               weights_parts={'resident_head_and_projection': est['weights']} if offload else fit.qlora_weights(shape, nums["upcast"]))
    if offload:
        return {**out, 'verdict': 'cannot_fit' if least > free else fit.verdict(est['total'], free), 'refuse': least > free,
                'experimental': True, 'hardware_validated': False,
                'message': f"CPU weight offload estimates {gb(est['total'])} GPU memory and {gb(est['host_required'])} host RAM. "
                           'Weights and optimizer state move through CPU RAM. A selected-model smoke check is required; speed and fit are not measured.'}
    if least > free:
        return {**out, "verdict": "cannot_fit", "refuse": True,
                "message": f"{model_id} needs at least {gb(least)} of video memory for its weights and adapters (its embedding table and output head stay 16-bit or larger when the "
                           f"rest is loaded in 4 bits), and {gb(free)} is free: it cannot run on this card. "
                           "Train it on Colab or Kaggle with the notebook, or choose a smaller model."}
    verdict = fit.verdict(est["total"], free)
    note = {"fits": f"The estimate at the full context of {nums['context']} tokens is {gb(est['total'])}, which fits in the {gb(free)} that is free.",
            "tight": f"The estimate at the full context of {nums['context']} tokens is {gb(est['total'])}, which fits in the {gb(free)} that is free only without room to spare.",
            "too_big": f"The estimate at the full context of {nums['context']} tokens is {gb(est['total'])}, more than the {gb(free)} that is free; views shorter than the "
                       "context need less, and the run stops with an out-of-memory message if one is too long."}[verdict]
    return {**out, "verdict": verdict, "message": note}


def command(kind: str, python: str, work: pathlib.Path, out: pathlib.Path, *, model: str, steps: int, stage: int, eval_max: int | None,
            max_views: int, batch_size: int, device: str = "gpu") -> list[str]:
    """The runner's command line for a smoke training run or for a smoke evaluation of the untouched model; ``device`` "cpu" holds the model whole in 32 bits on the processor."""
    base = [python, "-m", "training.finetune.runner", "--bundle", str(work), "--run-dir", str(out), "--smoke-model", model]
    if device == "cpu":
        base += ["--device", "cpu"]
    if kind == "smoke":
        return base + ["--steps", str(steps), "--stage", str(stage)] + (["--eval-max", str(eval_max)] if eval_max else [])
    return base + ["--evaluate", "--baseline", "B0", "--max-views", str(max_views), "--batch-size", str(batch_size), "--tasks", "discovery,repair"]


@guarded
def run(kind: str, *, run_dir, model: str = DEFAULT_MODEL, steps: int = 3, stage: int = 1, eval_max: int | None = 16, max_views: int = 8,
        batch_size: int = 2, python: str | None = None, timeout: int = 3600, min_free_mb: int = MIN_FREE_MB, device: str = "gpu") -> dict:
    """Unpack the run's bundle in a fresh folder and run the runner there on the GPU (or, with ``device`` "cpu", on the processor with the model held whole in 32 bits);
    the report is written beside the outputs."""
    if kind not in KINDS:
        raise ValueError(f"unknown GPU job {kind!r}; it is one of {KINDS}")
    if device not in ("gpu", "cpu"):
        raise ValueError(f"the device is gpu or cpu, not {device!r}")
    run_dir = pathlib.Path(run_dir)
    prepared = util.read_json(run_dir / fp.PREPARED)
    if not prepared:
        raise FileNotFoundError(f"{run_dir / fp.PREPARED} does not exist: prepare the run first")
    problems = bd.verify_bundle(run_dir / "bundle.zip")
    if problems:
        raise bd.BundleError("the bundle does not verify: " + "; ".join(problems))
    if device == "cpu":
        python = python or sys.executable                       # the app's own interpreter has the processor build of torch, transformers and peft
        return _run_checked(kind, run_dir, prepared, python, {}, cpu_gate(model), model=model, steps=steps, stage=stage, eval_max=eval_max, max_views=max_views,
                            batch_size=batch_size, timeout=timeout, device=device)
    python = python or compute.gpu_python()
    if not python:
        raise RuntimeError("no GPU interpreter: set one in Settings > Compute, or create .venv-gpu with CUDA torch")
    probe = compute.gpu_info(max_age=0)
    if not probe.get("available"):
        raise RuntimeError(f"the GPU is not available: {probe.get('reason') or 'unknown'}")
    with card_for_job(f"the {kind} check"):
        info = compute.gpu_info(max_age=0)                      # read again: whatever the gateway's models held is free now
        if info["free_mb"] < min_free_mb:
            raise GpuBusy(f"only {info['free_mb']} MB of the GPU's {info['total_mb']} MB are free (the card is shared with the desktop); "
                          f"close what uses it, or lower min_free_mb. This check needs about {min_free_mb} MB.")
        return _run_checked(kind, run_dir, prepared, python, info, fit_gate(kind, model, run_dir, info["free_mb"], info.get("total_mb")), model=model, steps=steps, stage=stage,
                            eval_max=eval_max, max_views=max_views, batch_size=batch_size, timeout=timeout, device=device)


def _run_checked(kind, run_dir, prepared, python, info, gate, *, model, steps, stage, eval_max, max_views, batch_size, timeout, device) -> dict:
    """The part of ``run`` after the memory has been checked: unpack the bundle, run the runner, write the report."""
    if gate["refuse"]:
        raise FitRefused(gate["message"])
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = run_dir / "local_gpu" / f"{kind}-{stamp}"
    work, out = base / "bundle", base / "run"
    with (Lock() if device == "gpu" else contextlib.nullcontext()):         # one job at a time holds the card; a run on the processor does not
        bd.unpack_bundle(run_dir / "bundle.zip", work)
        cmd = command(kind, python, work.resolve(), out.resolve(), model=model, steps=steps, stage=stage, eval_max=eval_max,
                      max_views=max_views, batch_size=batch_size, device=device)
        env = {**os.environ, "PYTHONPATH": str(compute.ROOT), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "ZTSA_COMPUTE_CHOICE": device}
        if device == "cpu":
            env["CUDA_VISIBLE_DEVICES"] = ""                      # the child never touches the card, even where one is installed
        started = time.monotonic()
        proc = compute.run_text(cmd, cwd=compute.ROOT, env=env, timeout=timeout)
        seconds = time.monotonic() - started
    fsio.write_text(base / "output.log", proc.stdout + "\n" + proc.stderr)
    config = util.read_json(out / "run_config.json") or util.read_json(out / "evaluation_config.json") or {}
    profile = util.read_json(out / "profile.json") or {}
    files = sorted(p.name for p in out.iterdir()) if out.exists() else []
    report = {"schema": "local-gpu/1", "kind": kind, "model": model, "returncode": proc.returncode, "seconds": round(seconds, 1), "device": device,
              "passed": proc.returncode == 0 and bool(files), "smoke_test": config.get("smoke_test"), "run_state": config.get("state"),
              "gpu": {k: info.get(k) for k in ("name", "total_mb", "free_mb", "capability")} if device == "gpu" else None, "python": python,
              "peak_vram_mb": round(profile["peak_reserved_bytes"] / 2 ** 20) if profile.get("peak_reserved_bytes") else None,
              "tokens_per_second": round(profile["tokens_per_second"], 1) if profile.get("tokens_per_second") else None,
              "outputs": files, "folder": str(out), "bundle_sha256": prepared["bundle"]["sha256"], "runner": "repository",
              "code_version": _code_version(), "fit": {k: gate[k] for k in ("verdict", "message")},
              "output_tail": "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-20:]), "note": NOTE, "checked_at": util.now_utc()}
    fsio.write_json(base / "report.json", report, indent=2)
    return report


def cpu_gate(model_id: str) -> dict:
    """Whether the model held whole in 32 bits fits in the memory that is free: refused only when the weights alone (and a fixed overhead) are more than what is free, which cannot
    work whatever the sequences are. An estimate, from the model's own configuration; ``unknown`` when it cannot be read."""
    shape = _shape_of(model_id)
    if shape is None or not shape.params:
        return {"refuse": False, "verdict": "unknown", "message": f"The size of {model_id} could not be read, so whether it fits in memory is not known; the run itself will stop if it does not."}
    need = shape.params * 4 + int(fit.OVERHEAD_GB * fit.GB)
    _, free_gb = compute.memory_gb()
    gb = lambda n: f"{n / 2 ** 30:.1f} GB"
    if free_gb is not None and need > free_gb * 2 ** 30:
        return {"refuse": True, "verdict": "cannot_fit", "message": f"{model_id} held whole in 32 bits needs at least {gb(need)} of memory, and {free_gb:.1f} GB is free: it cannot run on the processor here."}
    return {"refuse": False, "verdict": "fits", "message": f"The weights of {model_id} in 32 bits are about {gb(need)} (an estimate), before the activations."}


def _code_version() -> str | None:
    try:
        out = compute.run_text(["git", "rev-parse", "--short", "HEAD"], cwd=compute.ROOT, timeout=10)
        return out.stdout.strip() or None
    except Exception:  # ignored: git may be missing; the commit is recorded as unknown
        return None


# -- training on this computer ---------------------------------------------------------------------------------------------------------------------------

TRAIN_NOTE = ("Trained on this computer's graphics card with the repository's runner. A model other than the run's pinned base is a software check, "
              "never a result for the thesis; the pinned base is a real run only when it fits, which a 6 GB card does not allow.")
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def train_folder(run_dir, model: str, bundle_sha256: str) -> pathlib.Path:
    """The stable folder of the local training of one model on one bundle: the same call again finds its checkpoints and resumes from them."""
    return pathlib.Path(run_dir) / "local_gpu" / f"train-{_SLUG.sub('-', model).strip('-')}-{bundle_sha256[:8]}"


def train_command(python: str, work: pathlib.Path, out: pathlib.Path, *, model: str | None, steps: int | None, stage: int, eval_max: int | None,
                  session_minutes: float | None, loss_mode: str | None) -> list[str]:
    """The runner's command line for a local training run. ``model`` None means the run's pinned base (no smoke-model flag); the runner resumes by default."""
    cmd = [python, "-m", "training.finetune.runner", "--bundle", str(work), "--run-dir", str(out), "--stage", str(stage)]
    if model:
        cmd += ["--smoke-model", model]
    for flag, value in (("--steps", steps), ("--eval-max", eval_max), ("--session-minutes", session_minutes), ("--loss-mode", loss_mode)):
        if value:
            cmd += [flag, str(value)]
    return cmd


def _spawn(cmd, **kw) -> subprocess.Popen:
    """The runner as a child whose output is read line by line (a seam for the tests)."""
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=compute.NO_WINDOW, **kw)


def checkpoint_step(out: pathlib.Path) -> int:
    """The step of the newest complete checkpoint of a training folder (0 when there is none): where a run started again continues from. A folder still
    named ``.tmp-ckpt-...`` is a write that did not finish and does not count."""
    steps = [int(m.group(1)) for p in (out / "checkpoints").glob("ckpt-step*") if (m := re.fullmatch(r"ckpt-step([0-9]+)", p.name))]
    return max(steps, default=0)


def read_progress(out: pathlib.Path) -> dict | None:
    """The newest step of a running or finished training from ``metrics.jsonl``, with the total from ``run_config.json``; None before the first step."""
    try:
        lines = (out / "metrics.jsonl").read_text(encoding="utf-8").strip().splitlines()
        last = json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None
    if not last:
        return None
    config = util.read_json(out / "run_config.json") or {}
    return {"step": last.get("step"), "total": config.get("steps"), "loss": last.get("loss"), "lr": last.get("lr"), "family_pass": last.get("family_pass")}


class _Watcher(threading.Thread):
    """Reads the step and loss from ``metrics.jsonl`` every few seconds and hands them to ``on_progress`` while the runner is running."""

    def __init__(self, out: pathlib.Path, on_progress, started: float, first_step: int):
        super().__init__(daemon=True)
        self.out, self.on_progress, self.started, self.first_step = out, on_progress, started, first_step
        self.stop_event, self.seen = threading.Event(), None

    def tick(self) -> None:
        p = read_progress(self.out)
        if not p or p["step"] == self.seen:
            return
        self.seen = p["step"]
        elapsed = max(time.monotonic() - self.started, 1e-6)
        label = f"Training step {p['step']}" + (f" of {p['total']}" if p.get("total") else "") + (f", loss {p['loss']:.3f}" if isinstance(p["loss"], (int, float)) else "")
        self.on_progress(state="training", label=label, processed=p["step"], total=p.get("total"), loss=p["loss"],
                         steps_this_call=p["step"] - self.first_step, seconds=round(elapsed))

    def run(self) -> None:
        while not self.stop_event.wait(POLL_SECONDS):
            self.tick()

    def finish(self) -> None:
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=5)
        self.tick()


@guarded
def train(*, run_dir, model: str | None = None, steps: int | None = None, stage: int = 1, eval_max: int | None = 16, session_minutes: float | None = None,
          loss_mode: str | None = None, python: str | None = None, min_free_mb: int = MIN_FREE_MB, on_line=lambda line: None,
          on_progress=lambda **state: None) -> dict:
    """Train a prepared run on the local GPU: the runner on the bundle's data, in a folder that is the same on every call so that it resumes.

    ``model`` None (or the run's pinned base) trains the base itself, which the fit gate refuses unless it fits; any other model is a software check
    (``--smoke-model``). Returns the report, also written to ``report.json`` in the folder; ``complete`` is true when the runner finished the run, false
    when it stopped on its session budget or was interrupted (run it again to continue)."""
    run_dir = pathlib.Path(run_dir)
    prepared = util.read_json(run_dir / fp.PREPARED)
    if not prepared:
        raise FileNotFoundError(f"{run_dir / fp.PREPARED} does not exist: prepare the run first")
    problems = bd.verify_bundle(run_dir / "bundle.zip")
    if problems:
        raise bd.BundleError("the bundle does not verify: " + "; ".join(problems))
    python = python or compute.gpu_python()
    if not python:
        raise RuntimeError("no GPU interpreter: set one in Settings > Compute, or create .venv-gpu with CUDA torch")
    info = compute.gpu_info(max_age=0)
    if not info.get("available"):
        raise RuntimeError(f"the GPU is not available: {info.get('reason') or 'unknown'}")
    with card_for_job("the local training"):
        info = compute.gpu_info(max_age=0)                      # read again: whatever the gateway's models held is free now
        return _train_held(run_dir, prepared, python, info, model=model, steps=steps, stage=stage, eval_max=eval_max, session_minutes=session_minutes, loss_mode=loss_mode,
                           min_free_mb=min_free_mb, on_line=on_line, on_progress=on_progress)


def _train_held(run_dir, prepared, python, info, *, model, steps, stage, eval_max, session_minutes, loss_mode, min_free_mb, on_line, on_progress) -> dict:
    """The part of ``train`` that runs with the card taken from the model gateway."""
    pinned = _pinned_model(run_dir)
    real = model is None or model == pinned
    model_id = pinned if real and pinned else (model or DEFAULT_MODEL)
    if real and not pinned:
        raise RuntimeError("the run's resolved configuration cannot be read, so its pinned base model is not known: prepare the run again")
    gate = fit_gate("train", model_id, run_dir, info["free_mb"], info.get("total_mb"))
    if gate["refuse"]:
        raise FitRefused(gate["message"])
    base = train_folder(run_dir, model_id, prepared["bundle"]["sha256"])
    work, out = base / "bundle", base / "run"
    with Lock():
        if info["free_mb"] < min_free_mb:
            raise GpuBusy(f"only {info['free_mb']} MB of the GPU's {info['total_mb']} MB are free (the card is shared with the desktop); close what uses it. "
                          f"A run needs about {min_free_mb} MB.")
        if not (work / "bundle_manifest.json").exists():
            bd.unpack_bundle(run_dir / "bundle.zip", work)
        out.mkdir(parents=True, exist_ok=True)
        first_step = checkpoint_step(out)                  # the steps after the newest checkpoint are done again, and the runner drops their metric lines
        report = {"schema": "local-gpu/1", "kind": "train", "model": model_id, "pinned_base": real, "state": "running", "passed": None, "complete": False,
                  "resumed_from_step": first_step or None, "folder": str(out), "gpu": {k: info.get(k) for k in ("name", "total_mb", "free_mb", "capability")},
                  "python": python, "bundle_sha256": prepared["bundle"]["sha256"], "runner": "repository", "code_version": _code_version(),
                  "fit": {k: gate[k] for k in ("verdict", "message")}, "note": TRAIN_NOTE, "started_at": util.now_utc()}
        fsio.write_json(base / "report.json", report, indent=2)
        cmd = train_command(python, work.resolve(), out.resolve(), model=None if real else model_id, steps=steps, stage=stage, eval_max=eval_max,
                            session_minutes=session_minutes, loss_mode=loss_mode)
        env = {**os.environ, "PYTHONPATH": str(compute.ROOT), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        started = time.monotonic()
        tail: collections.deque = collections.deque(maxlen=TAIL_LINES)
        watcher = _Watcher(out, on_progress, started, first_step)
        proc = _spawn(cmd, cwd=compute.ROOT, env=env)
        try:
            with (base / "output.log").open("a", encoding="utf-8") as log:
                log.write(f"\n--- {util.now_utc()}: {' '.join(cmd[2:4])}, from step {first_step} ---\n")
                for line in (proc.stdout or ()):
                    line = line.rstrip("\n")
                    tail.append(line)
                    log.write(line + "\n")
                    on_line(line)
                    if not watcher.is_alive() and "training from step" in line:          # the runner has dropped the lines of the steps it will redo
                        watcher.start()
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
            watcher.finish()
    seconds = time.monotonic() - started
    config = util.read_json(out / "run_config.json") or {}
    profile = util.read_json(out / "profile.json") or {}
    now = read_progress(out)
    state = config.get("state")
    report.update(returncode=proc.returncode, seconds=round(seconds, 1), state=state or ("failed" if proc.returncode else "unknown"),
                  passed=proc.returncode == 0, complete=proc.returncode == 0 and state in ("finished", "early_stopped"),
                  steps_done=now["step"] if now else 0, steps=config.get("steps"), smoke_test=config.get("smoke_test"),
                  interrupted_by=config.get("interrupted_by"), loss=now["loss"] if now else None,
                  peak_vram_mb=round(profile["peak_reserved_bytes"] / 2 ** 20) if profile.get("peak_reserved_bytes") else None,
                  tokens_per_second=round(profile["tokens_per_second"], 1) if profile.get("tokens_per_second") else None,
                  outputs_zip=str(out / "outputs.zip") if (out / "outputs.zip").exists() else None,
                  output_tail="\n".join(tail), finished_at=util.now_utc())
    fsio.write_json(base / "report.json", report, indent=2)
    return report


def lock_alive() -> bool:
    """Whether a GPU job is running now (the lock exists and its owner is alive)."""
    try:
        return _pid_alive(int(LOCK.read_text().strip() or 0))
    except (OSError, ValueError):
        return False


def latest(run_dir, kind: str | None = None) -> dict | None:
    """The newest report of the local GPU jobs of a run (of one ``kind`` when given), for the Fine-tuning page. A training report that still says it is running, with no GPU job alive,
    belongs to a run that was cancelled or cut short: it is marked ``stopped`` so the page can say it resumes from its last checkpoint."""
    root = pathlib.Path(run_dir) / "local_gpu"
    reports = [r for r in (util.read_json(p) for p in root.glob("*/report.json")) if r] if root.exists() else []
    reports = [r for r in reports if kind is None or r.get("kind") == kind]
    if not reports:
        return None
    newest = max(reports, key=lambda r: r.get("finished_at") or r.get("started_at") or r.get("checked_at") or "")
    if newest.get("state") == "running" and not lock_alive():
        newest = {**newest, "state": "stopped", "passed": False}
        p = read_progress(pathlib.Path(newest["folder"])) if newest.get("folder") else None
        newest["steps_done"] = p["step"] if p else 0
    return newest
