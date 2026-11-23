"""Cohorts, plan, recipe and diagnostics of the grounded dataset kind (security-trial-dataset/3, tag grounded_v31).

The 30-minute trial machinery (sampler, trainer, deadline, comparison/2) is shared with the authored trial. What differs here: three tasks
(there is no insufficient_context pool), cohorts chosen from package-disjoint development views plus a transfer slice of weakness classes
that were never trained on, and the shown source for evidence grounding and patch application is read from the prompt itself.
"""
from __future__ import annotations

import copy
import json
import re
import hashlib
from collections import defaultdict

from training.finetune import security_trial_cohorts as base

TASKS = ('discovery', 'regression_test_design', 'repair')
SCHEMA_PLAN_3 = 'security-training-plan/3'
DATASET_SCHEMA = 'security-trial-dataset/3'
MAX_NEW_TOKENS = {'discovery': 512, 'regression_test_design': 896, 'repair': 896}
# Recipe 4: the recipe-2 schedule (warmup, cosine, peak rate, update cap) on the grounded corpus. Discovery carries the positives and every negative, so it
# gets three of eight draws; repair and tests are the tasks whose output failed execution in the authored trials.
RECIPE_4 = {'schema': 'security-training-recipe/4',
            'task_counts_per_batch': {'discovery': 3, 'repair': 3, 'regression_test_design': 2},
            'scheduler': 'cosine', 'schedule': 'warmup_cosine_positive_first_step_v1', 'warmup_steps': 3,
            'peak_learning_rate': 8e-5, 'max_steps_limit': 64, 'coverage_tasks': ['regression_test_design', 'repair'],
            'dataset_tag': 'grounded_v31'}
# Recipe 5: the same schedule, discovery and repair only. Upstream tests relied on fixtures and helpers the model cannot see (two of three bad rows in the last
# review), so the grounded corpus no longer carries regression-test views and the sampler never draws that task.
RECIPE_5 = {'schema': 'security-training-recipe/5',
            'task_counts_per_batch': {'discovery': 7, 'repair': 1},
            'scheduler': 'cosine', 'schedule': 'warmup_cosine_positive_first_step_v1', 'warmup_steps': 3,
            'peak_learning_rate': 8e-5, 'max_steps_limit': 64, 'coverage_tasks': ['repair'],
            'dataset_tag': 'grounded_v33'}
# Recipe 6: recipe 5's schedule on grounded_v34. Repair is drawn two of eight (v34 has about twice the repair views of v33, and the v33 run showed that two
# passes over 16 repair views did not teach the one-hunk format); discovery carries the positives and the class-confusion negatives.
RECIPE_6 = {'schema': 'security-training-recipe/6',
            'task_counts_per_batch': {'discovery': 6, 'repair': 2},
            'scheduler': 'cosine', 'schedule': 'warmup_cosine_positive_first_step_v1', 'warmup_steps': 3,
            'peak_learning_rate': 8e-5, 'max_steps_limit': 64, 'coverage_tasks': ['repair'],
            'dataset_tag': 'grounded_v34'}
# Recipe 7: grounded_v35 on security_chat_v3, for a long single-L4 run (several passes over a larger training mix). The draw ratio is recipe 6's; what changes is the
# ceiling on optimizer updates (a 2 to 3 hour run is about 400 to 500 updates at the measured 21 to 27 seconds each) and the dataset it belongs to. The deadline, not
# the ceiling, ends the run, and the update count is re-planned from the measured update time as before.
RECIPE_7 = {'schema': 'security-training-recipe/7',
            'task_counts_per_batch': {'discovery': 6, 'repair': 2},
            'scheduler': 'cosine', 'schedule': 'warmup_cosine_positive_first_step_v1', 'warmup_steps': 8,
            'peak_learning_rate': 6e-5, 'max_steps_limit': 640, 'coverage_tasks': ['repair'],
            'dataset_tag': 'grounded_v35'}
