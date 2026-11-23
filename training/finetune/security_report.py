"""Render the plain-ASCII results report of a security trial from its measured comparison and optional verifier sidecar.

It lists what was demonstrated, which gates failed, and which evidence does not exist. It never turns an unmeasured gate into a pass and
never claims independent security accuracy or production readiness.
"""
from __future__ import annotations

DEFAULT_HARDWARE = {'NVIDIA L4': 'not validated in this report', 'NVIDIA T4': 'not validated in this report',
                    'other CUDA GPUs': 'not validated in this report', 'TPU (experimental recipe)': 'not validated in this report'}


def _ascii(text):
    return text.replace('–', '-').replace('—', '-').encode('ascii', 'replace').decode('ascii')


def _number(value):
    return 'n/a' if value is None else f'{value:.6f}'


def render_training_summary(status):
    """How much of the training data one run used and how its learning rate went, from the run's security_trial.json. Plain ASCII, no verdicts."""
    sampling = status.get('training_sampling') or {}
    lines = ['## How the run used the training data', '',
             f'- Sampling policy: {sampling.get("policy", "unknown")}'
             + (f' with task counts per optimizer batch {sampling["task_counts_per_batch"]}.' if sampling.get('task_counts_per_batch') else '.'),
             f'- Updates done: {status.get("steps_done", 0)} of {status.get("planned_steps", "?")} planned'
             + (f' (stopped by {status["training_stopped"]}).' if status.get('training_stopped') else '.'),
             f'- Learning schedule completed: {"yes" if status.get("schedule_completed") else "no"}'
             + ('' if status.get('schedule_completed') else ' (the learning rate did not finish decaying).')]
    trace = status.get('learning_rate_trace') or []
    if trace:
        lines.append(f'- Learning rate: first update {trace[0]:.2e}, peak {max(trace):.2e}, last update {trace[-1]:.2e}.')
    gate = status.get('training_view_coverage_gate')
    if gate:
        scope = f'Training views of {", ".join(gate["tasks"])}' if gate.get('tasks') else 'Training views'
        lines.append(f'- {scope} drawn at least once: {gate["views_drawn"]} of {gate["total_views"]} '
                     + ('(reported only: a 30-minute run cannot draw every view of a large corpus, and full coverage is not required here).' if gate['met'] is None else
                        '(met).' if gate['met'] else f'(NOT met; needs {gate["updates_for_full_coverage"]} updates, {gate["steps_done"]} done).'))
    coverage = status.get('view_coverage') or {}
    if coverage.get('draws_per_view_by_task'):
        lines.append('- Draws per training view by task: ' + ', '.join(
            f'{task} {value:.2f} ({coverage.get("full_passes_by_task", {}).get(task, 0)} full passes)'
            for task, value in sorted(coverage['draws_per_view_by_task'].items())) + '.')
    windows = status.get('training_loss_windows')
    if windows:
        lines.append(f'- Mean training loss over the first {windows["window"]} updates {_number(windows["first_mean"])}, '
                     f'over the last {windows["window"]} updates {_number(windows["last_mean"])}. '
                     'Training loss on views already drawn is not held-out performance.')
    lines.append('')
    return _ascii('\n'.join(lines))


