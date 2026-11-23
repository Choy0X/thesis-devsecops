"""Exact pinned-tokenizer audit of every staged SFT view, without truncation."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
import re

from training.finetune import serialization, sft_serialization, tokenization, sft_contracts, contracts

SCHEMA = 'sft-token-audit/1'
DECISION_STATUSES = frozenset(('finding', 'no_finding', 'insufficient_context'))


@dataclass(frozen=True)
class AuditContext:
    max_total_tokens: int = 4096
    ordinary_completion_reserve: int = 1024
    repair_completion_reserve: int = 1536
    long_context_ablation_tokens: int = 4096


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def tokenizer_fingerprint(tokenizer):
    backend = getattr(tokenizer, 'backend_tokenizer', None)
    if not getattr(tokenizer, 'is_fast', False) or not callable(getattr(backend, 'to_str', None)):
        raise ValueError('Exact audit requires a fast tokenizer with inspectable tokenizer bytes')
    body = {'backend': backend.to_str(), 'template': getattr(tokenizer, 'chat_template', None),
            'eos_token_id': tokenizer.eos_token_id, 'special_tokens': tokenizer.special_tokens_map}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def implementation_identity():
    files = [Path(__file__), *(Path(module.__file__) for module in
             (serialization, sft_serialization, tokenization, sft_contracts, contracts))]
    return {'schema': 'sft-token-implementation/1',
            'files': {str(path.resolve()): _sha(path) for path in files}}


def audit(dataset, *, tokenizer, tokenizer_identity, check=None, progress=None, max_tokens=4096):
    import pyarrow.parquet as pq
    dataset = Path(dataset)
    if max_tokens != 4096:
        raise ValueError('SFT audit requires the approved 4096-token limit')
    identity = dict(tokenizer_identity)
    if not isinstance(identity.get('model_id'), str) or not identity['model_id'].strip():
        raise ValueError('Missing pinned tokenizer model identity')
    if not re.fullmatch(r'[0-9a-f]{40}', str(identity.get('revision') or '')):
        raise ValueError('Tokenizer revision must be an immutable commit')
    original_tokenizer = tokenizer_fingerprint(tokenizer)
    if identity.get('tokenizer_sha256') != original_tokenizer:
        raise ValueError('Pinned tokenizer fingerprint mismatch')
    source = dataset / 'records.parquet'
    views = dataset / 'views.parquet'
    source_sha, views_sha = _sha(source), _sha(views)
    implementation = implementation_identity()
    capture_path = dataset / 'source_capture.json'
    capture_sha = _sha(capture_path) if capture_path.is_file() else None
    captured_files = json.loads(capture_path.read_text(encoding='utf-8'))['policy']['files'] if capture_sha else {}
    if capture_sha and any(captured_files.get(path) != sha for path, sha in implementation['files'].items()):
        raise ValueError('Staged token audit implementation identity mismatch')
    accepted, excluded, seen = [], [], set()
    candidates, prompt_statuses = [], {}
    context = AuditContext()
    counts = {'encoded': 0, 'excluded': 0, 'training_views': 0, 'prompt_tokens': 0, 'completion_tokens': 0}
    maximum = 0
    protocol_hash = serialization.serialization_hash(sft_serialization.PROTOCOL, tokenizer)

    def fence():
        if check is not None and check() is False:
            raise InterruptedError('SFT token audit cancelled')

    fence()
    reader = pq.ParquetFile(views)
    try:
        for batch in reader.iter_batches(batch_size=16):
            fence()
            for row in batch.to_pylist():
                fence()
                view_id = row.get('view_id')
                if not isinstance(view_id, str) or view_id in seen:
                    raise ValueError('Missing or duplicate SFT view identity')
                seen.add(view_id)
                reason, encoded = None, None
                if not row.get('eligible') or not (row.get('weight') or 0) > 0:
                    reason = 'view_not_eligible'
                else:
                    try:
                        response = json.loads(row['response_json'])
                        example = serialization.serialize_example(row['task'], row['context_text'], response,
                            protocol=sft_serialization.PROTOCOL, tokenizer=tokenizer)
                        from training.finetune.sft_contracts import CORPUS_TASKS
                        if example.reserved_heading_in_context and row['task'] not in CORPUS_TASKS:
                            reason = 'reserved_heading_in_context'
                        else:
                            encoded, reason = tokenization.try_encode(tokenizer, example, max_total=max_tokens, context=context)
                    except (ValueError, KeyError, TypeError) as error:
                        reason = 'invalid_response:' + str(error)[:240]
                if encoded is None:
                    counts['excluded'] += 1
                    excluded.append({'view_id': view_id, 'record_id': row.get('record_id'),
                                     'task': row.get('task'), 'reason': reason or 'not_encoded'})
                else:
                    counts['encoded'] += 1
                    counts['prompt_tokens'] += encoded.prompt_tokens
                    counts['completion_tokens'] += encoded.completion_tokens
                    counts['training_views'] += int(row.get('split') == 'train')
                    maximum = max(maximum, len(encoded.input_ids))
                    # Cache only bounded per-view accounting and identifiers,
                    # never source, responses, or the encoded token sequences.
                    status = response.get('status') if isinstance(response, dict) else None
                    prompt_sha = hashlib.sha256(json.dumps(encoded.input_ids[:encoded.prompt_tokens],
                        separators=(',', ':')).encode('ascii')).hexdigest()
                    candidates.append({'view_id': view_id, 'record_id': row.get('record_id'),
                        'task': row['task'], 'training': row.get('split') == 'train', 'status': status,
                        'prompt_token_ids_sha256': prompt_sha, 'prompt_tokens': encoded.prompt_tokens,
                        'completion_tokens': encoded.completion_tokens, 'total_tokens': len(encoded.input_ids)})
                    if status in DECISION_STATUSES:
                        prompt_statuses.setdefault(prompt_sha, set()).add(status)
                if progress is not None:
                    progress({'state': 'token_audit', 'processed': len(seen), 'counts': dict(counts),
                              'acceptance': 'provisional_until_prompt_conflict_check'})
    finally:
        reader.close()
    conflicting = {key: sorted(statuses) for key, statuses in prompt_statuses.items() if len(statuses) > 1}
    counts = {'encoded': 0, 'excluded': len(excluded), 'training_views': 0, 'prompt_tokens': 0, 'completion_tokens': 0}
    maximum, conflict_views = 0, 0
    for candidate in candidates:
        fence()
        prompt_sha = candidate['prompt_token_ids_sha256']
        if prompt_sha in conflicting and candidate['status'] in DECISION_STATUSES:
            excluded.append({key: candidate[key] for key in
                ('view_id', 'record_id', 'task', 'status', 'prompt_token_ids_sha256')})
            excluded[-1].update(reason='conflicting_prompt_supervision',
                conflicting_statuses=conflicting[prompt_sha],
                remediation='Inspect all views linked by this prompt fingerprint and their immutable source references; '
                            'recover distinguishable grounded inputs or consistent evidence before re-auditing.')
            counts['excluded'] += 1
            conflict_views += 1
        else:
            accepted.append(candidate['view_id'])
            counts['encoded'] += 1
            counts['training_views'] += int(candidate['training'])
            counts['prompt_tokens'] += candidate['prompt_tokens']
            counts['completion_tokens'] += candidate['completion_tokens']
            maximum = max(maximum, candidate['total_tokens'])
    if progress is not None:
        progress({'state': 'token_audit', 'processed': len(seen), 'counts': dict(counts), 'acceptance': 'final'})
    fence()
    if _sha(source) != source_sha or _sha(views) != views_sha:
        raise ValueError('Staged source or views changed during exact token audit')
    if tokenizer_fingerprint(tokenizer) != original_tokenizer:
        raise ValueError('Tokenizer changed during exact audit')
    if implementation_identity() != implementation or capture_sha and _sha(capture_path) != capture_sha:
        raise ValueError('Token audit implementation changed during exact audit')
    manifest_path = dataset / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.is_file() else {}
    coverage = None
    if manifest.get('coverage_policy', {}).get('schema') == 'mined-corpus/1':
        with pq.ParquetFile(source) as records:
            expected = {row['record_id'] for batch in records.iter_batches(batch_size=256, columns=['record_id'])
                        for row in batch.to_pylist()}
        kept = set(accepted)
        covered = {candidate['record_id'] for candidate in candidates if candidate['view_id'] in kept}
        missing = expected - covered
        if missing:
            raise ValueError('Mined corpus token audit lost record coverage: ' + str(len(missing)))
        coverage = {'schema': 'mined-corpus-coverage/1', 'source_records': len(expected),
                    'eligible_records': len(covered), 'missing_records': 0}
    return {'schema': SCHEMA, 'policy': 'sft-admission/1', 'protocol': sft_serialization.PROTOCOL,
            'record_coverage': coverage,
            'implementation': implementation,
            'context_budget': asdict(context),
            'prompt_conflict_audit': {'schema': 'sft-prompt-conflicts/1',
                'fingerprint': 'sha256_compact_json_encoded_prompt_token_ids',
                'policy': 'exclude_all_conflicting_decision_classes', 'candidate_views': len(candidates),
                'conflicting_prompts': len(conflicting), 'excluded_views': conflict_views},
            'serialization_sha256': protocol_hash, 'tokenizer': identity, 'max_tokens': max_tokens,
            'source_sha256': source_sha, 'views_sha256': views_sha, 'considered_views': len(seen),
            'accepted_view_ids': accepted, 'excluded_views': excluded, 'max_total_tokens': maximum,
            'counts': counts, 'passed': bool(counts['training_views']), 'model_performance': 'unmeasured'}