# Recipe 8: grounded_v36 (mined from the fresh npm and PyPI store) on security_chat_v3. Same schedule and ceiling as recipe 7. The draw ratio moves from 6/2 to 7/1: v36 has about
# 150 training repair views, so 2 of 8 per batch over about 500 updates would pass over them about 7 times (the memorisation risk the v35 note named); 1 of 8 gives about 3,
# as recipe 5 did.
RECIPE_8 = {'schema': 'security-training-recipe/8',
            'task_counts_per_batch': {'discovery': 7, 'repair': 1},
            'scheduler': 'cosine', 'schedule': 'warmup_cosine_positive_first_step_v1', 'warmup_steps': 8,
            'peak_learning_rate': 6e-5, 'max_steps_limit': 640, 'coverage_tasks': ['repair'],
            'dataset_tag': 'grounded_v36'}
# Recipe 9: grounded_v37 (the recovery build: evidence tiers, unknown licences admitted, the security_chat_v4 catalogue). The schedule, ceiling and draw ratio are recipe 8's;
# recipes are frozen to the dataset they were designed for, so a new dataset tag needs a new recipe. The repair share is re-checked on the built mix (Task 8 notes).
RECIPE_9 = {'schema': 'security-training-recipe/9',
            'task_counts_per_batch': {'discovery': 7, 'repair': 1},
            'scheduler': 'cosine', 'schedule': 'warmup_cosine_positive_first_step_v1', 'warmup_steps': 8,
            'peak_learning_rate': 6e-5, 'max_steps_limit': 640, 'coverage_tasks': ['repair'],
            'dataset_tag': 'grounded_v37'}
# Recipe 10: grounded_v38 (v37's evidence tiers with the answers made to describe only the code the model is shown, see grounded_claim_gate). Two changes against recipe 9:
# (1) paired batches: a vulnerable function and its fixed twin are drawn in the same optimizer batch (the contrast that fine-tuned models lack, arXiv 2601.22655), which needs an even
# discovery count per batch, so the draw ratio is 6 discovery : 2 repair (recipe 6 and 7 used it; v38 has about 400 repair views, so a 3,200 draw run passes over them under twice);
# (2) the schedule, ceiling and peak rate are recipe 9's.
RECIPE_10 = {'schema': 'security-training-recipe/10',
             'task_counts_per_batch': {'discovery': 6, 'repair': 2},
             'scheduler': 'cosine', 'schedule': 'warmup_cosine_positive_first_step_v1', 'warmup_steps': 8,
             'peak_learning_rate': 6e-5, 'max_steps_limit': 640, 'coverage_tasks': ['repair'],
             'dataset_tag': 'grounded_v38', 'pairing': 'finding_fixed_twin_v1'}
# Recipe 11: grounded_v39, the twin-margin recipe. Against recipe 10: (1) the discovery epoch is a PLAN (training.finetune.allocation): draws are water-filled over the units by how
# provable their label is (analyser-proved units about twice the draws of LLM-attributed ones; v38's review weights were inert), and every finding view is drawn together with its fixed
# twin in the same prompt form, so both are in one optimizer batch; (2) the language-model loss gets the twin margin on the status logit gap
# (losses.twin_margin_loss): mean softplus(-(s_V - s_F - margin)) over the pairs of a batch, weight 0.5, margin 1.7 (the median teacher-forced twin gap of v38 on analyser-proved pairs, 0.85, plus
# one standard deviation of the per-function offset, 0.86); (3) 600 updates: 3,600 discovery draws, about 8 per proved function against v38's 4 (the measured fixed-code clear rate follows
# 1 - 0.835 exp(-d/4) in draws per function d, which gives 0.65 at 4 and 0.88 at 10); (4) draw ratio 6 discovery : 2 repair (the batch size of eight is fixed in the supervisor, and pairs need an even discovery count); the repair set is 217 views plus 136 rename variants
# (the LLM-attributed units left with their repair views), so 1,200 repair draws pass over each about 3.4 times. Schedule and peak rate are recipe 10's.
RECIPE_11 = {'schema': 'security-training-recipe/11',
             'task_counts_per_batch': {'discovery': 6, 'repair': 2},
             'scheduler': 'cosine', 'schedule': 'warmup_cosine_positive_first_step_v1', 'warmup_steps': 8,
             'peak_learning_rate': 6e-5, 'max_steps_limit': 600, 'coverage_tasks': ['repair'],
             'dataset_tag': 'grounded_v39', 'pairing': 'finding_fixed_twin_v2',
             'allocation': {'policy': 'water_fill_pairs_v1', 'tau': 4.0, 'd_max': 12.0, 'lone_share': 0.2, 'total_discovery_draws': 3600},
             'twin_margin': {'weight': 0.5, 'margin': 1.7}}
