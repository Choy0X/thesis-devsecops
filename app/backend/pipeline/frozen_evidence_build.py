"""Bounded policy re-evaluation of registered immutable source records.

Original source code and historical assignments remain unchanged. Optional pinned
candidate shards append independently collected units with fresh admission and
derived leakage quarantine. Old review labels never become new proof.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import pathlib
import re
import shutil
import sqlite3
from collections import Counter
from types import SimpleNamespace

from . import automatic_admission, automatic_evidence, build_catalog, build_reviewed as br
from . import context_builder, evidence_recovery, funcsplit, leakage, negatives, security_quality, versions
from .schemas import reviewed
from . import views as vw
from . import appended_evidence, source_fingerprints
from .views import pools, catalog, rendering
from training.finetune import serialization

SCHEMA = 'frozen-evidence-source/1'
COPY_FILES = ('edges.parquet', 'groups.parquet', 'split_manifest.json', 'security_partitions.json')


def capture(tag, *, root=None, candidate_source=None, include_candidates=True):
    root = build_catalog._root(root)
    if not isinstance(tag, str) or not re.fullmatch(r'v[1-9][0-9]*', tag):
        raise ValueError('invalid frozen parent identity')
    name = 'dataset_' + tag
    entry = versions.load_registry(root)['frozen'].get(name)
    if not entry or entry.get('base') != name:
        raise ValueError('source must be a registered immutable dataset')
    folder = root / name
    required = {'manifest.json', 'records.parquet', 'stats.json', *COPY_FILES}
    files = entry.get('files') or {}
    if not required <= set(files):
        raise ValueError('frozen source is incomplete')
    for rel, expected in files.items():
        path = folder / rel
        if not path.resolve().is_relative_to(folder.resolve()) or not path.is_file() or versions._sha256(path) != expected:
            raise ValueError('frozen source hash mismatch: ' + rel)
    manifest = build_catalog.read_manifest(folder / 'manifest.json')
    if (manifest.get('tag') != tag or manifest.get('dataset_status') != 'frozen'
            or manifest.get('quality_profile') != 'strict'
            or manifest.get('admission_policy') not in ('automatic-evidence/1', 'automatic-evidence/2', 'automatic-evidence/3')):
        raise ValueError('source is not a supported strict evidence dataset')
    with evidence_recovery._connect(root) as db:
        repairs = [json.loads(row[0]) for row in db.execute('SELECT body FROM repairs ORDER BY rowid')]
    result = {'schema': SCHEMA, 'parent_tag': tag, 'parent_files': dict(files),
            'parent_version': manifest.get('split_manifest_version'),
            'parent_profile': manifest.get('build_profile'), 'parent_counts': manifest.get('counts') or {},
            'builder_sha256': hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
            'append_builder_sha256': hashlib.sha256(pathlib.Path(appended_evidence.__file__).read_bytes()).hexdigest(),
            'fingerprint_policy_sha256': hashlib.sha256(pathlib.Path(source_fingerprints.__file__).read_bytes()).hexdigest(),
            'input_rendering': rendering.identity(),
            'derived_evidence': {'repairs': evidence_recovery.repair_groups(repairs)},
            'source_policy': 'unchanged_source_and_partitions_fresh_admission/1'}
    if include_candidates:
        if candidate_source is None:
            from . import npm_case_collector
            candidate_source = npm_case_collector.capture(root=root)
        if candidate_source.get('shards'):
            # Validate pinned content before reserving a dataset identity.
            for _ in appended_evidence.candidate_records(candidate_source, root=root):
                pass
            result.update(candidate_source=copy.deepcopy(candidate_source),
                source_policy='append_exact_candidates_preserve_historical_roles/1')
    return result


def _validated_parent(source, root):
    current = capture(source['parent_tag'], root=root, include_candidates=False)
    if (current['parent_files'] != source['parent_files'] or current['parent_profile'] != source['parent_profile']
            or current['builder_sha256'] != source.get('builder_sha256')
            or current['append_builder_sha256'] != source.get('append_builder_sha256')
            or current['fingerprint_policy_sha256'] != source.get('fingerprint_policy_sha256')
            or current['input_rendering'] != source.get('input_rendering')):
        raise ValueError('frozen parent hash or profile changed after capture')
    return pathlib.Path(root) / ('dataset_' + source['parent_tag'])


def iter_records(source, *, root=None):
    """Stream the exact captured parent and candidates; labels remain untrusted."""
    root = build_catalog._root(root)
    from . import evidence_source_supersession as supersession
    if not re.fullmatch(r'v[1-9][0-9]*', str(source.get('parent_tag') or '')):
        raise ValueError('invalid captured parent identity')
    parent = root / ('dataset_' + source['parent_tag'])
    if versions._sha256(parent / 'records.parquet') != source['parent_files']['records.parquet']:
        raise ValueError('captured parent record hash mismatch')
    # Only advisory metadata is retained. Candidate bodies are streamed again,
    # allowing new aliases of existing units to affect old/new leakage together.
    candidates = {}
    for row in appended_evidence.candidate_records(source.get('candidate_source'), root=root):
        rid, bound = row['record_id'], appended_evidence.identity(row)
        entry = candidates.setdefault(rid, {'identity': bound, 'source_reference': supersession.reference(row),
            **{key: set() for key in appended_evidence.MERGE_FIELDS}})
        if entry['identity'] != bound:
            raise ValueError('candidate record identity collision within captured shards')
        for key in appended_evidence.MERGE_FIELDS:
            entry[key].update(row.get(key) or [])

    def merge(row):
        entry = candidates.get(row['record_id'])
        if entry is None:
            return row
        if appended_evidence.identity(row) != entry['identity']:
            raise ValueError('candidate record identity collision with captured source')
        row = supersession.enrich(row, entry['source_reference'], root=root)
        return {**row, **{key: sorted(set(row.get(key) or []) | entry[key]) for key in appended_evidence.MERGE_FIELDS}}

    seen = set()
    for row in evidence_recovery._records(parent):
        if row['record_id'] in seen:
            raise ValueError('duplicate historical record identity')
        seen.add(row['record_id'])
        yield merge(row)
    for row in appended_evidence.candidate_records(source.get('candidate_source'), root=root):
        if row['record_id'] not in seen:
            seen.add(row['record_id'])
            yield merge(row)


def _record_batches(records, size=16):
    batch = []
    for row in records:
        batch.append(row)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _view_rows(record, role, tag, *, fixed_is_vulnerable=False, exclusions=None):
    exclusions = exclusions if exclusions is not None else Counter()
    subjects = negatives.subjects_from_records([record])
    views, subject_of, bundles = [], {}, {}
    for subject in subjects:
        if fixed_is_vulnerable and subject.kind == 'fixed_twin':
            exclusions['chained_vulnerable_elsewhere'] += 1
            continue
        if not negatives.eligible(subject, 'auto_checked'):
            continue
        problems = security_quality.admission_problems(record, fixed_twin=subject.kind == 'fixed_twin',
                                                       admission_policy=record.get('admission_policy') or automatic_evidence.POLICY)
        if problems:
            exclusions.update('security_admission:' + p for p in problems)
            continue
        side = 'after' if subject.kind == 'fixed_twin' else 'before'
        bundle = context_builder.Bundle.from_dict(json.loads(record['context_' + side]))
        bundles[subject.subject_id] = bundle
        subject_of[subject.subject_id] = subject
        selected = record
        if subject.kind == 'fixed_twin':
            fixed = json.loads(record['fixed_review'])
            subject = dataclasses.replace(subject, condition_scope=fixed['scope'])
            selected = {**record, 'condition_scope':fixed['scope'], 'decisive_safeguard':fixed['safeguard'], 'evidence_spans':fixed['evidence']}
        tasks = ['discovery', 'condition_assessment', 'localization', 'insufficient_context']
        if subject.kind == 'vulnerable' and subject.role == 'vulnerable':
            tasks.append('repair')
        for task in tasks:
            extra = {'report':record['evidence_summary']} if task == 'repair' and record.get('evidence_summary') else None
            view = vw.build_view(task, subject, selected, [i.as_dict() for i in bundle.items],
                                 extra=extra, context_hash=bundle.bundle_hash, strict=True)
            if isinstance(view, vw.View): views.append(view)
            else: exclusions[task + ':' + view.reason.split(':')[0]] += 1
    families = sorted(catalog.ATTACK_FAMILIES)
    for base in list(views):
        if base.task != 'discovery' or base.pool != 'supported': continue
        h = int(hashlib.sha1(('inject|' + base.view_id).encode()).hexdigest(),16)
        if (h % 10000) / 10000 >= .1: continue
        bundle = bundles[base.subject_id]
        injected = vw.build_injection(base, subject_of[base.subject_id], [i.as_dict() for i in bundle.items],
                                      family=families[(h // 10000) % len(families)],seed=tag)
        if isinstance(injected,vw.View): views.append(injected)
        else: exclusions['instruction_integrity:' + injected.reason] += 1
    result = []
    for view in views:
        completion = serialization.render_completion(view.task,view.response)[0]
        result.append(dict(view_id=view.view_id,task=view.task,subject_id=view.subject_id,record_id=view.record_id,
            issue_group_id=view.issue_group_id,family_id=view.family_id,pool=view.pool,split=record['split'],val_role=role,
            weight=float(view.weight),review_status=view.review_status,eligible=bool(view.eligible),context_hash=view.context_hash,
            flags_json=json.dumps(view.flags,sort_keys=True,default=str),lineage_json=json.dumps(view.lineage,sort_keys=True),
            context_text=view.context_text,response_json=json.dumps(view.response,ensure_ascii=False),
            prompt_chars=len(view.student_text()),completion_chars=len(completion)))
    return result


def _refresh_context_metadata(record):
    bundle=context_builder.Bundle.from_dict(json.loads(record['context_before']))
    record.update(evidence_completeness=bundle.completeness,omitted_context_reasons=list(bundle.omissions),
        context_manifest=json.dumps({'policy':bundle.policy,'bundle_hash':bundle.bundle_hash,
            'completeness':bundle.completeness,'omissions':bundle.omissions,
            'items':[{'kind':i.kind,'path':i.path,'name':i.name,'chars':len(i.text)} for i in bundle.items]},sort_keys=True),
        callers=[i.name for i in bundle.items if i.kind=='caller' and i.name],
        callees=[i.name for i in bundle.items if i.kind=='callee' and i.name],
        imports=[line for i in bundle.items if i.kind=='imports' for line in i.text.splitlines()],
        relevant_configuration=[i.path for i in bundle.items if i.kind in ('manifest','configuration')])


def _leakage_add(record,role,row_views,active,violations):
    """Audit every used code side across train, selection, calibration and test.

    Exact keys include short units. Longer units additionally use the existing
    unsigned token-shingle algorithm with exact Jaccard comparison, avoiding
    LSH sampling misses. Source bodies never enter the retained audit rows.
    """
    import numpy as np
    for kind,field in (('vulnerable','vulnerable_code'),('fixed_twin','fixed_code')):
        sid=negatives._sid(record['record_id'],kind)
        if not any(v['subject_id']==sid and v['eligible'] and v['weight']>0 for v in row_views):continue
        tokens=funcsplit.tokens(record['language'],record[field])
        key=hashlib.sha1('\x1f'.join(tokens).encode()).hexdigest()
        shingles=leakage._shingles(tokens) if len(tokens)>=leakage.MIN_TOKENS else None
        partition=(record['split'],role)
        current={'record_id':record['record_id'],'subject_id':sid,'partition':partition,'key':key,'shingles':shingles}
        for other in active:
            if other['partition']==partition:continue
            exact=other['key']==key
            similar=False
            if not exact and shingles is not None and other['shingles'] is not None:
                a,b=len(shingles),len(other['shingles'])
                if min(a,b)/max(a,b)>=leakage.NEAR_CLONE_JACCARD:
                    intersection=len(np.intersect1d(shingles,other['shingles'],assume_unique=True))
                    similar=intersection/(a+b-intersection)>=leakage.NEAR_CLONE_JACCARD
            if exact or similar:
                violations.append({'source':other['subject_id'],'target':sid,'source_partition':other['partition'],
                    'target_partition':partition,'kind':'exact' if exact else 'near_clone'})
        active.append(current)


def build(source, *, root, reserved_tag, owner, profile, progress=None, publication_guard=None):
    with automatic_evidence.source_scope(root):
        return _build(source, root=root, reserved_tag=reserved_tag, owner=owner, profile=profile,
                      progress=progress, publication_guard=publication_guard)


def _build(source, *, root, reserved_tag, owner, profile, progress=None, publication_guard=None):
    import pyarrow as pa
    import pyarrow.parquet as pq
    root = pathlib.Path(root)
    policy = profile.get('admission_policy') or automatic_evidence.POLICY
    parent = _validated_parent(source,root)
    parent_manifest = build_catalog.read_manifest(parent/'manifest.json')
    if source['parent_profile'].get('id') != profile['id']:
        raise ValueError('frozen policy rebuild cannot change source profile')
    prog = progress or (lambda **kw:None)
    staging = root / '.build_requests' / owner / 'artifact'
    staging.mkdir(parents=True,exist_ok=False)
    partitions = json.loads((parent/'security_partitions.json').read_text(encoding='utf-8'))
    append_plan = None
    if source.get('candidate_source'):
        append_plan = appended_evidence.prepare(iter_records(source, root=root), partitions['records'],
            workspace=staging.parent, progress=prog)
        partitions = {**partitions, 'records': append_plan['records']}
    expected_records = len(partitions['records'])
    # An SSD index preserves the full-corpus chained-fix check without source bodies in RAM.
    db = sqlite3.connect(staging.parent/'source-keys.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS vulnerable_keys (key TEXT,record_id TEXT,PRIMARY KEY(key,record_id))')
    for batch in _record_batches(iter_records(source, root=root), 64):
        from .automatic_evidence_v3 import repairable_tangled
        db.executemany('INSERT OR IGNORE INTO vulnerable_keys VALUES (?,?)',
            [(leakage.code_key(r['language'],r['vulnerable_code']),r['record_id']) for r in batch
             if r['data_quality_status']=='ok' or policy == 'automatic-evidence/3' and repairable_tangled(r)])
    db.commit()
    counts, reasons, tiers, exclusions = Counter(),Counter(),Counter(),Counter()
    compact, views, seen, active, violations = [], [], set(), [], []
    repairs = source.get('derived_evidence',{}).get('repairs',{})
    try:
        with pq.ParquetWriter(staging/'records.parquet',reviewed.arrow_schema()) as writer:
            for batch in _record_batches(iter_records(source, root=root)):
                written = []
                for original in batch:
                    rid = original['record_id']
                    if rid in seen or rid not in partitions['records']:
                        raise ValueError('duplicate record or missing historical partition')
                    seen.add(rid)
                    assignment = partitions['records'][rid]
                    if not append_plan and assignment['split'] != original['split']:
                        raise ValueError('historical source/partition mismatch')
                    row = evidence_recovery.apply_repairs(original,repairs)
                    if append_plan:
                        row = {**row, 'split': assignment['split'], 'issue_group_id': assignment['component']}
                        if assignment.get('quarantined'):
                            row.update(data_quality_status='quarantined',
                                quarantine_reasons=sorted(set(row.get('quarantine_reasons') or []) | {'append_source_cross_partition_conflict'}))
                    _refresh_context_metadata(row)
                    gate = {'failed':[r for r in original.get('admission_reasons',[]) if r in automatic_admission._HARD_GATE]}
                    row = automatic_evidence.admit_record(row,gate=gate,policy=policy)
                    counts['records'] += 1;counts[row['admission_outcome']] += 1
                    suggestion=json.loads(row['context_suggestions'])
                    counts['context_suggestions'] += suggestion.get('quality')!='strong' and bool(suggestion.get('reasons'))
                    tiers[row['review_status']] += 1;reasons.update(row['admission_reasons'])
                    row_views = []
                    if row['admission_outcome'] in ('admitted','insufficient_context'):
                        chained = db.execute('SELECT 1 FROM vulnerable_keys WHERE key=? AND record_id<>? LIMIT 1',
                            (leakage.code_key(row['language'],row['fixed_code']),rid)).fetchone() is not None
                        row_views = _view_rows(row,assignment.get('role'),reserved_tag,fixed_is_vulnerable=chained,exclusions=exclusions)
                        _leakage_add(row,assignment.get('role'),row_views,active,violations)
                        compact.append(row)
                    else:
                        compact.append({k:row.get(k) for k in ('record_id','admission_outcome','label_state','cwe_ids','data_quality_status','review_status','split',
                            'ecosystem','language','advisory_first_published_at','issue_group_id','repository_identity')})
                    row['eligible_tasks'] = sorted({v['task'] for v in row_views})
                    failures = reviewed.validate(row)
                    if failures: raise ValueError('invalid derived record '+rid+': '+str(failures[:4]))
                    views.extend(row_views);written.append(row)
                writer.write_table(pa.Table.from_pylist(written,schema=reviewed.arrow_schema()))
                prog(state='records',label='Revalidating source-bound supervision',processed=counts['records'],
                     total=expected_records,counts=dict(counts))
    finally:
        db.close()
    if counts['records'] != expected_records or seen != set(partitions['records']):
        raise ValueError('immutable parent record/partition coverage changed')
    views.sort(key=lambda v:v['view_id'])
    readiness = security_quality.readiness(compact,views,languages=set(profile['languages']) or None,
                                         admission_policy=policy,
                                         partition_groups={rid:assignment['component'] for rid,assignment in partitions['records'].items()},
                                         assignment_roles={rid:assignment.get('role') if assignment['split'] == 'val'
                                             else 'untouched_test' if assignment['split'] == 'test' else assignment['split']
                                             for rid,assignment in partitions['records'].items()})
    if violations:
        readiness['blockers'].append('cross_partition_source_leakage')
        readiness['training_ready']=False
    leakage_audit={'schema':'frozen-source-leakage-audit/1','active_subjects':len(active),
        'near_clone_jaccard':leakage.NEAR_CLONE_JACCARD,'min_near_clone_tokens':leakage.MIN_TOKENS,
        'violations':violations,'passed':not violations}
    evidence = security_quality.evidence_counts(compact,views)
    automatic = {'automatically_eligible':counts['admitted']+counts['insufficient_context'],
                 'insufficient_context':counts['insufficient_context'],'excluded':counts['excluded'],
                 'context_suggestions':counts['context_suggestions'],**evidence}
    old_stats = json.loads((parent/'stats.json').read_text(encoding='utf-8'))
    pool_counts=pools.count_pools(SimpleNamespace(**v) for v in views)
    from training.finetune.config import load
    cfg=load()
    task_plan=pools.plan_task_shares(pool_counts,cfg.sampling.task_shares)
    decision_plan=pools.plan_decision_pools(pool_counts.get('discovery',{}),cfg.sampling.decision_pool_targets)
    split_manifest=json.loads((parent/'split_manifest.json').read_text(encoding='utf-8'))
    # Coverage, labels, views and task plans are recomputed. Only unchanged source
    # graph statistics and raw collection diagnostics can be inherited.
    if not append_plan and 'summary' in split_manifest and 'val_roles' in split_manifest:
        stats=br._stats(compact,views,split_manifest,
            SimpleNamespace(stats=old_stats.get('graph',{}),oversized=old_stats.get('oversized_groups',[])),
            Counter(old_stats.get('exclusions',{}).get('gate_failures',{})),exclusions,
            old_stats.get('exclusions',{}).get('hidden_fact_risk',{}),task_plan,decision_plan,
            [None]*parent_manifest.get('counts',{}).get('duplicates',0),[None]*counts['records'])
    else:
        stats={'units':counts['records'],'label_states':dict(Counter(r['label_state'] for r in compact)),
               'task_shares':task_plan,'decision_pools':decision_plan}
    outcomes={k:counts[k] for k in ('admitted','insufficient_context','excluded')}
    stats.update(admission={'policy':policy,'outcomes':outcomes,**automatic},
                 admission_reasons=dict(reasons),view_exclusions=dict(exclusions),
                 derivation={'parent':source['parent_tag'],'source_and_partitions':'appended_with_historical_roles' if append_plan else 'unchanged',
                    'security_labels':'recomputed', 'appended_records': append_plan['added_records'] if append_plan else 0,
                    'quarantined_components': append_plan['conflicted_components'] if append_plan else []})
    if append_plan:
        appended_evidence.write_graph(append_plan, parent, staging)
        appended_evidence.write_provenance(source['candidate_source'], root=root, destination=staging / 'candidate_provenance.jsonl')
    else:
        for name in COPY_FILES: shutil.copyfile(parent/name,staging/name)
    (staging/'views.parquet').write_bytes(br._table_bytes(views,br._VIEW_SCHEMA))
    (staging/'stats.json').write_bytes(br._json_bytes(stats,indent=1))
    (staging/'security_readiness.json').write_bytes(br._json_bytes(readiness,indent=1))
    (staging/'source_leakage_audit.json').write_bytes(br._json_bytes(leakage_audit,indent=1))
    files={p.name:{'sha256':versions._sha256(p),'bytes':p.stat().st_size} for p in staging.iterdir() if p.is_file()}
    files['records.parquet']['rows']=counts['records'];files['views.parquet']['rows']=len(views)
    manifest=copy.deepcopy(parent_manifest)
    manifest.update(tag=reserved_tag,admission_policy=policy,build_profile=profile,
        security_readiness=readiness,counts={**parent_manifest.get('counts',{}),'records':counts['records'],'views':len(views),
                                           'views_by_task':dict(Counter(v['task'] for v in views)),**automatic},
        tier_mix=dict(tiers),min_review_tier_for_training='auto_checked',files=files,
        source={**parent_manifest.get('source',{}),'snapshot_sha256':build_catalog.canonical_snapshot_sha256(source),
                'frozen_parent':source['parent_tag'],'frozen_parent_manifest_sha256':source['parent_files']['manifest.json']},
        blocked=br._blocked(pool_counts,automatic=True))
    if append_plan:
        manifest['counts']['groups'] = len(append_plan['groups'])
        manifest['source']['candidate_source'] = copy.deepcopy(source['candidate_source'])
    manifest['versions']['build']='frozen-evidence-build/1'
    if policy == 'automatic-evidence/3':
        manifest['input_rendering'] = rendering.identity()
        manifest['versions']['input_rendering'] = rendering.POLICY
    else:
        manifest.pop('input_rendering', None)
        manifest['versions'].pop('input_rendering', None)
    manifest.pop('context_cache_signature',None)
    manifest['task_share_plan']=task_plan
    manifest['decision_pool_plan']=decision_plan
    (staging/'manifest.json').write_bytes(br._json_bytes(manifest,indent=1))
    # Recheck the original, registered bytes before immutable publication.
    _validated_parent(source,root)
    if source.get('candidate_source'):
        for _ in appended_evidence.candidate_records(source['candidate_source'], root=root):
            pass
    final=versions.publish_reserved(staging,reserved_tag,root,owner=owner,publication_guard=publication_guard)
    prog(state='done',label='Dataset published',processed=counts['records'],total=counts['records'])
    return {'path':str(final),'tag':reserved_tag}
