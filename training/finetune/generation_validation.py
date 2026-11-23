"""Static checks of generated answers, with no model, tools, or code execution.

Quote presence is a string/source provenance check. It is not a judgment that a
quote proves the claimed vulnerability, root cause, trace, impact, or repair.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path, PurePosixPath

from training.finetune import serialization


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def _sources(view, record, data_dir):
    """Recheck the caller's authenticated benchmark snapshots, read-only."""
    snapshots = _json(record.get('source_snapshots'))
    if not isinstance(snapshots, list) or not snapshots or data_dir is None:
        return 'unavailable', [], None
    revision = view.get('source_revision') or record.get('relevant_parent_commit')
    if not revision:
        return 'requested_revision_unavailable', [], revision
    known = {record.get('relevant_parent_commit'), record.get('fix_commit')} - {None}
    if known and revision not in known:
        return 'unknown_requested_revision', [], revision
    selected = [source for source in snapshots if isinstance(source, dict) and source.get('revision') == revision]
    if not selected:
        return 'requested_revision_unavailable', [], revision
    root, sources = Path(data_dir).resolve(), []
    try:
        for descriptor in selected:
            name = descriptor.get('path')
            if not isinstance(name, str):
                raise ValueError('missing snapshot path')
            rel = PurePosixPath(name)
            if not name or rel.is_absolute() or '\\' in name or ':' in name or '..' in rel.parts:
                raise ValueError('unsafe snapshot path')
            path = root.joinpath(*rel.parts)
            if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root)
                    or path.stat().st_size > 16 << 20):
                raise ValueError('unsafe or over-budget snapshot')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != descriptor.get('sha256'):
                raise ValueError('snapshot hash mismatch')
            source_path = descriptor.get('source_path')
            if not isinstance(source_path, str) or not source_path:
                raise ValueError('missing logical source path')
            sources.append((source_path, raw.decode('utf-8')))
    except (ValueError, OSError, TypeError):
        return 'snapshot_integrity_failed', [], revision
    return 'verified_snapshot_bytes', sources, revision