# Recipe 12: grounded_v40 (plan v6: the v39 rows plus units of seven more ecosystems, security_chat_v5). Recipe 11 unchanged except the dataset tag and the size of the plan: the training set
# has about 2.2 times the units, and water-filling the old 3,600 draws over them would leave the analyser-proved units 5.8 draws each (v39: about 8; the measured fixed-code clear rate
# follows 1 - 0.835 exp(-d/4)) and the strict-attribution units none. 7,800 discovery draws (1,300 updates at 6 discovery : 2 repair; sized on the compute balance of 56.5 units: about 9 hours of L4, 17 units) give the real upstream fixes 9.5 draws per unit,
# diff_guard 4.5, diff_sink 4.0 and strict_attribution 2.0 (training.finetune.allocation, worth by proof strength). Everything else is recipe 11's.
RECIPE_12 = {**copy.deepcopy(RECIPE_11), 'schema': 'security-training-recipe/12', 'dataset_tag': 'grounded_v40', 'max_steps_limit': 1300,
             'allocation': {'policy': 'water_fill_pairs_v1', 'tau': 4.0, 'd_max': 12.0, 'lone_share': 0.2, 'total_discovery_draws': 7800,
                            # worth of a draw by origin, overriding allocation.ETA: the old flow-proved origins are raised so that they keep at least the exposure v39 gave them (about 2,900
                            # draws; simulated on the final dataset: 3,274 draws with these values, 2,548 without), measured with tools.simulate_allocation
                            'eta': {'upstream_fix': 1.6, 'sanitiser_removal': 1.6, 'unsafe_substitution': 1.6, 'recovered_D': 1.1, 'recovered_L': 0.5}}}
# Recipe 13: stage B of the staged v40 run. The same dataset and the same draw plan as recipe 12 (7,800 discovery draws), a different seed (a different draw order and a different cycle of
# prompt forms and variants over every unit), a lower peak rate (3e-5: a warm restart on a trained adapter) and the adapter of stage A as the starting point (plan `init_adapter`, required).
# Stage A and stage B together give every drawn unit about twice the draws of one stage (v39: about 9 draws per proved function, the exposure the measured clear rate follows).
RECIPE_13 = {**copy.deepcopy(RECIPE_12), 'schema': 'security-training-recipe/13', 'peak_learning_rate': 3e-5, 'pilot_seed': 29, 'init_adapter': True}
RECIPES = (RECIPE_4, RECIPE_5, RECIPE_6, RECIPE_7, RECIPE_8, RECIPE_9, RECIPE_10, RECIPE_11, RECIPE_12, RECIPE_13)
# The fixed twin of a finding view, by the kind of the finding view (the scope form decides which negative kind shows the same function in the same prompt form).
PAIR_KINDS = {'discovery_open': 'negative_fixed_open', 'discovery_scoped': 'negative_fixed', 'discovery_set': 'negative_fixed_set'}
LOSS_PER_TASK = 8


