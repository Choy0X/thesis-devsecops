"""Which device a heavy job can run on, what each choice honestly means for it, and what it would need.

Three choices are always shown: the processor, the graphics card, and both. A choice a job has no real path for is shown disabled with a one-line reason, never faked: the table below
says, for every job that holds a model on a device itself, which of the three it supports (``None``) and why not (a sentence). Jobs that only send requests to the model gateway (the language model, the embedding model), that
read and write data, or that wait for a network have no choice here: the gateway places its models, and the others do not run a model.

The numbers are estimates and say so. The memory comes from the fit estimator (``fit.py``) for the model that would be loaded; the time is never predicted: it is the time this app
measured on earlier runs of the same job on that device, from the job records, or "not measured yet". A choice is never presented as faster than another.
"""
from __future__ import annotations

import json
import statistics

DEVICES = ("cpu", "gpu", "hybrid")
LABELS = {"cpu": "Processor", "gpu": "Graphics card", "hybrid": "Processor and card"}

NO_SPLIT_STEP = "One training or checking step cannot be shared by the processor and the card; only a model server can split a model's layers between them."
NO_CPU_TRAIN = "Training in 4 bits needs a CUDA card; on the processor the model would be held in 32 bits and one step would take hours."

# stage -> kind and, per device, None when the job has that path, else why not
TABLE: dict[str, dict] = {
    "train_local": {"kind": "train", "cpu": NO_CPU_TRAIN, "gpu": None, "hybrid": NO_SPLIT_STEP},
    "gpu_smoke": {"kind": "smoke", "cpu": None, "gpu": None, "hybrid": NO_SPLIT_STEP},
    "gpu_eval": {"kind": "eval", "cpu": None, "gpu": None, "hybrid": NO_SPLIT_STEP},
}
MEANS = {
    ("train", "gpu"): "The model is held in 4 bits on the card and trained there. Nothing else can use the card meanwhile.",
    ("smoke", "gpu"): "A few training steps of a small public model, held in 4 bits on the card.",
    ("smoke", "cpu"): "The same few steps with the small model held in 32 bits in memory and run on the processor. Slow, and a software check like the card's.",
    ("eval", "gpu"): "The untouched small model, in 4 bits on the card, answers a few validation views.",
    ("eval", "cpu"): "The untouched small model, in 32 bits in memory, answers the same views on the processor.",
}

# Jobs that can have a device chosen: the flag jobs.DEVICE_CAPABLE exposes.
DEVICE_CAPABLE: dict[str, tuple[str, ...]] = {stage: tuple(d for d in DEVICES if row[d] is None) for stage, row in TABLE.items()}


def is_heavy(stage: str) -> bool:
    return stage in TABLE


def validate(stage: str, device) -> str | None:
    """The device a job is asked to run on, checked: ``None`` for no choice; a ValueError with a one-line reason for a choice the job cannot honour."""
    if device in (None, ""):
        return None
    if stage not in TABLE:
        raise ValueError(f"{stage.replace('_', ' ')} has no choice of device: it does not run a model on the processor or the card itself.")
    if device not in DEVICES:
        raise ValueError(f"the device must be one of {', '.join(DEVICES)}, not {device!r}.")
    why = TABLE[stage][device]
    if why:
        raise ValueError(why)
    if device != "cpu":
        from app.backend import compute
        if not compute.gpu_enabled():
            raise ValueError(compute.OFF_REASON)
    return device


# -- the choices a person asked to be remembered ----------------------------------------------------------------------------------------------------

def remembered(raw: str | None) -> dict[str, str]:
    """The saved "remember for this job type" choices (a JSON object, stage -> device). Anything that is not a valid choice for its job is left out, never guessed."""
    try:
        data = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {stage: device for stage, device in data.items() if stage in TABLE and device in DEVICES and TABLE[stage][device] is None}


