"""Matched, isolated optimizer-update comparisons with complete main-run restoration."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import os
import pathlib
import time

import torch

from training.finetune.analytics import AnalyticsWriter

VERSION = "controller-benchmark/1"
_MISSING = object()
_TRAINER_FIELDS = (
    "counters", "history", "meta", "evaluations", "best", "stopped", "chunk", "micro_batch_size",
    "batch_schedule", "_last_save_seconds", "_last_ckpt_time", "_defer_metrics", "_prepared_batch", "_loss_metadata",
    "_active_choice", "_active_length", "_benchmark_active", "_controller_baseline", "_draw_groups",
)


def _clone(value):
    if torch.is_tensor(value):
        return value.detach().cpu().clone()
    if isinstance(value, dict):
        return type(value)((key, _clone(item)) for key, item in value.items())
    if isinstance(value, list):
        return [_clone(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clone(item) for item in value)
    return copy.deepcopy(value)


def _plain(value):
    if dataclasses.is_dataclass(value):
        return _plain(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "to_dict"):
        return _plain(value.to_dict())
    if hasattr(value, "__dict__"):
        return _plain({key: item for key, item in vars(value).items() if not key.startswith("_")})
    return str(value)


def _identity(trainer, version):
    files = ("controller_benchmark.py", "resource_controller.py", "online_training.py",
             "resource_metrics.py", "staging.py", "analytics.py", "trainer.py", "runner.py",
             "losses.py", "collate.py", "accelerator.py", "throughput.py", "sampler.py",
             "model.py", "head.py", "offload.py", "tokenization.py")
    sources = {name: hashlib.sha256(pathlib.Path(__file__).with_name(name).read_bytes()).hexdigest()
               for name in files}
    sampler = trainer.sampler.state_dict()
    return dict(schema=VERSION, controller_schema=trainer.controller.VERSION, runtime_version=version,
        source_hashes=sources, hardware=_plain(trainer.controller.monitor.hardware_signature),
        identity={key: trainer.meta.get(key) for key in ("base_commit", "tokenizer_revision", "serialization_hash",
                  "dataset_manifest_sha256", "config_sha256", "stage", "seed")},
        sampler_fingerprint=sampler.get("fingerprint"), training=_plain(trainer.training),
        loss=_plain(getattr(trainer, "loss_cfg", None)), lora=_plain(getattr(trainer, "lora", None)),
        model_config=_plain(getattr(trainer.model, "config", None)),
        trainable_parameters=[(name, list(param.shape), str(param.dtype))
                              for name, param in trainer.model.named_parameters() if param.requires_grad],
        head_parameters=[(name, list(param.shape), str(param.dtype)) for name, param in trainer.head.named_parameters()],
        chunk=getattr(trainer.training, "lm_loss_chunk_tokens", trainer.controller.chunk if hasattr(trainer.controller, "chunk") else trainer.chunk),
        micro_batch_size=getattr(trainer.training, "per_device_train_batch_size", trainer.controller.safe_batch),
        controller_mode=trainer.controller.mode, pack=bool(getattr(trainer, "pack", False)))


def _object_state(obj, references=()):
    return {key: value if key in references else _clone(value) for key, value in vars(obj).items()}


def _restore_object(obj, state, references=()):
    obj.__dict__.clear()
    obj.__dict__.update({key: value if key in references else _clone(value) for key, value in state.items()})


def _snapshot(trainer):
    trainer.backend.synchronize()
    parameters = {id(param): param for param in trainer.model.parameters() if param.requires_grad}
    parameters.update({id(param): param for param in trainer.head.parameters()})
    return dict(parameters=[(param, _clone(param), _clone(param.grad)) for param in parameters.values()],
        head_buffers=[(buffer, _clone(buffer)) for buffer in trainer.head.buffers()],
        modes=[(module, module.training) for owner in (trainer.model, trainer.head) for module in owner.modules()],
        checkpointing=bool(getattr(trainer.model, "is_gradient_checkpointing", False)),
        optimizer=_clone(trainer.optimizer.state_dict()), scheduler=_clone(trainer.scheduler.state_dict()),
        scaler=_clone(trainer.scaler.state_dict()) if trainer.scaler is not None else None,
        sampler=_clone(trainer.sampler.state_dict()), rng=_clone(trainer.backend.rng_state()),
        trainer={name: _clone(trainer.__dict__[name]) if name in trainer.__dict__ else _MISSING
                 for name in _TRAINER_FIELDS if name != "_draw_groups"},
        draw=trainer.__dict__.get("_draw_groups", _MISSING),
        controller=_object_state(trainer.controller, ("monitor", "clock")),
        monitor=_object_state(trainer.controller.monitor, ("backend", "clock", "_nvml", "_handle", "_process")),
        run_dir=trainer.run_dir, analytics=trainer.analytics, stager=getattr(trainer, "stager", _MISSING))


def _restore(trainer, saved):
    trainer.backend.synchronize()
    with torch.no_grad():
        for param, value, grad in saved["parameters"]:
            param.copy_(value)
            param.grad = None if grad is None else grad.to(device=param.device, dtype=param.dtype).clone()
        for buffer, value in saved["head_buffers"]:
            buffer.copy_(value)
    trainer.optimizer.load_state_dict(_clone(saved["optimizer"]))
    trainer.scheduler.load_state_dict(_clone(saved["scheduler"]))
    if trainer.scaler is not None:
        trainer.scaler.load_state_dict(_clone(saved["scaler"]))
    trainer.sampler.load_state_dict(_clone(saved["sampler"]))
    for name, value in saved["trainer"].items():
        if value is _MISSING:
            trainer.__dict__.pop(name, None)
        else:
            setattr(trainer, name, _clone(value))
    if saved["draw"] is _MISSING:
        trainer.__dict__.pop("_draw_groups", None)
    else:
        trainer._draw_groups = saved["draw"]
    trainer.run_dir, trainer.analytics = saved["run_dir"], saved["analytics"]
    if saved["stager"] is _MISSING:
        trainer.__dict__.pop("stager", None)
    else:
        trainer.stager = saved["stager"]
    _restore_object(trainer.controller, saved["controller"], ("monitor", "clock"))
    _restore_object(trainer.controller.monitor, saved["monitor"], ("backend", "clock", "_nvml", "_handle", "_process"))
    checkpointing = saved["checkpointing"]
    if hasattr(trainer.model, "gradient_checkpointing_enable"):
        from training.finetune.throughput import _checkpointing
        _checkpointing(trainer.model, checkpointing)
    elif hasattr(trainer.model, "is_gradient_checkpointing"):
        trainer.model.is_gradient_checkpointing = checkpointing
    for module, mode in saved["modes"]:
        module.training = mode
    trainer.backend.restore_rng(_clone(saved["rng"]))
    trainer.backend.synchronize()


def _estimate(trainer):
    # The first session update can include checkpoint creation, lazy optimizer
    # initialization and device warmup. Prefer subsequent productive timings.
    history = trainer.history[1:] if len(trainer.history) >= 2 else trainer.history
    observed = [row.get("step_seconds", 0) for row in history[-8:]]
    observed = [float(value) for value in observed if value and math.isfinite(value) and value > 0]
    if observed:
        return max(observed) * 1.5
    rows = trainer.controller.observations
    compatible = [row["seconds"] for row in rows if row.get("seconds", 0) > 0 and math.isfinite(row["seconds"])]
    if compatible:
        batches = math.ceil(trainer.training.effective_sequence_batch_size / max(1, trainer.controller.safe_batch))
        return max(compatible) * batches * 2
    return None


def _atomic_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, allow_nan=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _arm(trainer, saved, directory, mode, clock):
    began = clock()
    _restore(trainer, saved)
    trainer.run_dir = directory
    directory.mkdir(parents=True, exist_ok=True)
    trainer._benchmark_active = True
    trainer._controller_baseline = mode == "baseline"
    trainer.controller.force_baseline = False
    trainer.analytics = AnalyticsWriter(directory, identity={"purpose": "isolated_matched_comparison", "mode": mode})
    stager = None
    ids = []
    draw = trainer._draw_groups

    def record_draws():
        groups, n = draw()
        ids.extend(example.view_id for group in groups for example in group)
        return groups, n

    try:
        trainer.analytics.start_segment(resume_step=trainer.step)
        if mode == "baseline":
            trainer._defer_metrics = False
            trainer._prepared_batch = trainer._loss_metadata = None
            trainer.batch_schedule = _clone(getattr(trainer.controller, "baseline_schedule", []))
            trainer.micro_batch_size = trainer.controller.safe_batch
            trainer.chunk = getattr(trainer.training, "lm_loss_chunk_tokens", saved["trainer"]["chunk"])
            if hasattr(trainer.model, "gradient_checkpointing_enable"):
                from training.finetune.throughput import _checkpointing
                _checkpointing(trainer.model, True)
            elif hasattr(trainer.model, "is_gradient_checkpointing"):
                trainer.model.is_gradient_checkpointing = True
            if saved["stager"] is not _MISSING:
                trainer.stager = None
        else:
            trainer.analytics.enable_async()
        if mode != "baseline" and saved["stager"] is not _MISSING and saved["stager"] is not None:
            old = saved["stager"]
            stager = type(old)(old.backend, old.pad_id, old.max_host_bytes, enabled=old.enabled)
            trainer.stager = stager
        trainer._draw_groups = record_draws
        row = trainer._optimizer_step()
        trainer.backend.synchronize()
        loss, tokens = float(row["loss"]), int(row["tokens"])
        if not math.isfinite(loss) or tokens <= 0 or not ids:
            raise ValueError("Comparison requires a finite productive loss and real supervised examples")
    finally:
        try:
            trainer.analytics.close()
        finally:
            if stager is not None:
                stager.close()
    elapsed = clock() - began
    return dict(mode=mode, execution="legacy" if mode == "baseline" else "online_controller",
                seconds=elapsed, tokens=tokens, loss=loss, example_ids=ids,
                optimizer_updates=1, directory=str(directory.relative_to(saved["run_dir"])))


def compare(trainer, *, seconds=180, log=lambda message: None, clock=time.perf_counter, version=None):
    """Compare two matched pairs without advancing the main training state.

    The 0..180 second budget includes snapshots, restoration, staging and log I/O.
    Update admission reserves conservative observed update costs and restoration;
    an individual device update cannot be preempted if its runtime is surprising.
    Such overruns invalidate the result and retain the baseline policy.
    """
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 <= seconds <= 180:
        raise ValueError("comparison seconds must be finite and between 0 and 180")
    began = clock()
    identity = _plain(_identity(trainer, version))
    signature = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    path = pathlib.Path(trainer.run_dir) / "benchmark" / signature / "report.json"
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("schema") == VERSION and cached.get("identity") == identity:
            trainer.controller.force_baseline = bool(cached["force_baseline"])
            log("Reusing matched resource-controller comparison " + signature[:12])
            return cached
    report = dict(schema=VERSION, signature=signature, identity=identity, state="inconclusive",
                  force_baseline=True, budget_seconds=seconds, pairs=[],
                  scope="isolated productive optimizer updates; main weights and sampling restored")
    if seconds == 0:
        trainer.controller.force_baseline = True
        report.update(reason="comparison disabled", total_seconds=clock() - began)
        return report
    saved = None
    restore_seconds = 0.
    try:
        if trainer.analytics:
            trainer.analytics.flush()
        stager = getattr(trainer, "stager", None)
        if stager is not None and getattr(stager, "pending_bytes", 0):
            raise RuntimeError("Comparison requires an optimizer boundary without staged inputs")
        estimate = _estimate(trainer)
        snapshot_started = clock()
        saved = _snapshot(trainer)
        snapshot_seconds = clock() - snapshot_started
        report["snapshot_seconds"] = snapshot_seconds
        reserve = max(.05, snapshot_seconds * 2)
        if estimate is None:
            report["reason"] = "no observed update duration to admit bounded matched work"
        else:
            completed = 0
            for pair_index, modes in enumerate((("baseline", "adaptive"), ("adaptive", "baseline")), 1):
                arms = []
                for mode in modes:
                    remaining = 4 - completed
                    if clock() - began + remaining * (estimate + reserve) + reserve >= seconds:
                        report["reason"] = "insufficient budget for remaining matched repeats and restoration"
                        break
                    row = _arm(trainer, saved, path.parent / f"pair-{pair_index}" / mode, mode, clock)
                    arms.append(row)
                    completed += 1
                    estimate = max(estimate, row["seconds"] * 1.25)
                    reserve = max(reserve, snapshot_seconds * 2)
                if len(arms) != 2:
                    if arms:
                        report["unmatched_arms"] = arms
                    break
                baseline = next(row for row in arms if row["mode"] == "baseline")
                adaptive = next(row for row in arms if row["mode"] == "adaptive")
                matched = baseline["example_ids"] == adaptive["example_ids"] and baseline["tokens"] == adaptive["tokens"]
                report["pairs"].append(dict(arms=arms, matched=matched))
                if not matched:
                    report["reason"] = "sampler sequence or supervised token count differed"
                    break
    except Exception as error:
        report["error"] = dict(type=type(error).__name__, detail=str(error))
        report["reason"] = "comparison stopped after failure; no optimizer retry"
    finally:
        if saved is not None:
            restore_started = clock()
            _restore(trainer, saved)
            restore_seconds = clock() - restore_started
    report["restore_seconds"] = restore_seconds
    elapsed = clock() - began
    arm_seconds = sum(arm["seconds"] for pair in report["pairs"] for arm in pair["arms"])
    overhead_per_arm = max(0., elapsed - arm_seconds) / 4
    for pair in report["pairs"]:
        for arm in pair["arms"]:
            arm["accounted_seconds"] = arm["seconds"] + overhead_per_arm
            arm["tokens_per_second"] = arm["tokens"] / max(arm["accounted_seconds"], 1e-9)
        baseline = next(row for row in pair["arms"] if row["mode"] == "baseline")
        adaptive = next(row for row in pair["arms"] if row["mode"] == "adaptive")
        pair["improvement_fraction"] = adaptive["tokens_per_second"] / baseline["tokens_per_second"] - 1
    if len(report["pairs"]) == 2 and all(pair["matched"] for pair in report["pairs"]) and "error" not in report:
        accepted = all(pair["improvement_fraction"] >= .03 for pair in report["pairs"])
        report.update(state="accepted" if accepted else "baseline_retained", force_baseline=not accepted)
    if elapsed > seconds:
        report.update(state="inconclusive", force_baseline=True, reason="wall-time budget exceeded by an uninterruptible operation")
    report["total_seconds"] = elapsed
    trainer.controller.force_baseline = report["force_baseline"]
    _atomic_report(path, report)
    report["total_seconds"] = clock() - began
    if report["total_seconds"] > seconds:
        report.update(state="inconclusive", force_baseline=True, reason="wall-time budget exceeded including report I/O")
        trainer.controller.force_baseline = True
    _atomic_report(path, report)
    log(f"Matched resource-controller comparison: {report['state']}; {report['total_seconds']:.2f}s total")
    return report
