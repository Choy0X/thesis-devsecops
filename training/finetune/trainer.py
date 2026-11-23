"""The training loop: accumulation to the effective batch, schedule, clipping, checkpoints, exact resume, evaluation.

One optimizer step draws sequences from the sampler until the effective batch (32) is reached, runs them as micro-batches and
accumulates gradients scaled so that the sum equals the gradient of the whole batch. In stage 2 an example that has a pair partner
(the vulnerable and the protected version of one record) is drawn together with it, the two are encoded independently, and the pair
loss compares their decision scores; each such pair counts as two sequences. The optimizer is AdamW over the adapter and the
decision head with a cosine schedule and linear warm-up; gradients are clipped (the norm before clipping is logged); a loss that is
not finite stops the run. Checkpoints (by steps and by minutes) hold everything needed to continue exactly: an interrupted run that is
resumed finishes with the same adapter, head, sampler state and losses as one that never stopped. At the family passes of the
contract an evaluator can be called; the best result is kept as the selected checkpoint and training stops after the patience.
"""
from __future__ import annotations

import json
import math
import pathlib
import time
from dataclasses import dataclass

import torch

from training.finetune import checkpoint as ck, collate as co, head as hd, invariance as iv, losses as ls, model as md, tokenization as tk
from training.finetune.accelerator import select_backend
from training.finetune.offload import saved_activations

IDENTITY_KEYS = ("base_commit", "tokenizer_revision", "serialization_hash", "dataset_manifest_sha256", "config_sha256")


@dataclass
class Example:
    encoded: tk.Encoded
    label_state: str = "unreviewed"
    weight: float = 1.0
    pair_id: str | None = None
    role: str | None = None                 # "vulnerable" or "protected" inside a pair
    variant_of: str | None = None           # the view id of the example this is a harmless variant of
    transform: str | None = None            # the registry transform that made the variant
    view_id: str | None = None              # set by the dataset


class Dataset:
    """Examples by view id, with the partner of each paired example."""

    def __init__(self, examples: dict[str, Example]):
        self._ex = dict(examples)
        self._pairs: dict[str, dict[str, str]] = {}
        self._variants: dict[str, list[str]] = {}
        for vid, ex in self._ex.items():
            ex.view_id = ex.view_id or vid
            if ex.pair_id and ex.role:
                self._pairs.setdefault(ex.pair_id, {})[ex.role] = vid
            if ex.variant_of:
                self._variants.setdefault(ex.variant_of, []).append(vid)
        for vs in self._variants.values():
            vs.sort()

    def get(self, view_id: str) -> Example:
        return self._ex[view_id]

    def ids(self) -> list[str]:
        return sorted(self._ex)

    def variants(self, view_id: str) -> list[str]:
        return self._variants.get(view_id, [])

    def partner(self, view_id: str) -> str | None:
        ex = self._ex[view_id]
        other = {"vulnerable": "protected", "protected": "vulnerable"}.get(ex.role or "")
        return self._pairs.get(ex.pair_id or "", {}).get(other) if other else None


def make_optimizer(params, training):
    """The optimizer the run asked for. ``adamw_torch`` is the default; ``adafactor`` keeps no second-moment matrix per weight (less memory) and runs at the learning rate given;
    ``adamw_8bit`` stores its state in 8 bits (bitsandbytes, on the machine that trains)."""
    name = training.optimizer
    if name == 'adamw_cpu':
        from training.finetune.offload import CPUAdamW
        return CPUAdamW(params, lr=training.learning_rate, betas=(training.adam_beta1, training.adam_beta2), eps=training.adam_epsilon, weight_decay=training.weight_decay)
    if name == "adamw_torch":
        return torch.optim.AdamW(params, lr=training.learning_rate, betas=(training.adam_beta1, training.adam_beta2), eps=training.adam_epsilon, weight_decay=training.weight_decay)
    if name == "adafactor":
        from transformers.optimization import Adafactor
        return Adafactor(params, lr=training.learning_rate, scale_parameter=False, relative_step=False, warmup_init=False, weight_decay=training.weight_decay)
    if name == "adamw_8bit":
        try:
            import bitsandbytes as bnb
        except Exception as e:                     # the import can fail for more than one reason (no package, no matching CUDA library)
            raise RuntimeError(f"the 8-bit optimizer needs bitsandbytes, which cannot be loaded here ({type(e).__name__}); choose adamw_torch or adafactor") from e
        return bnb.optim.AdamW8bit(params, lr=training.learning_rate, betas=(training.adam_beta1, training.adam_beta2), eps=training.adam_epsilon, weight_decay=training.weight_decay)
    raise ValueError(f"unknown optimizer {name!r}")


