"""Run in a child process so the notebook kernel does not hold the TPU device."""
from __future__ import annotations

import argparse
import json
import pathlib
import tempfile
import time

import torch

from training.finetune.accelerator import select_backend


def exercise_selected_model(base, cfg, backend):
    """Synthetic capacity probe; never reported as dataset training or model quality."""
    from training.finetune import checkpoint as ck, head as hd, losses as ls, model as md
    length = cfg.context.max_total_tokens
    reserve = min(length - 1, max(cfg.context.ordinary_completion_reserve, cfg.context.repair_completion_reserve))
    trained = md.add_adapter(base, cfg.lora).to(backend.device)
    checkpoint_fn = None
    if backend.name == "tpu":
        from torch_xla.utils.checkpoint import checkpoint as checkpoint_fn
    md.prepare_for_training(trained, gradient_checkpointing=True, checkpoint_fn=checkpoint_fn)
    decision = hd.DecisionHead(base.config.hidden_size).to(backend.device)
    params = [p for p in trained.parameters() if p.requires_grad] + list(decision.parameters())
    optimizer = torch.optim.AdamW(params, lr=cfg.training.learning_rate)
    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda _: 1.)
    ids_cpu = (torch.arange(length).view(1, -1) % base.config.vocab_size).long()
    labels = ids_cpu.clone()
    labels[:, :length - reserve] = -100
    fields = torch.zeros_like(labels)
    ids, mask = ids_cpu.to(backend.device), torch.ones_like(ids_cpu).to(backend.device)
    def objective():
        with backend.autocast():
            hidden = md.hidden_states(trained, ids, mask)
        tokens = ls.token_losses(hidden, md.lm_head_of(trained), labels.to(backend.device), fields.to(backend.device),
                                 static=(labels, fields), checkpoint=trained.training)
        lm, _ = ls.per_example_completion_ce(tokens, 1)
        # Exercise the FP32 decision head and its optimizer state as well as every adapter target.
        return lm + .2 * decision(hidden[:, length - reserve - 1]).float().square().mean()
    def step():
        trained.train()
        optimizer.zero_grad(set_to_none=True)
        loss = objective()
        md.assert_finite(loss, 0)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(params, cfg.training.max_grad_norm)
        backend.optimizer_step(optimizer)
        scheduler.step()
        return float(loss.detach().cpu())
    first = step()
    with tempfile.TemporaryDirectory(prefix="tpu-selected-check-") as folder:
        saved = ck.save_checkpoint(folder, step=1, model=trained, head=decision, optimizer=optimizer, scheduler=scheduler,
                                   scaler=None, sampler_state={}, counters={}, meta={}, backend=backend)
        reference = step()
        ck.load_checkpoint(saved, model=trained, head=decision, optimizer=optimizer, scheduler=scheduler, scaler=None, backend=backend)
        restored = step()
        if abs(reference - restored) > max(1e-4, abs(reference) * .005):
            raise RuntimeError(f"Selected-model checkpoint/resume mismatch: {reference} versus {restored}")
    optimizer.zero_grad(set_to_none=True)
    trained.eval()
    with torch.no_grad():
        evaluated = float(objective().cpu())
    backend.synchronize()
    return {"context_tokens": length, "completion_tokens": reserve, "rank": cfg.lora.r, "loss": first,
            "evaluation_loss": evaluated, "checkpoint_resume": "passed", "memory": backend.memory(),
            "scope": "Synthetic capacity and mechanics probe; no downstream accuracy or dataset training result"}


def validate_selected_model(bundle, output):
    from training.finetune import resolve
    from training.finetune.tpu_quantization import load_tpu_model
    report = {"schema": "tpu-selected-model-validation/1", "state": "running", "full_7b_validation": "pending"}
    output = pathlib.Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        cfg = resolve.read_resolved(bundle)
        backend = select_backend("tpu")
        base = load_tpu_model(cfg, revision=cfg.model.revision, backend=backend)
        result = exercise_selected_model(base, cfg, backend)
        import hashlib
        report.update(state="passed", full_7b_validation="passed", model=cfg.model.id, revision=cfg.model.revision,
                      backend=backend.name, resolved_config_sha256=hashlib.sha256((pathlib.Path(bundle) / "resolved_config.json").read_bytes()).hexdigest(), **result)
    except BaseException as error:
        report.update(state="failed", error={"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        report["seconds"] = time.perf_counter() - started
        output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    return report


def validate_runtime(output, backend=None):
    output = pathlib.Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema": "tpu-runtime-validation/1", "state": "running", "full_7b_validation": "pending",
              "scope": "Tiny quantized model only; does not establish 7B memory fit"}
    started = time.perf_counter()
    try:
        backend = backend or select_backend("tpu")
        if backend.name != "tpu":
            raise RuntimeError("TPU hardware is required for TPU validation")
        from training.finetune import checkpoint as ck, config, head, model
        from training.finetune.tpu_quantization import quantize_model
        settings = config.load().lora.model_copy(update={"r": 4, "lora_alpha": 8, "lora_dropout": 0.})
        base = quantize_model(model.build_tiny_model(64).to(torch.bfloat16))
        trained = model.add_adapter(base, settings).to(backend.device)
        decision = head.DecisionHead(32).to(backend.device)
        optimizer = torch.optim.AdamW([p for p in trained.parameters() if p.requires_grad], lr=.001)
        scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda _: 1.)
        ids = torch.tensor([[2, 3, 4, 5]], device=backend.device)
        def step():
            optimizer.zero_grad()
            loss = trained(input_ids=ids).logits.float().square().mean()
            loss.backward()
            backend.optimizer_step(optimizer)
            scheduler.step()
            return float(loss.cpu())
        report["loss"] = step()
        with tempfile.TemporaryDirectory(prefix="tpu-check-") as folder:
            saved = ck.save_checkpoint(folder, step=1, model=trained, head=decision, optimizer=optimizer, scheduler=scheduler,
                                       scaler=None, sampler_state={}, counters={}, meta={}, backend=backend)
            reference = step()
            ck.load_checkpoint(saved, model=trained, head=decision, optimizer=optimizer, scheduler=scheduler, scaler=None, backend=backend)
            restored = step()
            if abs(reference - restored) > 1e-4:
                raise RuntimeError(f"TPU checkpoint resume mismatch: {reference} versus {restored}")
        report.update(state="passed", backend=backend.name, memory=backend.memory(), checkpoint_resume="passed")
    except BaseException as error:
        report.update(state="failed", error={"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        report["seconds"] = time.perf_counter() - started
        output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--bundle", default=None)
    args = parser.parse_args()
    if args.bundle:
        validate_selected_model(args.bundle, args.output)
    else:
        validate_runtime(args.output)
