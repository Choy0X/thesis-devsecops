"""Verify a published security-trial-dataset/3 directory (grounded_v30).

This module lives under training/ because the Colab bundle carries only training/. The runner must verify the data it was given without the
application package, which is not in the bundle. The builder (app.backend.pipeline.grounded_dataset) uses the same functions.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq

SCHEMA = 'security-trial-dataset/3'


def file_entry(path):
    data = Path(path).read_bytes()
    return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def balance_report(data_dir):
    """Finding share of the training discovery views by scope form and by scope class, recomputed from the published rows."""
    rows = pq.read_table(data_dir / 'views.parquet', columns=['partition_role', 'task', 'scope_form', 'scope_classes_json', 'response_json']).to_pylist()
    forms, classes = {}, {}
    for row in rows:
        if row['partition_role'] != 'train' or row['task'] != 'discovery':
            continue
        slot = 0 if json.loads(row['response_json']).get('status') == 'finding' else 1
        forms.setdefault(row['scope_form'], [0, 0])[slot] += 1
        for cwe in json.loads(row['scope_classes_json']):
            classes.setdefault(cwe, [0, 0])[slot] += 1
    return forms, classes


def check_balance(data_dir, policy):
    """Refuse a publication in which the form of the prompt, or the class named in the scope, predicts the answer (security_chat_v3 datasets)."""
    forms, classes = balance_report(data_dir)
    low, high = policy['form_share']
    for form, (findings, others) in sorted(forms.items()):
        if findings + others and not low <= findings / (findings + others) <= high:
            raise ValueError(f'prompt form {form!r} predicts the answer: {findings} findings, {others} no_findings in the training views')
    low, high = policy['class_share']
    for cwe, (findings, others) in sorted(classes.items()):
        if min(findings, others) < policy['class_min_each'] or not low <= findings / (findings + others) <= high:
            raise ValueError(f'scope class {cwe} predicts the answer: {findings} findings, {others} no_findings in the training views')
    return {'forms': forms, 'classes': classes}


def check_claims(data_dir):
    """Refuse a publication with an answer that talks about the fix or names code the prompt does not show (grounded_claim_gate, strict). Counts every view
    of every published partition: the model is trained on the train rows and judged on the others."""
    from training.finetune import grounded_claim_gate as claim_gate
    rows = pq.read_table(Path(data_dir) / 'views.parquet', columns=['view_id', 'partition_role', 'context_text', 'response_json']).to_pylist()
    bad = []
    for row in rows:
        ok, why = claim_gate.gate(json.loads(row['response_json']), row['context_text'])
        if not ok:
            bad.append((row['view_id'], why[0]))
    if bad:
        raise ValueError(f'{len(bad)} of {len(rows)} answers fail the claim gate, for example {bad[0][0]}: {bad[0][1]}')
    return len(rows)


def verify(data_dir):
    data_dir = Path(data_dir)
    manifest = json.loads((data_dir / 'dataset_manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != SCHEMA or manifest.get('dataset_status') != 'frozen':
        raise ValueError('not a frozen security-trial-dataset/3 publication')
    for name, entry in manifest['files'].items():
        path = data_dir / name
        if not path.is_file() or file_entry(path) != entry:
            raise ValueError(f'file hash mismatch: {name}')
    rows = pq.read_table(data_dir / 'views.parquet', columns=['partition_role', 'view_id']).to_pylist()
    if any(r['partition_role'] == 'blind_test' for r in rows):
        raise ValueError('blind views must not be inside the main publication')
    if len({r['view_id'] for r in rows}) != len(rows):
        raise ValueError('duplicate view ids')
    expected = sum(v for k, v in manifest['counts']['views_by_role'].items() if k != 'blind_test')
    if len(rows) != expected:
        raise ValueError('view count differs from the manifest')
    if manifest.get('balance_policy'):
        check_balance(data_dir, manifest['balance_policy'])
    if manifest.get('claim_gate') == 'strict':
        check_claims(data_dir)
    return {'state': 'verified', 'manifest_sha256': hashlib.sha256((data_dir / 'dataset_manifest.json').read_bytes()).hexdigest(), 'views': len(rows)}
