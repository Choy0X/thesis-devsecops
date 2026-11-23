"""Data-only evaluation preflight. Never imports torch, models or application storage.

An independent benchmark is a frozen, hash-pinned reviewed artifact. Its records
and views use the existing SFT format, with authenticated source_snapshots added
to each record. Independence is checked against both recorded source revisions
and the training prompts, not historical split assignments.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from collections import defaultdict

from training.finetune import sft_contracts

SCHEMA = 'security-benchmark/1'
ROLES = {('val', 'selection'), ('val', 'calibration'), ('test', 'untouched_test')}
LABELS = {'finding': 'vulnerability_supported', 'no_finding': 'no_supported_finding_in_scope',
          'insufficient_context': 'insufficient_context'}
IDENTITIES = ('record_id', 'raw_pair_key', 'repository_identity', 'repository_family', 'issue_group_id',
              'fix_family_id', 'fix_commit', 'advisory_aliases', 'clone_group_ids', 'lineage_parent_ids',
              'source_record_hashes', 'file_blob_hashes', 'before_tree_hash', 'after_tree_hash')
CODE = ('vulnerable_code', 'fixed_code', 'context_before', 'context_after', 'context_text')


class EvaluationBlocked(ValueError):
    def __init__(self, code, detail, remediation='Supply an independently reviewed, frozen benchmark with authenticated source snapshots and an overlap review.'):
        super().__init__(detail)
        self.code, self.detail, self.remediation = code, detail, remediation

    def as_dict(self):
        return {'schema': 'evaluation-readiness/1', 'state': 'blocked', 'reason_code': self.code,
                'detail': self.detail, 'remediation': self.remediation}


def sha_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _strings(value):
    value = _json(value)
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return [str(value)] if value not in (None, '') else []


def _identity_space(key):
    if key in ('record_id', 'lineage_parent_ids', 'raw_pair_key', 'source_record_hashes'):
        return 'source_lineage'
    if key in ('issue_group_id', 'fix_family_id'):
        return 'issue_family'
    return key


def _identity_value(key, value):
    value = value.casefold().rstrip('/')
    if key in ('repository_identity', 'repository_family'):
        value = re.sub(r'^(?:https?://|git://|ssh://|git@)', '', value).removesuffix('.git').replace('github.com:', 'github.com/')
    return value


def _rows(path, columns=None):
    import pyarrow.parquet as pq
    with pq.ParquetFile(path) as source:
        columns = [name for name in columns if name in source.schema_arrow.names] if columns is not None else None
        for batch in source.iter_batches(batch_size=128, columns=columns):
            yield from batch.to_pylist()


def _identity(manifest, path):
    return {'version': manifest.get('tag'), 'status': manifest.get('dataset_status', 'unfrozen'),
            'manifest_sha256': sha_file(path / 'manifest.json')}


def _member(root, name):
    rel = pathlib.PurePosixPath(name)
    if not name or rel.is_absolute() or '\\' in name or ':' in name or '..' in rel.parts:
        raise EvaluationBlocked('invalid_benchmark', f'Unsafe benchmark member: {name!r}')
    path = root.joinpath(*rel.parts)
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise EvaluationBlocked('invalid_benchmark', f'Missing or unsafe benchmark member: {name}')
    return path


def _tokens(text):
    # Preserve string literals and identifiers; ignore only whitespace.
    return re.findall(r'\w+|[^\w\s]', text)


def _context_matches(view, snapshots, root, revision, language):
    """Re-render source spans. No arbitrary unverified target can be appended."""
    available = {source['path']: source for source in snapshots if source['revision'] == revision}
    spans = _json(view.get('context_sources'))
    if spans is None:
        return any(view.get('context_text') == _member(root, name).read_text(encoding='utf-8') for name in available)
    if not isinstance(spans, list) or not spans:
        return False
    target, items = None, []
    for index, span in enumerate(spans):
        if not isinstance(span, dict) or span.get('snapshot') not in available:
            return False
        source = available[span['snapshot']]
        body = _member(root, source['path']).read_bytes()
        start, end = span.get('byte_start'), span.get('byte_end')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(body):
            return False
        try:
            code = body[start:end].decode('utf-8')
        except UnicodeDecodeError:
            return False
        kind = span.get('kind')
        entry = {'kind': kind, 'path': source['source_path'], 'language': language, 'code': code}
        if index == 0:
            if kind != 'target':
                return False
            target = entry
        elif kind in ('caller', 'callee', 'import', 'config', 'entrypoint', 'dependency'):
            items.append(entry)
        else:
            return False
    from training.finetune import serialization
    return view.get('context_text') == serialization.render_context(target, items)


def audit_overlap(training_dir, evaluation_dir, records=None, views=None):
    """Conservative repository/family/alias/revision and source-window overlap.

    A shared repository is rejected even if different functions were selected.
    Sixty-four token windows detect reused code in larger files and prompts;
    short complete snippets are compared exactly after whitespace normalization.
    Fork and generated-origin review is a separate required adjudication record.
    """
    train, evaluation = pathlib.Path(training_dir), pathlib.Path(evaluation_dir)
    records = list(records if records is not None else _rows(evaluation / 'records.parquet'))
    views = list(views if views is not None else _rows(evaluation / 'views.parquet'))
    identities = defaultdict(set)
    candidate_texts = set()
    for record in records:
        for key in IDENTITIES:
            identities[_identity_space(key)].update(_identity_value(key, text) for text in _strings(record.get(key)))
        for key in CODE:
            candidate_texts.update(_strings(record.get(key)))
        for snapshot in _json(record.get('source_snapshots')) or []:
            candidate_texts.add(_member(evaluation, snapshot['path']).read_text(encoding='utf-8'))
    for view in views:
        candidate_texts.add(view.get('context_text') or '')
    normalized = {' '.join(_tokens(text)) for text in candidate_texts if text.strip()}
    short = {text for text in normalized if len(text.split(' ')) < 64}
    windows = set()
    for text in normalized:
        tokens = text.split(' ')
        windows.update(' '.join(tokens[i:i + 64]) for i in range(max(0, len(tokens) - 63)))
    overlaps = set()
    def scan(row):
        for key in IDENTITIES:
            for value in _strings(row.get(key)):
                if _identity_value(key, value) in identities[_identity_space(key)]:
                    overlaps.add(key)
        for key in CODE:
            for text in _strings(row.get(key)):
                tokens = _tokens(text)
                value = ' '.join(tokens)
                if value and value in normalized:
                    overlaps.add('normalized_source')
                if any((' ' + item + ' ') in (' ' + value + ' ') for item in short):
                    overlaps.add('normalized_source_fragment')
                if 4 <= len(tokens) < 64 and any((' ' + value + ' ') in (' ' + text + ' ') for text in normalized):
                    overlaps.add('training_fragment_in_benchmark')
                if windows and any(' '.join(tokens[i:i + 64]) in windows for i in range(max(0, len(tokens) - 63))):
                    overlaps.add('source_window_64_tokens')
    count = 0
    for row in _rows(train / 'records.parquet', IDENTITIES + CODE):
        scan(row)
        count += 1
    for row in _rows(train / 'views.parquet', CODE + ('split',)):
        if row.get('split') == 'train':
            scan(row)
    return {'schema': 'evaluation-overlap/1', 'state': 'failed' if overlaps else 'passed',
            'matches': sorted(overlaps), 'training_records_checked': count,
            'evaluation_records_checked': len(records), 'source_window_tokens': 64,
            'short_training_fragment_minimum_tokens': 4,
            'scope': 'record identities, both source revisions, benchmark snapshots and published training prompts',
            'limitations': ['Base-model pretraining contamination cannot be audited locally.',
                            'Fork aliases and generated derivatives also require recorded independent review.']}


def preflight(training_dir, cfg, evaluation_dir=None, expected_manifest_sha256=None,
              split='val', role='selection', tasks=('discovery', 'repair')):
    train = pathlib.Path(training_dir)
    try:
        training = json.loads((train / 'manifest.json').read_text(encoding='utf-8'))
        if sha_file(train / 'manifest.json') != cfg.data.manifest_sha256:
            raise EvaluationBlocked('training_identity_mismatch', 'Training manifest differs from the original resolved training configuration.')
        if (split, role) not in ROLES:
            raise EvaluationBlocked('invalid_partition', 'Evaluation requires selection, calibration or explicitly untouched test roles.')
        if not tasks or set(tasks) - {'discovery', 'repair'}:
            raise EvaluationBlocked('empty_tasks', 'No supported eligible generation tasks requested. Choose discovery and/or repair.')
        independent = evaluation_dir is not None
        root = pathlib.Path(evaluation_dir) if independent else train
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        if not independent and manifest.get('coverage_policy', {}).get('schema') == 'mined-corpus/1':
            raise EvaluationBlocked('unsupported_corpus',
                'This mined source-learning corpus assigns all records to training and supplies no held-out security ground truth. '
                'Training loss and source-task exposure cannot measure security detection or repair.',
                'Keep v29 frozen. Supply --evaluation-data DIR and --evaluation-manifest-sha256 SHA for a separately reviewed nonoverlapping benchmark.')
        if independent:
            for name in ('records.parquet', 'views.parquet'):
                expected = (training.get('files') or {}).get(name) or {}
                path = train / name
                if not path.is_file() or sha_file(path) != expected.get('sha256') or path.stat().st_size != expected.get('bytes'):
                    raise EvaluationBlocked('training_identity_mismatch', f'Original training exposure member is missing or differs from its frozen manifest: {name}')
            if not expected_manifest_sha256:
                raise EvaluationBlocked('missing_benchmark_identity', 'Independent evaluation requires an expected benchmark manifest SHA256.')
            if sha_file(root / 'manifest.json') != expected_manifest_sha256:
                raise EvaluationBlocked('benchmark_identity_mismatch', 'Independent benchmark manifest differs from its pinned SHA256.')
            if manifest.get('schema') != SCHEMA or manifest.get('dataset_status') != 'frozen':
                raise EvaluationBlocked('invalid_benchmark', 'Independent evaluation requires a frozen security-benchmark/1 manifest.')
            files = manifest.get('files') or {}
            if not {'records.parquet', 'views.parquet'} <= files.keys():
                raise EvaluationBlocked('invalid_benchmark', 'Benchmark must authenticate records.parquet and views.parquet.')
            for name, identity in files.items():
                path = _member(root, name)
                if sha_file(path) != identity.get('sha256') or path.stat().st_size != identity.get('bytes'):
                    raise EvaluationBlocked('invalid_benchmark', f'Benchmark member identity differs: {name}')
            policy = manifest.get('benchmark') or {}
            review = policy.get('overlap_review') or {}
            if (policy.get('context_acquisition') != 'deployment_retrieval' or policy.get('adjudication') != 'independent_review'
                    or policy.get('frozen_before_tuning') is not True or not review.get('reviewer')
                    or review.get('fork_families_reviewed') is not True or review.get('generated_derivatives_reviewed') is not True):
                raise EvaluationBlocked('invalid_benchmark', 'Benchmark lacks deployment context acquisition, independent adjudication, freeze or fork/derivative review provenance.')
        records = list(_rows(root / 'records.parquet'))
        by_id = {r['record_id']: r for r in records}
        if independent and (len(by_id) != len(records) or any(not isinstance(key, str) or not key.strip() for key in by_id)):
            raise EvaluationBlocked('invalid_benchmark', 'Duplicate or missing benchmark record identity.')
        all_views = list(_rows(root / 'views.parquet'))
        partitions = defaultdict(set)
        seen_ids = set()
        for view in all_views:
            if not view.get('eligible') or (view.get('weight') or 0) <= 0:
                continue
            if independent:
                if view['view_id'] in seen_ids:
                    raise EvaluationBlocked('invalid_benchmark', 'Duplicate benchmark view identity.')
                seen_ids.add(view['view_id'])
                record = by_id.get(view['record_id'])
                if not record:
                    raise EvaluationBlocked('missing_labels', 'Benchmark view has no reviewed source record.')
                label = view.get('reviewed_label_state') or record.get('label_state')
                if view.get('reviewed_label_state') and not view.get('reviewed_by'):
                    raise EvaluationBlocked('missing_labels', 'A separately scoped view label requires its own reviewer identity.')
                response = _json(view.get('response_json'))
                if (not record.get('reviewer_or_verifier_identity') or record.get('review_status') not in ('reviewed', 'adjudicated', 'human_reviewed')
                        or not isinstance(response, dict) or LABELS.get(response.get('status')) != label
                        or sft_contracts.validate_response(view['task'], response)):
                    raise EvaluationBlocked('missing_labels', 'Benchmark eligible views require adjudicated task labels matching valid reference responses.')
                if not record.get('repository_identity') or not record.get('relevant_parent_commit'):
                    raise EvaluationBlocked('invalid_benchmark', 'Benchmark records require repository and exact source revision identities.')
                snapshots = _json(record.get('source_snapshots'))
                if not isinstance(snapshots, list) or not snapshots:
                    raise EvaluationBlocked('invalid_benchmark', 'Benchmark record has no authenticated source snapshots.')
                revision = view.get('source_revision') or record['relevant_parent_commit']
                if revision not in (record['relevant_parent_commit'], record.get('fix_commit')):
                    raise EvaluationBlocked('invalid_benchmark', 'Benchmark view requests an unknown source revision.')
                source_text = []
                for source in snapshots:
                    path = _member(root, source['path'])
                    if (source['path'] not in manifest['files'] or sha_file(path) != source.get('sha256')
                            or source.get('revision') not in (record['relevant_parent_commit'], record.get('fix_commit')) or not source.get('source_path')):
                        raise EvaluationBlocked('invalid_benchmark', 'Benchmark source snapshot has stale or wrong-revision provenance.')
                    if source.get('revision') == revision:
                        source_text.append(path.read_text(encoding='utf-8'))
                if not source_text:
                    raise EvaluationBlocked('invalid_benchmark', 'Benchmark view has no source snapshot at its requested revision.')
                if not _context_matches(view, snapshots, root, revision, record.get('language')):
                    raise EvaluationBlocked('invalid_benchmark', 'Benchmark context must exactly render authenticated source spans at the requested revision.')
                for evidence in response.get('evidence', []):
                    quote = evidence.get('quote', '')
                    if not quote or quote not in view.get('context_text', '') or not any(quote in text for text in source_text):
                        raise EvaluationBlocked('invalid_benchmark', 'Reference evidence quote is absent from rendered context or source snapshot.')
                partition = (view.get('split'), view.get('val_role'))
                if partition not in ROLES:
                    raise EvaluationBlocked('invalid_partition', 'Independent benchmarks contain only explicit held-out partition roles.')
                for source in snapshots:
                    partitions[('source_snapshot', source['sha256'])].add(partition)
                partitions[('normalized_context', hashlib.sha256(' '.join(_tokens(view['context_text'])).encode()).hexdigest())].add(partition)
                groups = [(key, value) for key in ('record_id', 'repository_identity', 'repository_family', 'issue_group_id', 'fix_family_id',
                                                    'advisory_aliases', 'clone_group_ids', 'lineage_parent_ids')
                          for value in _strings(record.get(key))]
                for group in groups:
                    key, value = group
                    partitions[(_identity_space(key), _identity_value(key, value))].add(partition)
        if any(len(values) > 1 for values in partitions.values()):
            raise EvaluationBlocked('partition_overlap', 'A repository, issue, pair, alias or derivative group spans multiple evaluation roles.')
        views = sorted([v for v in all_views if v.get('eligible') and (v.get('weight') or 0) > 0
                        and v.get('split') == split and v.get('val_role') == role and v.get('task') in tasks], key=lambda v: v['view_id'])
        if not views:
            raise EvaluationBlocked('empty_tasks', 'No eligible labeled views match the requested tasks and partition role.')
        overlap = audit_overlap(train, root, records, all_views) if independent else {'state': 'legacy_same_dataset', 'independence': 'not_established'}
        if overlap['state'] == 'failed':
            raise EvaluationBlocked('training_overlap', 'Independent benchmark overlaps training exposure: ' + ', '.join(overlap['matches']))
        return {'schema': 'evaluation-readiness/1', 'state': 'ready', 'independent': independent,
                'training_dataset': _identity(training, train), 'evaluation_dataset': _identity(manifest, root),
                'manifest': manifest, 'views': views, 'overlap_audit': overlap}
    except EvaluationBlocked:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        code = 'missing_benchmark' if evaluation_dir is not None and not pathlib.Path(evaluation_dir).exists() else 'invalid_benchmark'
        raise EvaluationBlocked(code, f'Evaluation data cannot be verified: {error}') from error
