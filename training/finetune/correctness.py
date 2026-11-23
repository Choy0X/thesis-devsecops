"""Bounded correctness pilot. Produces evidence, never a deployment approval.

Run automatically before a new full run. The original bundle and training settings are
read-only; pilot samples, metrics and checkpoints live in a separate directory.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import pathlib
import math


def encoded_checks(data):
    from training.finetune import tokenization as tk
    checks = {'serialization': bool(data.ids()), 'completion_masking': True, 'field_ownership': True}
    for vid in data.ids():
        e = data.get(vid).encoded
        checks['serialization'] &= len(e.input_ids) == len(e.labels) == len(e.field_ids)
        checks['completion_masking'] &= (all(x == tk.IGNORE for x in e.labels[:e.prompt_tokens])
            and any(x != tk.IGNORE for x in e.labels[e.prompt_tokens:])
            and all(y == tk.IGNORE or x == y for x, y in zip(e.input_ids, e.labels)))
        checks['field_ownership'] &= all((y == tk.IGNORE and f == -1) or
            (y != tk.IGNORE and 0 <= f < len(tk.FIELD_GROUPS)) for y, f in zip(e.labels, e.field_ids))
    return checks


def shifting_check():
    import torch
    from training.finetune import losses as ls, tokenization as tk
    g = torch.Generator().manual_seed(13)
    h = torch.randn(1, 5, 4, generator=g)
    projection = torch.nn.Linear(4, 7, bias=False)
    with torch.no_grad(): projection.weight.copy_(torch.randn(7, 4, generator=g))
    labels = torch.tensor([[tk.IGNORE, tk.IGNORE, 2, 3, 4]])
    fields = torch.tensor([[-1, -1, 0, 1, 2]])
    actual = ls.token_losses(h, projection, labels, fields, checkpoint=False).loss
    expected = torch.nn.functional.cross_entropy(projection(h[:, :-1]).reshape(-1, 7),
        labels[:, 1:].reshape(-1), ignore_index=tk.IGNORE, reduction='none')[1:]
    return bool(torch.allclose(actual, expected, atol=1e-6))


def select_pairs(views, count=2, *, allow_unpaired=False):
    from training.finetune.sft_contracts import CORPUS_TASKS
    source_views = [v for v in views if v['task'] in CORPUS_TASKS]
    if source_views:
        # Source objectives have no vulnerable/protected decision labels. Sample
        # independent families per objective for the same optimizer/masking pilot.
        selected = []
        for task in CORPUS_TASKS:
            families = set()
            for view in sorted(source_views, key=lambda v: v['view_id']):
                if view['task'] == task and view['family_id'] not in families:
                    selected.append(view)
                    families.add(view['family_id'])
                    if len(families) >= count:
                        break
        return selected
    pairs = {}
    for v in views:
        if v['task'] == 'discovery' and v.get('pool') in ('supported', 'scoped_negative'):
            pairs.setdefault(v['record_id'], {})[v['pool']] = v
    selected = [p for _, p in sorted(pairs.items()) if len(p) == 2][:count]
    if allow_unpaired and not selected:
        supported = [v for v in views if v['task'] == 'discovery' and v.get('pool') == 'supported']
        protected = [v for v in views if v['task'] == 'discovery' and v.get('pool') == 'scoped_negative']
        if supported and protected:
            return sorted(supported, key=lambda v: v['record_id'])[:count] + sorted(protected, key=lambda v: v['record_id'])[:count]
    if not selected:
        raise ValueError('The pilot requires admitted vulnerable/protected examples in each partition.')
    return [v for p in selected for v in p.values()]


def diagnostic_cohorts(train_views, validation_views, *, all_training=False, allow_unpaired=False):
    train = select_pairs(train_views, allow_unpaired=allow_unpaired)
    return train, [] if all_training else select_pairs(validation_views, allow_unpaired=allow_unpaired)


def run(bundle, output, *, steps=16, device='auto'):
    import torch
    from training.finetune import runner as rn, resolve as rs, hub, model as md, head as hd
    from training.finetune import trainer as tr, losses as ls, checkpoint as ck, quality_gate, accelerator
    bundle, output = pathlib.Path(bundle), pathlib.Path(output)
    if not 2 <= steps <= 50:
        raise ValueError('A diagnostic pilot must use 2 to 50 optimizer updates.')
    if output.exists():
        raise ValueError('Use a new pilot directory; previous evidence is never overwritten.')
    cfg = rs.read_resolved(bundle)
    identity = {'dataset_manifest_sha256': rn._sha_file(bundle / 'data/manifest.json'),
                'config_sha256': json.loads((bundle / rs.FILE).read_text())['config_sha256']}
    manifest = json.loads((bundle / 'data/manifest.json').read_text(encoding='utf-8'))
    readiness = manifest.get('security_readiness', {})
    all_training = manifest.get('coverage_policy', {}).get('evaluation') == 'none_all_records_training'
    automatic = quality_gate.automatic_readiness(readiness)
    blockers = quality_gate.check(bundle / 'data', purpose='train')['blockers']
    blockers = [b for b in blockers if b != 'missing_correctness_diagnostics']
    if blockers:
        raise ValueError('Correctness pilot blocked: ' + '; '.join(blockers))
    train, unseen = diagnostic_cohorts(rn.load_views(bundle / 'data', 'train'),
        [] if all_training else [v for v in rn.load_views(bundle / 'data', 'val') if v.get('val_role') == 'selection'],
        all_training=all_training, allow_unpaired=automatic)
    if {v['family_id'] for v in train} & {v['family_id'] for v in unseen}:
        raise ValueError('Pilot train and unseen selection pairs share a family.')
    output.mkdir(parents=True)
    report = {'schema': quality_gate.diagnostic_schema(readiness), 'identity': identity, 'checks': {}, 'state': 'running',
              'admission_policy': readiness.get('admission_policy'),
              'example_provenance': {'basis': 'automatic_admission' if automatic else 'legacy_admission_policy',
                  'human_review_required': not automatic,
                  'samples': [{key: view.get(key) for key in ('view_id', 'record_id', 'family_id', 'pool', 'review_status')}
                              for view in train + unseen]},
              'scope': 'Short correctness diagnostic, not security deployment validation.',
              'train_views': [v['view_id'] for v in train], 'unseen_views': [v['view_id'] for v in unseen],
              'pilot': {'steps': steps, 'learning_rate': cfg.training.learning_rate, 'context': cfg.context.max_total_tokens,
                        'rank': cfg.lora.r, 'model': cfg.model.id, 'revision': cfg.model.revision}}
    from training.finetune.sft_contracts import CORPUS_TASKS
    if any(v['task'] in CORPUS_TASKS for v in train):
        report['diagnostic_policy'] = 'source-optimizer-integrity/1'
        report['example_provenance'].update(basis='mined_source_objectives', human_review_required=False)
        report['example_provenance']['unseen_admitted_pairs_meaning'] = (
            'No held-out examples: every record is used for training.' if all_training else
            'Independent held-out source examples, without security decision labels.')
        report['evaluation_state'] = 'not_available_all_records_training' if all_training else 'held_out_source_examples'
    def save():
        rn._write_json(output / 'correctness_diagnostics.json', report)
    save()
    try:
        backend = accelerator.select_backend(device)
        report['backend'] = backend.name
        if backend.name != 'cuda':
            raise ValueError('The selected-model correctness pilot currently requires CUDA. TPU needs its separate hardware preflight.')
        tokenizer = hub.load_tokenizer(cfg.model.id, cfg.model.tokenizer_revision)
        data, _, stats = rn.build_examples(train + unseen, {}, tokenizer, cfg.context, protocol=cfg.model.serialization)
        if stats['encoded'] != len(train + unseen):
            raise ValueError('An admitted pilot example exceeds the selected context budget: ' + str(stats['excluded']))
        report['checks'].update(encoded_checks(data), token_shifting=shifting_check())
        base = md.load_base_model(cfg, revision=cfg.model.revision)
        model = md.add_adapter(base, cfg.lora)
        if cfg.precision.mode == 'cpu_weight_offload':
            from training.finetune.offload import enable
            enable(model, pin=cfg.training.pin_frozen_weights)
        md.prepare_for_training(model, gradient_checkpointing=cfg.training.gradient_checkpointing)
        if getattr(base, 'hf_device_map', None) is None: model.to(backend.device)
        head = hd.DecisionHead(base.config.hidden_size).to(backend.device)
        trainer = tr.Trainer(model=model, head=head, dataset=data, sampler=None, training=cfg.training,
            lora=cfg.lora, loss_cfg=ls.LossConfig.for_stage(1, mode='per_example_completion_ce'),
            run_dir=output, pad_id=tokenizer.pad_token_id or tokenizer.eos_token_id, seed=13, total_steps=steps,
            meta=identity, device=str(backend.device), dtype=backend.dtype, backend=backend)
        def losses(views):
            model.eval()
            with torch.no_grad(): return [float(trainer.compute_loss([data.get(v['view_id'])])[0]) for v in views]
        initial, unseen_initial = losses(train), losses(unseen)
        # Adapter tensors only: retaining a full base copy would defeat weak-GPU diagnostics.
        before = {n: p.detach().cpu().clone() for n, p in model.named_parameters() if p.requires_grad}
        history = []
        for step in range(steps):
            model.train(); trainer.optimizer.zero_grad(set_to_none=True)
            value = trainer.compute_loss([data.get(train[step % len(train)]['view_id'])])[0]
            if not torch.isfinite(value): raise ValueError('Non-finite pilot loss')
            if trainer.scaler:
                trainer.scaler.scale(value).backward(); trainer.scaler.unscale_(trainer.optimizer)
                torch.nn.utils.clip_grad_norm_(trainer.params, cfg.training.max_grad_norm)
                trainer.scaler.step(trainer.optimizer); trainer.scaler.update()
            else:
                value.backward(); torch.nn.utils.clip_grad_norm_(trainer.params, cfg.training.max_grad_norm)
                trainer.optimizer.step()
            trainer.scheduler.step()
            history.append({'step': step + 1, 'loss': float(value.detach())})
            print(f"Correctness pilot {step + 1}/{steps}: {history[-1]['loss']:.6f}", flush=True)
        final, unseen_final = losses(train), losses(unseen)
        report['checks']['adapter_updates'] = any(not torch.equal(p.detach().cpu(), before[n])
            for n, p in model.named_parameters() if n in before)
        checkpoint = ck.save_checkpoint(output / 'checkpoints', step=steps, model=model, head=head,
            optimizer=trainer.optimizer, scheduler=trainer.scheduler, scaler=trainer.scaler,
            sampler_state={}, counters={'step': steps}, meta=identity)
        # Perturb and restore adapter, head, optimizer and scheduler through the production path.
        with torch.no_grad():
            for p in model.parameters():
                if p.requires_grad: p.add_(.01)
        ck.load_checkpoint(checkpoint, model=model, head=head, optimizer=trainer.optimizer,
                           scheduler=trainer.scheduler, scaler=trainer.scaler, expect=identity)
        restored = losses(train)
        report['checks']['checkpoint_restoration'] = all(abs(a - b) <= 1e-4 * max(1, abs(a)) for a, b in zip(final, restored))
        mean = lambda xs: sum(xs) / len(xs)
        report['checks']['tiny_overfit' if automatic else 'reviewed_tiny_overfit'] = mean(final) <= .5 * mean(initial)
        report['checks']['unseen_admitted_pairs' if automatic else 'unseen_reviewed_pairs'] = (
            all(torch.isfinite(torch.tensor(unseen_final))) and mean(unseen_final) <= 1.1 * mean(unseen_initial)
            if unseen else None)
        report['checks']['finite_losses'] = all(math.isfinite(value) for value in initial + final + unseen_initial + unseen_final)
        report.update(losses={'train_before': initial, 'train_after': final, 'unseen_before': unseen_initial,
                             'unseen_after': unseen_final}, history=history)
        required = quality_gate.REQUIRED_CHECKS if automatic else quality_gate.LEGACY_CHECKS
        if report.get('diagnostic_policy') == 'source-optimizer-integrity/1':
            required = quality_gate.SOURCE_CHECKS
        report['state'] = 'passed' if all(report['checks'].get(k) is True for k in required) else 'failed'
    except Exception as error:
        report.update(state='failed', error=str(error)); save(); raise
    save()
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle', required=True); p.add_argument('--output', required=True)
    p.add_argument('--steps', type=int, default=16); p.add_argument('--device', default='auto')
    a = p.parse_args()
    result = run(a.bundle, a.output, steps=a.steps, device=a.device)
    print(json.dumps(result, indent=2))
    return 0 if result['state'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
