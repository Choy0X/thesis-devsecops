"""Checkpoints: atomic, complete, pruned by a policy, and enough to resume exactly.

A checkpoint is written into ``.tmp-ckpt-step<N>`` and renamed to ``ckpt-step<N>`` in one step, so a crash never leaves a
half-written folder that looks complete. It holds the adapter (peft format), the decision head, the optimizer, scheduler and
scaler, the random states, the sampler state, the counters and ``meta.json`` (base commit, tokenizer reference, serialization
and its hash, label order, pooling rule, calibration, config and dataset hashes). The two most recent checkpoints are kept
and the selected one (the best on validation) is never pruned.
"""
from __future__ import annotations

import json
import os
import pathlib
import random
import shutil
import time
from dataclasses import dataclass

import torch

from training.finetune import head as hd

SELECTED = "SELECTED"
_NAME = "ckpt-step"


@dataclass(frozen=True)
class Policy:
    every_steps: int
    every_minutes: int | None
    keep_recent: int = 2

    def due(self, *, step: int, now: float, last_time: float) -> bool:
        """A checkpoint is due at every ``every_steps`` optimizer steps, or when ``every_minutes`` have passed since the last."""
        if step <= 0:
            return False
        if step % self.every_steps == 0:
            return True
        return self.every_minutes is not None and now - last_time >= self.every_minutes * 60


def _name(step: int) -> str:
    return f"{_NAME}{step:06d}"


def list_checkpoints(root) -> list[pathlib.Path]:
    root = pathlib.Path(root)
    if not root.exists():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir() and p.name.startswith(_NAME)), key=lambda p: p.name)


def latest_checkpoint(root) -> pathlib.Path | None:
    found = list_checkpoints(root)
    return found[-1] if found else None


def _write_parts(tmp: pathlib.Path, *, model, head, optimizer, scheduler, scaler, sampler_state, counters, meta, backend=None) -> None:
    portable = backend.to_cpu if backend else lambda x: x
    if backend:
        from peft import get_peft_model_state_dict
        backend.synchronize()
        # Only adapter tensors leave the accelerator, never a CPU copy of the frozen 7B base.
        adapter_names = {n: p for n, p in model.state_dict().items() if "lora_" in n}
        model.save_pretrained(tmp / "adapter", state_dict=portable(adapter_names))
    else:
        model.save_pretrained(tmp / "adapter")
    torch.save(portable(head.state_dict()), tmp / "head.pt")
    torch.save(portable(optimizer.state_dict()), tmp / "optimizer.pt")
    torch.save(scheduler.state_dict(), tmp / "scheduler.pt")
    if scaler is not None:
        torch.save(scaler.state_dict(), tmp / "scaler.pt")
    rng = backend.rng_state() if backend else {"python": random.getstate(), "torch": torch.get_rng_state(),
           "cuda": torch.cuda.get_rng_state_all() if torch.cuda.is_available() else None}
    torch.save(rng, tmp / "rng.pt")
    (tmp / "sampler.json").write_text(json.dumps(sampler_state), encoding="utf-8")
    (tmp / "counters.json").write_text(json.dumps(counters), encoding="utf-8")
    (tmp / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True), encoding="utf-8")


def _replace(src: pathlib.Path, dst: pathlib.Path, attempts: int = 6, wait: float = 0.25) -> None:
    """``os.replace`` that tries again when Windows refuses with "access denied": a folder cannot be renamed while a virus scanner or the indexer still has one
    of its new files open, which passes within seconds (seen on the real GPU run: the rename of a finished checkpoint failed once, when the earlier ones
    had not). Any other error, and a refusal that lasts, is raised."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(wait * 2 ** i)


def save_checkpoint(root, *, step: int, model, head, optimizer, scheduler, scaler, sampler_state: dict, counters: dict,
                    meta: dict, backend=None) -> pathlib.Path:
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    final, tmp = root / _name(step), root / f".tmp-{_name(step)}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    full = {**meta, "step": step, "label_order": list(hd.LABELS), "pooling": hd.POOLING}
    if backend:
        full["accelerator_backend"] = backend.name
    try:
        _write_parts(tmp, model=model, head=head, optimizer=optimizer, scheduler=scheduler, scaler=scaler,
                     sampler_state=sampler_state, counters=counters, meta=full, backend=backend)
        if final.exists():
            shutil.rmtree(final)
        _replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return final


def load_weights(path, *, model, head) -> None:
    """Only the adapter and the decision head of a checkpoint (to take the selected state at the end of a run; nothing else is touched)."""
    from peft import load_peft_weights, set_peft_model_state_dict
    path = pathlib.Path(path)
    set_peft_model_state_dict(model, load_peft_weights(str(path / "adapter")))
    head.load_state_dict(torch.load(path / "head.pt", map_location="cpu", weights_only=True))


def load_checkpoint(path, *, model, head, optimizer, scheduler, scaler, expect: dict | None = None, backend=None) -> dict:
    """Restore a checkpoint into the given objects (and the random state); returns the sampler state, counters and meta."""
    path = pathlib.Path(path)
    for name in ("adapter", "head.pt", "optimizer.pt", "scheduler.pt", "rng.pt", "sampler.json", "counters.json", "meta.json"):
        if not (path / name).exists():
            raise FileNotFoundError(f"checkpoint {path} is incomplete: {name} is missing")
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    if backend and meta.get("accelerator_backend", backend.name if backend.name != "tpu" else None) != backend.name:
        raise ValueError("checkpoint accelerator backend differs; exact cross-backend resume is not supported")
    for key, want in (expect or {}).items():
        if meta.get(key) != want:
            raise ValueError(f"checkpoint {path.name} does not match this run: {key} is {meta.get(key)!r}, expected {want!r}")
    load_weights(path, model=model, head=head)
    optimizer.load_state_dict(torch.load(path / "optimizer.pt", map_location="cpu", weights_only=True))
    scheduler.load_state_dict(torch.load(path / "scheduler.pt", map_location="cpu", weights_only=True))
    if scaler is not None and (path / "scaler.pt").exists():
        scaler.load_state_dict(torch.load(path / "scaler.pt", map_location="cpu", weights_only=True))
    rng = torch.load(path / "rng.pt", map_location="cpu", weights_only=True)            # tensors and plain tuples only
    if backend:
        backend.restore_rng(rng)
    else:
        random.setstate(rng["python"])
        torch.set_rng_state(rng["torch"])
        if rng.get("cuda") is not None and torch.cuda.is_available():
            torch.cuda.set_rng_state_all(rng["cuda"])
    return {"sampler": json.loads((path / "sampler.json").read_text(encoding="utf-8")),
            "counters": json.loads((path / "counters.json").read_text(encoding="utf-8")), "meta": meta}


def mark_selected(root, path) -> None:
    (pathlib.Path(root) / SELECTED).write_text(pathlib.Path(path).name, encoding="utf-8")


def selected_checkpoint(root) -> pathlib.Path | None:
    f = pathlib.Path(root) / SELECTED
    if not f.exists():
        return None
    p = pathlib.Path(root) / f.read_text(encoding="utf-8").strip()
    return p if p.exists() else None


def prune(root, keep_recent: int = 2) -> list[pathlib.Path]:
    """Delete all but the most recent ``keep_recent`` checkpoints and the selected one; returns what was deleted."""
    found = list_checkpoints(root)
    keep = set(found[-keep_recent:]) if keep_recent > 0 else set()
    sel = selected_checkpoint(root)
    if sel is not None:
        keep.add(sel)
    removed = [p for p in found if p not in keep]
    for p in removed:
        shutil.rmtree(p)
    return removed
