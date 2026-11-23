"""Cohorts, plan/2 and comparison/2 for the security trial. Pure functions; no model, no torch.

* ``select_cohorts``: 24 development loss views (one per task, language and mechanism), a mandatory set of six generation cases that
  keeps all four tasks and both decision contrasts, six conditional cases that extend coverage, and every legacy regression view.
* ``decide_extra_generations``: the six conditional cases run only when the measured baseline generation cost still leaves time for one
  training update, the adapted evaluation with its safety multiplier, and the export.
* ``build_plan_v2`` / ``validate_plan_v2`` / ``read_plan``: ``security-training-plan/2`` freezes cohorts, sampling and the verification
  policy before training; the historical ``/1`` plan is still read.
* ``compare_v2``: ``security-training-comparison/2`` reports loss, JSON syntax, schema, status, scoped false positives, missed findings,
  insufficient-context behaviour, evidence, patch applicability and static test checks separately, keeping every planned case in its denominator.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASKS = ('discovery', 'insufficient_context', 'regression_test_design', 'repair')
SCHEMA_PLAN_2 = 'security-training-plan/2'
SCHEMA_COMPARISON_2 = 'security-training-comparison/2'
FIELD_WEIGHTS = {'decision': 2., 'explanation': 1., 'evidence': 2., 'uncertainty': 1., 'patch': 2., 'structure_eos': .5}
MAX_NEW_TOKENS = {'discovery': 384, 'insufficient_context': 384, 'repair': 640, 'regression_test_design': 768}
SIDE_OF = {'supported': 'vulnerable', 'scoped_negative': 'fixed', 'insufficient_context': 'withheld'}
# Training recipe 2 (security trial v5): every view is drawn before any repeats, the hard tasks get more of each batch of eight, and the
# learning rate warms up for three updates then decays. A plan without this block keeps the v4 behaviour (constant rate, equal task cycle, 32 updates).
RECIPE_2 = {'schema': 'security-training-recipe/2',
            'task_counts_per_batch': {'regression_test_design': 3, 'repair': 2, 'discovery': 2, 'insufficient_context': 1},
            'scheduler': 'cosine', 'schedule': 'warmup_cosine_positive_first_step_v1', 'warmup_steps': 3,
            'peak_learning_rate': 8e-5, 'max_steps_limit': 64}


# Training recipe 3 (security trial v6): exactly recipe 2 (scheduler, warmup, peak rate, update cap and batch mix) run on training supplement 1, so the
# training data is the only thing that differs from the v5 run. What differs in the reading of the result: the supplement's prelude-plus-call shapes are
# structurally close to development form 3, so development views no longer test transfer to an unseen shape. The coverage gate asks that every repair and
# regression-test view is drawn: with 360 training views and about 44 updates, drawing every view is no longer the aim, and those two tasks are the ones
# whose output failed execution.
RECIPE_3 = {**copy.deepcopy(RECIPE_2), 'schema': 'security-training-recipe/3',
            'coverage_tasks': ['regression_test_design', 'repair'], 'dataset_tag': 'security_trial_v2_s1'}
RECIPES = (RECIPE_2, RECIPE_3)
RECIPES_BY_NUMBER = {2: RECIPE_2, 3: RECIPE_3}


def check_recipe_dataset(recipe, dataset_tag):
    """A recipe must run on the dataset it declares; one that declares none (recipe 2) runs on the plain v2 corpus only."""
    wanted = recipe.get('dataset_tag', 'security_trial_v2')
    if dataset_tag != wanted:
        raise ValueError(f'The training recipe requires the dataset tagged {wanted}, not {dataset_tag}')


def coverage_gate(recipe, coverage, steps_done):
    """The software coverage gate of a run: every view drawn (recipe 2), or every view of the recipe's target tasks (recipe 3)."""
    tasks = recipe.get('coverage_tasks')
    if not tasks:
        return {'met': coverage['views_drawn'] == coverage['total_views'], 'views_drawn': coverage['views_drawn'], 'total_views': coverage['total_views'],
                'updates_for_full_coverage': coverage['updates_for_full_coverage'], 'steps_done': steps_done}
    counts, selected = recipe['task_counts_per_batch'], sorted(tasks)
    drawn = sum(coverage['views_drawn_by_task'][task] for task in selected)
    total = sum(coverage['views_by_task'][task] for task in selected)
    need = max(-(-coverage['views_by_task'][task] // counts[task]) for task in selected)
    return {'met': drawn == total, 'views_drawn': drawn, 'total_views': total, 'updates_for_full_coverage': need, 'steps_done': steps_done, 'tasks': selected}


def recipe_sampling_policy(recipe):
    counts = dict(sorted(recipe['task_counts_per_batch'].items()))
    policy = {'policy': 'weighted_task_epoch_v1', 'task_counts_per_batch': counts, 'examples_per_optimizer_batch': sum(counts.values())}
    if recipe.get('pairing'):
        policy['pairing'] = recipe['pairing']                     # recipe 10: a finding view and its fixed twin in one optimizer batch (sampler.paired_epoch_v1)
    if recipe.get('allocation'):
        policy['allocation'] = recipe['allocation']               # recipe 11: the discovery epoch is a water-filled plan (training.finetune.allocation, sampler planned_epoch_v1)
    if recipe.get('twin_margin'):
        policy['twin_margin'] = recipe['twin_margin']             # recipe 11: the twin margin on the status logit gap is added to the language-model loss
    return policy


def check_recipe_against_config(recipe, *, training, task_shares):
    """Refuse a resolved config whose training settings differ from the recipe the plan declares (a silent mismatch would train another recipe)."""
    counts = recipe['task_counts_per_batch']
    batch = sum(counts.values())
    if (training.scheduler != recipe['scheduler'] or training.warmup_steps != recipe['warmup_steps'] or training.warmup_ratio != 0
            or abs(training.learning_rate - recipe['peak_learning_rate']) > 1e-12 or training.effective_sequence_batch_size != batch):
        raise ValueError('Resolved training settings differ from the declared training recipe (scheduler, warmup, peak rate or batch size)')
    if any(abs(task_shares.get(task, 0) - n / batch) > 1e-9 for task, n in counts.items()):
        raise ValueError('Resolved task shares differ from the declared training recipe task counts')


def plan_recipe(plan):
    """The frozen training recipe a plan declares, ``None`` for a plan without one. Any other recipe is refused."""
    block = plan.get('training_recipe')
    if block is None:
        return None
    frozen = next((recipe for recipe in RECIPES if block == recipe), None)
    if plan.get('schema') != SCHEMA_PLAN_2 or frozen is None:
        raise ValueError('Unsupported or altered training recipe; only a frozen security-training-recipe (2 or 3) in a plan/2 is accepted')
    return copy.deepcopy(frozen)


def _family_views(views):
    families = defaultdict(dict)
    for view in views:
        families[view['family_id']][(view['task'], view['pool'])] = view
    return families


def select_cohorts(development_views, legacy_views):
    """Deterministic cohorts; independent of the order of the inputs."""
    development = sorted(development_views, key=lambda v: v['view_id'])
    legacy = sorted(legacy_views, key=lambda v: v['view_id'])
    combos = defaultdict(list)
    for view in development:
        combos[(view['task'], view['language'], view['mechanism'])].append(view)
    loss, ordinal = [], Counter()
    for key in sorted(combos):
        candidates = combos[key]
        loss.append(candidates[ordinal[key[0]] % len(candidates)]['view_id'])      # rotate through candidates so both pools of a task appear
        ordinal[key[0]] += 1
    families = _family_views(development)
    ordered = sorted(families, key=lambda f: (next(iter(families[f].values()))['mechanism'], next(iter(families[f].values()))['language'], f))
    if len(ordered) != 12:
        raise ValueError('The development partition must hold twelve families to build the generation cohorts')

    def pick(family, task, pools):
        return [families[ordered[family]][(task, pool)]['view_id'] for pool in pools]
    contrasts = ('supported', 'scoped_negative')
    mandatory = (pick(0, 'discovery', contrasts) + pick(6, 'regression_test_design', contrasts) + pick(9, 'repair', ('supported',))
                 + pick(2, 'insufficient_context', ('insufficient_context',)))
    conditional = (pick(4, 'discovery', contrasts) + pick(10, 'regression_test_design', contrasts) + pick(3, 'repair', ('supported',))
                   + pick(7, 'insufficient_context', ('insufficient_context',)))
    legacy_ids = [v['view_id'] for v in legacy]
    return {'development_loss': loss, 'generation_mandatory': sorted(mandatory), 'generation_conditional': sorted(conditional),
            'legacy_regression_loss': legacy_ids, 'evaluation_views': sorted(set(loss) | set(mandatory) | set(conditional) | set(legacy_ids))}


def decide_extra_generations(*, remaining_seconds, evaluation_seconds, per_generation_seconds, extra_cases, first_update_seconds,
                             safety_multiplier, export_seconds):
    """Add the conditional cases only if time remains for them, one update, the adapted evaluation (with its multiplier) and the export."""
    if not per_generation_seconds:
        return {'add': False, 'reason': 'generation_cost_unmeasured', 'required_seconds': None, 'extra_generation_seconds': None}
    extra = max(per_generation_seconds) * extra_cases           # the slowest measured generation, so the estimate is not optimistic
    required = extra + first_update_seconds + safety_multiplier * (evaluation_seconds + extra) + export_seconds
    return {'add': remaining_seconds >= required, 'reason': 'time_available' if remaining_seconds >= required else 'insufficient_time',
            'remaining_seconds': remaining_seconds, 'required_seconds': required, 'extra_generation_seconds': extra,
            'evaluation_seconds': evaluation_seconds, 'first_update_seconds': first_update_seconds,
            'safety_multiplier': safety_multiplier, 'export_seconds': export_seconds}


def build_plan_v2(selection, *, total_budget_minutes, max_steps, tiny, recipe=None):
    plan = _build_plan_v2(selection, total_budget_minutes=total_budget_minutes, max_steps=max_steps, tiny=tiny)
    if recipe is not None:
        plan['training_recipe'] = copy.deepcopy(recipe)
        plan['sampling_policy'] = recipe_sampling_policy(recipe)
        plan['trial_overrides'].update(scheduler=recipe['scheduler'], warmup_steps=recipe['warmup_steps'],
                                       learning_rate=recipe['peak_learning_rate'])
    return plan


def _build_plan_v2(selection, *, total_budget_minutes, max_steps, tiny):
    return {'schema': SCHEMA_PLAN_2, 'frozen': True, 'total_budget_minutes': total_budget_minutes,
            'sampling_policy': {'policy': 'equal_task_cycle_v1', 'task_cycle': list(TASKS), 'examples_per_task_per_optimizer_batch': 2},
            'max_steps': max_steps, 'max_new_tokens': dict(MAX_NEW_TOKENS),
            'evaluation_reserve_multiplier': 1.5, 'export_reserve_seconds': 120,
            'training_loss': 'present_field_normalized_ce', 'field_weights': dict(FIELD_WEIGHTS),
            'fresh_adapter': True, 'software_test': tiny, 'evaluation_claim': 'synthetic development and legacy regression only',
            'cohorts': {'development_loss': {'split': 'val', 'view_ids': list(selection['development_loss'])},
                        'legacy_regression_loss': {'split': 'legacy', 'view_ids': list(selection['legacy_regression_loss'])},
                        'generation_mandatory': list(selection['generation_mandatory']),
                        'generation_conditional': list(selection['generation_conditional']),
                        'evaluation_views': list(selection['evaluation_views'])},
            'generation_policy': {'mandatory': len(selection['generation_mandatory']), 'conditional': len(selection['generation_conditional']),
                                  'conditional_rule': {'first_update_reserve_seconds': 120, 'safety_multiplier': 1.5, 'export_reserve_seconds': 120,
                                                       'cost_estimate': 'slowest measured baseline generation'},
                                  'frozen_before_training': True, 'both_arms_use_identical_cases': True},
            'verification_policy': {'sidecar_schema': 'security-behavior-verification/1', 'performed_in_notebook': False,
                                    'notebook_checks': ['json_syntax', 'schema', 'evidence_grounding', 'patch_applicability', 'test_static_checks'],
                                    'notebook_rule': 'The notebook never executes generated code.',
                                    'verifier': 'separate hardened container entry point (tools.verify_security_outputs)',
                                    'case_timeout_seconds': 10, 'batch_seconds': 300,
                                    'time_accounting': 'verification time is reported separately and is not part of the 30-minute notebook budget'},
            'blind_partition_in_bundle': False, 'blind_generator_in_bundle': True,
            'trial_overrides': {'effective_sequence_batch_size': 8, 'initial_microbatch': 1, 'initial_accumulation': 8, 'micro_batch_tuning': 'auto',
                                'scheduler': 'constant', 'warmup_steps': 0, 'training_loss': 'present_field_normalized_ce', 'train_decision_head': False}}


def validate_plan_v2(plan, views):
    """Raise ValueError when a plan/2 is not a frozen, bounded, blind-free plan over views that exist."""
    if plan.get('schema') != SCHEMA_PLAN_2 or plan.get('frozen') is not True or plan.get('blind_partition_in_bundle') is not False:
        raise ValueError('Not a frozen security-training-plan/2')
    recipe = plan_recipe(plan)
    limit = recipe['max_steps_limit'] if recipe else 32
    if type(plan.get('max_steps')) is not int or not 1 <= plan['max_steps'] <= limit:
        raise ValueError(f'max_steps must be 1 to {limit}')
    if plan.get('evaluation_reserve_multiplier') != 1.5 or plan.get('export_reserve_seconds') != 120:
        raise ValueError('Evaluation and export reservations differ from the declared policy')
    if (set(plan.get('max_new_tokens', {})) != set(TASKS) or any(type(n) is not int or not 1 <= n <= 1536 for n in plan['max_new_tokens'].values())
            or set(plan.get('field_weights', {})) != set(FIELD_WEIGHTS) or any(not (isinstance(w, (int, float)) and w > 0) for w in plan['field_weights'].values())):
        raise ValueError('Invalid bounded decoding or field weights')
    expected = build_plan_v2(
        {k: [] for k in ('development_loss', 'legacy_regression_loss', 'generation_mandatory', 'generation_conditional', 'evaluation_views')},
        total_budget_minutes=0, max_steps=1, tiny=False, recipe=recipe)
    overrides = plan.get('trial_overrides', {})
    if (plan.get('training_loss') != 'present_field_normalized_ce' or plan.get('sampling_policy') != expected['sampling_policy']
            or overrides != expected['trial_overrides']):
        raise ValueError('Invalid training loss, sampling policy or trial overrides for the declared recipe')
    policy = plan.get('verification_policy', {})
    if (policy.get('performed_in_notebook') is not False or policy.get('case_timeout_seconds') != 10 or policy.get('batch_seconds') != 300
            or policy.get('sidecar_schema') != 'security-behavior-verification/1'):
        raise ValueError('Invalid verification policy')
    cohort = plan.get('cohorts', {})
    by_id = {v['view_id']: v for v in views}
    groups = {'development_loss': cohort.get('development_loss', {}).get('view_ids', []),
              'legacy_regression_loss': cohort.get('legacy_regression_loss', {}).get('view_ids', []),
              'generation_mandatory': cohort.get('generation_mandatory', []), 'generation_conditional': cohort.get('generation_conditional', []),
              'evaluation_views': cohort.get('evaluation_views', [])}
    for name, ids in groups.items():
        if len(set(ids)) != len(ids):
            raise ValueError(f'{name} repeats a view')
        for view_id in ids:
            view = by_id.get(view_id)
            if view is None:
                raise ValueError(f'{name} names an unknown view: {view_id}')
            if view['partition_role'] == 'blind_test':
                raise ValueError('The blind partition must not appear in the plan')
            if name == 'development_loss' and view['partition_role'] != 'development':
                raise ValueError('Development loss views must be development views')
    if (len(groups['development_loss']) != 24 or len(groups['generation_mandatory']) != 6 or len(groups['generation_conditional']) != 6
            or set(groups['generation_mandatory']) & set(groups['generation_conditional'])):
        raise ValueError('Cohort sizes or overlap differ from the declared policy')
    scored = set(groups['evaluation_views'])
    if not (set(groups['development_loss']) | set(groups['generation_mandatory']) | set(groups['generation_conditional'])
            | set(groups['legacy_regression_loss'])) <= scored:
        raise ValueError('Every cohort view must be in the evaluation set')


def read_plan(plan):
    """Normalize a plan: ``/1`` is validated as before, ``/2`` structurally."""
    schema = plan.get('schema')
    if schema == 'security-training-plan/1':
        required = ('training_loss', 'max_new_tokens', 'field_weights', 'sampling_policy')
        if any(key not in plan for key in required):
            raise ValueError('Invalid security-training-plan/1')
        return {**plan, 'generation_cases': plan.get('generation_cases', 6)}
    if schema == SCHEMA_PLAN_2:
        if 'cohorts' not in plan:
            raise ValueError('security-training-plan/2 needs explicit cohorts')
        return plan
    raise ValueError('Unsupported training plan schema: ' + str(schema))


# -- comparison/2 --------------------------------------------------------------------------------------------------------------------------
def _same_cohort(base, tuned):
    """Same views, tasks, generation ids and decoding; prompts and targets must also agree wherever both arms scored a view."""
    if [(r['view_id'], r['task']) for r in base['rows']] != [(r['view_id'], r['task']) for r in tuned['rows']]:
        return False
    return all((l.get('prompt_sha256'), l.get('target_sha256'), l.get('supervised_tokens')) == (r.get('prompt_sha256'), r.get('target_sha256'), r.get('supervised_tokens'))
               for l, r in zip(base['rows'], tuned['rows']) if l['status'] == r['status'] == 'scored')


def _loss(rows):
    from training.finetune.security_trial_evaluation import summarize
    summary = summarize(rows)
    by_task = {}
    for row in rows:
        if row.get('status') == 'scored' and row.get('supervised_tokens'):
            total = by_task.setdefault(row['task'], {'scored': 0, 'supervised_tokens': 0, 'nll_sum': 0.0})
            total['scored'] += 1
            total['supervised_tokens'] += row['supervised_tokens']
            total['nll_sum'] += row['nll_sum']
    by_task = {task: {'scored': t['scored'], 'supervised_tokens': t['supervised_tokens'], 'token_mean_loss': t['nll_sum'] / t['supervised_tokens']}
               for task, t in sorted(by_task.items())}
    return {'planned': len(rows), 'scored': summary['scored_views'], 'supervised_tokens': summary['supervised_tokens'],
            'token_mean_loss': summary['token_mean_loss'], 'fields': summary['fields'], 'status_counts': summary['status_counts'],
            'by_task': by_task}


def _generation(rows, ids, diagnostics, views):
    from training.finetune import response_diagnostics as rd
    from training.finetune.security_serialization import parse_response
    by_id = {r['view_id']: r for r in rows}
    selected = {i: diagnostics.get(i) for i in ids}
    metrics = rd.summarize(selected)
    counts = Counter()
    for view_id in ids:
        row, view = by_id.get(view_id), views[view_id]
        generation = (row or {}).get('generation') or {}
        pool = view['pool']
        counts[f'planned_{pool}'] += 1
        counts['hit_limit'] += bool(generation.get('hit_limit'))
        counts['deadline_excluded'] += generation.get('state') in ('deadline_excluded', 'not_attempted')
        if generation.get('text') is None:
            continue
        answer, _ = parse_response(view['task'], generation['text'])
        if answer is None:
            continue
        expected = generation['expected_response']['status'] if generation.get('expected_response') else None
        counts['status_matches'] += answer['status'] == expected
        counts['scoped_false_positives'] += pool == 'scoped_negative' and answer['status'] == 'finding'
        counts['scoped_negative_correct'] += pool == 'scoped_negative' and answer['status'] == 'no_finding'     # malformed answers are never correct
        counts['supported_correct'] += pool == 'supported' and answer['status'] == 'finding'
        counts['missed_findings'] += pool == 'supported' and answer['status'] != 'finding'
        counts['insufficient_context_correct'] += pool == 'insufficient_context' and answer['status'] == 'insufficient_context'
    metrics.update(status_matches=counts['status_matches'], scoped_false_positives=counts['scoped_false_positives'],
                   scoped_negative_planned=counts['planned_scoped_negative'], scoped_negative_correct=counts['scoped_negative_correct'],
                   supported_correct=counts['supported_correct'], missed_findings=counts['missed_findings'],
                   supported_planned=counts['planned_supported'], insufficient_context_correct=counts['insufficient_context_correct'],
                   insufficient_context_planned=counts['planned_insufficient_context'], token_limit_hits=counts['hit_limit'],
                   deadline_excluded=counts['deadline_excluded'],
                   repair_planned=sum(views[i]['task'] == 'repair' for i in ids))
    return metrics


def compare_v2(base, tuned, *, plan, diagnostics, views):
    """Paired measurements of the pinned original and the fresh adapter on identical frozen cohorts."""
    result = {'schema': SCHEMA_COMPARISON_2, 'state': 'partial', 'synthetic': True, 'independent_security_accuracy': None,
              'independent_benchmark': {'state': 'not_measured', 'detail': 'No independently reviewed benchmark result exists for this trial; synthetic measurements are reported separately.'},
              'counts_as_evaluation': False, 'counts_as_full_training': False, 'cohorts': None, 'generation': None, 'gates': None,
              'verification': {'state': 'not_run_in_notebook', 'sidecar_schema': 'security-behavior-verification/1',
                               'detail': 'Generated tests and repairs are executed only by the separate hardened verifier; its time is not part of the 30-minute budget.'},
              'interpretation': 'Synthetic development and legacy regression measurements. No independent security accuracy, functional repair or production-readiness claim.'}
    if base is None or tuned is None:
        result['missing_phases'] = [name for name, value in (('baseline_evaluation', base), ('adapted_evaluation', tuned)) if value is None]
        return result
    if (not base['rows'] or not _same_cohort(base, tuned) or base['generation_ids'] != tuned['generation_ids']
            or base['max_new_tokens'] != tuned['max_new_tokens'] or base['serialization'] != tuned['serialization']):
        raise ValueError('Paired comparison requires the same prompt/target/token/decoding cohort')
    cohort = plan['cohorts']
    ids = list(base['generation_ids'])
    complete = base['state'] == tuned['state'] == 'finished'
    cohorts_out = {}
    for name in ('development_loss', 'legacy_regression_loss'):
        wanted = set(cohort[name]['view_ids'])
        b, t = _loss([r for r in base['rows'] if r['view_id'] in wanted]), _loss([r for r in tuned['rows'] if r['view_id'] in wanted])
        delta = (t['token_mean_loss'] - b['token_mean_loss']) if complete and b['token_mean_loss'] is not None and t['token_mean_loss'] is not None else None
        cohorts_out[name] = {'planned': len(wanted), 'base': b, 'tuned': t, 'loss_delta_tuned_minus_base': delta}
    generation_ids = set(ids)
    gb, gt = _loss([r for r in base['rows'] if r['view_id'] in generation_ids]), _loss([r for r in tuned['rows'] if r['view_id'] in generation_ids])
    cohorts_out['generation_views_loss'] = {'planned': len(generation_ids), 'base': gb, 'tuned': gt}
    b_metrics = _generation(base['rows'], ids, diagnostics.get('base', {}), views)
    t_metrics = _generation(tuned['rows'], ids, diagnostics.get('adapter', {}), views)
    mandatory, conditional = set(cohort['generation_mandatory']), set(cohort['generation_conditional'])
    decision = base.get('generation_decision')
    result['generation'] = {'planned': len(ids), 'frozen_ids': sorted(ids), 'mandatory': len(mandatory & generation_ids),
                            'conditional': {'planned': len(conditional), 'added': len(conditional & generation_ids), 'decision': decision},
                            'base': b_metrics, 'tuned': t_metrics,
                            'paired': [{'view_id': i, 'task': views[i]['task'], 'pool': views[i]['pool'],
                                        'base': (diagnostics.get('base', {}).get(i) or {}).get('primary'),
                                        'adapter': (diagnostics.get('adapter', {}).get(i) or {}).get('primary')} for i in sorted(ids)]}
    delta = cohorts_out['development_loss']['loss_delta_tuned_minus_base']
    full = b_metrics['not_generated'] == 0 and t_metrics['not_generated'] == 0

    def gate(met, detail):
        return {'met': met, 'detail': detail}
    result['gates'] = {
        'all_responses_strict_json_and_schema': gate(t_metrics['json_valid'] == t_metrics['schema_valid'] == t_metrics['planned'],
                                                     f"{t_metrics['schema_valid']} of {t_metrics['planned']} planned cases are strict-JSON and schema valid"),
        'every_generated_repair_applies_exactly': gate(t_metrics['patch_applies_exactly'] == t_metrics['repair_planned'],
                                                       f"{t_metrics['patch_applies_exactly']} of {t_metrics['repair_planned']} repairs apply exactly"),
        'completion_loss_improves': gate(None if delta is None else delta < 0, 'development loss delta (tuned minus base): ' + str(delta)),
        'scoped_negative_not_regressed': gate(None if not full else t_metrics['scoped_negative_correct'] >= b_metrics['scoped_negative_correct'],
                                              f"scoped negatives answered no_finding {b_metrics['scoped_negative_correct']} -> {t_metrics['scoped_negative_correct']} "
                                              f"of {t_metrics['scoped_negative_planned']} (false positives {b_metrics['scoped_false_positives']} -> {t_metrics['scoped_false_positives']})"),
        'insufficient_context_not_regressed': gate(None if not full else t_metrics['insufficient_context_correct'] >= b_metrics['insufficient_context_correct'],
                                                   f"correct {b_metrics['insufficient_context_correct']} -> {t_metrics['insufficient_context_correct']}"),
        'generated_tests_discriminate_under_trusted_verification': gate(None, 'requires the container verifier sidecar'),
        'repairs_functionally_and_securely_correct': gate(None, 'requires the container verifier sidecar')}
    result.update(state='finished' if complete else 'partial', cohorts=cohorts_out, missing_phases=[],
                  loss_delta_tuned_minus_base=delta)
    return result


def diagnose_report(report, views, data_dir):
    """Response diagnostics for every generation of a report, from the authenticated sources in the bundle's data folder."""
    from training.finetune import response_diagnostics as rd
    root, out = Path(data_dir), {}
    for row in report['rows']:
        generation = row.get('generation')
        if generation is None:
            continue
        if generation.get('text') is None:
            out[row['view_id']] = None
            continue
        view = views[row['view_id']]
        family, side = view['family_id'], view['view_id'].rsplit(':', 1)[1]
        source = (root / 'source' / family / f'{side}.txt').read_text(encoding='utf-8')
        fixed = (root / 'source' / family / 'fixed.txt').read_text(encoding='utf-8')
        out[row['view_id']] = rd.diagnose_response(view['task'], generation['text'], sources={view['allowed_patch_paths'][0]: source},
                                                   allowed_paths=list(view['allowed_patch_paths']), reference_fixed=fixed)
    return out
