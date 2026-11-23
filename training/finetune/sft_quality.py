"""Portable data readiness for evidence-grounded SFT, separate from proof counts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

POLICY = 'sft-admission/1'
SCHEMA = 'sft-readiness/1'
PROTOCOL = 'sft_native_chat_v1'


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _read(path):
    if path.stat().st_size > 64 << 20:
        raise ValueError('SFT metadata exceeds its bound')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('SFT metadata must be an object')
    return value


def assess_manifest(manifest):
    blockers = []
    def object_field(name):
        value = manifest.get(name)
        return value if isinstance(value, dict) else {}
    readiness = object_field('security_readiness')
    counts = object_field('counts')
    tokenizer = object_field('tokenizer')
    token = object_field('token_audit')
    if (manifest.get('admission_policy') != POLICY or manifest.get('quality_profile') != 'sft'
            or manifest.get('serialization_protocol') != PROTOCOL or manifest.get('dataset_status') != 'frozen'
            or readiness.get('schema') != SCHEMA or readiness.get('admission_policy') != POLICY):
        blockers.append('sft_policy_identity_mismatch')
    if readiness.get('training_ready') is not True or readiness.get('blockers'):
        blockers.append('sft_dataset_not_ready')
    total = counts.get('records')
    if (type(total) is not int or total <= 0 or any(type(counts.get(k)) is not int or counts[k] != total
            for k in ('raw_records', 'audited_records', 'dispositions')) or readiness.get('counts') != counts):
        blockers.append('incomplete_all_record_audit')
    if type(counts.get('eligible_views')) is not int or counts['eligible_views'] <= 0:
        blockers.append('missing_sft_training_views')
    if (not isinstance(tokenizer.get('model_id'), str) or not tokenizer['model_id'].strip()
            or not re.fullmatch(r'[0-9a-f]{40}', str(tokenizer.get('revision') or ''))
            or not re.fullmatch(r'[0-9a-f]{64}', str(tokenizer.get('tokenizer_sha256') or ''))):
        blockers.append('missing_pinned_sft_tokenizer')
    if (token.get('schema') != 'sft-token-audit/1' or token.get('max_tokens') != 4096
            or type(token.get('max_total_tokens')) is not int or not 0 < token['max_total_tokens'] <= 4096
            or not re.fullmatch(r'[0-9a-f]{64}', str(token.get('sha256') or ''))):
        blockers.append('missing_exact_sft_token_audit')
    return {'schema': 'training-readiness/1', 'allowed': not blockers, 'blockers': blockers,
            'admission_policy': POLICY, 'integrity': {'state': 'blocked' if blockers else 'passed', 'blockers': list(blockers)},
            'security_performance': {'state': 'unmeasured'},
            'full_run': {'state': 'blocked' if blockers else 'passed', 'blockers': list(blockers), 'limitations': []},
            'diagnostics': {'state': 'blocked' if blockers else 'pending'},
            'counts': counts, 'scope': 'Evidence-grounded supervised training; execution proof and held-out model performance are separate.'}


def data_preflight(folder, manifest):
    folder = Path(folder)
    result = assess_manifest(manifest)
    blockers = list(result['blockers'])
    try:
        if folder.is_symlink() or getattr(folder.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('Linked SFT dataset directory')
        files = manifest['files']
        required = {'records.parquet', 'views.parquet', 'instruction_views.jsonl', 'audit_report.json',
                    'token_audit.json', 'dispositions.jsonl', 'review_queue.jsonl'}
        if not isinstance(files, dict) or not required <= files.keys():
            raise ValueError('Missing SFT artifact inventory')
        for name, expected in files.items():
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts or not name:
                raise ValueError('Unsafe SFT artifact path')
            current = folder
            for part in relative.parts:
                current = current / part
                if current.is_symlink() or getattr(current.lstat(), 'st_file_attributes', 0) & 0x400:
                    raise ValueError('Linked SFT artifact')
            if (not current.is_file() or current.stat().st_size != expected['bytes']
                    or _hash(current) != expected['sha256']):
                raise ValueError('Changed SFT artifact: ' + name)
        if {p.name for p in folder.iterdir() if p.is_file() or p.is_symlink()} != set(files) | {'manifest.json'}:
            raise ValueError('Unregistered SFT dataset artifact')
        report = _read(folder / 'audit_report.json')
        token = _read(folder / 'token_audit.json')
        if report.get('counts') != manifest['counts'] or report.get('policy') != POLICY:
            raise ValueError('SFT audit report mismatch')
        if (token.get('schema') != 'sft-token-audit/1' or token.get('policy') != POLICY
                or token.get('protocol') != PROTOCOL or token.get('passed') is not True
                or token.get('max_tokens') != 4096 or token.get('tokenizer') != manifest['tokenizer']
                or token.get('max_total_tokens') != manifest['token_audit']['max_total_tokens']
                or token.get('source_sha256') != files['records.parquet']['sha256']
                or _hash(folder / 'token_audit.json') != manifest['token_audit']['sha256']):
            raise ValueError('SFT token audit binding mismatch')
        accepted = token.get('accepted_view_ids')
        if not isinstance(accepted, list) or len(set(accepted)) != len(accepted):
            raise ValueError('Invalid accepted SFT view identities')
        seen, positives, negatives, covered_records = set(), set(), set(), set()
        with (folder / 'instruction_views.jsonl').open(encoding='utf-8') as stream:
            for line in stream:
                if len(line.encode()) > 1 << 20:
                    raise ValueError('SFT view exceeds its portable bound')
                row = json.loads(line)
                key = row['view_id']
                if key in seen or row.get('eligible') is not True:
                    raise ValueError('Duplicate or ineligible published SFT view')
                seen.add(key)
                if (row.get('weight') or 0) > 0:
                    covered_records.add(row['record_id'])
                if row['split'] == 'train' and row['task'] == 'discovery':
                    response = json.loads(row['response_json'])
                    pair = (row['record_id'], row['family_id'])
                    if response.get('status') == 'finding':
                        positives.add(pair)
                    elif response.get('status') == 'no_finding':
                        negatives.add(pair)
        if seen != set(accepted) or len(seen) != manifest['counts']['eligible_views']:
            raise ValueError('Published SFT token membership mismatch')
        corpus = manifest.get('coverage_policy', {}).get('schema') == 'mined-corpus/1'
        if not corpus and not positives & negatives:
            raise ValueError('Missing matched SFT training pair')
        dispositions = set()
        with (folder / 'dispositions.jsonl').open(encoding='utf-8') as stream:
            for line in stream:
                if len(line.encode()) > 1 << 20:
                    raise ValueError('SFT disposition exceeds its portable bound')
                row = json.loads(line)
                key = row['record_id']
                if not isinstance(key, str) or key in dispositions:
                    raise ValueError('Duplicate SFT record disposition')
                dispositions.add(key)
        if len(dispositions) != manifest['counts']['records'] or any(rid not in dispositions for rid, _ in positives | negatives):
            raise ValueError('SFT dispositions do not cover all records')
        if corpus and covered_records != dispositions:
            raise ValueError('Mined corpus views do not cover every record')
    except (OSError, ValueError, KeyError, TypeError) as error:
        blockers.append('sft_artifact_integrity:' + str(error)[:300])
    blockers = list(dict.fromkeys(blockers))
    result.update(allowed=not blockers, blockers=blockers,
                  integrity={'state': 'blocked' if blockers else 'passed', 'blockers': blockers})
    result['full_run'] = {'state': 'blocked' if blockers else 'passed', 'blockers': blockers, 'limitations': []}
    result['diagnostics'] = {'state': 'blocked' if blockers else 'pending'}
    return result
