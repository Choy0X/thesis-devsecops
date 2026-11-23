"""Held-out source learning comparison. No vulnerability labels are inferred."""
from __future__ import annotations

import collections
import hashlib
import json
import math
import time


def generation_cohort(views, count=12):
    """Round-robin tasks in stable view order, never select by answer quality."""
    groups = collections.defaultdict(list)
    for view in sorted(views, key=lambda row: row['view_id']):
        groups[view['task']].append(view['view_id'])
    chosen = []
    for index in range(max((len(rows) for rows in groups.values()), default=0)):
        for task in sorted(groups):
            if index < len(groups[task]) and len(chosen) < count:
                chosen.append(groups[task][index])
    return chosen


def summarize(rows):
    scored = [row for row in rows if row['status'] == 'scored']
    tokens = sum(row['supervised_tokens'] for row in scored)
    loss = math.fsum(row['nll_sum'] for row in scored) / tokens if tokens else None
    return {'views': len(rows), 'scored_views': len(scored), 'supervised_tokens': tokens,
            'token_mean_loss': loss,
            'example_mean_loss': math.fsum(row['mean_loss'] for row in scored) / len(scored) if scored else None,
            'perplexity': math.exp(loss) if loss is not None and loss < 700 else None,
            'status_counts': dict(collections.Counter(row['status'] for row in rows)),
            'generation_status_counts': dict(collections.Counter(row['generation']['state'] for row in rows if row.get('generation')))}


def evaluate(model, tokenizer, views, cfg, backend, *, arm, budget, generation_ids, max_new_tokens=128,
             reserve_seconds=120, log=lambda message: None, snapshot=None):
    import torch
    from training.finetune import serialization as sz, tokenization as tk, collate, losses, model as md, infer
    started = budget.clock()
    views = sorted(views, key=lambda row: row['view_id'])
    rows = [{'view_id': view['view_id'], 'record_id': view['record_id'], 'task': view['task'],
             'status': 'not_attempted', 'generation': None} for view in views]
    was_training = model.training
    model.eval()
    pad = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id
    generation_ids = set(generation_ids)
    cost = 0.
    def report():
        complete = all(row['status'] == 'scored' and
                   (row['view_id'] not in generation_ids or (row.get('generation') or {}).get('state') in ('answered', 'token_limit')) for row in rows)
        return {'schema': 'source-trial-evaluation/1', 'state': 'finished' if complete and rows else 'partial', 'arm': arm,
            'security_evaluation': False, 'generation_ids': sorted(generation_ids), 'max_new_tokens': max_new_tokens,
            'seconds': budget.clock() - started, 'rows': rows, 'summary': summarize(rows),
            'tasks': {task: summarize([row for row in rows if row['task'] == task]) for task in sorted({row['task'] for row in rows})}}
    def save():
        if snapshot is not None:
            snapshot(report())
    save()
    try:
        for index, view in enumerate(views):
            row = rows[index]
            if not budget.can_start(max(1., cost * 1.5), reserve_seconds):
                row['status'] = 'deadline_excluded'
                save()
                continue
            before = budget.clock()
            try:
                response = json.loads(view['response_json'])
                serialized = sz.serialize_example(view['task'], view['context_text'], response,
                                                  protocol=cfg.model.serialization, tokenizer=tokenizer)
                total, reserve = tk.limits(view['task'], context=cfg.context)
                encoded = tk.encode_example(tokenizer, serialized, max_total=total, completion_reserve=reserve)
                batch = collate.collate([encoded], pad)
                static = False
                if backend.name == 'tpu':
                    batch = collate.bucket_batch(batch, pad, cfg.context.max_total_tokens)
                    static = (batch['labels'], batch['field_ids'])
                batch = {key: value.to(backend.device) for key, value in batch.items()}
                with torch.no_grad(), backend.autocast():
                    hidden = md.hidden_states(model, batch['input_ids'], batch['attention_mask'])
                    tokens = losses.token_losses(hidden, md.lm_head_of(model), batch['labels'], batch['field_ids'],
                                                 chunk=min(64, cfg.training.lm_loss_chunk_tokens), checkpoint=False, static=static)
                count = int(tokens.valid.sum().cpu()) if tokens.valid is not None else int(tokens.loss.numel())
                nll = float(tokens.loss.sum())
                if not count or not math.isfinite(nll):
                    raise ValueError('nonfinite_or_empty_supervised_loss')
                prompt = sz.serialize_prompt(view['task'], view['context_text'], protocol=cfg.model.serialization, tokenizer=tokenizer).prompt
                row.update(status='scored', supervised_tokens=count, nll_sum=nll, mean_loss=nll / count,
                           prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                           target_sha256=hashlib.sha256(serialized.completion.encode()).hexdigest(),
                           scoring_seconds=max(0., budget.clock() - before))
                del hidden, tokens, batch
                if view['view_id'] in generation_ids:
                    if budget.can_start(max(2., cost * 2), reserve_seconds):
                        row['generation'] = {'state': 'running', 'text': None}
                        save()
                        generation_started = budget.clock()
                        with backend.autocast():
                            generated = infer.generate(model, tokenizer, [prompt], max_new_tokens=max_new_tokens,
                                                       device=str(backend.device), batch_size=1)[0]
                        backend.synchronize()
                        text = generated.text
                        row['generation'] = {'state': 'token_limit' if generated.hit_limit else 'answered',
                            'text': text, 'target': serialized.completion, 'new_tokens': generated.new_tokens,
                            'hit_limit': generated.hit_limit, 'seconds': max(generated.seconds, budget.clock() - generation_started),
                            'exact_match': text == serialized.completion,
                            'whitespace_normalized_match': ' '.join(text.split()) == ' '.join(serialized.completion.split())}
                    else:
                        row['generation'] = {'state': 'deadline_excluded', 'text': None}
            except (tk.Rejected, ValueError, KeyError) as error:
                row.update(status='excluded', detail=str(error))
            except torch.OutOfMemoryError as error:
                row.update(status='resource_failure', detail=str(error))
                if backend.name == 'cuda':
                    torch.cuda.empty_cache()
            except TimeoutError as error:
                row['generation'] = {'state': 'timeout', 'text': None, 'detail': str(error)}
            backend.synchronize()
            cost = max(cost, budget.clock() - before)
            save()
            log(f'{arm}: {index + 1}/{len(views)} source views, {row["status"]}')
    finally:
        model.train(was_training)
    return report()