def twin_pairs(views):
    """[(finding view id, fixed twin view id)] among `views` (training views with task, kind, record_id and view_id): the same function, the same prompt form, once vulnerable and once
    fixed. A function whose twin was thinned away by the balancer simply has no pair. Sorted, so the sampler fingerprint is stable."""
    by_key = {}
    for view in sorted(views, key=lambda v: v['view_id']):
        if view['task'] == 'discovery':
            by_key.setdefault((view['record_id'], view['kind']), view['view_id'])
    pairs = []
    for (record, kind), finding in sorted(by_key.items()):
        twin = by_key.get((record, PAIR_KINDS.get(kind)))
        if twin is not None:
            pairs.append((finding, twin))
    return pairs


def active_tasks(recipe):
    """The tasks a recipe draws, sorted."""
    return tuple(sorted(recipe['task_counts_per_batch']))


def check_recipe_dataset(recipe, dataset_tag):
    wanted = recipe.get('dataset_tag')
    if dataset_tag != wanted:
        raise ValueError(f'The training recipe requires the dataset tagged {wanted}, not {dataset_tag}')


def plan_recipe(plan):
    block = plan.get('training_recipe')
    match = next((recipe for recipe in RECIPES if block == recipe), None)
    if match is None or plan.get('schema') != SCHEMA_PLAN_3:
        raise ValueError('Unsupported or altered training recipe; only the frozen security-training-recipe/4 to /13 in a plan/3 is accepted')
    return copy.deepcopy(match)


def _order(views):
    return sorted(views, key=lambda v: (hashlib.sha256(v['view_id'].encode()).hexdigest(), v['view_id']))


def _balanced(views, per_task):
    """per_task views per task, interleaving the (pool, kind) buckets so positives and every negative kind are present."""
    chosen = []
    for task in sorted({v['task'] for v in views}):
        buckets = defaultdict(list)
        for view in _order([v for v in views if v['task'] == task]):
            buckets[(view['pool'], view['kind'])].append(view)
        keys, taken, i = sorted(buckets), [], 0
        while len(taken) < per_task and any(buckets.values()):
            bucket = buckets[keys[i % len(keys)]]
            if bucket:
                taken.append(bucket.pop(0)['view_id'])
            i += 1
        chosen.extend(taken)
    return chosen


def _take(views, kind, used, role):
    for view in _order([v for v in views if v['kind'] == kind and v['partition_role'] == role]):
        if view['view_id'] not in used:
            used.add(view['view_id'])
            return view['view_id']
    return None


# Generation cases in order of preference; a kind the dataset does not have (neighbour negatives, test views) is skipped and the next preference fills its place.
_MANDATORY = (('development', 'discovery_open'), ('development', 'negative_fixed'), ('development', 'negative_other_class'), ('development', 'repair'),
              ('transfer', 'discovery_open'), ('transfer', 'negative_other_class'), ('development', 'negative_neighbour'), ('development', 'discovery_scoped'),
              ('transfer', 'negative_fixed'), ('development', 'regression_tests'), ('development', 'repair'), ('development', 'discovery_open'),
              ('transfer', 'repair'), ('transfer', 'discovery_scoped'), ('development', 'negative_fixed'))
_CONDITIONAL = (('development', 'negative_fixed_open'), ('development', 'repair'), ('transfer', 'negative_fixed'), ('development', 'discovery_scoped'),
                ('development', 'negative_neighbour'), ('transfer', 'repair'), ('development', 'regression_tests'), ('transfer', 'discovery_open'),
                ('development', 'negative_other_class'), ('development', 'discovery_open'), ('development', 'negative_fixed'), ('transfer', 'discovery_scoped'),
                ('transfer', 'negative_other_class'), ('transfer', 'negative_fixed'), ('development', 'repair'))


def _fill(views, preferences, used, wanted=6):
    chosen = []
    for role, kind in preferences:
        if len(chosen) == wanted:
            break
        taken = _take([v for v in views if v['partition_role'] == role], kind, used, role)
        if taken is not None:
            chosen.append(taken)
    if len(chosen) != wanted:
        raise ValueError(f'Only {len(chosen)} of {wanted} generation cases can be chosen from the development and transfer views')
    return chosen


