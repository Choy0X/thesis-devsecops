"""Fresh security-task LoRA trial under one setup-inclusive execution budget.

This version trains on authored synthetic construction labels. Collected-source
teaching candidates are never auto-admitted. Held-out synthetic results are not
independent security accuracy or verified repairs.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import shutil
import time

from training.finetune.trial import Budget, _write, _sha

MAX_STEPS_LIMIT = 2048          # the runner's own ceiling on optimizer updates; every recipe's max_steps_limit must stay under it (the first v40 launch asked for 1,300 against a ceiling of 1,024 and died after 51 seconds)

TASKS = {'discovery', 'repair', 'insufficient_context', 'regression_test_design'}


def training_allowance(budget, *, step_seconds, evaluation_seconds, max_steps):
    available = budget.remaining - max(0, evaluation_seconds) * 1.5 - 120
    return max(0, min(max_steps, int(available / max(1, step_seconds))))


def replanned_total(budget, *, done, measured_step_seconds, evaluation_seconds, max_steps):
    """Total updates once ``done`` real updates have been timed: the same reserves as ``training_allowance`` (adapted evaluation at 1.5x and 120 s),
    one update of start-up headroom at 1.5x, and a 10 percent margin on every further update. Never above the cap, never below ``done``."""
    measured = max(1.0, measured_step_seconds)
    available = budget.remaining - max(0, evaluation_seconds) * 1.5 - 120
    return min(max_steps, done + max(0, int((available - 1.5 * measured) / (1.1 * measured))))


def _verify_bundle(bundle):
    from training.finetune.security_training_bootstrap import _safe_name
    manifest = json.loads((bundle / 'bundle_manifest.json').read_text(encoding='utf-8'))
    files = manifest.get('files')
    if manifest.get('schema') != 'training-bundle/1' or not isinstance(files, dict) or len(files) > 4096:
        raise ValueError('Invalid security trial bundle inventory')
    for name, identity in files.items():
        _safe_name(name)
        path = bundle.joinpath(*name.split('/'))
        if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(bundle)
                or path.stat().st_size != identity.get('bytes') or _sha(path) != identity.get('sha256')):
            raise ValueError('Security trial bundle member changed: ' + name)
    return _sha(bundle / 'bundle_manifest.json')


def _load_model(cfg, backend, train_views, heldout, tiny):
    from training.finetune import runner, model, hub, trial
    if tiny:
        from training.finetune import serialization as _sz
        messages = _sz.security_module(cfg.model.serialization).messages
        texts = [view['context_text'] + view['response_json'] for view in train_views + heldout]
        texts += [json.dumps(messages(task, '')) for task in sorted(TASKS)]
        tokenizer = runner.tiny_tokenizer(texts, vocab_size=1200)
        return model.build_tiny_model(len(tokenizer), seed=cfg.training.pilot_seed).to(backend.device), tokenizer
    tokenizer = hub.load_tokenizer(cfg.model.id, cfg.model.tokenizer_revision)
    return trial.load_base(cfg, backend), tokenizer


def _adapter_sample(model):
    digest = hashlib.sha256()
    for name, parameter in model.named_parameters():
        if 'lora_' in name:
            digest.update(name.encode())
            digest.update(parameter.detach().flatten()[:16].float().cpu().numpy().tobytes())
    return digest.hexdigest()


def _save_adapter(model, backend, path):
    backend.synchronize()
    state = {name: value for name, value in model.state_dict().items() if 'lora_' in name}
    model.save_pretrained(path, state_dict=backend.to_cpu(state), safe_serialization=True)


def require_matching_schemas(dataset_schema, plan_schema):
    """A dataset/1 bundle takes plan/1 and a dataset/2 bundle takes plan/2; a mismatch would silently ignore the plan's cohorts."""
    expected = {'security-trial-dataset/1': 'security-training-plan/1', 'security-trial-dataset/2': 'security-training-plan/2',
                'security-trial-dataset/3': 'security-training-plan/3'}
    if expected.get(dataset_schema) != plan_schema:
        raise ValueError(f'The training plan schema {plan_schema} does not match dataset schema {dataset_schema}')


