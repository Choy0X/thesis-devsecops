"""Budgeted source-learning experiment. Never a security accuracy evaluation."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import time
import zipfile


class Budget:
    def __init__(self, minutes=30, *, deadline=None, clock=time.monotonic):
        if not math.isfinite(minutes) or minutes <= 0:
            raise ValueError('Trial budget must be finite and positive')
        self.clock, self.minutes = clock, minutes
        self.started = clock()
        self.deadline = min(self.started + minutes * 60, deadline) if deadline is not None else self.started + minutes * 60
        if not math.isfinite(self.deadline):
            raise ValueError('Trial deadline must be finite')
        if deadline is not None:
            self.started = self.deadline - minutes * 60

    @property
    def remaining(self):
        return max(0., self.deadline - self.clock())

    @property
    def elapsed(self):
        return self.clock() - self.started

    def can_start(self, seconds, reserve=0):
        return self.remaining > max(0., seconds) + max(0., reserve)

    def training_steps(self, *, step_seconds, evaluation_seconds, export_seconds=120, max_steps=32):
        available = self.remaining - max(0., evaluation_seconds) * 1.5 - export_seconds
        return max(0, min(max_steps, int(available / max(1., step_seconds))))


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + '\n', encoding='utf-8')
    os.replace(temp, path)


def _sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def generation_count_for_budget(probe, budget, *, evaluation_views, max_new_tokens=128):
    """Size paired generation from measured latency, not GPU product names."""
    scored = [row for row in probe['rows'] if row['status'] == 'scored']
    generated = [row['generation'] for row in scored if (row.get('generation') or {}).get('state') in ('answered', 'token_limit')]
    if not scored or not generated:
        return 0
    teacher_seconds = max(row.get('scoring_seconds', 0.) for row in scored) * evaluation_views
    token_seconds = max(row.get('seconds', 0.) / max(1, row.get('new_tokens', 0)) for row in generated)
    for count in (12, 6):
        paired_seconds = 2 * (teacher_seconds + token_seconds * max_new_tokens * count) * 1.5
        if budget.can_start(paired_seconds + 120, 120):
            return count
    return 0


def export_outputs(run_dir):
    run_dir = Path(run_dir)
    files = {}
    for path in sorted(run_dir.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(run_dir).as_posix()
        if (rel.startswith(('checkpoints/', 'bundle/', 'trial_data/')) or path.suffix in ('.zip', '.tmp')
                or path.name == 'trial_outputs_manifest.json'):
            continue
        files[rel] = {'sha256': _sha(path), 'bytes': path.stat().st_size}
    _write(run_dir / 'trial_outputs_manifest.json', {'schema': 'trial-outputs/1', 'files': files})
    target = run_dir / 'trial_outputs.zip'
    temp = target.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            archive.write(run_dir / name, name)
        archive.write(run_dir / 'trial_outputs_manifest.json', 'trial_outputs_manifest.json')
    os.replace(temp, target)
    return str(target)


def load_base(cfg, backend):
    if backend.name == 'tpu':
        from training.finetune.tpu_quantization import load_tpu_model
        return load_tpu_model(cfg, revision=cfg.model.revision, backend=backend)
    import torch
    from training.finetune import model
    torch.cuda.set_device(backend.device)
    return model.load_base_model(cfg, revision=cfg.model.revision, compute_dtype=backend.dtype,
                                 device_map={'': str(backend.device)})


def run(*, bundle_dir, run_dir, total_budget_minutes=30, deadline=None, tiny=False,
        max_steps=32, clock=time.monotonic, log=print, allow_experimental_tpu=False, software_int4=False):
    """Load once, score the base, train a new adapter, score the same cohort."""
    budget = Budget(total_budget_minutes, deadline=deadline, clock=clock)
    if software_int4 and not tiny:
        raise ValueError('software_int4 is only available with the random tiny software model')
    bundle_dir, run_dir = Path(bundle_dir).resolve(), Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    if (run_dir / 'trial_config.json').exists() or (run_dir / 'adapter').exists():
        raise ValueError('A source trial requires a fresh output directory')
    config = {'schema': 'source-trial/1', 'mode': 'trial', 'state': 'initializing', 'experimental': True,
              'security_evaluation': False, 'counts_as_full_training': False, 'counts_as_evaluation': False,
              'software_test': tiny, 'software_int4': software_int4, 'total_budget_minutes': total_budget_minutes,
              'baseline_report': None, 'tuned_report': None, 'comparison': 'trial_comparison.json',
              'resources': None, 'steps_done': 0, 'deadline_monotonic': budget.deadline}
    baseline = tuned = None
    trainer = None

    def event(phase, **facts):
        config['phase'] = phase
        config['remaining_seconds'] = budget.remaining
        _write(run_dir / 'trial_config.json', config)
        with (run_dir / 'trial_events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'phase': phase, 'elapsed_seconds': budget.elapsed,
                                     'remaining_seconds': budget.remaining, **facts}, default=str) + '\n')
        log(f'Trial {phase}; {budget.remaining:.1f}s remain')

    try:
        resolved_raw = json.loads((bundle_dir / 'resolved_config.json').read_text(encoding='utf-8'))
        model_raw = resolved_raw['config']['model']
        config.update(model=model_raw['id'], model_revision=model_raw['revision'], tokenizer_revision=model_raw['tokenizer_revision'],
                      dataset={'version': resolved_raw['dataset']['version'], 'manifest_sha256': resolved_raw['dataset']['manifest_sha256']},
                      bundle={'manifest_sha256': _sha(bundle_dir / 'bundle_manifest.json')},
                      resolved_config_sha256=_sha(bundle_dir / 'resolved_config.json'))
        shutil.copyfile(bundle_dir / 'resolved_config.json', run_dir / 'resolved_config.json')
        from training.finetune import resolve, trial_evaluation as evaluation, runner, model as md
        from training.finetune import accelerator, hub, runtime_resources, losses, head, trainer as trainers, online_training
        from training.finetune.sampler import Sampler
        import torch
        cfg = resolve.read_resolved(bundle_dir)
        source = bundle_dir / 'data'
        manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
        if _sha(source / 'manifest.json') != cfg.data.manifest_sha256:
            raise ValueError('Trial dataset identity differs from resolved config')
        if not manifest.get('trial') and not manifest.get('trial_version'):
            raise ValueError('Source trial requires a separately reserved trial derivative')
        for name, expected in manifest.get('files', {}).items():
            rel = Path(name)
            path = source / rel
            if rel.is_absolute() or '..' in rel.parts or not path.resolve().is_relative_to(source.resolve()) or path.is_symlink():
                raise ValueError('Unsafe trial data member')
            if not path.is_file() or _sha(path) != expected['sha256'] or path.stat().st_size != expected['bytes']:
                raise ValueError('Trial data member changed: ' + name)
        bundle_identity = runner._bundle_identity(bundle_dir)
        config.update(model=cfg.model.id, model_revision=cfg.model.revision, tokenizer_revision=cfg.model.tokenizer_revision,
                      serialization=cfg.model.serialization, dataset={'version': manifest['tag'], 'manifest_sha256': cfg.data.manifest_sha256},
                      bundle=bundle_identity, resolved_config_sha256=_sha(bundle_dir / resolve.FILE),
                      source_lineage=manifest.get('source'), context_tokens=cfg.context.max_total_tokens)
        shutil.copyfile(bundle_dir / resolve.FILE, run_dir / resolve.FILE)
        event('resource_preflight')
        backend = accelerator.select_backend('cpu' if tiny else 'auto')
        from training.finetune import runtime_compatibility as compatibility
        cfg, recipe = compatibility.effective_config(cfg, backend, allow_experimental_tpu=allow_experimental_tpu)
        config['runtime_recipe'] = recipe
        _write(run_dir / 'effective_config.json', cfg.model_dump(mode='json'))
        resources = ({'state': 'ready', 'software_test': True, 'resources': {'backend': 'cpu'}, 'execution': {}}
                     if tiny else runtime_resources.preflight(bundle_dir, run_dir, cfg, backend=backend))
        if not tiny and resources['state'] == 'ready':
            runtime_resources.apply_process_policy(resources)
        _write(run_dir / 'resource_report.json', resources)
        config['resources'] = 'resource_report.json'
        if resources['state'] != 'ready':
            config.update(state='blocked', reason='insufficient_resources', detail=resources.get('reasons'))
            return config
        if not tiny:
            event('runtime_kernel_validation')
            compatible = compatibility.validate_runtime(backend)
            _write(run_dir / 'runtime_compatibility.json', compatible)
            config['runtime_compatibility'] = 'runtime_compatibility.json'
            if compatible['state'] != 'ready':
                config.update(state='blocked', reason='incompatible_runtime', detail=compatible)
                return config
        train_views = runner.load_views(source, 'train')
        views = [view for view in runner.load_views(source, 'val') if view['val_role'] == 'source_trial']
        from training.finetune.sft_contracts import CORPUS_TASKS
        if not train_views or not views or set(v['task'] for v in views) != set(CORPUS_TASKS):
            raise ValueError('Trial requires nonempty training and held-out source cohorts with all three tasks')
        if not budget.can_start(5, 120):
            config.update(state='partial', reason='setup_exhausted_budget')
            return config
        event('loading_model')
        if tiny:
            tokenizer = runner.tiny_tokenizer([view['context_text'] + view['response_json'] for view in train_views + views])
            model = md.build_tiny_model(len(tokenizer), seed=cfg.training.pilot_seed)
            if software_int4:
                from training.finetune.tpu_quantization import quantize_model
                model = quantize_model(model)
        else:
            tokenizer = hub.load_tokenizer(cfg.model.id, cfg.model.tokenizer_revision)
            model = load_base(cfg, backend)
        if getattr(model, 'hf_device_map', None) is None:
            model.to(backend.device)
        config['model_loaded'] = runner.model_facts(model)
        event('evaluation_probe')
        probe_ids = evaluation.generation_cohort(views, 3)
        probe = evaluation.evaluate(model, tokenizer, [view for view in views if view['view_id'] in probe_ids], cfg, backend,
                    arm='base_probe', budget=budget, generation_ids=probe_ids, max_new_tokens=8, reserve_seconds=240,
                    log=log, snapshot=lambda value: _write(run_dir / 'evaluation_probe.json', value))
        generation_count = generation_count_for_budget(probe, budget, evaluation_views=len(views))
        config['evaluation_probe'] = 'evaluation_probe.json'
        if not generation_count:
            config.update(state='partial', reason='measured_compute_cannot_fit_minimum_paired_comparison')
            return config
        generation_ids = evaluation.generation_cohort(views, generation_count)
        config['generation'] = {'do_sample': False, 'max_new_tokens': 128, 'view_ids': generation_ids}
        config['baseline_report'] = 'baseline_report.json'
        event('baseline_evaluation')
        baseline = evaluation.evaluate(model, tokenizer, views, cfg, backend, arm='base', budget=budget,
                                       generation_ids=generation_ids, reserve_seconds=240, log=log,
                                       snapshot=lambda value: _write(run_dir / 'baseline_report.json', value))
        _write(run_dir / 'baseline_report.json', baseline)
        config['baseline_report'] = 'baseline_report.json'
        if baseline['state'] != 'finished':
            config.update(state='partial', reason='baseline_incomplete')
            return config
        evaluation_seconds = max(1., baseline['seconds'])
        if not budget.can_start(evaluation_seconds * 1.5 + 5, 120):
            config.update(state='partial', reason='insufficient_training_budget_after_baseline')
            return config
        event('preparing_adapter')
        torch.manual_seed(cfg.training.pilot_seed)
        model = md.add_adapter(model, cfg.lora)
        checkpoint_fn = None
        if backend.name == 'tpu':
            from torch_xla.utils.checkpoint import checkpoint as checkpoint_fn
        md.prepare_for_training(model, gradient_checkpointing=cfg.training.gradient_checkpointing and not tiny,
                                checkpoint_fn=checkpoint_fn)
        data, items, token_stats = runner.build_examples(train_views, runner.load_repositories(source), tokenizer,
                                                       cfg.context, protocol=cfg.model.serialization)
        if not items:
            raise ValueError('No trial training views fit the pinned tokenizer')
        sampler = Sampler(items, task_shares=cfg.sampling.task_shares, pool_targets=cfg.sampling.decision_pool_targets,
                          seed=cfg.training.pilot_seed)
        config['token_audit'] = token_stats
        decision_head = head.DecisionHead(model.config.hidden_size).to(backend.device)
        meta = {'base_commit': cfg.model.revision, 'tokenizer_revision': cfg.model.tokenizer_revision,
                'serialization': cfg.model.serialization, 'dataset_manifest_sha256': cfg.data.manifest_sha256,
                'config_sha256': config['resolved_config_sha256'], 'stage': 1, 'seed': cfg.training.pilot_seed,
                'serialization_hash': runner.sz.serialization_hash(cfg.model.serialization, tokenizer),
                'runtime_recipe': recipe}
        trainer = trainers.Trainer(model=model, head=decision_head, dataset=data, sampler=sampler,
                    training=cfg.training, lora=cfg.lora, loss_cfg=losses.LossConfig.for_stage(1), run_dir=run_dir,
                    pad_id=tokenizer.pad_token_id, seed=cfg.training.pilot_seed, total_steps=max_steps,
                    meta=meta, device=str(backend.device), dtype=backend.dtype, backend=backend, evaluator=None)
        online_training.configure(trainer, mode='auto', log=log)
        if not tiny:
            runtime_resources.apply_execution_policy(trainer, resources)
        event('throughput_probe')
        profile = trainer.profile()
        step_seconds = max(1., profile['seconds'] * profile['micro_batches_per_optimizer_step'] * 1.5)
        target_steps = budget.training_steps(step_seconds=step_seconds, evaluation_seconds=evaluation_seconds,
                                             max_steps=max_steps)
        config['planned_steps'] = target_steps
        if target_steps < 1:
            config.update(state='partial', reason='probe_left_no_training_budget')
            return config
        trainer.total_steps = target_steps
        trainer.scheduler = trainers.make_scheduler(trainer.optimizer, cfg.training, target_steps)
        event('training', planned_steps=target_steps)
        last_checkpoint = clock()
        resource_stop = None
        while trainer.step < target_steps and budget.can_start(step_seconds * 1.5, evaluation_seconds * 1.5 + 120):
            before = clock()
            try:
                trainer.train(max_steps=1)
            except online_training.ResourceLimit as error:
                # This exception is raised only before an optimizer update,
                # after partial gradients/RNG/sampler have been restored.
                resource_stop = error.report
                config['resource_stop'] = resource_stop
                config['steps_done'] = trainer.step
                event('training_resource_limit', consistent_step=trainer.step, detail=resource_stop)
                break
            step_seconds = max(step_seconds, clock() - before)
            config['steps_done'] = trainer.step
            event('training', step=trainer.step, loss=trainer.history[-1]['loss'])
            if clock() - last_checkpoint > 120 and budget.can_start(30, evaluation_seconds * 1.5 + 120):
                trainer.save_checkpoint()
                last_checkpoint = clock()
        if not trainer.step:
            config.update(state='blocked' if resource_stop else 'partial',
                          reason='resource_limit_before_first_update' if resource_stop else 'no_optimizer_step_completed')
            return config
        event('checkpoint')
        trainer.save_checkpoint()
        adapter = {name: value.detach().cpu() for name, value in model.state_dict().items() if 'lora_' in name}
        model.save_pretrained(run_dir / 'adapter', state_dict=adapter)
        del adapter
        config['adapter'] = {'sha256': _sha(run_dir / 'adapter/adapter_model.safetensors'), 'steps_done': trainer.step}
        _write(run_dir / 'exposure.json', sampler.exposure_report())
        # The recovery checkpoint already contains optimizer state. Evaluation
        # needs the adapter weights only; do not retain Adam's moments/gradients
        # on a memory-constrained device during the reserved comparison phase.
        optimizer_bytes = sum(value.numel() * value.element_size() for state in trainer.optimizer.state.values()
                              for value in state.values() if torch.is_tensor(value))
        trainer.optimizer.zero_grad(set_to_none=True)
        trainer.optimizer.state.clear()
        if backend.name == 'cuda':
            torch.cuda.empty_cache()
        config['evaluation_memory'] = {'optimizer_state_released_bytes': optimizer_bytes, 'gradients_cleared': True}
        config['tuned_report'] = 'tuned_report.json'
        event('adapted_evaluation')
        tuned = evaluation.evaluate(model, tokenizer, views, cfg, backend, arm='adapter', budget=budget,
                                   generation_ids=generation_ids, reserve_seconds=120, log=log,
                                   snapshot=lambda value: _write(run_dir / 'tuned_report.json', value))
        _write(run_dir / 'tuned_report.json', tuned)
        config['tuned_report'] = 'tuned_report.json'
        config['state'] = 'finished' if tuned['state'] == 'finished' and not resource_stop else 'partial'
        config['training_stopped'] = 'resource_limit' if resource_stop else 'step_target' if trainer.step >= target_steps else 'time_reserved_for_comparison'
        if resource_stop:
            config['reason'] = 'training_resource_limit_comparison_retained'
        return config
    except Exception as error:
        import traceback
        _write(run_dir / 'failure.json', {'type': type(error).__name__, 'detail': str(error),
                                        'traceback': traceback.format_exc(), 'phase': config.get('phase')})
        config.update(state='failed', reason=type(error).__name__, detail=str(error))
        log(f'Trial failed: {error}')
        return config
    finally:
        from training.finetune import trial_evaluation
        try:
            comparison = trial_evaluation.compare(baseline, tuned)
        except ValueError as error:
            comparison = {'schema': 'source-trial-comparison/1', 'state': 'partial', 'security_evaluation': False,
                          'loss_delta_tuned_minus_base': None, 'detail': str(error)}
            config['state'] = 'partial'
        _write(run_dir / 'trial_comparison.json', comparison)
        config.update(elapsed_seconds=budget.elapsed, remaining_seconds=budget.remaining)
        event('exporting', state=config['state'])
        export_outputs(run_dir)
        if trainer is not None and getattr(trainer, 'stager', None):
            trainer.stager.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--total-budget-minutes', type=float, default=30)
    parser.add_argument('--deadline-monotonic', type=float)
    parser.add_argument('--tiny', action='store_true')
    parser.add_argument('--max-steps', type=int, default=32)
    parser.add_argument('--allow-experimental-tpu', action='store_true')
    parser.add_argument('--software-int4', action='store_true')
    args = parser.parse_args(argv)
    if args.max_steps < 1:
        parser.error('--max-steps must be positive')
    result = run(bundle_dir=args.bundle, run_dir=args.run_dir, total_budget_minutes=args.total_budget_minutes,
                 deadline=args.deadline_monotonic, tiny=args.tiny, max_steps=args.max_steps,
                 allow_experimental_tpu=args.allow_experimental_tpu, software_int4=args.software_int4)
    print(json.dumps(result, default=str))
    return 0 if result['state'] == 'finished' else 2


if __name__ == '__main__':
    raise SystemExit(main())