# security_chat_v3 datasets: the generation cases cover every scope form and both answers on the same kinds of code, and the held-out class is asked without a hint.
_MANDATORY_V3 = (('development', 'discovery_open'), ('development', 'discovery_scoped'), ('development', 'negative_fixed'), ('development', 'negative_other_class'),
                 ('transfer', 'discovery_open'), ('transfer', 'negative_fixed_open'),
                 ('development', 'repair'), ('development', 'discovery_set'), ('transfer', 'discovery_scoped'), ('transfer', 'negative_other_class'),
                 ('development', 'negative_other_set'), ('development', 'negative_fixed_open'), ('transfer', 'negative_fixed'), ('development', 'repair_open'))
_CONDITIONAL_V3 = (('development', 'repair'), ('transfer', 'discovery_scoped'), ('development', 'discovery_set'), ('development', 'negative_other_set'),
                   ('transfer', 'negative_other_class'), ('development', 'negative_fixed_open'),
                   ('transfer', 'discovery_set'), ('development', 'negative_fixed_set'), ('development', 'negative_neighbour_open'), ('transfer', 'negative_fixed_set'),
                   ('development', 'discovery_open'), ('transfer', 'discovery_open'), ('development', 'negative_neighbour'), ('transfer', 'repair'),
                   ('development', 'negative_other_class'), ('transfer', 'negative_fixed'), ('development', 'repair_open'))
V3_KINDS = frozenset({'discovery_set', 'negative_fixed_set', 'negative_other_set', 'negative_neighbour_open'})


