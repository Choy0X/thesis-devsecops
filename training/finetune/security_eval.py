"""Evaluation-only run: the pinned original model and one or more saved adapters on one frozen list of held-out cases.

Nothing is trained. The bundle holds the grounded dataset, a plan (eval/plan.json) that names the cases and the adapters, and the adapter files.
Every arm answers the identical prompts with the same unconstrained greedy decoding as the training trial, so the arms are paired.
Held-out measurements on mined, unreviewed data; no independent security accuracy claim.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import time

from training.finetune.trial import Budget, _write, _sha

PLAN_PATH = 'eval/plan.json'
SCHEMA = 'grounded-eval-plan/1'
SCHEMA_2 = 'grounded-eval-plan/2'          # several view sets, each with the protocol its arms are prompted in (the cohort is the same functions in every set)
PROTOCOLS = ('security_chat_v2', 'security_chat_v3', 'security_chat_v4', 'security_chat_v5')
HELD_OUT = ('development', 'transfer')
ARM_NAME = re.compile(r'[a-z][a-z0-9_]{0,23}')
QUOTAS = {('development', 'discovery_open'): 6, ('development', 'discovery_scoped'): 6, ('development', 'negative_fixed'): 5,
          ('development', 'negative_other_class'): 5, ('development', 'negative_fixed_open'): 3, ('development', 'negative_neighbour'): 1,
          ('development', 'repair'): 4, ('development', 'repair_open'): 4,
          ('transfer', 'discovery_open'): 6, ('transfer', 'discovery_scoped'): 6, ('transfer', 'negative_fixed'): 5,
          ('transfer', 'negative_other_class'): 5, ('transfer', 'negative_fixed_open'): 2, ('transfer', 'negative_neighbour'): 2}


def view_unit(view):
    return json.loads(view['lineage_json']).get('unit')


def select_cases(views, *, forced=(), quotas=None):
    """A deterministic cohort: the forced views first, then each (partition, kind) stratum filled to its quota, preferring functions
    not yet drawn in that stratum, ties broken by a hash of the view id. Strata the data does not hold are skipped."""
    quotas = QUOTAS if quotas is None else quotas
    by_id = {view['view_id']: view for view in views}
    missing = [view_id for view_id in forced if view_id not in by_id]
    if missing:
        raise ValueError('Forced views are not held-out views of this bundle: ' + ', '.join(sorted(missing)[:3]))
    chosen = list(dict.fromkeys(forced))
    strata = defaultdict(list)
    for view in sorted(views, key=lambda row: row['view_id']):
        strata[(view['partition_role'], view['kind'])].append(view)
    for stratum, quota in sorted(quotas.items()):
        picked = [by_id[view_id] for view_id in chosen if (by_id[view_id]['partition_role'], by_id[view_id]['kind']) == stratum]
        drawn = Counter(view_unit(view) for view in picked)
        pool = [view for view in strata.get(stratum, []) if view['view_id'] not in chosen]
        while len(picked) < quota and pool:
            best = min(pool, key=lambda view: (drawn[view_unit(view)], hashlib.sha256(view['view_id'].encode()).hexdigest()))
            pool.remove(best)
            picked.append(best)
            drawn[view_unit(best)] += 1
            chosen.append(best['view_id'])
    return chosen


def validate_plan(plan, views):
    """The plan names real held-out views and well-formed, uniquely named adapters. The blind partition can never be named."""
    if plan.get('schema') != SCHEMA or plan.get('frozen') is not True:
        raise ValueError('Not a frozen grounded evaluation plan')
    ids = plan.get('view_ids')
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        raise ValueError('The evaluation plan must name a nonempty list of distinct views')
    by_id = {view['view_id']: view for view in views}
    for view_id in ids:
        if view_id not in by_id or by_id[view_id]['partition_role'] not in HELD_OUT:
            raise ValueError('The evaluation plan names a view that is not a held-out view of this bundle: ' + str(view_id))
    adapters = plan.get('adapters')
    if not isinstance(adapters, list) or not adapters:
        raise ValueError('The evaluation plan names no adapter')
    names = [row.get('name') for row in adapters]
    if len(set(names)) != len(names) or any(not isinstance(name, str) or not ARM_NAME.fullmatch(name) or name == 'base' for name in names):
        raise ValueError('Adapter names must be unique lower-case words other than base')
    for row in adapters:
        if row.get('directory') != 'eval/adapters/' + row['name'] or not isinstance(row.get('lora'), dict):
            raise ValueError('Adapter ' + row['name'] + ' has an unexpected directory or no declared LoRA shape')
        reference = row.get('reference_generation_sha256', {})
        if any(view_id not in ids for view_id in reference):
            raise ValueError('A reference generation names a view that is not in the cohort')
    return ids, adapters


def validate_plan_2(plan, bundle):
    """A plan/2 names view sets (eval/sets/<name>/views.parquet, each held-out rows only, with the protocol its prompts were built for), the sets the pinned
    original is measured on, and adapters, each measured on one set. Returns {set name: (protocol, views in view id order)} and the adapter rows."""
    from training.finetune import runner
    if plan.get('schema') != SCHEMA_2 or plan.get('frozen') is not True:
        raise ValueError('Not a frozen grounded evaluation plan/2')
    sets, loaded = plan.get('sets'), {}
    if not isinstance(sets, dict) or not sets:
        raise ValueError('The evaluation plan names no view set')
    for name, spec in sorted(sets.items()):
        if not ARM_NAME.fullmatch(name) or spec.get('directory') != 'eval/sets/' + name or spec.get('protocol') not in PROTOCOLS:
            raise ValueError('View set ' + str(name) + ' is malformed')
        ids = spec.get('view_ids')
        if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
            raise ValueError('View set ' + name + ' must name a nonempty list of distinct views')
        rows = {view['view_id']: view for split in ('val', 'transfer') for view in runner.load_views(Path(bundle) / spec['directory'], split)}
        for view_id in ids:
            if view_id not in rows or rows[view_id]['partition_role'] not in HELD_OUT:
                raise ValueError('View set ' + name + ' names a view that is not a held-out view: ' + str(view_id))
        loaded[name] = (spec['protocol'], [rows[view_id] for view_id in sorted(ids)])
    bases = plan.get('base_sets')
    if plan.get('no_base_arm') is True:                      # an evaluation of a saved adapter against saved numbers (the pre-declared rule): the pinned original is not measured again
        if bases != []:
            raise ValueError('a plan without a base arm names no base set')
    elif not isinstance(bases, list) or not bases or any(name not in loaded for name in bases) or len(set(bases)) != len(bases):
        raise ValueError('base_sets must name distinct view sets of the plan')
    adapters = plan.get('adapters')
    if not isinstance(adapters, list) or not adapters:
        raise ValueError('The evaluation plan names no adapter')
    names = [row.get('name') for row in adapters] + ['base_' + name for name in bases]
    if len(set(names)) != len(names) or any(not isinstance(name, str) or not ARM_NAME.fullmatch(name) for name in names) or any(
            row.get('name', '').startswith('base') for row in adapters):
        raise ValueError('Arm names must be unique lower-case words; adapters may not start with base')
    for row in adapters:
        if row.get('directory') != 'eval/adapters/' + row['name'] or not isinstance(row.get('lora'), dict) or row.get('set') not in loaded:
            raise ValueError('Adapter ' + str(row.get('name')) + ' has an unexpected directory, no declared LoRA shape or an unknown view set')
        if any(view_id not in sets[row['set']]['view_ids'] for view_id in row.get('reference_generation_sha256', {})):
            raise ValueError('A reference generation names a view that is not in the adapter\'s view set')
    return loaded, adapters, bases


def check_adapter_shape(directory, cfg, declared):
    """The saved adapter must have the rank, alpha and target modules of the resolved configuration, or loading it would silently mis-measure."""
    config = json.loads((Path(directory) / 'adapter_config.json').read_text(encoding='utf-8'))
    found = {'r': config.get('r'), 'lora_alpha': config.get('lora_alpha'), 'target_modules': sorted(config.get('target_modules') or [])}
    wanted = {'r': cfg.lora.r, 'lora_alpha': cfg.lora.lora_alpha, 'target_modules': sorted(cfg.lora.target_modules)}
    if found != wanted or {key: declared.get(key) for key in ('r', 'lora_alpha')} != {'r': wanted['r'], 'lora_alpha': wanted['lora_alpha']}:
        raise ValueError(f'The adapter differs from the resolved LoRA shape: found {found}, expected {wanted}')
    return found


def load_adapter_weights(model, directory):
    from peft import load_peft_weights, set_peft_model_state_dict
    result = set_peft_model_state_dict(model, load_peft_weights(str(directory)))
    unexpected = list(getattr(result, 'unexpected_keys', []) or [])
    if unexpected:
        raise ValueError('The adapter holds weights the model does not have: ' + ', '.join(unexpected[:3]))


def reference_check(report, reference):
    """How many generated texts are byte-identical to a recorded run (by hash). A mismatch is reported, never hidden."""
    if not reference:
        return None
    texts = {row['view_id']: (row.get('generation') or {}).get('text') for row in report['rows']}
    compared = [view_id for view_id in reference if texts.get(view_id) is not None]
    same = [view_id for view_id in compared if hashlib.sha256(texts[view_id].encode('utf-8')).hexdigest() == reference[view_id]]
    return {'recorded': len(reference), 'compared': len(compared), 'identical': len(same), 'different': sorted(set(compared) - set(same))}


def run(*, bundle_dir, run_dir, total_budget_minutes=120, deadline=None, max_steps=1, tiny=False, allow_experimental_tpu=False,
        accelerator_kind=None, clock=time.monotonic, log=print):
    from training.finetune import security_trial
    budget = Budget(total_budget_minutes, deadline=deadline, clock=clock)
    bundle, out = Path(bundle_dir).resolve(), Path(run_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'security_trial.json').exists():
        raise ValueError('The evaluation requires a fresh output directory')
    status = {'schema': 'security-training-trial/1', 'state': 'initializing', 'experimental': True, 'software_test': tiny, 'synthetic': False,
              'mode': 'evaluation_only', 'fresh_adapter': False, 'steps_done': 0, 'nonzero_lr_updates': 0, 'counts_as_full_training': False,
              'counts_as_evaluation': False, 'independent_security_accuracy': None, 'decision_head': 'not_used', 'calibration': 'not_fabricated',
              'total_budget_minutes': total_budget_minutes, 'deadline_monotonic': budget.deadline, 'arms': {}}
    reports, views_by_id, data_root = {}, {}, None

    def event(phase, **facts):
        status.update(phase=phase, elapsed_seconds=budget.elapsed, remaining_seconds=budget.remaining, **facts)
        _write(out / 'security_trial.json', status)
        with (out / 'security_trial_events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'phase': phase, 'elapsed_seconds': budget.elapsed, 'remaining_seconds': budget.remaining, **facts}, default=str) + '\n')
        log(f'Evaluation {phase}; {budget.remaining:.1f}s remain')

    try:
        event('data_preflight')
        status['bundle_manifest_sha256'] = security_trial._verify_bundle(bundle)
        from training.finetune import grounded_trial, grounded_verify, resolve, runner, model as md, accelerator, runtime_compatibility, runtime_resources
        from training.finetune import security_trial_evaluation as evaluation
        grounded_verify.verify(bundle / 'data')
        manifest = json.loads((bundle / 'data/dataset_manifest.json').read_text(encoding='utf-8'))
        cfg = resolve.read_resolved(bundle, config_path=bundle / 'security_trial_contract.yaml')
        if cfg.data.manifest_sha256 != _sha(bundle / 'data/dataset_manifest.json') or cfg.model.serialization not in ('security_chat_v2', 'security_chat_v3', 'security_chat_v4', 'security_chat_v5'):
            raise ValueError('Security dataset or versioned response protocol identity differs')
        trial_plan = json.loads((bundle / 'security_trial_plan.json').read_text(encoding='utf-8'))
        plan = json.loads((bundle / PLAN_PATH).read_text(encoding='utf-8'))
        train_views = runner.load_views(bundle / 'data', 'train')
        held_out = runner.load_views(bundle / 'data', 'val') + runner.load_views(bundle / 'data', 'transfer')
        if plan.get('schema') == SCHEMA_2:
            loaded, adapters, bases = validate_plan_2(plan, bundle)
            base_arms = [('base_' + name, loaded[name][0], loaded[name][1]) for name in bases]
            adapter_views = {row['name']: (loaded[row['set']][0], loaded[row['set']][1]) for row in adapters}
            pool = {view['view_id']: view for _, views_of in loaded.values() for view in views_of}
        else:
            ids, adapters = validate_plan(plan, held_out)
            selected = {view['view_id']: view for view in held_out if view['view_id'] in set(ids)}
            only = [selected[view_id] for view_id in sorted(selected)]
            base_arms = [('base', cfg.model.serialization, only)]
            adapter_views = {row['name']: (cfg.model.serialization, only) for row in adapters}
            pool = selected
        views_by_id, data_root = pool, bundle / 'data'
        views = sorted(pool.values(), key=lambda view: view['view_id'])
        max_new_tokens = {task: trial_plan['max_new_tokens'][task] for task in sorted({view['task'] for view in views})}
        shapes = {row['name']: check_adapter_shape(bundle / row['directory'], cfg, row['lora']) for row in adapters}
        status.update(model=cfg.model.id, model_revision=cfg.model.revision, tokenizer_revision=cfg.model.tokenizer_revision,
                      serialization=cfg.model.serialization, dataset={'tag': manifest['tag'], 'manifest_sha256': cfg.data.manifest_sha256},
                      resolved_config_sha256=_sha(bundle / 'resolved_config.json'), plan_sha256=_sha(bundle / PLAN_PATH),
                      evaluation={'cases': len(views), 'by_partition_and_kind': {f'{a}/{b}': n for (a, b), n in sorted(Counter((v['partition_role'], v['kind']) for v in views).items())},
                                  'units': len({view_unit(v) for v in views}), 'adapters': [row['name'] for row in adapters], 'lora': shapes,
                                  'arm_cases': {name: len(arm_views) for name, _, arm_views in base_arms} | {name: len(v[1]) for name, v in adapter_views.items()},
                                  'protocols': {name: protocol for name, protocol, _ in base_arms} | {name: v[0] for name, v in adapter_views.items()},
                                  'max_new_tokens': max_new_tokens})
        (out / 'resolved_config.json').write_bytes((bundle / 'resolved_config.json').read_bytes())
        (out / 'eval_plan.json').write_bytes((bundle / PLAN_PATH).read_bytes())
        event('resource_preflight')
        backend = (accelerator.select_backend('auto', physical_kind=accelerator_kind) if not tiny and accelerator_kind is not None
                   else accelerator.select_backend('cpu' if tiny else 'auto'))
        status['requested_accelerator_kind'] = accelerator_kind
        if not tiny and backend.name == 'cpu':
            _write(out / 'resource_report.json', runtime_resources.preflight(bundle, out, cfg, backend=backend))
            status.update(state='blocked', reason_code='accelerator_unavailable_after_setup',
                          detail='The evaluation process cannot access CUDA or TPU; no CPU fallback was started.')
            return status
        cfg, recipe = runtime_compatibility.effective_config(cfg, backend, allow_experimental_tpu=allow_experimental_tpu)
        status['runtime_recipe'] = recipe
        _write(out / 'effective_config.json', cfg.model_dump(mode='json'))
        resources = ({'state': 'ready', 'software_test': True, 'resources': {'backend': 'cpu'}, 'execution': {}} if tiny
                     else runtime_resources.preflight(bundle, out, cfg, backend=backend))
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
        # The software-test tokenizer is built from the training trial's texts (its evaluation views), so a tiny adapter is read back in the model it was made in.
        vocabulary_views = [view for view in held_out if view['view_id'] in set(trial_plan['cohorts']['evaluation_views'])] if tiny else views
        model, tokenizer = security_trial._load_model(cfg, backend, train_views, vocabulary_views, tiny)
        status['model_loaded'] = runner.model_facts(model)

        def measure(arm, reserve, protocol, arm_views):
            event(f'evaluating_{arm}')
            arm_cfg = cfg.model_copy(deep=True)
            arm_cfg.model.serialization = protocol
            report = evaluation.evaluate(model, tokenizer, arm_views, arm_cfg, backend, arm=arm, budget=budget, generation_ids=[v['view_id'] for v in arm_views],
                                         max_new_tokens=max_new_tokens, reserve_seconds=reserve,
                                         snapshot=lambda partial: _write(out / f'{arm}_report.json', partial), log=log)
            reports[arm] = report
            generated = [row for row in report['rows'] if (row.get('generation') or {}).get('text') is not None]
            status['arms'][arm] = {'state': report['state'], 'seconds': report['seconds'], 'generated': len(generated),
                                   'schema_valid': report['summary']['schema_valid'], 'token_mean_loss': report['summary']['token_mean_loss']}
            return report

        for arm_name, protocol, arm_views in base_arms:
            measure(arm_name, 240, protocol, arm_views)
            if reports[arm_name]['state'] != 'finished':
                status.update(state='partial', reason_code='baseline_incomplete')
                return status
        import torch
        torch.manual_seed(cfg.training.pilot_seed)
        model = md.add_adapter(model, cfg.lora)
        md.prepare_for_training(model, gradient_checkpointing=cfg.training.gradient_checkpointing and not tiny, checkpoint_fn=None)
        for row in adapters:
            before = security_trial._adapter_sample(model)
            load_adapter_weights(model, bundle / row['directory'])
            if security_trial._adapter_sample(model) == before:
                raise ValueError('Loading the adapter did not change the model: ' + row['name'])
            status['arms'][row['name']] = {'adapter_sha256': _sha(bundle / row['directory'] / 'adapter_model.safetensors'), 'source': row.get('source')}
            report = measure(row['name'], 120, *adapter_views[row['name']])
            status['arms'][row['name']]['reference_generations'] = reference_check(report, row.get('reference_generation_sha256'))
            if backend.name == 'cuda':
                torch.cuda.empty_cache()
        finished = all(reports[name]['state'] == 'finished' for name in reports)
        status.update(state='finished' if finished else 'partial', reason_code=None if finished else 'arm_incomplete')
        return status
    except Exception as error:
        import traceback
        status.update(state='blocked' if isinstance(error, ValueError) else 'failed', reason_code=type(error).__name__, detail=str(error))
        _write(out / 'failure.json', {'phase': status.get('phase'), 'type': type(error).__name__, 'detail': str(error), 'traceback': traceback.format_exc()})
        return status
    finally:
        try:
            from training.finetune import grounded_trial
            if reports and views_by_id:
                _write(out / 'response_diagnostics.json', {'schema': 'security-response-diagnostics/1',
                       'arms': {arm: grounded_trial.diagnose_report(report, views_by_id, data_root) for arm, report in reports.items()}})
                _write(out / 'answer_checks.json', grounded_trial.answer_checks(reports, views_by_id))
        except Exception as error:         # an analysis failure must never cost the generations of a finished run
            status['analysis_error'] = f'{type(error).__name__}: {error}'
            if status.get('state') == 'finished':
                status['state'] = 'partial'
        from training.finetune.security_training_bootstrap import export_outputs
        event('export')
        export_outputs(out, status, compact=False, clock=clock, deadline=budget.deadline)
