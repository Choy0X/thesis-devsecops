"""The runner: from a dataset folder to a trained adapter, a decision head, a calibration and a run folder that can be cited.

``run`` reads the training views of ``views.parquet`` (the ``train`` split only), serializes and tokenizes each one with the pinned
tokenizer (an example over budget is excluded with its reason, never truncated), builds the task and family sampler and the trainer,
trains with the validation evaluator at the family passes of the contract, restores the selected state, saves the adapter and the head,
fits the calibration on the validation calibration role, and writes ``run_config.json``, ``metrics.jsonl``,
``evaluations.json``, ``exposure.json``, ``profile.json``, ``outputs_manifest.json`` and ``outputs.zip`` (what is imported back into the
app). Nothing here imports the app: the same code runs from an unpacked training bundle on a GPU runtime.

* The evaluator scores the validation ``selection`` role. Until the shared evaluator exists (generated verdicts, the clean-utility and
  repair gates) the selection metric is the negative validation loss, or, in stage 2 with enough negatives, the recall of the decision
  head at the largest illustrative false-positive rate; the clean-utility and repair gates are recorded as ``not_evaluated``.
* A model under one billion parameters is a software test, never a training result: ``smoke_test`` is recorded as true.
* A run folder is never reused for another setting: the identity of the setting is part of every checkpoint, and a run folder that
  already trained is refused unless it is resumed.
* ``main`` is the command line for a bundle (``python -m training.finetune.runner --bundle DIR --run-dir DIR``); ``--tiny`` runs the same
  code over a tiny random model and a tokenizer trained on the data (the plumbing check, not a result).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.metadata as im
import json
import math
import os
import pathlib
import platform
import shutil
import sys
import zipfile
from collections import Counter

import torch

from training.finetune import (attempts as at, calibration as cal, checkpoint as ck, collate as co, config as cfgmod, contracts as ct, head as hd,
                               hub, infer, losses as ls, model as md, planning, resolve as rs, schedule, serialization as sz, subset, tokenization as tk,
                               trainer as tr)
from training.finetune.sampler import Item, Sampler
from training.finetune.accelerator import select_backend
from training.finetune.analytics import AnalyticsWriter
from training.finetune import evaluation_data as ed

RUN_SCHEMA = "finetune-run/1"
OUTPUTS_SCHEMA = "run-outputs/1"
PACKAGES = ("torch", "transformers", "peft", "accelerate", "bitsandbytes", "tokenizers", "safetensors", "pyarrow", "numpy", "scipy")
STATUS_TO_LABEL, PAIR_POOLS = infer.STATUS_TO_LABEL, infer.PAIR_POOLS
SMOKE_PARAMETER_LIMIT = 1_000_000_000
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)


class RunnerError(RuntimeError):
    """The run cannot start or continue as asked."""


# -- reading the dataset ----------------------------------------------------------------------------------------------------------------------

def load_views(data_dir, split: str) -> list[dict]:
    """The views of one split that can supervise a model (eligible, weight above zero), in view id order."""
    import pyarrow.parquet as pq
    table = pq.read_table(pathlib.Path(data_dir) / "views.parquet", filters=[("split", "=", split)])
    rows = [r for r in table.to_pylist() if r["eligible"] and (r["weight"] or 0) > 0]
    return sorted(rows, key=lambda r: r["view_id"])


def load_repositories(data_dir) -> dict[str, str]:
    """{record id: repository identity}; a record without a repository is its own owner so the exposure caps cannot lump unknowns."""
    import pyarrow.parquet as pq
    table = pq.read_table(pathlib.Path(data_dir) / "records.parquet", columns=["record_id", "repository_identity"])
    return {r["record_id"]: r["repository_identity"] or f"record:{r['record_id']}" for r in table.to_pylist()}


def build_examples(views, repositories: dict, tokenizer, context=None, *, progress=None, protocol=sz.SERIALIZATION_ID) -> tuple[tr.Dataset, list[Item], dict]:
    """(dataset, sampler items, stats): every view serialized with the contract and tokenized; an excluded view is counted by reason. ``context`` is the run's own context section
    (its token budget); without it the contract's defaults are used."""
    by_subject = {(v["task"], v["subject_id"]): v["view_id"] for v in views}
    examples: dict[str, tr.Example] = {}
    items: list[Item] = []
    excluded: Counter = Counter()
    tokens = {"prompt": 0, "completion": 0}
    by_task: Counter = Counter()
    serialized = []                                              # the views that can be tokenized, in order
    for v in views:
        task = v["task"]
        try:
            ser = sz.serialize_example(task, v["context_text"], json.loads(v["response_json"]), protocol=protocol, tokenizer=tokenizer)
        except (ValueError, KeyError):
            excluded["invalid_response"] += 1
            continue
        from training.finetune.sft_contracts import CORPUS_TASKS
        if ser.reserved_heading_in_context and not (protocol == 'sft_native_chat_v1' and task in CORPUS_TASKS):
            excluded["reserved_heading_in_context"] += 1
            continue
        serialized.append((v, ser))
    encoded = tk.encode_examples(tokenizer, [ser for _v, ser in serialized], context=context, progress=progress)
    for (v, _ser), (enc, reason) in zip(serialized, encoded):
        task = v["task"]
        if enc is None:
            excluded[reason] += 1
            continue
        response = json.loads(v["response_json"])
        status = response.get("status") if isinstance(response, dict) else None
        pair_id = role = None
        if task == "discovery" and v["pool"] in PAIR_POOLS:
            pair_id, role = f"discovery:{v['record_id']}", PAIR_POOLS[v["pool"]]
        lineage = json.loads(v.get("lineage_json") or "{}")
        variant_of = by_subject.get((task, lineage.get("parent_subject_id"))) if lineage.get("transform") else None
        examples[v["view_id"]] = tr.Example(enc, STATUS_TO_LABEL.get(status, "unreviewed"), float(v["weight"]), pair_id=pair_id, role=role,
                                            variant_of=variant_of, transform=lineage.get("transform") if variant_of else None,
                                            view_id=v["view_id"])
        items.append(Item(v["view_id"], task, v["pool"], v["family_id"], repositories.get(v["record_id"]) or f"record:{v['record_id']}",
                          float(v["weight"]), record_id=v['record_id']))
        tokens["prompt"] += enc.prompt_tokens
        tokens["completion"] += enc.completion_tokens
        by_task[task] += 1
    stats = {"views": len(views), "encoded": len(examples), "excluded": dict(sorted(excluded.items())),
             "by_task": dict(sorted(by_task.items())), "tokens": tokens}
    return tr.Dataset(examples), items, stats


# -- the environment and the model -----------------------------------------------------------------------------------------------------------------

def environment_snapshot(backend=None) -> dict:
    """What the run executed on: versions, accelerator and precision support. No environment variables, paths or host names."""
    def version(name):
        try:
            return im.version(name)
        except im.PackageNotFoundError:
            return None
    cuda = backend.name == "cuda" if backend else torch.cuda.is_available()
    gpus = [{"name": torch.cuda.get_device_name(i), "memory_bytes": torch.cuda.get_device_properties(i).total_memory}
            for i in range(torch.cuda.device_count())] if cuda else []
    return {"python": sys.version.split()[0], "platform": platform.platform(), "machine": platform.machine(),
            "cpu_count": os.cpu_count(), "packages": {n: version(n) for n in PACKAGES}, "cuda": cuda,
            "cuda_version": torch.version.cuda if cuda else None, "gpus": gpus,
            "bf16_supported": True if backend and backend.name == "tpu" else (md.bf16_native() if cuda else False),
            "backend": backend.name if backend else None,
            "torch_xla": version("torch_xla") if backend and backend.name == "tpu" else None}


def model_facts(model) -> dict:
    c = model.config
    return {"class": type(model).__name__, "hidden_size": c.hidden_size, "layers": c.num_hidden_layers, "vocab_size": c.vocab_size,
            "intermediate_size": getattr(c, "intermediate_size", None), "num_attention_heads": getattr(c, "num_attention_heads", None),
            "num_key_value_heads": getattr(c, "num_key_value_heads", None), "head_dim": getattr(c, "head_dim", None),      # what the importer needs to know the size an adapter of this model has
            "parameters": getattr(model, "tpu_quantization", {}).get("original_parameters", sum(p.numel() for p in model.parameters())),
            "quantized_4bit": bool(getattr(model, "is_loaded_in_4bit", False) or getattr(model, "tpu_quantization", None)),
            "tpu_quantization": getattr(model, "tpu_quantization", None)}