def warmup_steps_of(training, total_steps: int) -> int:
    """Steps of warm-up: the number asked for, else the share asked for of the run (rounded up); never more than the run."""
    n = training.warmup_steps if training.warmup_steps is not None else math.ceil(training.warmup_ratio * total_steps)
    return min(int(n), total_steps)


def make_scheduler(optimizer, training, total_steps: int):
    """The learning-rate schedule: cosine (the default), linear, constant, or constant after the warm-up. ``constant`` has no warm-up."""
    from transformers import get_scheduler
    return get_scheduler(training.scheduler, optimizer, num_warmup_steps=warmup_steps_of(training, total_steps), num_training_steps=total_steps)


def make_warmup_cosine(optimizer, *, warmup_steps: int, total_steps: int):
    """Warm-up then cosine decay where the FIRST update already has a positive rate (the usual scheduler starts at zero, which would waste
    the first update of a short run). Update ``k`` (from 0) uses ``(k+1)/(warmup+1)`` of the base rate while ``k < warmup``, the base rate at
    ``k == warmup``, then a half cosine that stays above zero through the last update. The warm-up is clamped so a very short run still decays."""
    from torch.optim.lr_scheduler import LambdaLR
    total = max(1, int(total_steps))
    warm = max(0, min(int(warmup_steps), total - 1))

    def factor(step: int) -> float:
        if step < warm:
            return (step + 1) / (warm + 1)
        return 0.5 * (1.0 + math.cos(math.pi * min(1.0, (step - warm) / max(1, total - warm))))
    return LambdaLR(optimizer, factor)