def _freeze_generation_cohort(model, tokenizer, heldout, cfg, backend, baseline, plan, budget, out, event, log):
    """Decide, from measured baseline cost, whether the conditional cases run; then freeze the final cohort before any training."""
    from training.finetune import security_trial_cohorts as cohort_lib, security_trial_evaluation as evaluation
    rule = plan['generation_policy']['conditional_rule']
    conditional = list(plan['cohorts']['generation_conditional'])
    mandatory = list(plan['cohorts']['generation_mandatory'])
    costs = [row['generation']['seconds'] for row in baseline['rows'] if (row.get('generation') or {}).get('seconds')]
    decision = cohort_lib.decide_extra_generations(
        remaining_seconds=budget.remaining, evaluation_seconds=baseline['seconds'], per_generation_seconds=costs, extra_cases=len(conditional),
        first_update_seconds=rule['first_update_reserve_seconds'], safety_multiplier=rule['safety_multiplier'], export_seconds=rule['export_reserve_seconds'])
    if decision['add']:
        event('extending_baseline_generation_cohort', extra_cases=len(conditional))
        baseline = evaluation.extend_generations(model, tokenizer, heldout, cfg, backend, report=baseline, extra_ids=conditional, budget=budget,
                                                 reserve_seconds=240, snapshot=lambda report: _write(out / 'baseline_report.json', report), log=log)
    dropped = sorted(set(baseline.pop('dropped_generation_ids', [])))
    final = sorted(mandatory + [i for i in conditional if i not in dropped]) if decision['add'] else sorted(mandatory)
    baseline['generation_decision'] = {**decision, 'dropped_ids': dropped}
    baseline['generation_ids'] = final
    _write(out / 'baseline_report.json', baseline)
    frozen = {'schema': 'security-generation-cohort-frozen/1', 'ids': final, 'mandatory': sorted(mandatory),
              'conditional_added': bool(decision['add']) and len(final) > len(mandatory), 'dropped_ids': dropped, 'decision': decision,
              'frozen_before_training': True}
    _write(out / 'generation_cohort_frozen.json', frozen)
    event('generation_cohort_frozen', generation_cases=len(final), conditional_added=bool(decision['add']) and len(final) > len(mandatory))
    return baseline, final