def tiny_tokenizer(texts, vocab_size: int = 800):
    """A small byte-level BPE trained on ``texts`` (for the software smoke test; deterministic for a fixed corpus)."""
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers
    from transformers import PreTrainedTokenizerFast
    tok = Tokenizer(models.BPE())
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tok.decoder = decoders.ByteLevel()
    tok.train_from_iterator(list(texts), trainers.BpeTrainer(vocab_size=vocab_size, special_tokens=["<|endoftext|>"],
                                                             initial_alphabet=pre_tokenizers.ByteLevel.alphabet()))
    result = PreTrainedTokenizerFast(tokenizer_object=tok, eos_token="<|endoftext|>", pad_token="<|endoftext|>")
    # Software-only tokenizer. It is never substituted for the selected model's
    # pinned tokenizer in real training or security evaluation.
    result.chat_template = "{% for message in messages %}{{ '<' + message['role'] + '>\\n' + message['content'] + '\\n' }}{% endfor %}{% if add_generation_prompt %}{{ '<assistant>\\n' }}{% endif %}"
    return result


# -- validation ---------------------------------------------------------------------------------------------------------------------------------

@torch.no_grad()
def forward_examples(model, head, examples, *, pad_id: int, device, dtype, chunk: int = ls.DEFAULT_TOKEN_CHUNK, batch_size: int = 4):
    """(per-example completion cross-entropy, decision logits) of ``examples`` in evaluation mode."""
    was = model.training, head.training
    model.eval()
    head.eval()
    cuda = str(device).startswith("cuda")
    tpu = str(device).startswith("xla")
    if tpu:
        batch_size = 1
    losses, logits = [], []
    try:
        for i in range(0, len(examples), batch_size):
            group = examples[i:i + batch_size]
            batch = co.collate([e.encoded for e in group], pad_id)
            if tpu:
                batch = co.bucket_batch(batch, pad_id)
            static = (batch["labels"], batch["field_ids"]) if tpu else False
            batch = {k: v.to(device) for k, v in batch.items()}
            with torch.autocast(device_type="cuda" if cuda else "cpu", dtype=dtype, enabled=cuda and dtype != torch.float32):
                hidden = md.hidden_states(model, batch["input_ids"], batch["attention_mask"])
            tl = ls.token_losses(hidden, md.lm_head_of(model), batch["labels"], batch["field_ids"], chunk=chunk, checkpoint=False, static=static)
            _, per = ls.per_example_completion_ce(tl, len(group))
            losses.append(per.float().cpu())
            logits.append(head(hd.pooled_prompt_state(hidden, batch["prompt_lengths"])).float().cpu())
    finally:
        model.train(was[0])
        head.train(was[1])
    if not losses:
        return torch.zeros(0), torch.zeros(0, len(hd.LABELS))
    return torch.cat(losses), torch.cat(logits)


def decision_rows(examples) -> tuple[list[int], torch.Tensor]:
    """(positions, class labels) of the discovery examples that have a decision class (what the head answers)."""
    keep = [i for i, e in enumerate(examples) if e.encoded.task == "discovery" and hd.label_index(e.label_state) >= 0]
    return keep, torch.tensor([hd.label_index(examples[i].label_state) for i in keep], dtype=torch.long)


class Evaluator:
    """Validation on the selection role at the family passes. Returns the metric the trainer maximizes and the facts behind it."""

    def __init__(self, examples, *, stage: int, min_negatives: int, fpr_targets, pad_id: int, chunk: int = ls.DEFAULT_TOKEN_CHUNK, batch_size: int = 4):
        self.examples = list(examples)
        self.stage, self.min_negatives, self.fpr_targets = stage, min_negatives, list(fpr_targets)
        self.pad_id, self.chunk, self.batch_size = pad_id, chunk, batch_size

    def __call__(self, trainer) -> dict:
        losses, logits = forward_examples(trainer.model, trainer.head, self.examples, pad_id=self.pad_id, device=trainer.device,
                                          dtype=trainer.dtype, chunk=self.chunk, batch_size=self.batch_size)
        val_loss = float(losses.mean()) if losses.numel() else math.inf
        keep, labels = decision_rows(self.examples)
        scores = hd.vulnerability_score(logits[keep]) if keep else torch.zeros(0)
        points = cal.operating_points(scores, (labels == 0).long(), min_negatives=self.min_negatives, targets=self.fpr_targets) if keep else []
        primary = points[-1] if points else None
        head_trained = self.stage == 2
        use_recall = head_trained and primary is not None and not primary["insufficient"] and primary["recall"] is not None
        if use_recall:
            # the validation loss only breaks a tie: its weight is far below any difference of two recalls
            metric, name = float(primary["recall"]) - 1e-9 * min(val_loss, 1e6), f"recall_at_fpr_{primary['target_fpr']}"
        else:
            metric, name = -val_loss, "negative_validation_loss"
        return {"metric": metric, "metric_name": name, "val_loss": val_loss, "n_examples": len(self.examples),
                "head_trained": head_trained, "operating_points": points,
                "gates": "not_evaluated"}                       # clean utility and repair need generation: the shared evaluator