def measure(raw_generations, views, records=None, data_dir=None, *, protocol=serialization.SERIALIZATION_ID):
    """Account for every selected view and validate literal evidence presence.

    ``records`` follows infer.load_record_facts: record_id to record metadata.
    Snapshot descriptors follow independent benchmark source_snapshots. Their
    trusted admission is the runner preflight's responsibility; this module
    rechecks hashes, paths, and the requested revision without issuing receipts.
    """
    views, raw_generations = list(views), list(raw_generations)
    ids = [view.get('view_id') for view in views]
    if any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids):
        raise ValueError('Selected evaluation views require unique nonempty identities')
    records = records or {}
    generated = defaultdict(list)
    unidentified = 0
    for raw in raw_generations:
        if not isinstance(raw, dict) or not isinstance(raw.get('view_id'), str):
            unidentified += 1
            continue
        generated[raw['view_id']].append(raw)
    selected_ids = set(ids)
    unexpected = sorted(set(generated) - selected_ids)
    quote_counts = Counter(total=0, rendered_valid=0, rendered_invalid=0,
                           source_valid=0, source_invalid=0, source_unavailable=0)
    rows, hit_limit = [], 0
    for view in views:
        key, task = view['view_id'], view.get('task')
        row = {'view_id': key, 'task': task, 'record_id': view.get('record_id'),
               'status': 'missing_generation', 'evidence': [], 'hit_token_limit': False,
               'semantic_adjudication': 'not_adjudicated',
               'repair': {'state': 'not_applicable', 'execution_state': 'not_run',
                          'applicability': 'not_run', 'security_regression': 'not_run',
                          'functionality_preservation': 'not_run', 'verifier_integrity': 'not_run'}}
        candidates = generated.get(key, [])
        if not candidates:
            row['detail'] = 'No raw output was retained. Exclusion, timeout, or runtime cause is not established by absence alone.'
            rows.append(row)
            continue
        if len(candidates) != 1:
            row.update(status='duplicate_generation', generation_count=len(candidates))
            rows.append(row)
            continue
        raw = candidates[0]
        row['hit_token_limit'] = bool(raw.get('hit_limit'))
        hit_limit += row['hit_token_limit']
        if raw.get('status') in ('timeout', 'timed_out') or raw.get('timeout') is True:
            row.update(status='timeout', detail=str(raw.get('detail') or raw.get('error') or 'Generation timed out.'))
            rows.append(row)
            continue
        if raw.get('error'):
            row.update(status='runtime_failure', detail=str(raw['error']))
            rows.append(row)
            continue
        if raw.get('task') != task:
            row.update(status='task_mismatch', generated_task=raw.get('task'))
            rows.append(row)
            continue
        text = raw.get('text')
        if not isinstance(text, str):
            answer, problems = None, ['Generation text is absent or is not a string.']
        else:
            try:
                answer, problems = (serialization.parse_response(task, text, protocol=protocol) if protocol in serialization.SECURITY_PROTOCOLS
                                    else serialization.parse_response(task, text))
            except (ValueError, TypeError, KeyError) as error:
                answer, problems = None, [str(error)]
        if answer is None:
            row.update(status='truncated_malformed' if row['hit_token_limit'] else 'malformed', problems=problems)
            rows.append(row)
            continue
        if not isinstance(answer, dict):
            row.update(status='answered_text', detail='This task has no structured security evidence contract.')
            rows.append(row)
            continue
        row.update(status='abstained' if answer.get('status') == 'insufficient_context' else 'answered',
                   answer_status=answer.get('status'), missing_context=answer.get('missing_context', []),
                   model_validation_state=answer.get('validation_state'))
        record = records.get(view.get('record_id')) or {}
        source_state, sources, revision = _sources(view, record, data_dir)
        row.update(source_snapshot_state=source_state, requested_revision=revision)
        context = view.get('context_text') or ''
        for entry in answer.get('evidence', []):
            quote = entry['quote']
            visible = quote in context
            paths = sorted({path for path, body in sources if quote in body})
            source_result = ('valid' if paths else 'quote_absent') if source_state == 'verified_snapshot_bytes' else source_state
            quote_counts['total'] += 1
            quote_counts['rendered_valid' if visible else 'rendered_invalid'] += 1
            quote_counts['source_valid' if paths else 'source_unavailable' if source_state == 'unavailable' else 'source_invalid'] += 1
            row['evidence'].append({'quote': quote, 'rendered_context': 'valid' if visible else 'invalid',
                'rendered_occurrences': context.count(quote), 'source_revision': source_result,
                'requested_revision': revision, 'source_paths': paths,
                'path_attribution': 'unique_quote_source' if len(paths) == 1 else 'ambiguous_quote_sources' if paths else 'unavailable',
                'supports_claim': 'not_adjudicated'})
        if task == 'repair':
            row['repair']['state'] = 'proposed_patch' if answer.get('patch') else 'no_patch'
        rows.append(row)
    counts = Counter(row['status'] for row in rows)
    answered = sum(counts[key] for key in ('answered', 'answered_text'))
    selected = len(views)
    return {'schema': 'generation-validation/1', 'state': 'measured' if selected else 'empty_eligible_set',
        'selected_views': selected, 'retained_raw_generations': len(raw_generations),
        'status_counts': dict(counts), 'hit_token_limit_views': hit_limit,
        'answered_coverage': answered / selected if selected else None,
        'abstention_rate': counts['abstained'] / selected if selected else None,
        'unexpected_generation_ids': unexpected, 'unidentified_raw_generations': unidentified,
        'quotes': dict(quote_counts), 'per_view': rows,
        'semantic_adjudication': {'state': 'not_adjudicated', 'root_cause_correctness': None,
            'trace_validity': None, 'impact_correctness': None, 'unsupported_claims': None,
            'finding_location_correctness': None, 'fix_addresses_supported_failure': None,
            'reason': 'Literal quote presence does not validate the explanation. Independent adjudication is required.'},
        'repair_execution': {'state': 'not_run', 'reason': 'This static module does not execute patches, tests, or collected code.'},
        'utility': {'state': 'unavailable', 'reason': 'General coding and instruction-following checks require a separate declared cohort.'},
        'limitations': ['Literal matching checks exact quotes only; semantic equivalence is not inferred.',
            'Source snapshot authentication is an upstream benchmark-preflight responsibility; hashes and requested revisions are rechecked here.',
            'Finding locations are free text in the existing response contract and remain unadjudicated.',
            'Missing raw output alone cannot distinguish an exclusion, timeout, or generation failure.',
            'A token-limit hit is retained even when the leading response object is complete, consistent with the existing parser.']}