def compare(base, tuned):
    if base is None or tuned is None:
        return {'schema': 'source-trial-comparison/1', 'state': 'partial', 'security_evaluation': False,
                'loss_delta_tuned_minus_base': None,
                'missing_phases': [name for name, value in [('baseline_evaluation', base), ('adapted_evaluation', tuned)] if value is None]}
    signature = lambda report: [(row['view_id'], row['task'], row.get('prompt_sha256'), row.get('target_sha256'),
                                 row.get('supervised_tokens')) for row in report['rows']]
    if not base['rows'] or signature(base) != signature(tuned):
        raise ValueError('Source comparison requires an identical nonempty prompt/target/token cohort')
    if base['generation_ids'] != tuned['generation_ids'] or base['max_new_tokens'] != tuned['max_new_tokens']:
        raise ValueError('Source comparison requires identical generation cohort and decoding limits')
    complete = base['state'] == tuned['state'] == 'finished'
    b, t = summarize(base['rows']), summarize(tuned['rows'])
    return {'schema': 'source-trial-comparison/1', 'state': 'finished' if complete else 'partial',
            'security_evaluation': False, 'baseline': b, 'tuned': t,
            'loss_delta_tuned_minus_base': t['token_mean_loss'] - b['token_mean_loss'] if complete and t['token_mean_loss'] is not None and b['token_mean_loss'] is not None else None,
            'missing_phases': [], 'interpretation': 'Descriptive held-out source-task comparison, not security accuracy or production readiness.',
            'examples': [{'view_id': b_row['view_id'], 'task': b_row['task'], 'base': b_row.get('generation'),
                          'tuned': t_row.get('generation')} for b_row, t_row in zip(base['rows'], tuned['rows']) if b_row.get('generation') or t_row.get('generation')]}
