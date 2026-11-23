"""Paired SFT evaluation accounting. This module never runs a model or a test."""
from __future__ import annotations

from datetime import datetime, timezone
from collections import defaultdict
import json

from training.finetune import security_validation


def _date(value):
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _selected(attempts, facts):
    rows, seen = [], set()
    for row in attempts:
        if row.get('task') != 'discovery' or row.get('track') != 'generated_decision':
            continue
        key = row.get('view_id')
        if not isinstance(key, str) or key in seen:
            raise ValueError('Duplicate or missing evaluation view identity')
        seen.add(key)
        fact = facts.get(row.get('record_id')) or {}
        if fact.get('split') not in ('val', 'test'):
            raise ValueError('Evaluation requires held-out record metadata')
        if row.get('truth') not in (security_validation.POS, security_validation.NEG, security_validation.INSUFFICIENT):
            raise ValueError('Evaluation requires an independently supported detection label')
        joined = dict(row)
        for key in tuple(dict.fromkeys(('language', 'cwe_ids') + security_validation.DEPENDENCE_KEYS)):
            # Pair and record identities belong to this specific attempted view.
            # Nullable record columns must never erase known view identities.
            if key in ('pair_id', 'record_id') and row.get(key) is not None:
                joined[key] = row[key]
            else:
                joined[key] = fact[key] if fact.get(key) is not None else row.get(key)
        joined['dependence_anchors'] = sorted(set(security_validation.dependence_anchors(row))
                                             | set(security_validation.dependence_anchors(fact)))
        rows.append(joined)
    return rows


def _metrics(rows, **kwargs):
    result = security_validation.measure(rows, **kwargs)
    tp, fp = result['counts']['tp'], result['counts']['fp']
    result.update(precision=tp / (tp + fp) if tp + fp else None,
                  false_positives_per_1000_negatives=(1000 * result['false_positive_rate']
                      if result['false_positive_rate'] is not None else None),
                  abstention_rate=result['abstentions'] / len(rows) if rows else None)
    return result


def measure(attempts, facts, *, training_record_ids=None, training_facts=None, **kwargs):
    rows = _selected(attempts, facts)
    reference = facts if training_facts is None else training_facts
    if training_record_ids is not None:
        training_record_ids = set(training_record_ids)
        if any(key not in reference or reference[key].get('split') != 'train' for key in training_record_ids):
            raise ValueError('Training slice references unknown or non-training source records')
    training = [fact for key, fact in reference.items() if fact.get('split') == 'train'
                and (training_record_ids is None or key in training_record_ids)]
    dates = [_date(fact.get('advisory_first_published_at')) for fact in training]
    repositories = {fact.get('repository_identity') for fact in training}
    cutoff = max(dates) if dates and all(dates) else None
    slices = {}
    for name, available in (('newer_date', cutoff is not None),
                            ('unseen_repository', bool(training) and None not in repositories and '' not in repositories)):
        selected, unknown = [], 0
        for row in rows:
            fact = facts[row['record_id']]
            value = _date(fact.get('advisory_first_published_at')) if name == 'newer_date' else fact.get('repository_identity')
            unknown += not bool(value)
            if available and value and (value > cutoff if name == 'newer_date' else value not in repositories):
                selected.append(row)
        slices[name] = {'state': 'measured' if available and selected else 'insufficient_data' if available else 'unavailable',
                        'views': len(selected), 'unknown_metadata_views': unknown,
                        'metrics': _metrics(selected, **kwargs) if available and selected else None}
    breakdowns = {}
    for dimension, key in (('language', 'language'), ('repository', 'repository_identity'), ('cwe', 'cwe_ids')):
        buckets = defaultdict(list)
        for row in rows:
            value = row.get(key)
            if dimension == 'cwe':
                if isinstance(value, str):
                    try:
                        value = json.loads(value) if value.startswith('[') else [value]
                    except ValueError:
                        value = None
                values = sorted(set(value or ['unknown']))
            else:
                values = [value or 'unknown']
            for item in values:
                buckets[str(item)].append(row)
        breakdowns[dimension] = {name: {'views': len(subset), 'metrics': _metrics(subset, **kwargs)}
                                 for name, subset in sorted(buckets.items())}
    return {'schema': 'sft-evaluation/1', 'all': _metrics(rows, **kwargs), 'slices': slices, 'breakdowns': breakdowns,
            'training_scope': {'records': len(training), 'basis': 'published_eligible_train_views'
                               if training_record_ids is not None else 'caller_supplied_training_metadata',
                               'separate_training_reference': training_facts is not None},
            'newer_date_cutoff': cutoff.isoformat() if cutoff else None,
            'date_basis': 'Strictly later advisory publication than every training record with complete date metadata.',
            'deployment_ready': False}


def compare(base_attempts, tuned_attempts, facts, **kwargs):
    base, tuned = _selected(base_attempts, facts), _selected(tuned_attempts, facts)
    def cohort(rows):
        return {row['view_id']: (row['record_id'], row['truth'], row.get('pair_id')) for row in rows}
    if cohort(base) != cohort(tuned) or not base:
        raise ValueError('Base and tuned evaluation require the same nonempty cohort and labels')
    tuned_index = {row['view_id']: row for row in tuned}
    paired = security_validation.paired_differences(base, [tuned_index[row['view_id']] for row in base],
                         bootstrap_samples=kwargs.get('bootstrap_samples', 1000), seed=kwargs.get('seed', 0))
    return {'schema': 'sft-model-comparison/1', 'views': len(base), 'paired_differences': paired,
            'base': measure(base, facts, **kwargs), 'tuned': measure(tuned, facts, **kwargs),
            'deployment_ready': False}


