"""Reassign an already verified mined corpus to training without regenerating code.

Only partition metadata changes. Published parent artifacts remain immutable, and
the resulting examples receive a fresh exact-token audit before publication.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json
from pathlib import Path
import shutil
import uuid

from . import sft_dataset_build as core, mined_sft, versions, bounded_parquet, oslock

POLICY = 'all_records_train/1'


def _check(check):
    if check is not None and check() is False:
        raise InterruptedError('All-record training preparation cancelled')


def _rewrite_parquet(source, target, replacements, *, check):
    import pyarrow as pa
    import pyarrow.parquet as pq
    with pq.ParquetFile(source) as reader:
        with bounded_parquet.BoundedParquetWriter(target, reader.schema_arrow) as writer:
            for batch in reader.iter_batches(batch_size=128):
                _check(check)
                table = pa.Table.from_batches([batch])
                for name, value in replacements.items():
                    if name in table.column_names:
                        index = table.schema.get_field_index(name)
                        table = table.set_column(index, table.schema.field(index),
                            pa.array([value] * table.num_rows, type=table.schema.field(index).type))
                writer.write_table(table)


def training_records(folder):
    """Verify physical rows, rather than relying on a manifest's split counts."""
    import pyarrow.parquet as pq
    records, views = set(), set()
    with pq.ParquetFile(folder / 'records.parquet') as reader:
        for batch in reader.iter_batches(batch_size=1024, columns=['record_id', 'split']):
            for row in batch.to_pylist():
                if row['split'] != 'train' or row['record_id'] in records:
                    raise ValueError('Every source record must occur once in the training split')
                records.add(row['record_id'])
    with pq.ParquetFile(folder / 'views.parquet') as reader:
        for batch in reader.iter_batches(batch_size=1024,
                columns=['record_id', 'split', 'role', 'val_role', 'eligible', 'weight']):
            for row in batch.to_pylist():
                if (any(row[key] != 'train' for key in ('split', 'role', 'val_role'))
                        or row['eligible'] is not True or not row['weight'] > 0):
                    raise ValueError('Every example must be enabled for training')
                views.add(row['record_id'])
    if records != views:
        raise ValueError('Training examples do not cover every source record')
    return records


def stage(source_tag='v28', *, root=None, progress=None, check=None):
    root = Path(core.build_catalog._root(root)).absolute()
    with oslock.FileLock(core._safe(root, root / '.sft-build.lock'), note=False):
        return _stage(source_tag, root=root, progress=progress, check=check)