def normalise_remembered(raw: str) -> str:
    """The text to save for a set of remembered choices: refused with a sentence if a job or a device in it is not one, "" for none."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("The remembered choices must be a JSON object of job to device.")
    if not isinstance(data, dict):
        raise ValueError("The remembered choices must be a JSON object of job to device.")
    for stage, device in data.items():
        if stage not in TABLE:
            raise ValueError(f"{stage} has no choice of device to remember.")
        if device not in DEVICES or TABLE[stage][device] is not None:
            raise ValueError(f"{stage} cannot run on {device}: {TABLE[stage].get(device) or 'not a device'}")
    return json.dumps(data, sort_keys=True) if data else ""


# -- what was measured -----------------------------------------------------------------------------------------------------------------------------------

def measured(stage: str, device: str, records=None) -> dict | None:
    """The times of earlier finished runs of ``stage`` that ran on ``device`` (the device that was actually used, as the job record says), or None when there is none. Not a prediction:
    a different model or size takes a different time."""
    if records is None:
        records = _records()
    seconds = [r["compute"]["seconds"] for r in records
               if r.get("stage") == stage and r.get("status") == "done" and isinstance(r.get("compute"), dict) and r["compute"].get("used") == device
               and isinstance(r["compute"].get("seconds"), (int, float))]
    if not seconds:
        return None
    return {"runs": len(seconds), "median_seconds": round(statistics.median(seconds), 1), "last_seconds": round(seconds[-1], 1)}


def _records() -> list[dict]:
    from app.backend import jobs
    out = []
    for path in sorted(jobs.LOG_DIR.glob("*.json")) if jobs.LOG_DIR.exists() else []:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            out.append(doc)
    return sorted(out, key=lambda r: str(r.get("started") or ""))


# -- memory ----------------------------------------------------------------------------------------------------------------------------------------

def _gb(n) -> float | None:
    return None if n is None else round(n / 2 ** 30, 1)


def _model_for(stage: str, params: dict) -> str | None:
    from app.backend.pipeline import gpu_local
    if stage in ("gpu_smoke", "gpu_eval"):
        return params.get("model") or gpu_local.DEFAULT_MODEL
    if stage == "train_local":
        return params.get("model") or None
    return None


def memory(stage: str, device: str, params: dict, free_vram_mb: int | None = None, total_vram_mb: int | None = None) -> dict:
    """``{vram_gb, ram_gb, basis}`` for a choice, each None when it cannot be estimated, with a sentence saying what the figure is. Estimates, from the model's own configuration."""
    from app.backend.pipeline import fit, gpu_local
    kind = TABLE[stage]["kind"]
    run = params.get("run")
    model = _model_for(stage, params)
    if kind in ("smoke", "eval", "train") and device == "gpu":
        if not run:
            return {"vram_gb": None, "ram_gb": None, "basis": "Choose a run first: its context length and settings decide the memory."}
        from app.backend.pipeline import finetune_prep as fp
        target = model or (gpu_local._pinned_model(fp.RUNS / run) or gpu_local.DEFAULT_MODEL)
        gate = gpu_local.fit_gate("train" if kind == "train" else kind, target, fp.RUNS / run, free_vram_mb or 0, total_vram_mb)
        total = gate.get("estimate_bytes")
        return {"vram_gb": _gb(total), "ram_gb": None,
                "basis": f"{gate['message']} Estimated from the model's configuration; the run itself shows the real peak." if total else gate["message"]}
    if kind in ("smoke", "eval") and device == "cpu":
        shape = gpu_local._shape_of(model or gpu_local.DEFAULT_MODEL)
        if shape is None or not shape.params:
            return {"vram_gb": None, "ram_gb": None, "basis": "The model's size could not be read, so the memory is not estimated."}
        total = shape.params * 4 + int(fit.OVERHEAD_GB * fit.GB)
        return {"vram_gb": None, "ram_gb": _gb(total), "basis": f"The weights in 32 bits ({shape.params:,} parameters x 4 bytes) and a fixed overhead, before the activations; an estimate."}
    return {"vram_gb": None, "ram_gb": None, "basis": "Not estimated."}


# -- the choices -----------------------------------------------------------------------------------------------------------------------------------------

def _gpu_state() -> tuple[bool, str | None, dict]:
    """Whether the card can be used now, and if not the one-line reason; the card's numbers."""
    from app.backend import compute
    if not compute.gpu_enabled():
        return False, compute.OFF_REASON, {}
    info = compute.gpu_info()
    if not info.get("available"):
        return False, f"No usable graphics card: {info.get('reason') or 'unknown'}.", {}
    return True, None, info


def choices(stage: str, params: dict | None = None, records=None, gpu_state=None) -> dict:
    """Every one of the three choices for ``stage``, each with whether it is available now and why not, what it means for this job, and its estimates."""
    if stage not in TABLE:
        return {"stage": stage, "heavy": False, "choices": []}
    params = params or {}
    kind = TABLE[stage]["kind"]
    gpu_ok, gpu_reason, info = gpu_state if gpu_state is not None else _gpu_state()
    from app.backend.pipeline import gpu_modes
    out = []
    for device in DEVICES:
        why = TABLE[stage][device]
        if why is None and device != "cpu":
            if not gpu_ok:
                why = gpu_reason
            else:
                conflict = gpu_modes.conflict("train")
                if conflict:
                    why = conflict["message"]
        mem = memory(stage, device, params, info.get("free_mb"), info.get("total_mb")) if why is None or TABLE[stage][device] is None else {"vram_gb": None, "ram_gb": None, "basis": None}
        done = measured(stage, device, records)
        out.append({"device": device, "label": LABELS[device], "available": why is None, "reason": why,
                    "means": MEANS.get((kind, device)) if TABLE[stage][device] is None else None,
                    "estimate": {**mem, "time": done, "time_basis": (f"Measured: {done['runs']} earlier run(s) of this job on this device, median {done['median_seconds']} s. Not a prediction for this run."
                                                                     if done else "Not measured yet on this device: no time is promised.")}})
    from app.backend import compute
    return {"stage": stage, "heavy": True, "kind": kind, "choices": out, "workers": compute.cpu_workers()}