def _dataset_metadata(data_dir, *, index_views=True):
    import pyarrow.parquet as pq
    from pathlib import Path
    columns = tuple(dict.fromkeys(('record_id', 'split', 'advisory_first_published_at', 'language', 'cwe_ids')
                                 + security_validation.DEPENDENCE_KEYS))
    facts = {}
    with pq.ParquetFile(Path(data_dir) / 'records.parquet') as source:
        available = [column for column in columns if column in source.schema_arrow.names]
        for batch in source.iter_batches(batch_size=256, columns=available):
            for row in batch.to_pylist():
                if row['record_id'] in facts:
                    raise ValueError('Duplicate dataset record identity')
                facts[row['record_id']] = row
    training_ids, indexed, splits = set(), {}, defaultdict(set)
    with pq.ParquetFile(Path(data_dir) / 'views.parquet') as views:
        wanted = tuple(dict.fromkeys(('view_id', 'record_id', 'task', 'split', 'val_role', 'eligible', 'weight',
                  'response_json', 'issue_group_id', 'pool') + security_validation.DEPENDENCE_KEYS)) if index_views else ('record_id', 'split', 'eligible', 'weight')
        columns = [key for key in wanted if key in views.schema_arrow.names]
        has_view_ids = 'view_id' in columns
        for batch in views.iter_batches(batch_size=256, columns=columns):
            for row in batch.to_pylist():
                rid = row['record_id']
                if rid not in facts:
                    raise ValueError('View references unknown dataset record identity')
                if row.get('split'):
                    splits[rid].add(row['split'])
                if row.get('split') == 'train' and row.get('eligible') is True and row.get('weight', 0) > 0:
                    training_ids.add(rid)
                if has_view_ids:
                    if not isinstance(row.get('view_id'), str) or row['view_id'] in indexed:
                        raise ValueError('Duplicate or missing dataset view identity')
                    indexed[row['view_id']] = row
    for rid, roles in splits.items():
        if len(roles) != 1:
            raise ValueError('Dataset record spans inconsistent train/evaluation view splits')
        derived = next(iter(roles))
        if facts[rid].get('split') and facts[rid]['split'] != derived:
            raise ValueError('Dataset record and view splits disagree')
        facts[rid]['split'] = derived
    return facts, training_ids, indexed


def from_dataset(attempts, data_dir, *, training_data_dir=None, excluded=None, **kwargs):
    """Join compact attempt rows to benchmark facts; keep training identity separate.

    Excluded views become metric-only failure rows. This function never rewrites
    stored attempts or infers security labels from source record field names.
    """
    facts, training_ids, views = _dataset_metadata(data_dir)
    if training_data_dir is not None:
        training_facts, training_ids, _ = _dataset_metadata(training_data_dir, index_views=False)
    else:
        training_facts = None
    joined, attempted = [], set()
    status_to_truth = {'finding': security_validation.POS, 'no_finding': security_validation.NEG,
                       'insufficient_context': security_validation.INSUFFICIENT}

    def enrich(row):
        key = row.get('view_id')
        view = views.get(key)
        if view is None:
            # Compatibility for old callers already carrying task and record_id.
            # A dataset with indexed view identities must never accept an absent one.
            if views or not row.get('task') or not row.get('record_id'):
                raise ValueError('Evaluation attempt has no matching benchmark view identity')
            return row
        for key in ('task', 'record_id'):
            if row.get(key) is not None and row[key] != view.get(key):
                raise ValueError('Evaluation attempt identity differs from benchmark view')
        truth = None
        if view.get('response_json'):
            response = json.loads(view['response_json'])
            truth = status_to_truth.get(response.get('status')) if isinstance(response, dict) else None
            if row.get('truth') is not None and row['truth'] != truth:
                raise ValueError('Evaluation attempt ground truth differs from benchmark view')
        pair = view.get('task') == 'discovery' and view.get('pool') in ('supported', 'scoped_negative')
        pair_facts = {'pair_id': f"discovery:{view['record_id']}",
                      'role': 'vulnerable' if view.get('pool') == 'supported' else 'protected'} if pair else {}
        if pair and row.get('pair_id') is not None and row['pair_id'] != pair_facts['pair_id']:
            raise ValueError('Evaluation attempt pair identity differs from benchmark view')
        return {**row, **{key: row[key] if row.get(key) is not None else value for key, value in pair_facts.items()},
                'task': view.get('task'), 'record_id': view['record_id'],
                'truth': truth if truth is not None else row.get('truth'),
                'issue_group_id': view['issue_group_id'] if view.get('issue_group_id') is not None else row.get('issue_group_id'),
                'dependence_anchors': sorted(set(security_validation.dependence_anchors(row))
                                            | set(security_validation.dependence_anchors(view)))}

    for row in attempts:
        if row.get('track') != 'generated_decision':
            continue
        joined.append(enrich(row))
        attempted.add(row.get('view_id'))
    for key, reason in (excluded or {}).items():
        if key in attempted:
            raise ValueError('An evaluation view cannot be both attempted and excluded')
        view = views.get(key)
        if view is None:
            raise ValueError('Excluded evaluation view has no benchmark identity')
        if view.get('task') == 'discovery':
            joined.append(enrich({'view_id': key, 'track': 'generated_decision', 'status': 'excluded',
                                  'prediction': None, 'detail': reason}))
    return measure(joined, facts, training_record_ids=training_ids, training_facts=training_facts, **kwargs)