class Trainer:
    def __init__(self, *, model, head, dataset: Dataset, sampler, training, lora, loss_cfg: ls.LossConfig, run_dir, pad_id: int,
                 seed: int, total_steps: int, meta: dict, device: str = "cpu", dtype: torch.dtype | None = None, evaluator=None,
                 eval_passes=None, patience: int | None = None, clock=time.monotonic, audit: bool = True, chunk: int = ls.DEFAULT_TOKEN_CHUNK,
                 session_seconds: float | None = None, pack_tokens: int | None = None, backend=None, analytics=None, stop_file=None):
        self.model, self.head, self.dataset, self.sampler = model, head, dataset, sampler
        self.training, self.lora, self.loss_cfg, self.meta = training, lora, loss_cfg, dict(meta)
        self.run_dir, self.pad_id, self.seed, self.total_steps = pathlib.Path(run_dir), pad_id, seed, total_steps
        self.device, self.dtype, self.clock, self.chunk = device, dtype or torch.float32, clock, chunk
        self.micro_batch_size = training.per_device_train_batch_size
        self.chunk = getattr(training, 'lm_loss_chunk_tokens', chunk) if chunk == ls.DEFAULT_TOKEN_CHUNK else chunk
        self.backend = backend or select_backend(str(device))
        if dtype is None:
            self.dtype = self.backend.dtype
        if backend is not None:
            self.device = str(backend.device)
        self.analytics, self.stop_file = analytics, pathlib.Path(stop_file) if stop_file else None
        self.lm_partner: dict[str, tuple[str, str]] = {}          # view id -> (twin view id, 'vulnerable' or 'protected'); set by set_twin_pairs
        if loss_cfg.lm_pair_weight > 0 and self.backend.name == 'tpu':
            raise ValueError('the twin margin loss reads one position of each row and is not built for the static TPU path')
        self.pack = bool(getattr(training, "pack_sequences", False))
        self.pack_tokens = pack_tokens
        if self.pack:
            if loss_cfg.decision_weight > 0 or loss_cfg.pair_enabled or loss_cfg.invariance_enabled:
                raise ValueError("training.pack_sequences is for the language-model loss alone: the decision head, the pair loss and the invariance loss read one example per row. "
                                 "Use stage 1 without the invariance loss, or turn packing off.")
            if not pack_tokens or pack_tokens < 1:
                raise ValueError("packing needs the longest row it may build (the context's total tokens)")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if audit:
            md.audit_adapter(model, model.config, lora)
        torch.manual_seed(seed)
        self.params = [p for p in model.parameters() if p.requires_grad] + list(head.parameters())
        self.optimizer = make_optimizer(self.params, training)
        self.scheduler = make_scheduler(self.optimizer, training, total_steps)
        cuda = self.backend.name == 'cuda'
        self.scaler = torch.amp.GradScaler("cuda") if md.needs_grad_scaler(self.dtype) and cuda else None
        self.policy = ck.Policy(training.checkpoint_every_optimizer_steps, training.checkpoint_every_minutes,
                                training.keep_recent_checkpoints)
        self.counters = {"step": 0, "sequences": 0, "tokens": 0, "eval_index": 0, "bad_evals": 0}
        self.history: list[dict] = []
        self.evaluator = evaluator
        self.eval_passes = list(eval_passes if eval_passes is not None else training.evaluate_at_family_passes)
        self.eval_every_steps = getattr(training, "evaluate_every_steps", None)
        self.patience = patience if patience is not None else training.early_stopping_patience_evaluations
        self.evaluations: list[dict] = []
        self.best: dict | None = None
        self.stopped: str | None = None
        self.session_seconds = session_seconds
        self._last_save_seconds = 0.0
        self._last_ckpt_time = clock()

    @property
    def step(self) -> int:
        return self.counters["step"]

    @property
    def checkpoint_dir(self) -> pathlib.Path:
        return self.run_dir / "checkpoints"

    def set_twin_pairs(self, pairs):
        """pairs: [(vulnerable view id, fixed twin view id)]. With loss_cfg.lm_pair_weight > 0 the two views of a pair are kept together in one device batch and the twin margin
        (losses.twin_margin_loss) is added to the language-model loss."""
        self.lm_partner = {}
        for vulnerable, fixed in pairs:
            if vulnerable in self.lm_partner or fixed in self.lm_partner or vulnerable == fixed:
                raise ValueError(f'twin pair {vulnerable}, {fixed}: a view is in at most one pair')
            self.lm_partner[vulnerable] = (fixed, 'vulnerable')
            self.lm_partner[fixed] = (vulnerable, 'protected')

    def _twin_loss(self, group, hidden, batch):
        """The twin margin of a group of exactly one vulnerable view and its fixed twin, else None."""
        if self.loss_cfg.lm_pair_weight <= 0 or len(group) != 2:
            return None
        roles = [self.lm_partner.get(e.view_id) for e in group]
        if None in roles or roles[0][0] != group[1].view_id or {r[1] for r in roles} != {'vulnerable', 'protected'}:
            return None
        v, f = (0, 1) if roles[0][1] == 'vulnerable' else (1, 0)
        status = ls.first_decision_tokens(batch['labels'], batch['field_ids'])
        if int(status[v]) == int(status[f]):
            return None                                           # the twins carry the same status: nothing to contrast
        a, b = status[v].expand(2), status[f].expand(2)
        gap, ok = ls.status_gap(hidden, md.lm_head_of(self.model), batch['labels'], batch['field_ids'], a, b)
        if not bool(ok.all()):
            return None
        return ls.twin_margin_loss(gap[v:v + 1], gap[f:f + 1], self.loss_cfg.lm_pair_margin)

    # -- one micro-batch -------------------------------------------------------------------------------------------------------
    @saved_activations
    def compute_loss(self, group: list[Example]):
        """(total loss, parts) of a micro-batch: the language-model loss and, in stage 2, the decision and rank losses."""
        deferred = getattr(self, '_defer_metrics', False)
        batch = getattr(self, '_prepared_batch', None)
        if batch is None:
            batch = co.collate([e.encoded for e in group], self.pad_id, label_states=[e.label_state for e in group],
                               weights=[e.weight for e in group])
        static = getattr(self,'_loss_metadata',None) or ((batch['labels'],batch['field_ids']) if deferred and batch['labels'].device.type=='cpu' else False)
        if self.backend.name == "tpu":
            batch = co.bucket_batch(batch, self.pad_id, self.pack_tokens or 4096)
            static = (batch["labels"], batch["field_ids"])
        target_tokens = sum(sum(v != tk.IGNORE for v in e.encoded.labels) for e in group)
        batch = {k: v.to(self.device) for k, v in batch.items()}
        with self.backend.autocast():
            hidden = md.hidden_states(self.model, batch["input_ids"], batch["attention_mask"])
            tl = ls.token_losses(hidden, md.lm_head_of(self.model), batch["labels"], batch["field_ids"], chunk=self.chunk,
                                 checkpoint=self.model.training, static=static)
        n = len(group)
        lm, _ = ls.language_model_loss(tl, n, self.loss_cfg, batch["weights"], defer_report=deferred)
        with torch.no_grad():
            _, per_example = ls.per_example_completion_ce(tl, n)
            _, fields = ls.field_normalized_ce(ls.TokenLosses(tl.loss.detach(), tl.example, tl.field, tl.valid), n, self.loss_cfg.field_weights,
                                               batch["weights"], defer_report=deferred)
        dec = rank = inv = None
        if self.loss_cfg.decision_weight > 0 or self.loss_cfg.pair_enabled or self.loss_cfg.invariance_enabled:
            logits = self.head(hd.pooled_prompt_state(hidden, batch["prompt_lengths"]))
            dec = hd.decision_loss(logits, batch["decision_labels"], batch["weights"])
            if self.loss_cfg.pair_enabled:
                rank = self._pair_loss(group, logits, batch)
            if self.loss_cfg.invariance_enabled:
                inv = self._invariance_loss(group, logits)
        twin = self._twin_loss(group, hidden, batch)
        total = ls.total_loss(lm, self.loss_cfg, decision=dec, rank=rank, invariance=inv, twin=twin)
        if not deferred:
            md.assert_finite(total, step=self.step + 1)
        return total, {"lm": lm.detach(), "decision": None if dec is None else dec.detach(),
                       "rank": None if rank is None else rank.detach(),
                       "invariance": None if inv is None else inv.detach(), "twin": None if twin is None else twin.detach(),
                       "fields": fields["unweighted"], "tokens": target_tokens, "per_example_completion_loss": per_example.detach() if deferred else per_example.detach().float().cpu().tolist(),
                       "padding_tokens": batch["input_ids"].numel() - sum(len(e.encoded.input_ids) for e in group)}

    def packs_of(self, groups: list[list[Example]]) -> list[list[Example]]:
        """The drawn examples as rows: consecutive examples go into one row while the row stays within ``pack_tokens`` tokens; an example longer than that is a row of its own. The order of
        the draw is kept, so a resumed run packs the same way."""
        packs: list[list[Example]] = []
        used = 0
        for group in groups:
            for e in group:
                n = len(e.encoded.input_ids)
                if packs and used + n <= self.pack_tokens:
                    packs[-1].append(e)
                    used += n
                else:
                    packs.append([e])
                    used = n
        return packs

    @saved_activations
    def compute_loss_packed(self, pack: list[Example]):
        """(loss, parts) of one packed row: the same loss the examples would have in rows of their own, because each attends only to itself, counts its own positions and is its own example
        for the loss. Only the language-model loss; the parts have the shape ``compute_loss`` returns."""
        batch = co.collate_packed([e.encoded for e in pack], label_states=[e.label_state for e in pack], weights=[e.weight for e in pack])
        batch = {k: v.to(self.device) for k, v in batch.items()}
        cuda = str(self.device).startswith("cuda")
        with torch.autocast(device_type="cuda" if cuda else "cpu", dtype=self.dtype, enabled=cuda and self.dtype != torch.float32):
            hidden = md.hidden_states(self.model, batch["input_ids"], md.packed_attention_bias(batch["segment_ids"], self.dtype), batch["position_ids"])
            tl = ls.token_losses(hidden, md.lm_head_of(self.model), batch["labels"], batch["field_ids"], chunk=self.chunk, checkpoint=self.model.training, example_ids=batch["segment_ids"])
        n = len(pack)
        lm, _ = ls.language_model_loss(tl, n, self.loss_cfg, batch["weights"])
        with torch.no_grad():
            _, per_example = ls.per_example_completion_ce(tl, n)
            _, fields = ls.field_normalized_ce(ls.TokenLosses(tl.loss.detach(), tl.example, tl.field), n, self.loss_cfg.field_weights, batch["weights"])
        md.assert_finite(lm, step=self.step + 1)
        return lm, {"lm": lm.detach(), "decision": None, "rank": None, "invariance": None, "fields": fields["unweighted"],
                    "tokens": int((batch["labels"] != tk.IGNORE).sum()), "padding_tokens": 0,
                    "per_example_completion_loss": per_example.float().cpu().tolist()}

    def _invariance_loss(self, group, logits):
        index = {e.view_id: i for i, e in enumerate(group)}
        pairs = [(index[e.variant_of], i, e.transform) for i, e in enumerate(group) if e.variant_of in index]
        if not pairs:
            return None
        parent = logits[torch.tensor([p[0] for p in pairs], device=logits.device)]
        variant = logits[torch.tensor([p[1] for p in pairs], device=logits.device)]
        return iv.invariance_loss(parent, variant, [p[2] for p in pairs])

    def _pair_loss(self, group, logits, batch):
        sides: dict[str, dict[str, int]] = {}
        for i, e in enumerate(group):
            if e.pair_id and e.role in ("vulnerable", "protected"):
                sides.setdefault(e.pair_id, {})[e.role] = i
        pairs = [(s["vulnerable"], s["protected"]) for s in sides.values() if len(s) == 2]
        if not pairs:
            return None
        z = hd.vulnerability_score(logits)
        iv = torch.tensor([p[0] for p in pairs], device=z.device)
        ip = torch.tensor([p[1] for p in pairs], device=z.device)
        known = batch["decision_labels"] >= 0
        valid = known[iv] & known[ip] & (batch["weights"][iv] > 0) & (batch["weights"][ip] > 0)
        return hd.pair_loss(z[iv], z[ip], self.loss_cfg.margin, valid)

    # -- one optimizer step ------------------------------------------------------------------------------------------------------
    def micro_batches_of(self, groups):
        """Combine atomic draws into actual device batches without changing membership."""
        groups = list(groups)
        # These reductions are defined on atomic draws. Preserve their existing
        # normalization when using pair/invariance losses or nonuniform weights.
        if (self.loss_cfg.pair_enabled or self.loss_cfg.invariance_enabled or self.loss_cfg.lm_pair_weight > 0
                or len({e.weight for group in groups for e in group}) > 1):
            return groups
        if (getattr(self.training, 'length_grouping', False)
                and not self.loss_cfg.pair_enabled and not self.loss_cfg.invariance_enabled):
            groups.sort(key=lambda group: max(len(e.encoded.input_ids) for e in group))
        schedule = getattr(self, 'batch_schedule', None)
        def capacity(length):
            if schedule:
                for row in schedule:
                    if length <= row['max_sequence_tokens']:
                        return row['micro_batch_size']
                raise ValueError('Sequence exceeds the verified device-batch schedule')
            return self.micro_batch_size
        batches, current, current_length = [], [], 0
        for group in groups:
            length = max(len(e.encoded.input_ids) for e in group)
            if current and len(current) + len(group) > capacity(max(current_length, length)):
                batches.append(current)
                current = []
                current_length = 0
            current.extend(group)
            current_length = max(current_length, length)
        if current:
            batches.append(current)
        return batches

    def _draw_groups(self) -> tuple[list[list[Example]], int]:
        eff, groups, n = self.training.effective_sequence_batch_size, [], 0
        while n < eff:
            item = self.sampler.draw()
            ex = self.dataset.get(item.view_id)
            group = [ex]
            if self.loss_cfg.pair_enabled and ex.pair_id:
                other_id = self.dataset.partner(item.view_id)
                if other_id:
                    other = self.dataset.get(other_id)
                    group = [ex, other] if ex.role == "vulnerable" else [other, ex]
            if self.loss_cfg.invariance_enabled:
                variants = self.dataset.variants(item.view_id)
                if variants:
                    group = group + [self.dataset.get(variants[self.sampler.draws % len(variants)])]
            if (self.loss_cfg.lm_pair_weight > 0 and len(group) == 1 and groups and len(groups[-1]) == 1
                    and self.lm_partner.get(ex.view_id, (None,))[0] == groups[-1][0].view_id):
                groups[-1].append(ex)                             # the twin was the previous draw of this batch: one device batch of two
                n += 1
                continue
            groups.append(group)
            n += len(group)
        return groups, n

    def _optimizer_step(self) -> dict:
        if getattr(self, 'controller', None) is not None and not getattr(self,'_controller_baseline',False):
            from training.finetune.online_training import optimizer_step
            return optimizer_step(self)
        step_started = time.perf_counter()
        groups, n = self._draw_groups()
        self.model.train()
        self.head.train()
        self.optimizer.zero_grad(set_to_none=True)
        agg = {"loss": 0.0, "lm_loss": 0.0, "decision_loss": 0.0, "rank_loss": 0.0, "invariance_loss": 0.0, "twin_loss": 0.0}
        seen = {"decision_loss": False, "rank_loss": False, "invariance_loss": False, "twin_loss": False}
        fields: dict[str, float] = {}
        tokens = 0
        micro_batches = self.packs_of(groups) if self.pack else self.micro_batches_of(groups)
        for micro_index, group in enumerate(micro_batches):
            micro_started = time.perf_counter()
            loss, parts = self.compute_loss_packed(group) if self.pack else self.compute_loss(group)
            scale = len(group) / n
            (self.scaler.scale(loss * scale) if self.scaler else loss * scale).backward()
            if self.analytics or self.backend.name == "tpu":
                self.backend.synchronize()
            if self.analytics:
                lengths = [len(e.encoded.input_ids) for e in group]
                self.analytics.emit("micro_batch", step=self.step + 1, micro_batch=micro_index,
                                    micro_batches=len(micro_batches), device_batch_size=self.micro_batch_size,
                                    examples=[e.view_id for e in group], loss=float(loss.detach()),
                                    per_example_completion_loss=parts.get("per_example_completion_loss"),
                                    lm_loss=float(parts["lm"]), fields=parts["fields"],
                                    components={k: None if parts[k] is None else float(parts[k]) for k in ("decision", "rank", "invariance")},
                                    weights=[e.weight for e in group], accumulation_scale=scale,
                                    sequences=len(group), tokens=parts["tokens"], sequence_lengths=lengths,
                                    prompt_tokens=sum(e.encoded.prompt_tokens for e in group),
                                    padding_tokens=parts.get("padding_tokens", 0),
                                    forward_backward_seconds=time.perf_counter() - micro_started)
            agg["loss"] += float(loss.detach()) * scale
            agg["lm_loss"] += float(parts["lm"]) * scale
            for key, name in (("decision", "decision_loss"), ("rank", "rank_loss"), ("invariance", "invariance_loss"), ("twin", "twin_loss")):
                if parts.get(key) is not None:
                    agg[name] += float(parts[key]) * scale
                    seen[name] = True
            for f, v in parts["fields"].items():
                fields[f] = fields.get(f, 0.0) + v * scale
            tokens += parts["tokens"]
        if self.scaler:
            self.scaler.unscale_(self.optimizer)
        grad_norm = float(torch.nn.utils.clip_grad_norm_(self.params, self.training.max_grad_norm))
        lr = self.optimizer.param_groups[0]["lr"]
        optimizer_started = time.perf_counter()
        if self.scaler:
            self.scaler.step(self.optimizer)
            self.scaler.update()
        else:
            self.backend.optimizer_step(self.optimizer)
        self.scheduler.step()
        self.counters["step"] += 1
        self.counters["sequences"] += n
        self.counters["tokens"] += tokens
        rec = {"step": self.step, "loss": agg["loss"], "lm_loss": agg["lm_loss"], "grad_norm": grad_norm, "lr": lr, "tokens": tokens,
               "sequences": n, "family_pass": self.sampler.family_passes, "fields": fields}
        rec.update({k: agg[k] for k, on in seen.items() if on})
        if self.analytics:
            self.backend.synchronize()
            seconds = time.perf_counter() - step_started
            rec.update(step_seconds=seconds, supervised_tokens_per_second=tokens / max(seconds, 1e-9),
                       optimizer_seconds=time.perf_counter() - optimizer_started)
            self.analytics.emit("optimizer_step", **rec, gradient_clip_threshold=self.training.max_grad_norm, counters=dict(self.counters))
            self.analytics.emit("resource", step=self.step, **self.backend.memory())
        self.history.append(rec)
        with (self.run_dir / "metrics.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        return rec

    # -- checkpoints, evaluation, the loop -------------------------------------------------------------------------------------------
    def save_checkpoint(self):
        began = self.clock()
        if self.analytics and hasattr(self.analytics,'flush'):
            self.analytics.flush()
        if getattr(self,'controller',None) is not None:
            self.meta['resource_controller'] = self.controller.state_dict()
        path = ck.save_checkpoint(self.checkpoint_dir, step=self.step, model=self.model, head=self.head, optimizer=self.optimizer,
                                  scheduler=self.scheduler, scaler=self.scaler, sampler_state=self.sampler.state_dict(),
                                  counters={**self.counters, "best": self.best}, meta=self.meta, backend=self.backend)
        ck.prune(self.checkpoint_dir, self.policy.keep_recent)
        self._last_ckpt_time = self.clock()
        self._last_save_seconds = self._last_ckpt_time - began
        if self.analytics:
            self.analytics.emit("checkpoint", step=self.step, path=str(path), seconds=self._last_save_seconds)
            if hasattr(self.analytics,'flush'):
                self.analytics.flush()
        return path

    def resume(self, path=None) -> bool:
        """Continue from a checkpoint (the latest by default); False when there is none. Refuses another setting's checkpoint."""
        path = path or ck.latest_checkpoint(self.checkpoint_dir)
        if path is None:
            return False
        expect = {k: self.meta[k] for k in IDENTITY_KEYS if k in self.meta}
        state = ck.load_checkpoint(path, model=self.model, head=self.head, optimizer=self.optimizer, scheduler=self.scheduler,
                                   scaler=self.scaler, expect=expect, backend=self.backend)
        if getattr(self.backend, 'rng_restore_report', None) is not None:
            self.meta['rng_resume'] = dict(self.backend.rng_restore_report)
        self.sampler.load_state_dict(state["sampler"])
        self._throughput_checkpoint = state['meta'].get('throughput_profile')
        self._controller_checkpoint = state['meta'].get('resource_controller')
        if getattr(self,'controller',None) is not None:
            self.controller.load_state_dict(self._controller_checkpoint)
        c = dict(state["counters"])
        self.best = c.pop("best", None)
        self.counters.update(c)
        return True

    def _maybe_evaluate(self) -> None:
        if self.evaluator is None:
            return
        if self.eval_every_steps:                                         # evaluate every N steps; the family pass reached is what the record names
            crossed = self.step // self.eval_every_steps
            threshold = round(self.sampler.family_passes, 4)
        else:
            crossed = sum(1 for p in self.eval_passes if self.sampler.family_passes >= p)
            threshold = self.eval_passes[crossed - 1] if crossed else None
        if crossed <= self.counters["eval_index"]:
            return
        self.counters["eval_index"] = crossed
        if self.analytics:
            self.analytics.emit("lifecycle", phase="evaluating", step=self.step)
        self.backend.synchronize()
        eval_started = time.perf_counter()
        result = dict(self.evaluator(self))
        self.backend.synchronize()
        result["evaluation_seconds"] = time.perf_counter() - eval_started
        rec = {"pass": threshold, "family_pass": self.sampler.family_passes, "step": self.step, **result}
        self.evaluations.append(rec)
        if self.analytics:
            self.analytics.emit("evaluation", **rec)
            self.analytics.emit("lifecycle", phase="training", step=self.step)
        if self.best is None or result["metric"] > self.best["metric"]:
            path = self.save_checkpoint()
            ck.mark_selected(self.checkpoint_dir, path)
            self.best = {"metric": result["metric"], "step": self.step, "pass": threshold, "checkpoint": path.name}
            self.counters["bad_evals"] = 0
        else:
            self.counters["bad_evals"] += 1
            if self.counters["bad_evals"] >= self.patience:
                self.stopped = "early_stopping"

    def after_last_evaluation(self) -> str | None:
        """A sentence when training ran past the last evaluation, so that the steps after it can never be selected; None when it did not."""
        if self.eval_every_steps:
            last = (self.step // self.eval_every_steps) * self.eval_every_steps
            return None if self.step <= last else f"training ran to step {self.step} but the last evaluation is at step {last}: steps after it can never be selected"
        last_pass = max(self.eval_passes) if self.eval_passes else None
        if last_pass is not None and self.sampler.family_passes > last_pass:
            return f"training ran to {self.sampler.family_passes:.2f} family passes but the last evaluation is at {last_pass}: steps after it can never be selected"
        return None

    def _session_spent(self, started: float, done: int) -> bool:
        """Whether another optimizer step and a checkpoint would not fit in the session budget (a paid runtime): the step time is the mean of
        this call's steps, the checkpoint time the last one measured, and at least half a step."""
        if self.session_seconds is None or done == 0:
            return False
        elapsed = self.clock() - started
        per_step = elapsed / done
        return elapsed + per_step + max(self._last_save_seconds, 0.5 * per_step) >= self.session_seconds

    def train(self, max_steps: int | None = None) -> dict:
        """Run until ``total_steps`` (or ``max_steps`` more steps, an early stop, or the session budget, which leaves a checkpoint)."""
        target = self.total_steps if max_steps is None else min(self.total_steps, self.step + max_steps)
        started, done = self.clock(), 0
        while self.step < target and not self.stopped:
            if self.stop_file and self.stop_file.exists():
                self.save_checkpoint()
                self.stopped = "user_requested"
                break
            self._optimizer_step()
            done += 1
            benchmark=getattr(self,'_pending_controller_benchmark',None)
            if benchmark and done>=2:
                from training.finetune.controller_benchmark import compare, _estimate
                estimate=_estimate(self)
                # Cold initialization and checkpoint I/O must not turn an
                # unadmitted comparison into a permanent execution restriction.
                # Continue productive learning until matched repeats can fit.
                if estimate is not None and estimate*4<benchmark*.75:
                    if self.analytics:
                        self.analytics.emit('lifecycle',phase='comparing_throughput')
                    self._controller_benchmark_result=compare(self,seconds=benchmark)
                    (self.run_dir/'controller_benchmark.json').write_text(json.dumps(self._controller_benchmark_result,indent=2),encoding='utf-8')
                    if self.analytics:
                        self.analytics.emit('metadata',data={'controller_benchmark':self._controller_benchmark_result})
                        self.analytics.emit('lifecycle',phase='training',target_steps=self.total_steps,backend=self.backend.name)
                    self._pending_controller_benchmark=None
            if self.policy.due(step=self.step, now=self.clock(), last_time=self._last_ckpt_time):
                self.save_checkpoint()
            self._maybe_evaluate()
            if not self.stopped and self.step < target and self._session_spent(started, done):
                latest = ck.latest_checkpoint(self.checkpoint_dir)
                if latest is None or latest.name != f"ckpt-step{self.step:06d}":
                    self.save_checkpoint()
                self.stopped = "session_budget"
        if self.step >= self.total_steps and not self.stopped:
            self.stopped = "finished"
        return {"step": self.step, "stopped": self.stopped, "best": self.best}

    # -- the profile step ---------------------------------------------------------------------------------------------------------
    def profile(self) -> dict:
        """One micro-batch forward and backward, timed, with memory and size facts; written to profile.json. No update is made."""
        cuda = str(self.device).startswith("cuda")
        rng = self.backend.rng_state()
        if getattr(self, 'batch_schedule', None):
            sample = [self.dataset.get(vid) for vid in self.dataset.ids()[:self.training.effective_sequence_batch_size]]
            batches = self.micro_batches_of([[ex] for ex in sample])
            examples, micro_batches = batches[0], len(batches)
        else:
            examples = [self.dataset.get(vid) for vid in self.dataset.ids()[:self.micro_batch_size]]
            micro_batches = math.ceil(self.training.effective_sequence_batch_size / len(examples))
        self.model.train()
        if cuda:
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
        t0 = time.perf_counter()
        loss, parts = self.compute_loss(examples)
        loss.backward()
        self.backend.synchronize()
        if cuda:
            torch.cuda.synchronize()
        seconds = time.perf_counter() - t0
        tokens = sum(len(ex.encoded.input_ids) for ex in examples)
        self.optimizer.zero_grad(set_to_none=True)
        self.backend.restore_rng(rng)
        trainable = sum(p.numel() for p in self.model.parameters() if p.requires_grad)
        out = {"seconds": seconds, "tokens": tokens, "tokens_per_second": tokens / max(seconds, 1e-9), "cuda": cuda,
               "sequences": len(examples),
               "micro_batches_per_optimizer_step": micro_batches,
               "lm_loss_chunk_tokens": self.chunk,
               "dtype": str(self.dtype), "trainable_parameters": trainable,
               "head_parameters": sum(p.numel() for p in self.head.parameters()),
               "optimizer_state_bytes": 2 * 4 * sum(p.numel() for p in self.params),
               "peak_allocated_bytes": torch.cuda.max_memory_allocated() if cuda else None,
               "peak_reserved_bytes": torch.cuda.max_memory_reserved() if cuda else None, "offload": False,
               "gradient_checkpointing": bool(getattr(self.model, "is_gradient_checkpointing", False))}
        (self.run_dir / "profile.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
        return out