# -- files ----------------------------------------------------------------------------------------------------------------------------------------------

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: pathlib.Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _align_metrics(run_dir: pathlib.Path, step: int) -> None:
    """Keep the metric lines up to ``step``; later ones (steps that will be redone) move to ``metrics.discarded.jsonl``."""
    path = run_dir / "metrics.jsonl"
    if not path.exists():
        return
    keep, drop = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        (keep if line.strip() and json.loads(line)["step"] <= step else drop).append(line)
    if drop:
        with (run_dir / "metrics.discarded.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("\n".join(drop) + "\n")
    path.write_text("".join(line + "\n" for line in keep), encoding="utf-8")


def _outputs(run_dir: pathlib.Path) -> dict:
    """The files that are imported back (no checkpoints: they hold optimizer state and are only for resuming)."""
    names = ["run_config.json", "resolved_config.json", "metrics.jsonl", "evaluations.json", "exposure.json", "profile.json",
             "decision_head.pt", "calibration.json", "training_events.jsonl", "training_analytics.json",
             "correctness_diagnostics.json", "training_preflight.json", "throughput_profile.json", "checkpoint_continuation.json",
             "tpu_7b_check.json", "tpu_runtime_check.json", "resource_controller.json", "controller_benchmark.json"]
    names += sorted(p.relative_to(run_dir).as_posix() for p in (run_dir / "adapter").rglob("*") if p.is_file())
    return {n: {"sha256": _sha_file(run_dir / n), "bytes": (run_dir / n).stat().st_size} for n in names if (run_dir / n).exists()}


def _write_outputs(run_dir: pathlib.Path) -> dict:
    files = _outputs(run_dir)
    manifest = {"schema": OUTPUTS_SCHEMA, "files": files}
    _write_json(run_dir / "outputs_manifest.json", manifest)
    with zipfile.ZipFile(run_dir / "outputs.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in [*files, "outputs_manifest.json"]:
            info = zipfile.ZipInfo(name, _FIXED_TIME)
            info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
            with z.open(info, "w") as target, (run_dir / name).open("rb") as source:
                shutil.copyfileobj(source, target)
    return manifest


def _check_dataset(data_dir: pathlib.Path, cfg) -> dict:
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.exists():
        raise RunnerError(f"{data_dir} has no manifest.json")
    if _sha(manifest_path.read_bytes()) != cfg.data.manifest_sha256:
        raise RunnerError("the dataset manifest does not match the resolved config (the dataset changed after the run was resolved)")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


# -- a run ------------------------------------------------------------------------------------------------------------------------------------------------

def run(*, run_dir, data_dir, tokenizer, model, cfg, stage: int = 1, total_steps: int | None = None, seed: int | None = None,
        resume: bool = True, eval_max: int | None = None, stop_after: int | None = None, smoke_test: bool = False,
        device: str | None = None, profile: bool = True, purpose: str = "train", bundle: dict | None = None,
        loss_mode: str | None = None, session_minutes: float | None = None, smoke_model: dict | None = None,
        log=lambda message: None, backend=None, analytics=None, stop_file=None,
        resource_controller='auto', controller_benchmark_seconds=0) -> dict:
    """Train ``model`` (a plain causal language model; the adapter is added here) on the ``train`` views of ``data_dir``.

    ``purpose`` is ``train`` or ``smoke`` (a short run on a GPU to check memory and speed); ``bundle`` names the training bundle the run
    came from (its manifest hash and source revision), so an imported run can be tied to the bundle that was exported.
    ``session_minutes`` is a wall-clock budget for a paid runtime: the run stops with a checkpoint before it is used up and is resumed in
    the next session (it defaults to ``runtime.session_budget_minutes`` of the contract and is not part of the run's identity).
    ``smoke_model`` ({"id", "revision"}) records that ``model`` is a small public model loaded only to exercise the loader and the GPU path:
    the run is then always a smoke test and never a result."""
    if purpose not in ("train", "smoke"):
        raise RunnerError(f"purpose must be train or smoke, got {purpose!r}")
    if resource_controller not in ('auto','observe','off'):
        raise RunnerError('resource-controller must be auto, observe, or off')
    if not 0 <= controller_benchmark_seconds <= 180:
        raise RunnerError('controller-benchmark-seconds must be between 0 and 180')
    if session_minutes is None:
        session_minutes = cfg.runtime.session_budget_minutes
    if session_minutes is not None and session_minutes <= 0:
        raise RunnerError(f"the session budget must be a positive number of minutes, got {session_minutes!r}")
    try:
        loss_cfg = ls.LossConfig.for_stage(stage, mode=loss_mode)
    except ValueError as e:
        raise RunnerError(f"loss mode: {e}") from e
    run_dir, data_dir = pathlib.Path(run_dir), pathlib.Path(data_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    resolved_path = run_dir / rs.FILE
    if not resolved_path.exists():
        raise RunnerError(f"{resolved_path} does not exist: resolve the run first")
    if not resume and (run_dir / "metrics.jsonl").exists():
        raise RunnerError(f"{run_dir} already holds a run; resume it or use a new run folder")
    manifest = _check_dataset(data_dir, cfg)
    seed = cfg.training.pilot_seed if seed is None else seed
    backend = backend or select_backend(device or "auto")
    device, dtype = str(backend.device), backend.dtype
    facts = model_facts(model)
    if backend.name == "tpu" and (run_dir / "tpu_7b_check.json").exists():
        evidence = json.loads((run_dir / "tpu_7b_check.json").read_text())
        facts["hardware_validation"] = evidence
        if facts.get("tpu_quantization") and evidence.get("state") == "passed":
            facts["tpu_quantization"]["hardware_validation"] = "passed"
    smoke_test = smoke_test or smoke_model is not None or facts["parameters"] < SMOKE_PARAMETER_LIMIT

    config_path = run_dir / "run_config.json"
    previous = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    dumped = cfg.model_dump(mode="json")
    doc = {"schema": RUN_SCHEMA, "task": "finetune", "model": cfg.model.id, "model_revision": cfg.model.revision,
           "tokenizer_revision": cfg.model.tokenizer_revision, "base_url": None, "temperature": None, "split": "train",
           "timestamp": previous.get("timestamp") or _now(), "segments": int(previous.get("segments", 0)) + 1,
           "prompt_text": {"serialization": cfg.model.serialization, "serialization_hash": sz.serialization_hash(cfg.model.serialization, tokenizer),
                           "headings": list(sz.HEADINGS),
                           "task_text": dict(sz.security_module(cfg.model.serialization).TASK_TEXT if cfg.model.serialization in sz.SECURITY_PROTOCOLS
                                             else __import__('training.finetune.sft_contracts', fromlist=['TASK_TEXT']).TASK_TEXT
                                             if cfg.model.serialization == 'sft_native_chat_v1' else ct.TASK_TEXT),
                           **({'chat_template': tokenizer.chat_template} if cfg.model.serialization in (sz.CHAT_ID, 'sft_native_chat_v1', *sz.SECURITY_PROTOCOLS) else {}),
                           **({'response_schemas': sz.security_module(cfg.model.serialization).RESPONSE_SCHEMAS,
                               'response_rules': sz.security_module(cfg.model.serialization).RESPONSE_RULES}
                              if cfg.model.serialization in sz.SECURITY_PROTOCOLS else {})},
           "rag_enabled": cfg.rag.enabled,
           "context_policy": cfg.context.policy, "input_policy_hash": cfg.input_policy_hash(),
           "dataset": {"version": cfg.data.dataset_version, "status": manifest.get("dataset_status", "unfrozen"),
                       "manifest_sha256": cfg.data.manifest_sha256},
           **({"external_data": manifest["external"]} if manifest.get("external") else {}),          # data that was imported, not built here: a run on it is never compared with a run on a frozen version as like for like
           "seeds": [seed], "stage": stage, "purpose": purpose, "bundle": bundle, "session_minutes": session_minutes, "smoke_model": smoke_model,
           "loss": {"mode": loss_cfg.mode, "stage": stage, "decision_weight": loss_cfg.decision_weight, "rank_weight": loss_cfg.rank_weight,
                    "pair_enabled": loss_cfg.pair_enabled, "invariance_enabled": loss_cfg.invariance_enabled},
           "device": device, "dtype": str(dtype), "compute_choice": os.environ.get("ZTSA_COMPUTE_CHOICE") or None,       # what the person chose in the Compute dialog; device is what ran it
           "environment": environment_snapshot(backend), "model_loaded": facts,
           "head": {"label_order": list(hd.LABELS), "pooling": hd.POOLING, "hidden_size": int(model.config.hidden_size),
                    "classes": len(hd.LABELS)},
           "smoke_test": smoke_test, "state": "running",
           "resolved_config_sha256": _sha(resolved_path.read_bytes())}
    if smoke_test:
        doc["note"] = ("software smoke test: " + (f"{smoke_model['id']} is " if smoke_model else "a model under one billion parameters is ")
                       + "not the pinned base and this is not a training result")
    continuation = run_dir / 'checkpoint_continuation.json'
    if continuation.exists():
        doc['checkpoint_continuation'] = json.loads(continuation.read_text(encoding='utf-8'))
    _write_json(config_path, doc)
    analytics = analytics or AnalyticsWriter(run_dir, identity={"model": cfg.model.id, "bundle": bundle})
    analytics.emit("metadata", data={"configuration": dumped, "environment": doc["environment"], "provenance": doc})
    try:
        base = (smoke_model["id"], smoke_model["revision"]) if smoke_model else (cfg.model.id, cfg.model.revision)           # where the full-precision weights are (LoftQ)
        return _train(run_dir, data_dir, tokenizer, model, cfg, doc, dumped, config_path, stage=stage, loss_cfg=loss_cfg,
                      total_steps=total_steps, seed=seed, session_minutes=session_minutes,
                      resume=resume, eval_max=eval_max, stop_after=stop_after, device=device, dtype=dtype, profile=profile, base=base, log=log,
                      backend=backend, analytics=analytics, stop_file=stop_file,
                      resource_controller=resource_controller, controller_benchmark_seconds=controller_benchmark_seconds)
    except BaseException as e:
        doc.update(state="failed", error={"type": type(e).__name__, "message": str(e)})
        _write_json(config_path, doc)
        analytics.emit("failure", error=doc["error"])
        analytics.consolidate("failed")
        raise
    finally:
        analytics.close()


def _train(run_dir, data_dir, tokenizer, model, cfg, doc, dumped, config_path, *, stage, loss_cfg, total_steps, seed, session_minutes,
           resume, eval_max, stop_after, device, dtype, profile, base, log, backend, analytics, stop_file,
           resource_controller='auto', controller_benchmark_seconds=0) -> dict:
    analytics.emit("lifecycle", phase="preparing_dataset")
    train_views = load_views(data_dir, "train")
    train_views, slice_report = subset.select(train_views, cfg.data.train_slice_start, cfg.data.train_slice_end)
    if slice_report["families"] != slice_report["families_total"]:
        log(f"train slice {slice_report['start']} to {slice_report['end']}: {slice_report['families']} of {slice_report['families_total']} families, "
            f"{slice_report['views']} of {slice_report['views_total']} views")
    repositories = load_repositories(data_dir)
    data, items, stats = build_examples(train_views, repositories, tokenizer, cfg.context, protocol=cfg.model.serialization,
                                      progress=lambda done, total: log(f"tokenizing training views: {done}/{total}"))
    stats["slice"] = slice_report
    log(f"train views: {stats['encoded']} of {stats['views']} encoded; excluded {stats['excluded'] or 'none'}")
    sampler = Sampler(items, task_shares=cfg.sampling.task_shares, pool_targets=cfg.sampling.decision_pool_targets, seed=seed)
    log(sampler.exposure_report()["plan_report"])
    if total_steps is None:
        total_steps = schedule.total_steps(family_passes=cfg.training.maximum_pilot_family_passes, families=sampler.progress_units,
                                           effective_batch=cfg.training.effective_sequence_batch_size)

    val_views = load_views(data_dir, "val")
    val_data, _, val_stats = build_examples(val_views, repositories, tokenizer, cfg.context, protocol=cfg.model.serialization,
                                          progress=lambda done, total: log(f"tokenizing validation views: {done}/{total}"))
    role_of = {v["view_id"]: v["val_role"] for v in val_views}
    selection = [val_data.get(i) for i in val_data.ids() if role_of.get(i) == "selection"]
    calibration_set = [val_data.get(i) for i in val_data.ids() if role_of.get(i) == "calibration"]
    if eval_max is not None:
        selection = selection[:eval_max]
    pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id
    config_sha = _sha(json.dumps({"config": dumped, "stage": stage, "loss_mode": loss_cfg.mode, "total_steps": total_steps, "seed": seed},
                                 sort_keys=True, separators=(",", ":")).encode("utf-8"))
    doc.update(n=stats["encoded"], steps=total_steps, config_sha256=config_sha,
               progress_unit=sampler.exposure_report()['progress_unit'],
               config_overridden=_sha(json.dumps(dumped, sort_keys=True, separators=(",", ":")).encode("utf-8"))
               != json.loads((run_dir / rs.FILE).read_text(encoding="utf-8"))["config_sha256"],
               data={"train": stats, "validation": {**val_stats, "selection": len(selection), "calibration": len(calibration_set)}})
    _write_json(config_path, doc)

    analytics.emit("lifecycle", phase="preparing_adapter")
    torch.manual_seed(seed)
    peft_model = md.add_adapter(model, cfg.lora)
    if cfg.precision.mode == 'cpu_weight_offload':
        from training.finetune import offload
        offload_info = offload.enable(peft_model, pin=cfg.training.pin_frozen_weights and backend.name == 'cuda')
        doc['cpu_weight_offload'] = offload_info
        _write_json(config_path, doc)
        log('Experimental CPU weight offload: frozen projections and embeddings in system RAM; output head and adapters on the accelerator.')
    checkpoint_fn = None
    if backend.name == "tpu":
        from torch_xla.utils.checkpoint import checkpoint as checkpoint_fn
    md.prepare_for_training(peft_model, gradient_checkpointing=cfg.training.gradient_checkpointing and backend.name != "cpu",
                            checkpoint_fn=checkpoint_fn)
    if device != "cpu" and getattr(model, "hf_device_map", None) is None:
        peft_model.to(device)
    head = hd.DecisionHead(model.config.hidden_size).to(device)
    evaluator = (Evaluator(selection, stage=stage, min_negatives=cfg.calibration.min_negatives, pad_id=pad_id,
                           fpr_targets=cfg.evaluation.illustrative_fpr_operating_points) if selection else None)
    meta = {"base_commit": cfg.model.revision, "tokenizer_revision": cfg.model.tokenizer_revision, "serialization": cfg.model.serialization,
            "serialization_hash": sz.serialization_hash(cfg.model.serialization, tokenizer), "calibration": None, "config_sha256": config_sha,
            "dataset_manifest_sha256": cfg.data.manifest_sha256, "stage": stage, "seed": seed}
    trainer = tr.Trainer(model=peft_model, head=head, dataset=data, sampler=sampler, training=cfg.training, lora=cfg.lora,
                         loss_cfg=loss_cfg, run_dir=run_dir, pad_id=pad_id, seed=seed, total_steps=total_steps,
                         meta=meta, device=device, dtype=dtype, evaluator=evaluator, pack_tokens=cfg.context.max_total_tokens,
                         session_seconds=None if session_minutes is None else session_minutes * 60,
                         backend=backend, analytics=analytics, stop_file=stop_file)
    if cfg.lora.init_loftq and not (resume and ck.latest_checkpoint(trainer.checkpoint_dir)):           # a resumed run takes its adapter from the checkpoint
        doc["loftq"] = md.init_loftq(peft_model, *base)
        _write_json(config_path, doc)
        log(f"LoftQ: the adapter starts from the 4-bit quantisation error of {doc['loftq']['layers']} layers ({doc['loftq']['seconds']} s)")
    resumed = trainer.resume() if resume else False
    from training.finetune import throughput as performance
    from training.finetune import online_training
    controller_state = online_training.configure(trainer,mode=resource_controller,log=log)
    if (run_dir / 'resource_report.json').is_file():
        from training.finetune import runtime_resources
        runtime_resources.apply_execution_policy(trainer, json.loads((run_dir / 'resource_report.json').read_text(encoding='utf-8')))
    if controller_state is None and cfg.training.micro_batch_tuning != 'off':
        analytics.emit('lifecycle', phase='tuning_throughput')
    throughput = None if controller_state is not None else performance.configure(trainer, log=log)
    if controller_state:
        doc['resource_controller'] = {'mode':resource_controller,'schema':controller_state['schema']}
    doc['runtime_implementation'] = {
        'mode': 'overlay' if performance.RUNTIME_PATCH else 'bundled',
        'trainer_sha256': _sha_file(pathlib.Path(tr.__file__)),
        'throughput_sha256': _sha_file(pathlib.Path(performance.__file__))}
    _write_json(config_path, doc)
    if throughput:
        doc['throughput'] = throughput
        _write_json(config_path, doc)
        analytics.emit('metadata', data={'throughput': throughput})
    analytics.start_segment(resume_step=trainer.step)
    analytics.emit("metadata", data={"dataset": {**doc["data"], "sampler": sampler.exposure_report()}, "provenance": doc})
    if not resumed:
        analytics.emit("lifecycle", phase="recording_catalogue")
        item_by_id = {item.view_id: item for item in items}
        views_by_id = {v["view_id"]: v for v in [*train_views, *val_views]}
        def catalogue():
            for split, examples in (("train", data), ("val", val_data)):
                for view_id in examples.ids():
                    ex, item, view = examples.get(view_id), item_by_id.get(view_id), views_by_id.get(view_id, {})
                    yield dict(view_id=view_id, split=split, task=ex.encoded.task,
                               family_id=item.family_id if item else view.get("family_id"),
                               repo=item.repo if item else view.get("repo"), pool=item.pool if item else view.get("pool"),
                               label_state=ex.label_state, quality_weight=ex.weight,
                               prompt_tokens=ex.encoded.prompt_tokens, completion_tokens=ex.encoded.completion_tokens,
                               sequence_tokens=len(ex.encoded.input_ids))
        analytics.emit_examples(catalogue())
        log(f"recorded example catalogue: {len(data.ids()) + len(val_data.ids())} views")
    _align_metrics(run_dir, trainer.step if resumed else 0)
    doc["resumed_from_step"] = trainer.step if resumed else None
    if profile and controller_state is None and not resumed and not (run_dir / "profile.json").exists():
        analytics.emit("lifecycle", phase="profiling")
        trainer.profile()
    if controller_state and resource_controller=='auto' and controller_benchmark_seconds:
        # Earn real timing evidence first. Both benchmark arms then restore the
        # same boundary; these initial main updates remain committed training.
        trainer._pending_controller_benchmark=controller_benchmark_seconds
    log(f"training from step {trainer.step} to {total_steps} (stage {stage}, device {device})")
    analytics.emit("lifecycle", phase="training", target_steps=total_steps, backend=backend.name)
    try:
        result = trainer.train(max_steps=stop_after)
    finally:
        if getattr(trainer,'stager',None):
            trainer.stager.close()
    if getattr(trainer,'_controller_benchmark_result',None):
        doc['controller_benchmark']=trainer._controller_benchmark_result
    if getattr(trainer,'controller',None):
        _write_json(run_dir/'resource_controller.json',trainer.controller.state_dict())

    previous_evaluations = []
    if (run_dir / "evaluations.json").exists():
        previous_evaluations = json.loads((run_dir / "evaluations.json").read_text(encoding="utf-8"))
    by_pass = {e["pass"]: e for e in previous_evaluations}
    by_pass.update({e["pass"]: e for e in trainer.evaluations})
    evaluations = [by_pass[k] for k in sorted(by_pass)]
    _write_json(run_dir / "evaluations.json", evaluations)

    if trainer.stopped in ("session_budget", "user_requested") or (trainer.stopped is None and trainer.step < total_steps):
        # stopped on request or on the session budget: leave a checkpoint to resume from, finalize nothing
        latest = ck.latest_checkpoint(trainer.checkpoint_dir)
        if latest is None or latest.name != f"ckpt-step{trainer.step:06d}":
            trainer.save_checkpoint()
        why = "session_budget" if trainer.stopped == "session_budget" else "interrupted"
        doc.update(state="interrupted", steps_done=trainer.step, interrupted_by=why)
        _write_json(config_path, doc)
        analytics.emit("metadata", data={"provenance": doc})
        analytics.consolidate("interrupted")
        log(f"stopped ({why}) at step {trainer.step} of {total_steps}; run it again to resume from the checkpoint")
        return {"steps": trainer.step, "stopped": why, "state": "interrupted", "best": trainer.best}

    selected = _restore_selected(trainer, peft_model, head)
    _write_json(run_dir / "exposure.json", sampler.exposure_report())
    analytics.emit("lifecycle", phase="calibrating")
    calibration = _calibrate(peft_model, head, calibration_set, cfg, stage, selected, pad_id=pad_id, device=device, dtype=dtype)
    analytics.emit("evaluation", phase="calibration", step=selected["step"], result=calibration)
    _write_json(run_dir / "calibration.json", calibration)
    hd.save_head(head, run_dir / "decision_head.pt",
                 {**meta, "trained": stage == 2, "calibration": "calibration.json", "selected": selected}, backend=backend)
    adapter_state = {n: p.detach().cpu() for n, p in peft_model.state_dict().items() if "lora_" in n}
    peft_model.save_pretrained(run_dir / "adapter", state_dict=adapter_state)
    state = "finished" if trainer.stopped == "finished" else "early_stopped"
    warnings = _warnings(trainer, evaluator)
    for w in warnings:
        log(f"warning: {w['detail']}")
    doc.update(state=state, steps_done=trainer.step, selected=selected, evaluations=len(evaluations), warnings=warnings,
               selection={"basis": evaluations[-1]["metric_name"] if evaluations else None, "gates": "not_evaluated"},
               adapter={"trainable_parameters": sum(p.numel() for p in peft_model.parameters() if p.requires_grad)})
    _write_json(config_path, doc)
    analytics.emit("metadata", data={"provenance": doc, "dataset": {**doc["data"], "sampler": sampler.exposure_report()}})
    analytics.consolidate(state)
    _write_outputs(run_dir)
    log(f"{state}: {trainer.step} steps; selected {selected['source']} at step {selected['step']}")
    return {"steps": trainer.step, "stopped": result["stopped"], "state": state, "best": trainer.best, "selected": selected}


def _warnings(trainer, evaluator) -> list[dict]:
    """What a reader of the run must know about how its state was chosen."""
    out = []
    if evaluator is None:
        out.append({"code": "no_validation_selection", "detail": "the validation split has no selection examples: nothing was evaluated "
                                                                  "and the final state is kept"})
    tail = trainer.after_last_evaluation() if evaluator is not None else None
    if tail:
        out.append({"code": "steps_beyond_last_evaluation", "detail": tail})
    return out


def _restore_selected(trainer, peft_model, head) -> dict:
    """Load the selected checkpoint's adapter and head (the best on validation) unless the final state already is that one."""
    best = trainer.best
    if best is None:
        return {"source": "final", "step": trainer.step, "checkpoint": None, "metric": None}
    if best["step"] != trainer.step:
        ck.load_weights(trainer.checkpoint_dir / best["checkpoint"], model=peft_model, head=head)
    return {"source": "selected_checkpoint", "step": best["step"], "checkpoint": best["checkpoint"], "metric": best["metric"],
            "pass": best["pass"]}


def _calibrate(model, head, examples, cfg, stage, selected, *, pad_id, device, dtype) -> dict:
    """Temperature scaling of the decision head on the validation calibration role (an interval only when negatives are few)."""
    _, logits = forward_examples(model, head, examples, pad_id=pad_id, device=device, dtype=dtype)
    keep, labels = decision_rows(examples)
    fit = cal.fit_temperature(logits[keep] if keep else torch.zeros(0, len(hd.LABELS)), labels, min_negatives=cfg.calibration.min_negatives)
    return {**fit, "role": "calibration", "head_trained": stage == 2, "n_examples": len(examples), "selected": selected}


def _bundle_identity(bundle: pathlib.Path) -> dict | None:
    """The hash of the unpacked bundle's manifest and its source revision, when the folder is an unpacked bundle."""
    manifest = bundle / "bundle_manifest.json"
    if not manifest.exists():
        return None
    revision = bundle / "SOURCE_REVISION"
    return {"manifest_sha256": _sha(manifest.read_bytes()),
            "source_revision": json.loads(revision.read_text(encoding="utf-8")) if revision.exists() else None}


# -- evaluation: a trained adapter (or the untouched base) answers the validation views ------------------------------------------------------------

EVAL_SCHEMA = "finetune-eval/1"
EVAL_OUTPUTS_SCHEMA = "eval-outputs/1"
TRAINED_CONTROLS = ("B2", "B3", "B4", "B5")


def _sha_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def _baseline_problem(baseline: str, adapter_run, trained: dict | None) -> str | None:
    """Why this baseline cannot be evaluated with these inputs (None when it can)."""
    if baseline in ("B1", "B6") or baseline not in ("B0", *TRAINED_CONTROLS):
        return (f"baseline {baseline} cannot be evaluated here: B1 needs its own separately pinned model run and B6 needs "
                "verified preference pairs and a preference stage; the others are B0, B2, B3, B4 and B5")
    if baseline == "B0":
        return "B0 is the untouched base: it takes no adapter" if adapter_run else None
    if not adapter_run or trained is None:
        return f"{baseline} is a trained control: give the adapter run (adapter_run) it was trained as"
    stage, mode, invariance = trained["stage"], trained["loss_mode"], trained["invariance"]
    if baseline == "B5":
        return None if invariance else "B5 needs a run with the invariance loss on; this run's invariance is off"
    if invariance:
        return f"{baseline} is not an invariance run; this adapter was trained with the invariance loss on (that is B5)"
    want = {"B2": (1, "per_example_completion_ce"), "B3": (1, "present_field_normalized_ce"), "B4": (2, None)}[baseline]
    if stage != want[0] or (want[1] and mode != want[1]):
        return (f"{baseline} is a stage {want[0]} run" + (f" with the {want[1]} loss" if want[1] else "")
                + f"; this adapter is stage {stage} with the {mode} loss")
    return None


def _trained_run(adapter_run: pathlib.Path, cfg, resolved_sha: str, bundle: dict | None, tokenizer=None) -> dict:
    """The identity of the training run an adapter came from, checked against this evaluation (same bundle, dataset, config, serializer)."""
    path = adapter_run / "run_config.json"
    if not path.exists():
        raise RunnerError(f"{adapter_run} has no run_config.json: it is not a training run")
    rc = json.loads(path.read_text(encoding="utf-8"))
    if rc.get("schema") != RUN_SCHEMA or rc.get("task") != "finetune":
        raise RunnerError(f"{path} is not a {RUN_SCHEMA} fine-tune run config")
    if rc.get("state") not in ("finished", "early_stopped"):
        raise RunnerError(f"the adapter run did not finish (state {rc.get('state')!r}); evaluate a finished run")
    if not bundle or (rc.get("bundle") or {}).get("manifest_sha256") != bundle.get("manifest_sha256"):
        raise RunnerError("the adapter was trained from another bundle than the one this evaluation runs in (the bundle manifest hash differs)")
    for key, want in (("resolved_config_sha256", resolved_sha), ("model", cfg.model.id), ("model_revision", cfg.model.revision), ("tokenizer_revision", cfg.model.tokenizer_revision)):
        if rc.get(key) != want:
            raise RunnerError(f"the adapter run's {key} differs from this evaluation's")
    if (rc.get("dataset") or {}).get("manifest_sha256") != cfg.data.manifest_sha256:
        raise RunnerError("the adapter run used another dataset manifest than this evaluation")
    if (rc.get('prompt_text') or {}).get('serialization') != cfg.model.serialization:
        raise RunnerError('the adapter run used another serialization protocol')
    hash_tokenizer = tokenizer
    if hash_tokenizer is None:
        from types import SimpleNamespace
        hash_tokenizer = SimpleNamespace(chat_template=(rc.get('prompt_text') or {}).get('chat_template'),
                                         apply_chat_template=lambda *args, **kwargs: '')
    if (rc.get("prompt_text") or {}).get("serialization_hash") != sz.serialization_hash(cfg.model.serialization, hash_tokenizer):
        raise RunnerError("the adapter run used another serializer than this code")
    for name in ("adapter/adapter_model.safetensors", "adapter/adapter_config.json", "decision_head.pt", "calibration.json"):
        if not (adapter_run / name).exists():
            raise RunnerError(f"{adapter_run / name} is missing")
    # Authenticate the downloaded run before loading any tensor. The manifest is
    # also checked by the application importer; analytics alone are insufficient.
    output_manifest = adapter_run / 'outputs_manifest.json'
    if not output_manifest.is_file():
        raise RunnerError('the adapter has no outputs manifest to authenticate its downloaded identity')
    identities = json.loads(output_manifest.read_text(encoding='utf-8')).get('files', {})
    for name in ('run_config.json', 'resolved_config.json', 'adapter/adapter_model.safetensors', 'adapter/adapter_config.json',
                 'decision_head.pt', 'calibration.json'):
        path = adapter_run / name
        if not path.is_file() or identities.get(name, {}).get('sha256') != _sha_file(path):
            raise RunnerError(f'the adapter output identity does not match its manifest: {name}')
    if _sha_file(adapter_run / 'resolved_config.json') != resolved_sha:
        raise RunnerError('the adapter original resolved config differs from the training configuration')
    adapter_config = json.loads((adapter_run / 'adapter/adapter_config.json').read_text(encoding='utf-8'))
    software_base = rc.get('smoke_test') is True and not adapter_config.get('base_model_name_or_path')
    if (not software_base and adapter_config.get('base_model_name_or_path') != cfg.model.id) or adapter_config.get('revision') not in (None, cfg.model.revision):
        raise RunnerError('the adapter configuration names an incompatible base model or revision')
    loss = rc.get("loss") or {}
    return {"rc": rc, "run_config_sha256": _sha_file(adapter_run / 'run_config.json'), "stage": rc.get("stage"), "loss_mode": loss.get("mode"),
            "invariance": bool(loss.get("invariance_enabled")), "state": rc.get("state"), "adapter_sha256": _sha_file(adapter_run / "adapter" / "adapter_model.safetensors")}


def evaluate(*, run_dir, data_dir, tokenizer, model, cfg, arm: str, baseline: str, adapter_run=None, split: str = "val", role: str = "selection",
             tasks=("discovery", "repair"), max_views: int | None = None, batch_size: int = 4, max_new_tokens: dict | None = None,
             smoke_test: bool = False, device: str | None = None, bundle: dict | None = None, log=lambda message: None, backend=None,
             evaluation_data=None, evaluation_manifest_sha256=None) -> dict:
    """Answer the ``split``/``role`` views with the untouched base (``adapter_run`` None) or with a finished run's adapter, greedily, and
    write the attempts for the shared evaluator with the identity an importer needs. Nothing is dropped: every view is attempted or listed
    as excluded with its reason, and a capped evaluation says it is partial."""
    run_dir, data_dir = pathlib.Path(run_dir), pathlib.Path(data_dir)
    training_data_dir = data_dir
    adapter_run = pathlib.Path(adapter_run) if adapter_run else None
    if (split, role) not in (('val', 'selection'), ('val', 'calibration'), ('test', 'untouched_test')):
        raise RunnerError('Evaluation requires validation selection/calibration or the explicit untouched test partition.')
    resolved_path = run_dir / rs.FILE
    if not resolved_path.exists():
        raise RunnerError(f"{resolved_path} does not exist: resolve the run first")
    training_manifest = _check_dataset(data_dir, cfg)
    if not evaluation_data and training_manifest.get('coverage_policy', {}).get('schema') == 'mined-corpus/1':
        raise RunnerError('Generated security evaluation requires separately reviewed ground truth. This mined corpus assigns all records to training.')
    readiness = ed.preflight(data_dir, cfg, evaluation_data, evaluation_manifest_sha256, split=split, role=role, tasks=tasks)
    manifest = readiness['manifest']
    resolved_sha = _sha(resolved_path.read_bytes())
    trained = _trained_run(adapter_run, cfg, resolved_sha, bundle, tokenizer=tokenizer) if adapter_run else None
    problem = _baseline_problem(baseline, adapter_run, trained)
    if problem:
        raise RunnerError(problem)
    data_dir = pathlib.Path(evaluation_data) if evaluation_data else data_dir
    backend = backend or select_backend(device or "auto")
    device, dtype = str(backend.device), backend.dtype
    facts = model_facts(model)
    smoke_test = smoke_test or facts["parameters"] < SMOKE_PARAMETER_LIMIT

    head = None
    temperature = None
    if trained:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, str(adapter_run / "adapter"))
        if trained["stage"] == 2:
            loaded, meta = hd.load_head(adapter_run / "decision_head.pt")
            if meta.get("trained"):
                head = loaded
        temperature = float(json.loads((adapter_run / "calibration.json").read_text(encoding="utf-8")).get("temperature") or 1.0)
    if cfg.precision.mode == 'cpu_weight_offload':
        from training.finetune import offload
        offload.enable(model, pin=cfg.training.pin_frozen_weights and backend.name == 'cuda')
    if device != "cpu" and getattr(model, "hf_device_map", None) is None:
        model.to(device)
    if head is not None:
        head = head.to(device)

    if batch_size == 0:
        batch_size = infer.automatic_batch_size(device)
        log(f'Automatic evaluation generation batch size: {batch_size}; CUDA OOM retries use smaller batches.')
    if batch_size < 1: raise RunnerError('Evaluation batch size must be zero (automatic) or positive.')
    views = readiness['views']
    available = len(views)
    if max_views is not None and max_views < 1:
        raise ed.EvaluationBlocked('empty_tasks', 'Evaluation max_views must be positive; zero is not a successful evaluation.')
    if max_views is not None:
        views = views[:max_views]
    records = infer.load_record_facts(data_dir)
    limits = max_new_tokens or {"discovery": cfg.evaluation.greedy_max_new_tokens, "repair": cfg.evaluation.repair_max_new_tokens}
    rows, info = infer.evaluate_views(model, tokenizer, views, records, arm=arm, tasks=tuple(tasks), max_new_tokens=limits, device=device,
                                      dtype=dtype, batch_size=batch_size, head=head, temperature=temperature or 1.0, log=log, context=cfg.context, protocol=cfg.model.serialization)
    run_dir.mkdir(parents=True, exist_ok=True)
    at.write_attempts(run_dir / "attempts.parquet", rows)
    from training.finetune import sft_evaluation, generation_validation
    raw_generations = info.pop('raw_generations', [])
    with (run_dir / 'raw_generations.jsonl').open('w', encoding='utf-8') as raw:
        for generation in raw_generations:
            raw.write(json.dumps(generation, ensure_ascii=False) + '\n')
    _write_json(run_dir / 'generation_validation.json', generation_validation.measure(raw_generations, views, records, data_dir))
    security_report = sft_evaluation.from_dataset(rows, data_dir, training_data_dir=training_data_dir, excluded=info['excluded'])
    _write_json(run_dir / 'security_validation.json', security_report['all'])
    if cfg.model.serialization == 'sft_native_chat_v1':
        _write_json(run_dir / 'sft_evaluation.json', security_report)
    adapter = None
    if trained:
        adapter = {"sha256": trained["adapter_sha256"], "run_config_sha256": trained["run_config_sha256"],
                   "config_sha256": trained["rc"].get("config_sha256"), "stage": trained["stage"], "loss_mode": trained["loss_mode"],
                   "invariance": trained["invariance"], "state": trained["state"], "purpose": trained["rc"].get("purpose"),
                   "steps_done": trained["rc"].get("steps_done"), "bundle_manifest_sha256": (trained["rc"].get("bundle") or {}).get("manifest_sha256"),
                   "head_trained": head is not None, "temperature": temperature}
    config = {"schema": EVAL_SCHEMA, "state": "finished", "baseline": baseline, "arm": arm, "split": split, "role": role, "tasks": list(tasks),
              "model": cfg.model.id, "model_revision": cfg.model.revision, "tokenizer_revision": cfg.model.tokenizer_revision,
              "dataset": readiness['evaluation_dataset'],
              "training_dataset": readiness['training_dataset'], "evaluation_dataset": readiness['evaluation_dataset'],
              "evaluation_data": {key: value for key, value in readiness.items() if key not in ('views', 'manifest')},
              **({"external_data": manifest["external"]} if manifest.get("external") else {}),
              "resolved_config_sha256": resolved_sha, "bundle": bundle, "serialization": {"id": cfg.model.serialization, "hash": sz.serialization_hash(cfg.model.serialization, tokenizer),
                  **({'chat_template': tokenizer.chat_template} if cfg.model.serialization in (sz.CHAT_ID, 'sft_native_chat_v1', *sz.SECURITY_PROTOCOLS) else {})},
              "rag_enabled": cfg.rag.enabled,
              "input_policy_hash": cfg.input_policy_hash(), "context_policy": cfg.context.policy, "adapter": adapter, "head": info["head"],
              "generation": info["generation"], "views": {"expected": [v["view_id"] for v in views]}, "excluded": info["excluded"],
              "status_counts": info["status_counts"], "max_views": max_views, "partial": max_views is not None and max_views < available,
              "model_loaded": facts, "smoke_test": smoke_test, "device": device, "dtype": str(dtype), "environment": environment_snapshot(backend),
              "compute_choice": os.environ.get("ZTSA_COMPUTE_CHOICE") or None, "timestamp": _now()}
    _write_json(run_dir / "evaluation_config.json", config)
    _write_json(run_dir / 'evaluation_status.json', {'schema': 'evaluation-readiness/1', 'state': 'finished',
                'baseline': baseline, 'training_dataset': readiness['training_dataset'],
                'evaluation_dataset': readiness['evaluation_dataset'], 'timestamp': _now()})
    files = {n: {"sha256": _sha((run_dir / n).read_bytes()), "bytes": (run_dir / n).stat().st_size}
             for n in ("evaluation_config.json", "attempts.parquet", rs.FILE, 'raw_generations.jsonl', 'security_validation.json',
                       'generation_validation.json', 'evaluation_status.json')}
    if cfg.model.serialization == 'sft_native_chat_v1':
        path = run_dir / 'sft_evaluation.json'
        files[path.name] = {'sha256': _sha(path.read_bytes()), 'bytes': path.stat().st_size}
    _write_json(run_dir / "eval_outputs_manifest.json", {"schema": EVAL_OUTPUTS_SCHEMA, "files": files})
    archive_name = f'eval_{baseline.lower()}_outputs.zip'
    with zipfile.ZipFile(run_dir / archive_name, "w", zipfile.ZIP_DEFLATED) as z:
        for name in [*files, "eval_outputs_manifest.json"]:
            info_ = zipfile.ZipInfo(name, _FIXED_TIME)
            info_.compress_type, info_.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
            z.writestr(info_, (run_dir / name).read_bytes())
    log(f"evaluated {baseline} ({arm}): {len(rows)} attempts, {len(info['excluded'])} excluded")
    return {"attempts": len(rows), "state": "finished", "excluded": len(info["excluded"]), "baseline": baseline,
            "path": str(run_dir / archive_name)}


# -- the command line ------------------------------------------------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m training.finetune.runner", description="Fine-tune from an unpacked training bundle.")
    p.add_argument("--bundle", required=True, help="the unpacked bundle: resolved_config.json and data/")
    p.add_argument("--run-dir", required=True, help="where the run is written (and resumed from)")
    p.add_argument("--stage", type=int, choices=(1, 2), default=1, help="1: language-model loss; 2: adds the decision head and pair losses")
    p.add_argument("--steps", type=int, default=None, help="optimizer steps (default: the contract's pilot family passes)")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--eval-max", type=int, default=None, help="cap on the validation examples scored at each evaluation")
    p.add_argument("--purpose", choices=("train", "smoke"), default="train", help="smoke: a short GPU run to check memory and speed")
    p.add_argument("--loss-mode", choices=ls.MODES, default=None,
                   help="per_example_completion_ce (the ordinary control, default) or present_field_normalized_ce (field-balanced targets)")
    p.add_argument("--session-minutes", type=float, default=None,
                   help="stop with a checkpoint before this many minutes are used up (a paid runtime); run again to resume")
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--evaluate", action="store_true", help="evaluate instead of training: answer the validation views and write attempts")
    p.add_argument("--baseline", default="B0", help="with --evaluate: B0 (the untouched base) or B2, B3, B4, B5 (a trained control)")
    p.add_argument("--adapter-run", default=None, help="with --evaluate: the finished training run folder whose adapter, head and calibration are used")
    p.add_argument('--evaluation-data', default=None, help='independent frozen security benchmark directory; training data remains bound to the original bundle')
    p.add_argument('--evaluation-manifest-sha256', default=None, help='expected independent benchmark manifest SHA256')
    p.add_argument('--training-data', default=None, help='with --evaluate: original training dataset directory, verified against the bundle resolved manifest (avoids copying frozen data)')
    p.add_argument('--preflight-only', action='store_true', help='with --evaluate: verify dataset/model artifact identities and stop before loading model weights')
    p.add_argument("--arm", default=None, help="with --evaluate: the label of this model in the results (default: base or adapter)")
    p.add_argument('--split', default='val', choices=('val', 'test'), help='with --evaluate: validation or final untouched test')
    p.add_argument("--role", default="selection", choices=("selection", "calibration", 'untouched_test'), help="with --evaluate: partition role")
    p.add_argument("--tasks", default="discovery,repair", help="with --evaluate: comma separated tasks (discovery, repair)")
    p.add_argument("--max-views", type=int, default=None, help="with --evaluate: cap on the views (the evaluation is then marked partial)")
    p.add_argument("--batch-size", type=int, default=0, help="with --evaluate: prompts per batch, 0 chooses from free accelerator memory")
    p.add_argument("--max-new-tokens", type=int, default=None,
                   help="with --evaluate: new tokens per answer for every task (a smoke test only: the contract's limits are the default)")
    p.add_argument("--device", choices=("auto", "cuda", "cpu", "tpu"), default="auto",
                   help="cpu: run on the processor, with the model held whole in 32 bits (no 4-bit loading); for --smoke-model or --tiny only, because the pinned base would need about 30 GB")
    p.add_argument("--tiny", action="store_true", help="software smoke test on a tiny random model (not a training result)")
    p.add_argument("--stop-file", default=None, help="stop and checkpoint at an optimizer boundary when this file exists")
    p.add_argument('--resource-controller',choices=('auto','observe','off'),default='auto',
                   help='runtime execution control; does not change checkpoint configuration identity')
    p.add_argument('--controller-benchmark-seconds',type=float,default=0,
                   help='matched comparison budget including tuning and I/O, 0 disables, maximum 180 seconds')
    p.add_argument("--tpu-validation-report", default=None, help="identity-bound selected-model TPU preflight report")
    p.add_argument("--smoke-model", default=None, metavar="REPO_ID",
                   help="dev only: a small public model (for example Qwen/Qwen2.5-Coder-0.5B) loaded in 4 bit on a CUDA GPU to exercise the loader, "
                        "the fp16 path and the real tokenizer; never the pinned base, and the run is recorded as a smoke test")
    args = p.parse_args(argv)
    bundle, run_dir = pathlib.Path(args.bundle), pathlib.Path(args.run_dir)
    analytics = None
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        if args.evaluate and ((run_dir / 'evaluation_config.json').exists() or (run_dir / 'eval_outputs_manifest.json').exists()):
            raise ed.EvaluationBlocked('output_directory_in_use', 'This directory already contains an evaluation. Existing outputs were preserved.',
                                       'Use a new --run-dir for each model, benchmark cohort and evaluation attempt.')
        source, target = bundle / rs.FILE, run_dir / rs.FILE
        if not source.exists():
            raise RunnerError(f"{source} does not exist: the bundle is not unpacked here")
        if not target.exists():
            shutil.copyfile(source, target)
        elif target.read_bytes() != source.read_bytes():
            raise RunnerError(f"{run_dir} was resolved against other inputs than this bundle; use a new run folder")
        cfg = rs.read_resolved(run_dir)
        if not args.evaluate and not args.tiny and not args.smoke_model:
            from training.finetune import quality_gate, runtime_resources
            def check_runtime_before_diagnostics():
                if args.device == 'tpu' or (args.device == 'auto' and cfg.precision.mode == 'tpu_int4_bf16'):
                    return
                detected = select_backend(args.device)
                report = runtime_resources.preflight(bundle, run_dir, cfg, backend=detected)
                if report['state'] == 'ready':
                    runtime_resources.apply_process_policy(report)
                _write_json(run_dir / 'resource_report.json', report)
                if report['state'] != 'ready':
                    raise RunnerError('Runtime resources are insufficient: ' + '; '.join(report.get('reasons', [])))
                from training.finetune.runtime_compatibility import validate_runtime
                compatibility = validate_runtime(detected)
                _write_json(run_dir / 'runtime_compatibility.json', compatibility)
                if compatibility['state'] != 'ready':
                    raise RunnerError('Runtime kernel validation failed: ' + str(compatibility.get('detail')))
            quality_gate.ensure_diagnostics(bundle, run_dir, purpose=args.purpose, device=args.device,
                                            resource_check=check_runtime_before_diagnostics)
        if not args.evaluate:
            analytics = AnalyticsWriter(run_dir, identity={"model": cfg.model.id, "model_revision": cfg.model.revision,
                                                          "bundle": _bundle_identity(bundle)})
            analytics.emit("metadata", data={"configuration": cfg.model_dump(mode="json"),
                                              "provenance": {"model": cfg.model.id, "model_revision": cfg.model.revision,
                                                             "resolved_config_sha256": _sha(target.read_bytes()),
                                                             "bundle": _bundle_identity(bundle)}})
            analytics.emit("lifecycle", phase="initializing")
        if (args.training_data or args.preflight_only) and not args.evaluate:
            raise RunnerError('--training-data and --preflight-only apply only to evaluation')
        data_dir = pathlib.Path(args.training_data) if args.training_data else bundle / "data"
        evaluation_dir = pathlib.Path(args.evaluation_data) if args.evaluation_data else None
        evaluation_sha = args.evaluation_manifest_sha256
        if args.evaluate:
            # Data and adapter identity checks precede backend, tokenizer and model
            # loading. A failed preflight never writes attempts or success metrics.
            if evaluation_dir is None and (bundle / 'eval_data').is_dir():
                evaluation_dir = bundle / 'eval_data'
                bundle_doc = json.loads((bundle / 'bundle_manifest.json').read_text(encoding='utf-8'))
                evaluation_sha = evaluation_sha or (bundle_doc.get('evaluation_dataset') or {}).get('manifest_sha256')
            readiness = ed.preflight(data_dir, cfg, evaluation_dir, evaluation_sha, split=args.split,
                         role='untouched_test' if args.split == 'test' else args.role,
                         tasks=tuple(t.strip() for t in args.tasks.split(',') if t.strip()))
            if args.max_views is not None and args.max_views < 1:
                raise ed.EvaluationBlocked('empty_tasks', 'Evaluation max_views must be positive.')
            if args.batch_size < 0 or (args.max_new_tokens is not None and args.max_new_tokens < 1):
                raise ed.EvaluationBlocked('invalid_generation', 'Generation batch size must be nonnegative and token limits positive.')
            try:
                trained = _trained_run(pathlib.Path(args.adapter_run), cfg, _sha(target.read_bytes()), _bundle_identity(bundle)) if args.adapter_run else None
                problem = _baseline_problem(args.baseline, args.adapter_run, trained)
                if problem:
                    raise RunnerError(problem)
            except (RunnerError, OSError, ValueError) as error:
                raise ed.EvaluationBlocked('missing_adapter' if args.adapter_run and
                                           (not pathlib.Path(args.adapter_run).is_dir() or 'missing' in str(error).lower() or 'no run_config' in str(error).lower())
                                           else 'incompatible_model_protocol', str(error),
                                           'Supply the finished matching adapter run with its original configuration, output manifest and pinned model/tokenizer/protocol.') from error
            if args.preflight_only:
                _write_json(run_dir / 'evaluation_status.json', {key: value for key, value in readiness.items() if key not in ('manifest', 'views')})
                print(json.dumps({'state': 'ready', 'inference': 'not_run', 'eligible_views': len(readiness['views'])}))
                return 0
        seed = cfg.training.pilot_seed if args.seed is None else args.seed
        if args.evaluate and args.adapter_run and args.seed is None:
            trained_seeds = (json.loads((pathlib.Path(args.adapter_run) / "run_config.json").read_text(encoding="utf-8")).get("seeds") or [])
            seed = trained_seeds[0] if trained_seeds else seed                      # the tiny base is rebuilt from the seed it was trained with
        if args.smoke_model and args.tiny:
            raise RunnerError("--smoke-model and --tiny are alternatives: choose one")
        cpu_run = args.device == "cpu"
        if cpu_run and not (args.smoke_model or args.tiny):
            raise RunnerError("--device cpu is for a small public model (--smoke-model) or --tiny: the pinned base held in 32 bits would need about 30 GB")
        if cpu_run:
            os.environ["CUDA_VISIBLE_DEVICES"] = ""                      # before anything asks the driver: this process never touches the card
        requested = "tpu" if args.device == "auto" and cfg.precision.mode == "tpu_int4_bf16" and not args.tiny else args.device
        backend = select_backend(requested)
        if backend.name == "tpu":
            from training.finetune.tpu_quantization import validate_tpu_config
            validate_tpu_config(cfg)
            if args.smoke_model:
                raise RunnerError("TPU uses the configured pinned model; --smoke-model is supported only on CPU/CUDA. Use the TPU runtime check for a tiny smoke test.")
            if not args.tiny and not args.evaluate:
                evidence_path = pathlib.Path(args.tpu_validation_report) if args.tpu_validation_report else run_dir / "tpu_7b_check.json"
                if not evidence_path.is_file():
                    raise RunnerError("TPU training requires the selected-model preflight. Run python -m training.finetune.tpu_validation --bundle BUNDLE --output RUN/tpu_7b_check.json first.")
                evidence = json.loads(evidence_path.read_text())
                if (evidence.get("state") != "passed" or evidence.get("backend") != "tpu"
                        or evidence.get("resolved_config_sha256") != _sha(target.read_bytes())
                        or evidence.get("revision") != cfg.model.revision or evidence.get("model") != cfg.model.id
                        or evidence.get("context_tokens") != cfg.context.max_total_tokens or evidence.get("rank") != cfg.lora.r):
                    raise RunnerError("TPU preflight evidence does not match this model/configuration; rerun the selected-model check")
                if evidence_path.resolve() != (run_dir / "tpu_7b_check.json").resolve():
                    shutil.copyfile(evidence_path, run_dir / "tpu_7b_check.json")
        elif cfg.precision.mode == "tpu_int4_bf16" and not args.tiny:
            raise RunnerError("The TPU INT4 profile requires --device tpu; CUDA NF4 is a separate experiment")
        if not args.tiny and not args.smoke_model:
            from training.finetune import runtime_resources
            resources = runtime_resources.preflight(bundle, run_dir, cfg, backend=backend)
            if resources['state'] == 'ready':
                runtime_resources.apply_process_policy(resources)
            _write_json(run_dir / 'resource_report.json', resources)
            if resources['state'] != 'ready':
                raise RunnerError('Runtime resources are insufficient: ' + '; '.join(resources.get('reasons', [])))
            from training.finetune.runtime_compatibility import validate_runtime
            compatibility = validate_runtime(backend)
            _write_json(run_dir / 'runtime_compatibility.json', compatibility)
            if compatibility['state'] != 'ready':
                raise RunnerError('Runtime kernel validation failed: ' + str(compatibility.get('detail')))
            if backend.name == 'cuda':
                torch.cuda.set_device(backend.device)
        smoke = None
        if args.tiny:
            texts = [v["context_text"] + v["response_json"] for s in ("train", "val") for v in load_views(data_dir, s)]
            tokenizer = tiny_tokenizer(texts + [sz.render_prompt(t, "x") for t in ct.TASK_TEXT])
            torch.manual_seed(seed)
            model = md.build_tiny_model(len(tokenizer), seed=seed)
            if backend.name == "tpu":
                from training.finetune.tpu_quantization import quantize_model
                model = quantize_model(model.to(torch.bfloat16))
        else:
            if backend.name == "cpu" and not cpu_run:
                raise RunnerError("no CUDA device: the 7B fine-tune needs a GPU runtime (Colab or Kaggle); use --tiny for a software test, or --device cpu with --smoke-model")
            if args.smoke_model:
                revision = hub.resolve_revision(args.smoke_model)
                smoke = {"id": args.smoke_model, "revision": revision}
                tokenizer = hub.load_tokenizer(args.smoke_model, revision)
                model = md.load_base_model(cfg, revision=revision, model_id=args.smoke_model, quantize=False if cpu_run else None,
                                           compute_dtype=torch.float32 if cpu_run else None)
            else:
                if analytics:
                    analytics.emit("lifecycle", phase="loading_tokenizer", backend=backend.name)
                tokenizer = hub.load_tokenizer(cfg.model.id, cfg.model.tokenizer_revision)
                if analytics:
                    analytics.emit("lifecycle", phase="loading_model", backend=backend.name)
                if backend.name == "tpu":
                    from training.finetune.tpu_quantization import load_tpu_model
                    model = load_tpu_model(cfg, revision=cfg.model.revision, backend=backend)
                else:
                    model = md.load_base_model(cfg, revision=cfg.model.revision, compute_dtype=backend.dtype,
                                               device_map={'': str(backend.device)})
        if args.evaluate:
            arm = args.arm or ("adapter" if args.adapter_run else "base")
            summary = evaluate(run_dir=run_dir, data_dir=data_dir, tokenizer=tokenizer, model=model, cfg=cfg, arm=arm, baseline=args.baseline,
                               adapter_run=args.adapter_run, split=args.split, role='untouched_test' if args.split == 'test' else args.role,
                               tasks=tuple(t for t in args.tasks.split(",") if t),
                               max_views=args.max_views, batch_size=args.batch_size, smoke_test=args.tiny or smoke is not None, bundle=_bundle_identity(bundle),
                               max_new_tokens=({t: args.max_new_tokens for t in ("discovery", "repair")} if args.max_new_tokens else None),
                               evaluation_data=evaluation_dir, evaluation_manifest_sha256=evaluation_sha,
                               device=str(backend.device), backend=backend, log=print)
            print(json.dumps({k: summary[k] for k in ("attempts", "state", "baseline")}))
            return 0
        summary = run(run_dir=run_dir, data_dir=data_dir, tokenizer=tokenizer, model=model, cfg=cfg, stage=args.stage,
                      total_steps=args.steps, seed=seed, resume=not args.no_resume, eval_max=args.eval_max, smoke_test=args.tiny,
                      purpose=args.purpose, bundle=_bundle_identity(bundle), loss_mode=args.loss_mode,
                      session_minutes=args.session_minutes, smoke_model=smoke, device=str(backend.device), log=print,
                      backend=backend, analytics=analytics, stop_file=args.stop_file,
                      resource_controller=args.resource_controller,controller_benchmark_seconds=args.controller_benchmark_seconds)
    except ed.EvaluationBlocked as e:
        _write_json(run_dir / 'evaluation_status.json', {**e.as_dict(), 'baseline': args.baseline, 'timestamp': _now()})
        print(f'blocked [{e.code}]: {e.detail}', file=sys.stderr)
        return 1
    except (RunnerError, planning.EmptyCorePool, rs.ResolveError, cfgmod.ConfigError) as e:
        if args.evaluate:
            _write_json(run_dir / 'evaluation_status.json', {'schema': 'evaluation-readiness/1', 'state': 'blocked',
                        'reason_code': 'incompatible_model_protocol', 'detail': str(e), 'timestamp': _now()})
        if analytics and not getattr(analytics,'_closed',False):
            analytics.emit("failure", error={"type": type(e).__name__, "message": str(e)})
            analytics.consolidate("failed")
        print(f"error: {e}", file=sys.stderr)
        return 1
    except BaseException as error:
        if args.evaluate:
            _write_json(run_dir / 'evaluation_status.json', {'schema': 'evaluation-readiness/1', 'state': 'failed',
                        'reason_code': 'runtime_failure', 'detail': str(error), 'timestamp': _now()})
        if analytics and not getattr(analytics,'_closed',False):
            analytics.emit("failure", error={"type": type(error).__name__, "message": str(error)})
            analytics.consolidate("failed")
        raise
    finally:
        if analytics:
            analytics.close()
    print(json.dumps({k: summary[k] for k in ("steps", "stopped", "state")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