def render_report(comparison, *, sidecar=None, hardware=None, title='Security training trial results'):
    gates = comparison.get('gates') or {}
    met = [(name, g) for name, g in gates.items() if g['met'] is True]
    failed = [(name, g) for name, g in gates.items() if g['met'] is False]
    unknown = [(name, g) for name, g in gates.items() if g['met'] is None]
    lines = [f'# {title}', '',
             f'Comparison schema: {comparison.get("schema")}. State: {comparison.get("state")}. Synthetic authored data only.', '']
    cohorts = comparison.get('cohorts') or {}
    if cohorts:
        dev, legacy = cohorts['development_loss'], cohorts['legacy_regression_loss']
        lines += ['## Measured loss', '',
                  f'- Development loss ({dev["planned"]} views): original {_number(dev["base"]["token_mean_loss"])}, adapter {_number(dev["tuned"]["token_mean_loss"])}, '
                  f'delta {_number(dev["loss_delta_tuned_minus_base"])}.',
                  f'- Legacy regression loss ({legacy["planned"]} views, no longer untouched): original {_number(legacy["base"]["token_mean_loss"])}, '
                  f'adapter {_number(legacy["tuned"]["token_mean_loss"])}, delta {_number(legacy["loss_delta_tuned_minus_base"])}.', '']
        base_tasks, tuned_tasks = dev['base'].get('by_task') or {}, dev['tuned'].get('by_task') or {}
        if base_tasks:
            lines[-1:] = ['', 'Development loss by task (original -> adapter, per-token mean over the scored views):', '']
            lines += [f'- {task}: {_number(base_tasks[task]["token_mean_loss"])} -> '
                      f'{_number((tuned_tasks.get(task) or {}).get("token_mean_loss"))} ({base_tasks[task]["scored"]} views)' for task in base_tasks] + ['']
    generation = comparison.get('generation')
    if generation:
        b, t = generation['base'], generation['tuned']
        lines += ['## Generation cohort (frozen before training)', '',
                  f'- Planned cases: {generation["planned"]} (conditional cases added: {generation["conditional"]["added"]} of {generation["conditional"]["planned"]}).',
                  f'- Strict JSON valid: original {b["json_valid"]}, adapter {t["json_valid"]}. Schema valid: original {b["schema_valid"]}, adapter {t["schema_valid"]}.',
                  f'- Expected-status matches: original {b["status_matches"]}, adapter {t["status_matches"]}.',
                  f'- Scoped false positives: original {b["scoped_false_positives"]}, adapter {t["scoped_false_positives"]}. '
                  f'Missed findings: original {b["missed_findings"]}, adapter {t["missed_findings"]}.',
                  f'- Insufficient-context correct: original {b["insufficient_context_correct"]}, adapter {t["insufficient_context_correct"]}.',
                  f'- Repairs applying exactly: original {b["patch_applies_exactly"]}, adapter {t["patch_applies_exactly"]} of {t["repair_planned"]}.',
                  f'- Not generated (kept in the denominator): original {b["not_generated"]}, adapter {t["not_generated"]}. '
                  f'Token-limit hits: original {b["token_limit_hits"]}, adapter {t["token_limit_hits"]}.', '']
    improvements = []
    if generation:
        b, t = generation['base'], generation['tuned']
        for key, label in (('json_valid', 'strict-JSON valid'), ('schema_valid', 'schema valid'), ('status_matches', 'expected-status matches'),
                           ('scoped_negative_correct', 'scoped negatives answered no_finding'), ('insufficient_context_correct', 'insufficient-context answered correctly'),
                           ('patch_applies_exactly', 'repairs applying exactly'), ('tests_static_ok', 'generated tests passing static checks')):
            if t[key] > b[key]:
                improvements.append(f'{label}: {b[key]} -> {t[key]} (of {generation["planned"]} planned cases)')
    if cohorts and cohorts['development_loss']['loss_delta_tuned_minus_base'] is not None and cohorts['development_loss']['loss_delta_tuned_minus_base'] < 0:
        improvements.append(f'development completion loss: {_number(cohorts["development_loss"]["base"]["token_mean_loss"])} -> '
                            f'{_number(cohorts["development_loss"]["tuned"]["token_mean_loss"])}')
    lines += ['## Gates met', '', 'A met gate is a threshold crossed, not necessarily an improvement over the original.', '']
    lines += [f'- {name}: {g["detail"]}' for name, g in met] or ['- None of the measured gates was met.']
    lines += ['', '## Demonstrated improvements', '', 'Only strict adapter-over-original differences on the frozen synthetic cohorts.', '']
    lines += [f'- {item}' for item in improvements] or ['- None of the measured values improved.']
    lines += ['', '## Failed gates', '']
    lines += [f'- {name}: {g["detail"]}' for name, g in failed] or ['- No measured gate failed.']
    lines += ['', '## Unavailable evidence', '']
    lines += [f'- {name}: {g["detail"]}' for name, g in unknown]
    lines += ['- Independent security accuracy: not measured. No independently reviewed benchmark result exists for this trial.']
    if sidecar:
        counts = sidecar['counts']
        lines += [f'- Behavioural verification: runner {sidecar["runner"]["kind"]}; planned {sidecar["planned"]}; outcomes '
                  + ', '.join(f'{name} {counts[name]}' for name in counts) + '.']
        reasons = sorted({u['reason'] for u in sidecar['unexecuted']})
        lines += [f'- Unexecuted verification cases: {len(sidecar["unexecuted"])}' + (f' (reasons: {", ".join(reasons)}).' if reasons else '.')]
        lines += [f'- Verification time {sidecar.get("elapsed_seconds")} s is reported separately from the 30-minute notebook budget.']
    else:
        lines += ['- Behavioural verification: no sidecar supplied; every repair and generated test is unverified.']
    lines += ['', '## Hardware validation', '']
    lines += [f'- {name}: {state}' for name, state in {**DEFAULT_HARDWARE, **(hardware or {})}.items()]
    lines += ['', '## Limits', '',
              '- Authored synthetic template corpus; partitions use different code forms, wrappers and names but the same three mechanisms.',
              '- Independent security accuracy: not measured. Passing these gates is not evidence of production use.',
              '- All results are experimental and are not promoted to security-evaluation or deployment status.']
    return _ascii('\n'.join(lines) + '\n')