def _stage(source_tag, *, root, progress, check):
    import pyarrow as pa
    import pyarrow.parquet as pq
    _check(check)
    parent = core._capture(source_tag, root)
    if parent['manifest'].get('coverage_policy', {}).get('schema') != mined_sft.POLICY:
        raise ValueError('All-training reassignment requires a published mined corpus')
    source = root / ('dataset_' + source_tag)
    policy = core._policy(mined_sft.admit_record, mined_sft.build_views)
    policy['files'][str(Path(__file__).resolve())] = versions._sha256(Path(__file__))
    capture = {'schema': core.SCHEMA, 'source': parent, 'authority': parent, 'policy': policy,
               'partition_transform': {'schema': POLICY, 'supervision': 'unchanged_from_frozen_parent'}}
    key = core._digest(capture)
    workspace = core._safe(root, root / '.sft_build' / ('all-training-' + key))
    workspace.mkdir(parents=True, exist_ok=True)
    owner_file = workspace / 'owner.json'
    if owner_file.exists():
        state = json.loads(owner_file.read_text(encoding='utf-8'))
        if state.get('capture') != capture:
            raise ValueError('All-training staging ownership differs')
        if state.get('complete'):
            core._verify_inventory(workspace / 'artifact', state['inventory'])
            return core._result(state, workspace)
    else:
        if any(workspace.iterdir()):
            raise ValueError('Unowned all-training staging directory')
        owner = 'all-training-' + uuid.uuid4().hex
        tag = versions.reserve_version(root=root, owner=owner, minimum=int(source_tag[1:]) + 1)
        state = {'owner': owner, 'tag': tag, 'capture': capture, 'source_identity': key, 'complete': False}
        core._write(owner_file, state)
    artifact = workspace / 'artifact'
    artifact.mkdir(exist_ok=True)
    rewritten = {'manifest.json', 'source_capture.json', 'audit_report.json', 'stats.json',
                 'records.parquet', 'views.parquet', 'instruction_views.jsonl', 'dispositions.jsonl',
                 'review_queue.jsonl', 'security_partitions.json', 'split_manifest.json', 'groups.parquet',
                 'development_exposure.json', 'token_audit.json', 'token_exclusions.jsonl'}
    for name in parent['files']:
        if name not in rewritten:
            target = core._safe(artifact, artifact / name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
    for name in ('token_audit.json', 'token_exclusions.jsonl'):
        if (source / name).is_file():
            shutil.copyfile(source / name, artifact / ('parent_' + name))
    _rewrite_parquet(source / 'records.parquet', artifact / 'records.parquet', {'split': 'train'}, check=check)
    _rewrite_parquet(source / 'groups.parquet', artifact / 'groups.parquet',
                     {'split': 'train', 'role': 'train', 'reason': POLICY}, check=check)
    with pq.ParquetFile(source / 'views.parquet') as reader, \
            bounded_parquet.BoundedParquetWriter(artifact / 'views.parquet', reader.schema_arrow) as writer, \
            (artifact / 'instruction_views.jsonl').open('w', encoding='utf-8', newline='\n') as native:
        processed = 0
        for batch in reader.iter_batches(batch_size=256):
            _check(check)
            rows = batch.to_pylist()
            for row in rows:
                row.update(split='train', val_role='train', role='train', eligible=True)
                if not row['weight'] > 0:
                    raise ValueError('Parent corpus contains a zero-weight training example')
                native.write(core._json({**row, 'messages': json.loads(row['messages_json']),
                                       'response': json.loads(row['response_json'])}) + '\n')
            writer.write_table(pa.Table.from_pylist(rows, schema=reader.schema_arrow))
            processed += len(rows)
            if progress:
                progress(state='all_training', processed=processed,
                         total=parent['manifest']['counts']['eligible_views'])
    partitions = json.loads((source / 'security_partitions.json').read_text(encoding='utf-8'))
    for assignment in partitions['records'].values():
        assignment['previous_published_assignment'] = {k: assignment.get(k) for k in ('split', 'role', 'quarantined')}
        assignment.update(split='train', role='train', quarantined=False)
    total = len(partitions['records'])
    partitions.update(counts={'train': total}, partition_policy=POLICY, evaluation_available=False,
        test_is_untouched=False, eligible_test_is_untouched=False, development_exposed_test_records=[])
    core._write(artifact / 'security_partitions.json', partitions)
    split = json.loads((source / 'split_manifest.json').read_text(encoding='utf-8'))
    for group in split.get('groups', {}).values():
        group.update(split='train', role='train', overrides={}, reason=POLICY)
    split['partition_policy'] = POLICY
    split.setdefault('append_source', {})['historical_assignments'] = 'Metadata only; all records train'
    core._write(artifact / 'split_manifest.json', split)
    exposure = json.loads((source / 'development_exposure.json').read_text(encoding='utf-8'))
    exposure.update(excluded_untouched_test_records=[], evaluation_available=False, partition_policy=POLICY)
    core._write(artifact / 'development_exposure.json', exposure)
    with (artifact / 'dispositions.jsonl').open('w', encoding='utf-8', newline='\n') as dispositions, \
            (artifact / 'review_queue.jsonl').open('w', encoding='utf-8', newline='\n') as review:
        for row in core._jsonl(source / 'dispositions.jsonl'):
            _check(check)
            row['previous_published_assignment'] = copy.deepcopy(row['assignment'])
            row['assignment'] = partitions['records'][row['record_id']]
            row['training_eligibility'] = 'eligible'
            row.setdefault('evaluation_exposure', {})['untouched_test_eligible'] = False
            if 'published_view_eligibility' in row:
                row['published_view_eligibility'].update(train_allowed=True, evaluation_allowed=False)
            value = core._json(row) + '\n'
            dispositions.write(value)
            if row.get('review_required'):
                review.write(value)
    report = core._report(core._jsonl(artifact / 'dispositions.jsonl'),
                          core._jsonl(artifact / 'instruction_views.jsonl'), total)
    report['counts'].update(training_records=total, held_out_records=0)
    if report['counts']['eligible_records'] != total or len(training_records(artifact)) != total:
        raise ValueError('All-training transformation lost record coverage')
    core._write(artifact / 'audit_report.json', report)
    core._write(artifact / 'stats.json', report)
    core._write(artifact / 'source_capture.json', capture)
    manifest = copy.deepcopy(parent['manifest'])
    manifest.update(tag=state['tag'], source={'frozen_parent': source_tag, 'snapshot_sha256': key},
        coverage_policy=mined_sft.coverage_policy(), counts=report['counts'], tasks=report['tasks'],
        evaluation={'available': False, 'held_out_records': 0, 'reason': 'all_records_training'},
        partition_transform={'schema': POLICY, 'parent': source_tag, 'supervision': 'unchanged'})
    manifest.pop('token_audit', None)
    manifest['security_readiness'].update(counts=report['counts'], training_ready=False,
                                          blockers=['exact_token_audit_pending'])
    manifest['files'] = {name: {'sha256': sha, 'bytes': (artifact / name).stat().st_size}
                         for name, sha in core._inventory(artifact).items()}
    core._write(artifact / 'manifest.json', manifest)
    state.update(complete=True, inventory=core._inventory(artifact))
    core._write(owner_file, state)
    return core._result(state, workspace)


def publish(staged, audit, *, root=None, check=None):
    root = Path(core.build_catalog._root(root)).absolute()
    workspace = core._safe(root, Path(staged['workspace']))
    state = json.loads((workspace / 'owner.json').read_text(encoding='utf-8'))
    if (staged['owner'] != state['owner'] or not state['complete']
            or staged.get('source_identity') != state['source_identity'] or staged.get('tag') != state['tag']):
        raise ValueError('All-training staging ownership mismatch')
    folder = workspace / 'artifact'
    with oslock.FileLock(root / '.sft-build.lock', note=False):
        final = root / ('dataset_' + state['tag'])
        if final.exists():
            # A crash can happen after the atomic move but before CLI receipt
            # writing. The retained reservation/intent verifies the exact files.
            final = versions.recover_publication(state['tag'], root, owner=state['owner'])
            manifest = json.loads((final / 'manifest.json').read_text(encoding='utf-8'))
            if manifest.get('source', {}).get('snapshot_sha256') != state['source_identity']:
                raise ValueError('Recovered publication belongs to another source capture')
            core.build_catalog.register_artifact(final, manifest, root=root)
            state.update(published=True, published_path=str(final))
            core._write(workspace / 'owner.json', state)
            return core._result(state, workspace, published=True, path=final)
        core._verify_inventory(folder, state['inventory'])
        from .sft_token_audit import implementation_identity
        implementation = implementation_identity()
        if (audit.get('implementation') != implementation or audit.get('passed') is not True
                or audit.get('schema') != 'sft-token-audit/1' or audit.get('policy') != core.POLICY
                or audit.get('protocol') != core.PROTOCOL or audit.get('max_tokens') != 4096
                or not 0 < audit.get('max_total_tokens', 0) <= 4096
                or audit.get('source_sha256') != state['inventory']['records.parquet']
                or audit.get('views_sha256') != state['inventory']['views.parquet']):
            raise ValueError('All-training exact-token audit mismatch')
        membership = core._verified_views(folder)
        if (audit.get('excluded_views') or set(audit['accepted_view_ids']) != set(membership)
                or len(audit['accepted_view_ids']) != len(membership)
                or audit.get('considered_views') != len(membership)
                or audit['counts']['training_views'] != len(membership)):
            raise ValueError('All-training token audit must accept every reassigned example')
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        total = manifest['counts']['records']
        if (len(training_records(folder)) != total
                or audit.get('tokenizer') != state['capture']['source']['manifest'].get('tokenizer')
                or any(state['capture']['policy']['files'].get(name) != sha
                       for name, sha in implementation['files'].items())):
            raise ValueError('All-training record membership or tokenizer identity mismatch')
        if audit.get('record_coverage') != {'schema': 'mined-corpus-coverage/1',
                'source_records': total, 'eligible_records': total, 'missing_records': 0}:
            raise ValueError('All-training token audit lacks full record coverage')
        # Keep the audited stage unchanged so every failure before commit can
        # resume with the same source, token receipt, version and owner.
        ready = workspace / ('publication-' + uuid.uuid4().hex)
        shutil.copytree(folder, ready)
        core._verify_inventory(ready, state['inventory'], 'copied staging')
        report = json.loads((folder / 'audit_report.json').read_text(encoding='utf-8'))
        report.update(token_audit='passed', token_counts=audit['counts'])
        core._write(ready / 'audit_report.json', report)
        core._write(ready / 'stats.json', report)
        core._write(ready / 'token_audit.json', audit)
        (ready / 'token_exclusions.jsonl').write_text('', encoding='utf-8')
        manifest.update(tokenizer=audit['tokenizer'], token_audit={'schema': audit['schema'],
            'max_tokens': 4096, 'max_total_tokens': audit['max_total_tokens'],
            'sha256': versions._sha256(ready / 'token_audit.json')})
        manifest['security_readiness'].update(training_ready=True, blockers=[])
        manifest['files'] = {name: {'sha256': sha, 'bytes': (ready / name).stat().st_size}
            for name, sha in core._inventory(ready).items() if name != 'manifest.json'}
        core._write(ready / 'manifest.json', manifest)
        inventory = core._inventory(ready)

        @contextmanager
        def fence():
            _check(check)
            core._verify_inventory(folder, state['inventory'])
            core._verify_inventory(ready, inventory)
            for name, expected in state['capture']['policy']['files'].items():
                if versions._sha256(Path(name)) != expected:
                    raise ValueError('All-training producer changed before publication')
            if core._capture(state['capture']['source']['tag'], root) != state['capture']['source']:
                raise ValueError('Frozen parent changed during all-training reassignment')
            yield

        final = versions.publish_reserved(ready, state['tag'], root, owner=state['owner'], publication_guard=fence)
        core.build_catalog.register_artifact(final, manifest, root=root)
        state.update(published=True, published_path=str(final))
        core._write(workspace / 'owner.json', state)
        return core._result(state, workspace, published=True, path=final)