def select_cohorts(views, *, loss_per_task=LOSS_PER_TASK):
    """Deterministic cohorts over development and transfer views; independent of input order."""
    dev = [v for v in views if v['partition_role'] == 'development']
    transfer = [v for v in views if v['partition_role'] == 'transfer']
    used = set()
    v3 = any(v['kind'] in V3_KINDS for v in views)
    mandatory = _fill(views, _MANDATORY_V3 if v3 else _MANDATORY, used)
    conditional = _fill(views, _CONDITIONAL_V3 if v3 else _CONDITIONAL, used)
    dev_loss = _balanced(dev, loss_per_task)
    transfer_loss = _balanced(transfer, max(2, loss_per_task // 2))
    evaluation = sorted(set(dev_loss) | set(transfer_loss) | set(mandatory) | set(conditional))
    return {'development_loss': dev_loss, 'transfer_loss': transfer_loss, 'generation_mandatory': sorted(mandatory),
            'generation_conditional': sorted(conditional), 'evaluation_views': evaluation}


def build_plan(selection, *, total_budget_minutes, max_steps, tiny, recipe=None):
    recipe = RECIPE_4 if recipe is None else recipe
    legacy_key = {**selection, 'legacy_regression_loss': selection['transfer_loss']}      # comparison/2 reads the transfer cohort under this name
    plan = base._build_plan_v2(legacy_key, total_budget_minutes=total_budget_minutes, max_steps=max_steps, tiny=tiny)
    plan['schema'] = SCHEMA_PLAN_3
    plan['max_new_tokens'] = {task: MAX_NEW_TOKENS[task] for task in active_tasks(recipe)}
    plan['training_recipe'] = copy.deepcopy(recipe)
    plan['sampling_policy'] = base.recipe_sampling_policy(recipe)
    plan['trial_overrides'].update(scheduler=recipe['scheduler'], warmup_steps=recipe['warmup_steps'], learning_rate=recipe['peak_learning_rate'])
    plan['cohorts']['legacy_regression_loss']['split'] = 'transfer'
    plan['cohorts']['legacy_regression_loss']['label'] = 'transfer: weakness classes held out of training'
    plan['evaluation_claim'] = 'development and transfer measurements on unreviewed mined data only'
    plan['cohort_policy'] = {'development_loss': len(selection['development_loss']), 'transfer_loss': len(selection['transfer_loss']),
                             'generation_mandatory': 6, 'generation_conditional': 6}
    return plan


def validate_plan(plan, views):
    if plan.get('schema') != SCHEMA_PLAN_3 or plan.get('frozen') is not True or plan.get('blind_partition_in_bundle') is not False:
        raise ValueError('Not a frozen security-training-plan/3')
    recipe = plan_recipe(plan)
    if type(plan.get('max_steps')) is not int or not 1 <= plan['max_steps'] <= recipe['max_steps_limit']:
        raise ValueError(f"max_steps must be 1 to {recipe['max_steps_limit']}")
    if plan.get('evaluation_reserve_multiplier') != 1.5 or plan.get('export_reserve_seconds') != 120:
        raise ValueError('Evaluation and export reservations differ from the declared policy')
    if (set(plan.get('max_new_tokens', {})) != set(active_tasks(recipe)) or any(type(n) is not int or not 1 <= n <= 1536 for n in plan['max_new_tokens'].values())
            or set(plan.get('field_weights', {})) != set(base.FIELD_WEIGHTS)
            or any(not (isinstance(w, (int, float)) and w > 0) for w in plan['field_weights'].values())):
        raise ValueError('Invalid bounded decoding or field weights')
    expected = build_plan({k: [] for k in ('development_loss', 'transfer_loss', 'generation_mandatory', 'generation_conditional', 'evaluation_views')},
                          total_budget_minutes=0, max_steps=1, tiny=False, recipe=recipe)
    if (plan.get('training_loss') != 'present_field_normalized_ce' or plan.get('sampling_policy') != expected['sampling_policy']
            or plan.get('trial_overrides') != expected['trial_overrides']):
        raise ValueError('Invalid training loss, sampling policy or trial overrides for the declared recipe')
    policy = plan.get('verification_policy', {})
    if policy.get('performed_in_notebook') is not False or policy.get('sidecar_schema') != 'security-behavior-verification/1':
        raise ValueError('Invalid verification policy')
    cohort, by_id = plan.get('cohorts', {}), {v['view_id']: v for v in views}
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
            if name == 'legacy_regression_loss' and view['partition_role'] != 'transfer':
                raise ValueError('Transfer loss views must be transfer views')
    sizes = plan.get('cohort_policy', {})
    if (len(groups['development_loss']) != sizes.get('development_loss') or len(groups['legacy_regression_loss']) != sizes.get('transfer_loss')
            or len(groups['generation_mandatory']) != 6 or len(groups['generation_conditional']) != 6
            or set(groups['generation_mandatory']) & set(groups['generation_conditional'])):
        raise ValueError('Cohort sizes or overlap differ from the declared policy')
    scored = set(groups['evaluation_views'])
    if not (set(groups['development_loss']) | set(groups['legacy_regression_loss']) | set(groups['generation_mandatory'])
            | set(groups['generation_conditional'])) <= scored:
        raise ValueError('Every cohort view must be in the evaluation set')


LINE_NUMBER = re.compile(r'(?m)^[ \d]{4}\|(?: |$)')


def strip_line_numbers(text):
    """Remove the right-aligned line numbers ("   1| ") that a numbered repair prompt puts in front of every line."""
    return LINE_NUMBER.sub('', text)


def shown_source(context_text):
    """The code under review, exactly as the prompt shows it (between the target header and the scope line); a header marked numbered is stripped of
    its line numbers so that patches and quotes are checked against the plain code."""
    header, body = context_text.split('\n', 1)
    code = body.split('\n[scope]\n', 1)[0]
    return strip_line_numbers(code) if header.rstrip().endswith(', numbered)') else code


def _scope_line(context_text):
    return context_text.split('\n[scope]\n', 1)[-1]


def _old_side_blocks(patch):
    blocks, current = [], None
    for line in patch.split('\n'):
        if line.startswith('@@ '):
            current = []
            blocks.append(current)
        elif current is not None and line[:1] in (' ', '-') and line != '' and not line.startswith('\\'):
            current.append(line[1:])
    return blocks


def _applies_ignoring_line_numbers(patch, source):
    """Every hunk's old text (context and removed lines) occurs as one contiguous block of the shown code; the hunk header numbers are not looked at."""
    blocks = _old_side_blocks(patch)
    lines = source.split('\n')
    return bool(blocks) and all(block and any(lines[i:i + len(block)] == block for i in range(len(lines) - len(block) + 1)) for block in blocks)


_KEBAB = re.compile(r'(?<![\w@/.-])[a-z0-9][a-z0-9.]*-[a-z0-9][a-z0-9-]*(?![\w-])')


def answer_checks(reports, views):
    """Per arm, counts the failures the aggregate gates cannot see: an answer that claims a fix exists upstream, a hyphenated name in a finding that is
    neither in the shown code nor in the scope line (an invented product), and for repairs whether the patch applies exactly and whether it would apply
    if the hunk line numbers were ignored. The second is informational; the exact gate is unchanged."""
    from training.finetune import repair_edits, repair_validation
    out = {}
    for arm, report in reports.items():
        if report is None:
            continue
        claims, names, patches = 0, set(), {}
        for row in report['rows']:
            generation, view = row.get('generation'), views.get(row['view_id'])
            text = generation.get('text') if generation else None
            if not text or view is None:
                continue
            lowered = text.lower()
            claims += 'upstream' in lowered or 'fix commit' in lowered
            try:
                answer = json.loads(text)
            except ValueError:
                answer = None
            source = shown_source(view['context_text'])
            known = (source + '\n' + _scope_line(view['context_text'])).lower()
            if isinstance(answer, dict):
                prose = ' '.join(str(finding.get(key, '')) for finding in answer.get('findings') or [] if isinstance(finding, dict) for key in ('summary', 'impact'))
                names |= {token for token in _KEBAB.findall(prose) if token not in known}
                if view['task'] == 'repair' and isinstance(answer.get('patch'), str):
                    path = list(view['allowed_patch_paths'])[0]
                    if repair_edits.looks_like_edits(answer['patch']):                      # security_chat_v3: an edit list, with a reason for every failure
                        verdict = repair_edits.assess(answer['patch'], source, path)
                        exact = verdict['state'] == 'applies_exactly'
                        patches[row['view_id']] = {'applies_exactly': exact, 'applies_ignoring_line_numbers': exact or verdict['relocated_exactly'],
                                                   'format': 'edits', 'code': verdict['code']}
                    else:
                        exact = repair_validation.assess_patch(answer['patch'], {path: source}, allowed_paths={path})['state'] == 'applies_exactly'
                        patches[row['view_id']] = {'applies_exactly': exact,
                                                   'applies_ignoring_line_numbers': exact or _applies_ignoring_line_numbers(answer['patch'], source)}
        out[arm] = {'claims_upstream_fix': claims, 'names_not_in_shown_code': sorted(names), 'patches': patches,
                    'patches_exact': sum(p['applies_exactly'] for p in patches.values()),
                    'patches_ignoring_line_numbers': sum(p['applies_ignoring_line_numbers'] for p in patches.values())}
    return out


def diagnose_report(report, views, data_dir=None):
    """Response diagnostics for every generation; the authenticated source is the code shown in the view's own prompt."""
    from training.finetune import response_diagnostics as rd
    out = {}
    for row in report['rows']:
        generation = row.get('generation')
        if generation is None:
            continue
        if generation.get('text') is None:
            out[row['view_id']] = None
            continue
        view = views[row['view_id']]
        paths = list(view['allowed_patch_paths'])
        out[row['view_id']] = rd.diagnose_response(view['task'], generation['text'], sources={paths[0]: shown_source(view['context_text'])},
                                                   allowed_paths=paths, reference_fixed=None, language=view.get('language'))
    return out