def run(*, bundle_dir, run_dir, total_budget_minutes=30, deadline=None, max_steps=32, tiny=False,
        allow_experimental_tpu=False, accelerator_kind=None, clock=time.monotonic, log=print):
    if (Path(bundle_dir) / 'eval' / 'plan.json').is_file():     # an evaluation-only bundle (saved adapters, no training): same supervisor, same archive
        from training.finetune import security_eval
        return security_eval.run(bundle_dir=bundle_dir, run_dir=run_dir, total_budget_minutes=total_budget_minutes, deadline=deadline, max_steps=max_steps,
                                 tiny=tiny, allow_experimental_tpu=allow_experimental_tpu, accelerator_kind=accelerator_kind, clock=clock, log=log)
    budget = Budget(total_budget_minutes, deadline=deadline, clock=clock)
    if type(max_steps) is not int or not 1 <= max_steps <= MAX_STEPS_LIMIT:
        raise ValueError(f'Security trial max_steps must be from 1 to {MAX_STEPS_LIMIT}')
    bundle, out = Path(bundle_dir).resolve(), Path(run_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'security_trial.json').exists() or (out / 'adapter').exists():
        raise ValueError('Security trial requires a fresh output directory')
    status = {'schema': 'security-training-trial/1', 'state': 'initializing', 'experimental': True,
        'software_test': tiny, 'synthetic': True, 'fresh_adapter': True, 'steps_done': 0,
        'nonzero_lr_updates': 0, 'counts_as_full_training': False, 'counts_as_evaluation': False,
        'independent_security_accuracy': None, 'decision_head': 'not_used', 'calibration': 'not_fabricated',
        'total_budget_minutes': total_budget_minutes, 'deadline_monotonic': budget.deadline,
        'training_task_exposure': {}, 'field_supervised_tokens': {}}
    baseline = tuned = None
    trainer = None
    plan2, views_by_id, data_root = None, {}, None
    kind3 = False

    def event(phase, **facts):
        status.update(phase=phase, elapsed_seconds=budget.elapsed, remaining_seconds=budget.remaining, **facts)
        _write(out / 'security_trial.json', status)
        with (out / 'security_trial_events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'phase': phase, 'elapsed_seconds': budget.elapsed,
                'remaining_seconds': budget.remaining, **facts}, default=str) + '\n')
        log(f'Security trial {phase}; {budget.remaining:.1f}s remain')

    try:
        event('data_preflight')
        manifest_path = bundle / 'data/dataset_manifest.json'
        dataset_schema = json.loads(manifest_path.read_text()).get('schema') if manifest_path.is_file() else None
        if dataset_schema not in ('security-trial-dataset/1', 'security-trial-dataset/2', 'security-trial-dataset/3'):
            status.update(state='blocked', reason_code='security_training_data_required',
                detail='Use a newly prepared authored synthetic security trial, not a source-only corpus or unreviewed collected candidates.')
            return status
        status['bundle_manifest_sha256'] = _verify_bundle(bundle)
        from training.finetune import security_trial_data, security_trial_data_v2, resolve, config, security_trial_evaluation as evaluation
        from training.finetune import security_trial_cohorts as cohort_lib
        from training.finetune import runner, model as md, accelerator, runtime_compatibility, runtime_resources
        from training.finetune import trainer as trainers, online_training, losses, generation_validation, repair_validation
        from training.finetune.sampler import Sampler
        import pyarrow.parquet as pq
        import torch
        kind3 = dataset_schema == 'security-trial-dataset/3'          # grounded mined corpus: three tasks, no insufficient_context pool
        if kind3:
            from training.finetune import grounded_trial, grounded_verify
            grounded_verify.verify(bundle / 'data')
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            run_tasks = set(grounded_trial.TASKS)
        else:
            manifest = (security_trial_data_v2 if dataset_schema == 'security-trial-dataset/2' else security_trial_data).verify_dataset(bundle / 'data')
            run_tasks = TASKS
        cfg = resolve.read_resolved(bundle, config_path=bundle / 'security_trial_contract.yaml')
        if cfg.data.manifest_sha256 != _sha(manifest_path) or cfg.model.serialization not in ('security_chat_v2', 'security_chat_v3', 'security_chat_v4', 'security_chat_v5'):
            raise ValueError('Security dataset or versioned response protocol identity differs')
        plan = json.loads((bundle / 'security_trial_plan.json').read_text(encoding='utf-8'))
        if kind3:
            run_tasks = set(grounded_trial.active_tasks(grounded_trial.plan_recipe(plan)))       # recipe 5 draws discovery and repair only
        if (plan.get('schema') not in ('security-training-plan/1', 'security-training-plan/2', 'security-training-plan/3')
                or plan.get('training_loss') != 'present_field_normalized_ce'
                or set(plan.get('max_new_tokens', {})) != run_tasks
                or any(type(n) is not int or not 1 <= n <= 1536 for n in plan['max_new_tokens'].values())
                or set(plan.get('field_weights', {})) != set(losses.FIELD_GROUPS)
                or any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 for value in plan['field_weights'].values())):
            raise ValueError('Invalid bounded security training/decoding plan')
        require_matching_schemas(dataset_schema, plan['schema'])
        trial_recipe = grounded_trial.plan_recipe(plan) if kind3 else cohort_lib.plan_recipe(plan)    # None: the v4 recipe (constant rate, equal task cycle); else a frozen v5, v6 or grounded recipe
        if trial_recipe is not None:
            (grounded_trial if kind3 else cohort_lib).check_recipe_dataset(trial_recipe, manifest.get('tag'))
        if trial_recipe is None:
            if (cfg.training.warmup_ratio != 0 or cfg.training.warmup_steps not in (None, 0)
                    or cfg.training.scheduler != 'constant'):
                raise ValueError('Short security trial requires explicit positive-first-step constant learning rate')
        else:
            cohort_lib.check_recipe_against_config(trial_recipe, training=cfg.training, task_shares=cfg.sampling.task_shares)
        max_steps = min(max_steps, plan.get('max_steps', max_steps))
        cycle = sorted(run_tasks)
        sampling_policy = ({'policy': 'equal_task_cycle_v1', 'task_cycle': cycle, 'examples_per_task_per_optimizer_batch': 2}
                           if trial_recipe is None else cohort_lib.recipe_sampling_policy(trial_recipe))
        if (plan.get('sampling_policy') != sampling_policy
                or cfg.training.effective_sequence_batch_size != 8
                or (trial_recipe is None and any(abs(cfg.sampling.task_shares.get(task, 0) - .25) > 1e-9 for task in TASKS))):
            raise ValueError('Security trial requires a declared equal task cycle with two examples per task per optimizer batch'
                             if trial_recipe is None else 'Security trial requires the declared recipe task counts per optimizer batch')
        status.update(model=cfg.model.id, model_revision=cfg.model.revision, tokenizer_revision=cfg.model.tokenizer_revision,
            serialization=cfg.model.serialization, dataset={'tag': manifest['tag'], 'manifest_sha256': cfg.data.manifest_sha256},
            resolved_config_sha256=_sha(bundle / 'resolved_config.json'), plan_sha256=_sha(bundle / 'security_trial_plan.json'),
            training_sampling=sampling_policy,
            training_recipe={'loss': plan['training_loss'], 'field_weights': plan['field_weights'],
                'effective_batch_size': cfg.training.effective_sequence_batch_size, 'learning_rate': cfg.training.learning_rate,
                'scheduler': cfg.training.scheduler, 'warmup_steps': cfg.training.warmup_steps or 0, **(trial_recipe or {})})
        shutil.copyfile(bundle / 'resolved_config.json', out / 'resolved_config.json')
        shutil.copyfile(bundle / 'security_trial_plan.json', out / 'security_trial_plan.json')
        train_views, heldout = runner.load_views(bundle / 'data', 'train'), runner.load_views(bundle / 'data', 'val')
        if dataset_schema in ('security-trial-dataset/2', 'security-trial-dataset/3'):
            legacy = runner.load_views(bundle / 'data', 'transfer' if kind3 else 'legacy')    # the transfer slice plays the legacy cohort's role
            plan2 = plan if kind3 else cohort_lib.read_plan(plan)
            (grounded_trial.validate_plan if kind3 else cohort_lib.validate_plan_v2)(plan2, train_views + heldout + legacy)
            wanted = set(plan2['cohorts']['evaluation_views'])
            heldout = [v for v in heldout + legacy if v['view_id'] in wanted]
            if len(heldout) != len(wanted):
                raise ValueError('The plan names evaluation views that the bundle does not hold')
            views_by_id, data_root = {v['view_id']: v for v in heldout}, bundle / 'data'
        if not train_views or not heldout or set(v['task'] for v in train_views) != run_tasks or set(v['task'] for v in heldout) != run_tasks:
            raise ValueError('Security trial requires every task of the dataset kind in both partitions')
        generation_ids = (list(plan2['cohorts']['generation_mandatory']) if plan2 is not None
                          else evaluation.generation_cohort(heldout, plan.get('generation_cases', 6)))
        records = {row['record_id']: row for row in pq.read_table(bundle / 'data/records.parquet').to_pylist()}
        status['coverage'] = {'dataset_schema': dataset_schema, 'plan_schema': plan['schema'], 'train_views': len(train_views), 'heldout_views': len(heldout),
            'train_by_task': dict(Counter(v['task'] for v in train_views)), 'generation_ids': generation_ids}
        event('resource_preflight')
        backend = (accelerator.select_backend('auto', physical_kind=accelerator_kind)
                   if not tiny and accelerator_kind is not None else accelerator.select_backend('cpu' if tiny else 'auto'))
        status['requested_accelerator_kind'] = accelerator_kind
        if not tiny and backend.name == 'cpu':
            resources = runtime_resources.preflight(bundle, out, cfg, backend=backend)
            _write(out / 'resource_report.json', resources)
            status.update(state='blocked', reason_code='accelerator_unavailable_after_setup',
                detail='The training process cannot access CUDA or TPU. Inspect actual hardware and fresh PyTorch probe reports; no CPU or tiny-model fallback was started.')
            return status
        cfg, recipe = runtime_compatibility.effective_config(cfg, backend, allow_experimental_tpu=allow_experimental_tpu)
        status['runtime_recipe'] = recipe
        _write(out / 'effective_config.json', cfg.model_dump(mode='json'))
        resources = ({'state': 'ready', 'software_test': True, 'resources': {'backend': 'cpu'}, 'execution': {}}
                     if tiny else runtime_resources.preflight(bundle, out, cfg, backend=backend))
        _write(out / 'resource_report.json', resources)
        if resources['state'] != 'ready':
            status.update(state='blocked', reason_code='insufficient_resources', detail=resources.get('reasons'))
            return status
        if not tiny:
            runtime_resources.apply_process_policy(resources)
            kernels = runtime_compatibility.validate_runtime(backend)
            _write(out / 'runtime_compatibility.json', kernels)
            if kernels['state'] != 'ready':
                status.update(state='blocked', reason_code='incompatible_runtime', detail=kernels)
                return status
        if not budget.can_start(5, 240):
            status.update(state='partial', reason_code='setup_exhausted_budget')
            return status
        event('loading_pinned_original')
        model, tokenizer = _load_model(cfg, backend, train_views, heldout, tiny)
        status['model_loaded'] = runner.model_facts(model)
        event('token_audit')
        dataset, items, audit = runner.build_examples(train_views, runner.load_repositories(bundle / 'data'), tokenizer,
            cfg.context, protocol=cfg.model.serialization)
        if audit['encoded'] != len(train_views) or audit['excluded']:
            raise ValueError('Security supervision does not fit the pinned tokenizer: ' + str(audit))
        available_fields = Counter()
        for example in dataset._ex.values():
            available_fields.update(example.encoded.field_tokens)
        if any(available_fields.get(field, 0) == 0 for field in ('decision', 'evidence', 'patch')):
            raise ValueError('Security training has no decision/evidence/patch supervision')
        status['training_token_audit'] = {**audit, 'fields': dict(available_fields)}
        _write(out / 'training_token_audit.json', status['training_token_audit'])
        event('baseline_evaluation')
        baseline = evaluation.evaluate(model, tokenizer, heldout, cfg, backend, arm='base', budget=budget,
            generation_ids=generation_ids, max_new_tokens=plan['max_new_tokens'], reserve_seconds=240,
            snapshot=lambda report: _write(out / 'baseline_report.json', report), log=log)
        if baseline['state'] != 'finished':
            status.update(state='partial', reason_code='baseline_incomplete')
            return status
        if plan2 is not None:
            baseline, generation_ids = _freeze_generation_cohort(model, tokenizer, heldout, cfg, backend, baseline, plan2, budget, out, event, log)
            if baseline['state'] != 'finished':
                status.update(state='partial', reason_code='baseline_incomplete')
                return status
        evaluation_seconds = max(1, baseline['seconds'])
        if not budget.can_start(5, evaluation_seconds*1.5+120):
            status.update(state='partial', reason_code='no_training_budget_after_baseline')
            return status
        event('preparing_fresh_adapter')
        torch.manual_seed(cfg.training.pilot_seed)
        model = md.add_adapter(model, cfg.lora)
        init_adapter = plan.get('init_adapter')
        if bool(trial_recipe and trial_recipe.get('init_adapter')) != bool(init_adapter):
            raise ValueError('The recipe and the plan disagree about starting from a saved adapter')
        if init_adapter:                                 # stage B of a staged run: start from the adapter of stage A (a warm restart; the optimizer and the schedule start fresh)
            from training.finetune import security_eval
            adapter_dir = bundle / init_adapter['directory']
            if hashlib.sha256((adapter_dir / 'adapter_model.safetensors').read_bytes()).hexdigest() != init_adapter['sha256']:
                raise ValueError('The initial adapter differs from the one the plan names')
            security_eval.check_adapter_shape(adapter_dir, cfg, init_adapter['lora'])
            security_eval.load_adapter_weights(model, adapter_dir)
            status['init_adapter'] = {'sha256': init_adapter['sha256'], 'source': init_adapter.get('source')}
            status['fresh_adapter'] = False
            event('init_adapter_loaded')
        checkpoint_fn = None
        if backend.name == 'tpu':
            from torch_xla.utils.checkpoint import checkpoint as checkpoint_fn
        md.prepare_for_training(model, gradient_checkpointing=cfg.training.gradient_checkpointing and not tiny,
            checkpoint_fn=checkpoint_fn)
        initial_probe = _adapter_sample(model)
        if trial_recipe is None:
            sampler = Sampler(items, task_shares=cfg.sampling.task_shares, pool_targets=cfg.sampling.decision_pool_targets,
                seed=cfg.training.pilot_seed, task_cycle=cycle)
            batch_task_counts = {task: 2 for task in TASKS}
        else:
            counts = trial_recipe['task_counts_per_batch']
            pairs = None
            if trial_recipe.get('pairing'):
                have = {item.view_id for item in items}
                pairs = [pair for pair in grounded_trial.twin_pairs(train_views) if pair[0] in have and pair[1] in have]
                status['training_pairs'] = {'pairs': len(pairs), 'policy': trial_recipe['pairing']}
            epoch_plan = None
            if pairs is not None and trial_recipe.get('allocation'):
                from training.finetune import allocation as allocation_lib
                spec = trial_recipe['allocation']
                plan3 = allocation_lib.plan_epoch([v for v in train_views if v['view_id'] in have], pairs, total_draws=spec['total_discovery_draws'], seed=cfg.training.pilot_seed,
                                                   tau=spec['tau'], d_max=spec['d_max'], lone_share=spec['lone_share'], eta=({**allocation_lib.ETA, **spec['eta']} if spec.get('eta') else None))
                epoch_plan = {'discovery': plan3['rounds']}
                status['training_allocation'] = plan3['report']
                event('allocation_planned', **{k: plan3['report'][k] for k in ('draws', 'share_in_co_drawn_pairs', 'share_no_finding', 'paired_units')})
            sampler = Sampler(items, task_shares={task: n / sum(counts.values()) for task, n in counts.items()},
                pool_targets=cfg.sampling.decision_pool_targets, seed=cfg.training.pilot_seed, task_counts=counts, pairs=pairs, epoch_plan=epoch_plan)
            batch_task_counts = dict(counts)
        twin_margin = (trial_recipe or {}).get('twin_margin') or {}
        loss_cfg = losses.LossConfig(mode=plan['training_loss'], field_weights=plan['field_weights'],
            decision_weight=0, rank_weight=0, pair_enabled=False, margin=1,
            lm_pair_weight=float(twin_margin.get('weight', 0.0)), lm_pair_margin=float(twin_margin.get('margin', 2.2)))
        recipe_identity = hashlib.sha256((status['resolved_config_sha256'] + status['plan_sha256']).encode()).hexdigest()
        meta = {'base_commit': cfg.model.revision, 'tokenizer_revision': cfg.model.tokenizer_revision,
            'serialization': cfg.model.serialization, 'serialization_hash': runner.sz.serialization_hash(cfg.model.serialization, tokenizer),
            'dataset_manifest_sha256': cfg.data.manifest_sha256, 'config_sha256': recipe_identity,
            'stage': 1, 'seed': cfg.training.pilot_seed, 'runtime_recipe': recipe, 'plan_sha256': status['plan_sha256']}
        trainer = trainers.Trainer(model=model, head=torch.nn.Identity(), dataset=dataset, sampler=sampler,
            training=cfg.training, lora=cfg.lora, loss_cfg=loss_cfg, run_dir=out, pad_id=tokenizer.pad_token_id,
            seed=cfg.training.pilot_seed, total_steps=max_steps, meta=meta, device=str(backend.device), dtype=backend.dtype,
            backend=backend, evaluator=None, clock=clock)
        if twin_margin:
            trainer.set_twin_pairs([pair for pair in pairs] if pairs else [])
            status['twin_margin'] = {**twin_margin, 'pairs': len(pairs or [])}
        online_training.configure(trainer, mode='auto', log=log)
        if not tiny:
            runtime_resources.apply_execution_policy(trainer, resources)
        # This supervisor admits checkpoint I/O against its shared reserve.
        from training.finetune.checkpoint import Policy
        trainer.policy = Policy(1000000, None, 1)
        event('bounded_training_probe')
        profile = trainer.profile()
        step_seconds = max(1, profile['seconds'] * profile['micro_batches_per_optimizer_step'] * 1.5)
        profile_step_estimate = step_seconds
        planned = training_allowance(budget, step_seconds=step_seconds, evaluation_seconds=evaluation_seconds, max_steps=max_steps)
        if not planned:
            status.update(state='partial', reason_code='probe_left_no_training_budget')
            return status
        trainer.total_steps = planned
        trainer.scheduler = (trainers.make_scheduler(trainer.optimizer, cfg.training, planned) if trial_recipe is None
                             else trainers.make_warmup_cosine(trainer.optimizer, warmup_steps=trial_recipe['warmup_steps'], total_steps=planned))
        if trainer.optimizer.param_groups[0]['lr'] <= 0:
            raise ValueError('First security update must have a positive learning rate')
        status['planned_steps'] = planned
        last_checkpoint = clock()
        event('training')
        durations, iteration_times = [], []
        while trainer.step < planned and budget.can_start(step_seconds*1.5, evaluation_seconds*1.5+120):
            before = clock()
            try:
                trainer.train(max_steps=1)
            except online_training.ResourceLimit as error:
                status.update(resource_stop=error.report, reason_code='training_resource_limit')
                break
            durations.append(clock()-before)
            step_seconds = max(step_seconds, durations[-1])
            state = sampler.state_dict()
            fields = Counter()
            for view_id, draws in state['by_view'].items():
                for field, count in dataset.get(view_id).encoded.field_tokens.items():
                    fields[field] += draws*count
            status.update(steps_done=trainer.step, nonzero_lr_updates=sum(row['lr'] > 0 for row in trainer.history),
                training_task_exposure=state['by_task'], field_supervised_tokens=dict(fields))
            if trainer.step == 1:
                status['first_optimizer_batch'] = {'step': 1, 'draws': state['draws'],
                    'task_counts': state['by_task'], 'by_view': state['by_view'],
                    'field_supervised_tokens': dict(fields)}
                if (state['by_task'] != batch_task_counts or fields['patch'] <= 0):
                    raise ValueError('First security optimizer batch did not train every task and patch supervision')
            exposure = sampler.exposure_report()
            status['learning_rate_trace'] = [row['lr'] for row in trainer.history]
            _write(out / 'training_exposure.json', {**exposure, 'by_view': state['by_view'],
                'field_supervised_tokens': dict(fields), 'first_optimizer_batch': status.get('first_optimizer_batch')})
            event('training', step=trainer.step, loss=trainer.history[-1]['loss'])
            if (trainer.step == 1 or clock()-last_checkpoint > 120) and budget.can_start(30, evaluation_seconds*1.5+120):
                event('recovery_checkpoint')
                checkpoint = trainer.save_checkpoint()
                status['latest_recovery_checkpoint'] = str(checkpoint)
                last_checkpoint = clock()
            iteration_times.append(clock()-before)                      # the whole iteration: update, exposure files, events and checkpoint
            if trial_recipe is not None and trainer.step == trial_recipe['warmup_steps'] + 1 and 'replanned' not in status:
                # The first estimate comes from one short profiled example. With real update times in hand, re-plan the horizon once so the
                # cosine decay can finish on the clock (and the idle minutes of an over-cautious estimate are used); the reserves are unchanged.
                measured = sorted(iteration_times)[len(iteration_times) // 2]       # the median: the first iteration also writes a recovery checkpoint
                total = replanned_total(budget, done=trainer.step, measured_step_seconds=measured,
                                        evaluation_seconds=evaluation_seconds, max_steps=max_steps)
                if total > trainer.step and total != planned:
                    import warnings
                    status['replanned'] = {'at_step': trainer.step, 'from_steps': planned, 'to_steps': total,
                                           'measured_step_seconds': measured, 'initial_estimate_seconds': profile_step_estimate}
                    trainer.total_steps = planned = total
                    trainer.scheduler = trainers.make_warmup_cosine(trainer.optimizer, warmup_steps=trial_recipe['warmup_steps'], total_steps=planned)
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore')               # advancing a new scheduler before its optimizer step is intended here
                        for _ in range(trainer.step):
                            trainer.scheduler.step()
                    step_seconds = max(durations + iteration_times)
                    status['planned_steps'] = planned
        status['schedule_completed'] = trainer.step >= planned          # false: the clock ended the run before the learning rate finished decaying
        step_losses = [row['loss'] for row in trainer.history]
        if len(step_losses) >= 16:                                      # two windows of eight that do not overlap
            status['training_loss_windows'] = {'window': 8, 'first_mean': sum(step_losses[:8]) / 8, 'last_mean': sum(step_losses[-8:]) / 8}
        if trial_recipe is not None:
            coverage = sampler.exposure_report()['view_coverage']
            status['view_coverage'] = coverage
            status['training_view_coverage_gate'] = cohort_lib.coverage_gate(trial_recipe, coverage, trainer.step)
            if kind3:      # the grounded corpus is far larger than a 30-minute run can draw; coverage is reported, never a gate
                status['training_view_coverage_gate']['met'] = None
        if not trainer.step:
            status.update(state='partial', reason_code='no_optimizer_update')
            return status
        event('saving_adapter')
        _save_adapter(model, backend, out / 'adapter')
        status['adapter'] = {'sha256': _sha(out / 'adapter/adapter_model.safetensors'), 'steps_done': trainer.step,
            'sample_before_sha256': initial_probe, 'sample_after_sha256': _adapter_sample(model)}
        status['adapter_samples_changed'] = status['adapter']['sample_before_sha256'] != status['adapter']['sample_after_sha256']
        if not status['adapter_samples_changed']:
            raise ValueError('Security adapter sample weights did not change')
        trainer.optimizer.zero_grad(set_to_none=True)
        trainer.optimizer.state.clear()
        if backend.name == 'cuda':
            torch.cuda.empty_cache()
        event('adapted_evaluation')
        tuned = evaluation.evaluate(model, tokenizer, heldout, cfg, backend, arm='adapter', budget=budget,
            generation_ids=generation_ids, max_new_tokens=plan['max_new_tokens'], reserve_seconds=120,
            snapshot=lambda report: _write(out / 'tuned_report.json', report), log=log)
        for name, report in (('base', baseline), ('adapter', tuned)):
            raw = [{'view_id': row['view_id'], 'task': row['task'], 'text': row['generation']['text'],
                    'hit_limit': row['generation'].get('hit_limit', False)} for row in report['rows']
                   if (row.get('generation') or {}).get('text') is not None]
            not_applicable = {'state': 'not_applicable', 'detail': 'The grounded corpus keeps the shown code in the prompt; response_diagnostics.json holds evidence and patch checks.'}
            _write(out / f'{name}_static_checks.json', {'evidence': not_applicable if kind3 else generation_validation.measure(raw, heldout, records, bundle/'data', protocol=cfg.model.serialization),
                'repair': not_applicable if kind3 else repair_validation.measure(raw, heldout, records, bundle/'data', protocol=cfg.model.serialization),
                'strict_response_contract': {row['view_id']: row.get('response_contract') for row in report['rows'] if row.get('generation')},
                'interpretation': 'Static diagnostics cannot override the strict security_chat_v2 response contract, establish semantic support or prove a functional/security repair.'})
        status.update(state='finished' if tuned['state'] == 'finished' and not status.get('resource_stop') else 'partial',
            training_stopped='resource_limit' if status.get('resource_stop') else 'step_target' if trainer.step >= planned else 'evaluation_reserve')
        return status
    except Exception as error:
        import traceback
        status.update(state='blocked' if isinstance(error, ValueError) else 'failed', reason_code=type(error).__name__, detail=str(error))
        _write(out / 'failure.json', {'phase': status.get('phase'), 'type': type(error).__name__, 'detail': str(error), 'traceback': traceback.format_exc()})
        return status
    finally:
        from training.finetune import security_trial_evaluation as evaluation
        from training.finetune.security_training_bootstrap import export_outputs
        try:
            if plan2 is not None:
                from training.finetune import security_trial_cohorts as cohort_lib
                if kind3:
                    from training.finetune import grounded_trial
                diagnose = grounded_trial.diagnose_report if kind3 else cohort_lib.diagnose_report
                diagnostics = {arm: diagnose(report, views_by_id, data_root)
                               for arm, report in (('base', baseline), ('adapter', tuned)) if report is not None}
                _write(out / 'response_diagnostics.json', {'schema': 'security-response-diagnostics/1', 'arms': diagnostics})
                comparison = cohort_lib.compare_v2(baseline, tuned, plan=plan2, diagnostics=diagnostics, views=views_by_id)
                if kind3 and comparison.get('gates'):
                    comparison['gates']['insufficient_context_not_regressed'] = {'met': None, 'detail': 'not covered: the grounded corpus has no insufficient_context pool'}
                    comparison['dataset_kind'] = {'schema': 'security-trial-dataset/3', 'legacy_regression_loss_is': 'transfer: weakness classes held out of training',
                                                  'interpretation': 'Development and transfer measurements on unreviewed mined data; no independent security accuracy claim.'}
                    try:
                        comparison['dataset_kind']['answer_checks'] = grounded_trial.answer_checks({'base': baseline, 'adapter': tuned}, views_by_id)
                    except Exception as check_error:      # informational only: it must never cost the comparison
                        comparison['dataset_kind']['answer_checks'] = {'state': 'unavailable', 'detail': f'{type(check_error).__name__}: {check_error}'}
            else:
                comparison = evaluation.compare(baseline, tuned)
        except Exception as error:         # an analysis failure must never cost the adapter and reports of a finished run
            comparison = {'schema': 'security-training-comparison/2' if plan2 is not None else 'security-training-comparison/1',
                'state': 'partial', 'detail': f'{type(error).__name__}: {error}',
                'loss_delta_tuned_minus_base': None, 'independent_security_accuracy': None}
            status['state'] = 'partial'
        _write(out / 'security_trial_comparison.json', comparison)
        if trainer is not None and getattr(trainer, 'stager', None):
            trainer.stager.close()
        event('export')
        export_outputs(out, status, compact=False, clock=clock, deadline=budget.deadline)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle-dir', required=True)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--total-budget-minutes', type=float, default=30)
    parser.add_argument('--deadline', type=float)
    parser.add_argument('--max-steps', type=int, default=32)
    parser.add_argument('--tiny', action='store_true')
    parser.add_argument('--allow-experimental-tpu', action='store_true')
    parser.add_argument('--accelerator-kind', choices=('cuda', 'tpu'))
    status = run(**vars(parser.parse_args()))
    print(json.dumps({key: status.get(key) for key in ('state', 'reason_code', 'steps_done', 'training_task_exposure')}))
    # A truthful partial/blocked report is a normal process outcome, not a crash.
    return 0 if status['state'] in ('finished', 'partial', 'blocked') else 2


if __name__ == '__main__':
    raise SystemExit(main())
