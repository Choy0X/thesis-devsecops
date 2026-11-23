"""Paired synthetic security-task measurement, never independent accuracy."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math


def generation_cohort(views, count=6):
    strata = defaultdict(list)
    for view in sorted(views, key=lambda row: row['view_id']):
        strata[(view['task'], view['pool'])].append(view)
    if count < len(strata):
        raise ValueError('Generation cohort must preserve all security task/pool contrasts')
    chosen, families, paired = [], Counter(), {}
    ordered = sorted(strata, key=lambda key: (key[0], key[1] != 'supported', key[1]))
    for key in ordered:
        task, pool = key
        preferred = paired.get(task) if pool == 'scoped_negative' else None
        options = strata[key]
        view = min(options, key=lambda row: (row.get('family_id') != preferred if preferred else False,
                   families[row.get('family_id')], row['view_id']))
        chosen.append(view['view_id'])
        families[view.get('family_id')] += 1
        if pool == 'supported':
            paired[task] = view.get('family_id')
    for view in sorted(views, key=lambda row: row['view_id']):
        if len(chosen) >= count:
            break
        if view['view_id'] not in chosen:
            chosen.append(view['view_id'])
    return chosen


def summarize(rows):
    scored = [row for row in rows if row['status'] == 'scored']
    tokens = sum(row['supervised_tokens'] for row in scored)
    generated = [row for row in rows if row.get('generation') is not None]
    valid = [row for row in generated if row.get('response_contract', {}).get('state') == 'valid']
    fields = {}
    for row in scored:
        for name, facts in row.get('fields', {}).items():
            aggregate = fields.setdefault(name, {'tokens': 0, 'nll_sum': 0.})
            aggregate['tokens'] += facts['tokens']; aggregate['nll_sum'] += facts['nll_sum']
    for facts in fields.values():
        facts['mean_loss'] = facts['nll_sum'] / facts['tokens'] if facts['tokens'] else None
    return {'views': len(rows), 'scored_views': len(scored), 'supervised_tokens': tokens,
        'token_mean_loss': math.fsum(row['nll_sum'] for row in scored) / tokens if tokens else None,
        'fields': fields, 'status_counts': dict(Counter(row['status'] for row in rows)),
        'generation_selected': len(generated), 'schema_valid': len(valid),
        'schema_valid_rate': len(valid) / len(generated) if generated else None,
        'synthetic_status_matches': sum(bool(row.get('response_contract', {}).get('synthetic_status_match')) for row in generated),
        'generation_status_counts': dict(Counter(row['generation']['state'] for row in generated)),
        'token_limit_hits': sum(bool(row['generation'].get('hit_limit')) for row in generated)}


def _build_report(arm, cfg, rows, selected, max_new_tokens, started, budget, views):
    complete = bool(rows) and all(row['status'] == 'scored' and (row['generation'] is None
        or row['generation']['state'] in ('answered', 'token_limit')) for row in rows)
    return {'schema': 'security-trial-evaluation/1', 'state': 'finished' if complete else 'partial',
        'arm': arm, 'serialization': cfg.model.serialization, 'synthetic': True,
        'independent_security_accuracy': None, 'counts_as_evaluation': False,
        'generation_ids': sorted(selected), 'max_new_tokens': max_new_tokens, 'rows': rows,
        'seconds': budget.clock() - started, 'summary': summarize(rows),
        'tasks': {task: summarize([row for row in rows if row['task'] == task]) for task in sorted({v['task'] for v in views})}}


def _status_pair(tokenizer):
    """(token id of 'finding', token id of 'no') at the first differing token of the two status strings, or None."""
    try:
        a = tokenizer('{"status": "finding', add_special_tokens=False)['input_ids']
        b = tokenizer('{"status": "no_finding', add_special_tokens=False)['input_ids']
        return next(((x, y) for x, y in zip(a, b) if x != y), None)
    except Exception:
        return None


def _status_gap(hidden, model, batch, status_pair, static):
    """The model's status decision on this view, teacher-forced: the log-odds of 'finding' against 'no_finding' at the position that predicts the first decision token
    (losses.status_gap), with whether that token is one of the two. Recorded so the evaluation can place a threshold or rank twins without another pass; never fails the run."""
    if status_pair is None or static is not False:
        return None
    try:
        import torch
        from training.finetune import losses, model as md
        first = losses.first_decision_tokens(batch['labels'], batch['field_ids'])
        device = batch['labels'].device
        gap, ok = losses.status_gap(hidden, md.lm_head_of(model), batch['labels'], batch['field_ids'],
                                    torch.tensor([status_pair[0]], device=device), torch.tensor([status_pair[1]], device=device))
        if not bool(ok.all()):
            return None
        return {'finding_minus_no_finding': float(gap[0].detach()), 'expected_first_token_is_a_status': int(first[0]) in status_pair, 'tokens': list(status_pair)}
    except Exception:
        return None


def _generate_row(model, tokenizer, view, row, prompt, response, cfg, backend, max_new_tokens):
    """Generate once from the exact prompt, unconstrained and unrepaired, and record the raw text with its strict contract result."""
    from training.finetune import infer
    from training.finetune.security_serialization import parse_response
    with backend.autocast():
        generated = infer.generate(model, tokenizer, [prompt], max_new_tokens=max_new_tokens[view['task']],
                                   device=str(backend.device), batch_size=1)[0]
    backend.synchronize()
    answer, problems = parse_response(view['task'], generated.text)
    row['generation'] = {'state': 'token_limit' if generated.hit_limit else 'answered',
        'text': generated.text, 'new_tokens': generated.new_tokens, 'hit_limit': generated.hit_limit,
        'seconds': generated.seconds, 'expected_response': response}
    row['response_contract'] = {'state': 'valid' if answer is not None else 'malformed',
        'problems': problems, 'synthetic_status_match': answer is not None and answer['status'] == response['status']}


def evaluate(model, tokenizer, views, cfg, backend, *, arm, budget, generation_ids, max_new_tokens,
             reserve_seconds=120, snapshot=None, log=lambda message: None):
    import torch
    from training.finetune import serialization as sz, tokenization as tk, collate, losses, model as md
    started, cost = budget.clock(), 0.
    selected = set(generation_ids)
    views = sorted(views, key=lambda view: view['view_id'])
    rows = [{'view_id': view['view_id'], 'record_id': view['record_id'], 'task': view['task'],
             'status': 'not_attempted', 'generation': {'state': 'not_attempted', 'text': None} if view['view_id'] in selected else None}
            for view in views]
    for row, view in zip(rows, views):
        if 'partition_role' in view:
            row['partition_role'] = view['partition_role']

    def report():
        return _build_report(arm, cfg, rows, selected, max_new_tokens, started, budget, views)

    def save():
        if snapshot:
            snapshot(report())

    was = model.training
    model.eval()
    save()
    status_pair = _status_pair(tokenizer)
    try:
        for row, view in zip(rows, views):
            if not budget.can_start(max(1, cost * 1.5), reserve_seconds):
                row['status'] = 'deadline_excluded'
                if row['generation'] is not None:
                    row['generation']['state'] = 'deadline_excluded'
                save(); continue
            before = budget.clock()
            try:
                response = json.loads(view['response_json'])
                ser = sz.serialize_example(view['task'], view['context_text'], response,
                    protocol=cfg.model.serialization, tokenizer=tokenizer)
                total, reserve = tk.limits(view['task'], context=cfg.context)
                encoded = tk.encode_example(tokenizer, ser, max_total=total, completion_reserve=reserve)
                batch = collate.collate([encoded], tokenizer.pad_token_id)
                static = False
                if backend.name == 'tpu':
                    batch = collate.bucket_batch(batch, tokenizer.pad_token_id, cfg.context.max_total_tokens)
                    static = (batch['labels'], batch['field_ids'])
                batch = {key: value.to(backend.device) for key, value in batch.items()}
                with torch.no_grad(), backend.autocast():
                    hidden = md.hidden_states(model, batch['input_ids'], batch['attention_mask'])
                    token_loss = losses.token_losses(hidden, md.lm_head_of(model), batch['labels'], batch['field_ids'],
                        chunk=min(64, cfg.training.lm_loss_chunk_tokens), checkpoint=False, static=static)
                    status_gap = _status_gap(hidden, model, batch, status_pair, static)
                values, owners = token_loss.loss.detach().cpu(), token_loss.field.detach().cpu()
                valid = token_loss.valid.detach().cpu() if token_loss.valid is not None else torch.ones_like(values, dtype=torch.bool)
                values, owners = values[valid], owners[valid]
                count, nll = values.numel(), float(values.sum())
                if not count or not math.isfinite(nll):
                    raise ValueError('Nonfinite or empty supervised loss')
                fields = {name: {'tokens': int((owners == index).sum()), 'nll_sum': float(values[owners == index].sum())}
                          for index, name in enumerate(tk.FIELD_GROUPS)}
                prompt = sz.serialize_prompt(view['task'], view['context_text'], protocol=cfg.model.serialization, tokenizer=tokenizer).prompt
                row.update(status='scored', supervised_tokens=count, nll_sum=nll, mean_loss=nll/count, status_gap=status_gap,
                    fields=fields, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                    target_sha256=hashlib.sha256(ser.completion.encode()).hexdigest(), scoring_seconds=budget.clock()-before)
                del hidden, token_loss, batch
                if row['generation'] is not None:
                    if budget.can_start(max(2, cost * 1.5), reserve_seconds):
                        row['generation'] = {'state': 'running', 'text': None}; save()
                        _generate_row(model, tokenizer, view, row, prompt, response, cfg, backend, max_new_tokens)
                    else:
                        row['generation']['state'] = 'deadline_excluded'
            except (tk.Rejected, ValueError, KeyError) as error:
                row.update(status='excluded', detail=str(error))
            except (RuntimeError, MemoryError, TimeoutError) as error:
                row.update(status='runtime_failure', detail=type(error).__name__ + ': ' + str(error))
                if backend.name == 'cuda':
                    torch.cuda.empty_cache()
            backend.synchronize()
            cost = max(cost, budget.clock() - before)
            save()
            log(f'{arm}: {view["task"]}, {row["status"]}')
    finally:
        model.train(was)
    return report()


def extend_generations(model, tokenizer, views, cfg, backend, *, report, extra_ids, budget, reserve_seconds=120, snapshot=None,
                       log=lambda message: None):
    """Add generation cases to a finished report from the same model state, without rescoring loss.

    Used once, after the baseline and before the adapter exists, so the extra cases come from the pinned original. The caller freezes the
    final cohort before training; both arms then use the identical ids."""
    from training.finetune import serialization as sz
    started = budget.clock()
    by_view = {view['view_id']: view for view in views}
    rows = {row['view_id']: row for row in report['rows']}
    was = model.training
    model.eval()
    cost, dropped = 0., []
    try:
        for view_id in sorted(extra_ids):
            row, view = rows[view_id], by_view[view_id]
            if row['status'] != 'scored' or not budget.can_start(max(2, cost * 1.5), reserve_seconds):
                row['generation'] = None; dropped.append(view_id)       # an optional case that cannot finish is dropped, not left half done
                continue
            before = budget.clock()
            row['generation'] = {'state': 'running', 'text': None}
            try:
                prompt = sz.serialize_prompt(view['task'], view['context_text'], protocol=cfg.model.serialization, tokenizer=tokenizer).prompt
                _generate_row(model, tokenizer, view, row, prompt, json.loads(view['response_json']), cfg, backend, report['max_new_tokens'])
            except (RuntimeError, MemoryError, TimeoutError, ValueError) as error:
                row['generation'] = None; dropped.append(view_id)
                log(f'{report["arm"]}: dropped optional case after {type(error).__name__}')
                if backend.name == 'cuda':
                    import torch
                    torch.cuda.empty_cache()
                continue
            cost = max(cost, budget.clock() - before)
            if snapshot:
                snapshot(report)
            log(f'{report["arm"]}: extra {view["task"]}')
    finally:
        model.train(was)
    selected = (set(report['generation_ids']) | set(extra_ids)) - set(dropped)
    rebuilt = _build_report(report['arm'], cfg, list(report['rows']), selected, report['max_new_tokens'], started - report['seconds'], budget,
                            [by_view[r['view_id']] for r in report['rows']])
    rebuilt['dropped_generation_ids'] = sorted(dropped)
    return rebuilt


def compare(base, tuned):
    result = {'schema': 'security-training-comparison/1', 'state': 'partial', 'synthetic': True,
              'independent_security_accuracy': None, 'counts_as_evaluation': False, 'counts_as_full_training': False,
              'loss_delta_tuned_minus_base': None, 'examples': [],
              'interpretation': 'Synthetic held-out completion/schema/status measurements. No independent security accuracy, executed repair or production-readiness claim.'}
    if base is None or tuned is None:
        result['missing_phases'] = [name for name, value in (('baseline_evaluation', base), ('adapted_evaluation', tuned)) if value is None]
        return result
    def signature(report):
        return [(r['view_id'], r['task'], r.get('prompt_sha256'), r.get('target_sha256'), r.get('supervised_tokens')) for r in report['rows']]
    if (not base['rows'] or signature(base) != signature(tuned) or base['generation_ids'] != tuned['generation_ids']
            or base['max_new_tokens'] != tuned['max_new_tokens'] or base['serialization'] != tuned['serialization']):
        raise ValueError('Paired comparison requires the same prompt/target/token/decoding cohort')
    b, t = summarize(base['rows']), summarize(tuned['rows'])
    complete = base['state'] == tuned['state'] == 'finished'
    result.update(state='finished' if complete else 'partial', baseline=b, tuned=t, missing_phases=[],
        loss_delta_tuned_minus_base=t['token_mean_loss']-b['token_mean_loss'] if complete and b['token_mean_loss'] is not None and t['token_mean_loss'] is not None else None,
        examples=[{'view_id': left['view_id'], 'task': left['task'], 'base': left.get('generation'), 'adapter': right.get('generation'),
                   'base_contract': left.get('response_contract'), 'adapter_contract': right.get('response_contract')}
                  for left, right in zip(base['rows'], tuned['rows']) if left.get('generation') is not None])
    return result
