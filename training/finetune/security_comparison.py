"""Paired generated security responses from a verified experimental adapter.

Reviewed benchmarks and unreviewed development probes have separate accounting.
No classifier head, calibration, source-code execution or readiness promotion is
inferred from a source-learning adapter. Use security_bootstrap for a parent
process that enforces the deadline across installation and downloads as well.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import time
import zipfile


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False, default=str) + '\n', encoding='utf-8')
    os.replace(temp, path)


def _member(root, name):
    from training.finetune.trial_adapter import _safe
    if not _safe(name):
        raise ValueError('Unsafe publication member')
    root = Path(root).resolve()
    path = root.joinpath(*PurePosixPath(name).parts)
    if (not path.is_file() or not path.resolve().is_relative_to(root)
            or any(p.is_symlink() or getattr(p.lstat(), 'st_file_attributes', 0) & 0x400
                   for p in (path, *path.parents) if p.is_relative_to(root))):
        raise ValueError('Linked or missing publication member')
    return path


def _source(descriptor):
    """Verify inert full-file bytes, Git blob identity and the quoted byte span."""
    from training.finetune.trial_adapter import _safe
    if not isinstance(descriptor, dict) or not _safe(descriptor.get('path')):
        raise ValueError('Invalid logical source path')
    if not re.fullmatch('[a-f0-9]{40}', descriptor.get('revision', '')):
        raise ValueError('Invalid source revision')
    text, full = descriptor.get('text'), descriptor.get('file_text')
    if not isinstance(text, str) or not isinstance(full, str) or len(full) > 2 << 20:
        raise ValueError('Missing or oversized source bytes')
    raw, fragment = full.encode('utf-8'), text.encode('utf-8')
    span = descriptor.get('byte_span')
    if (not isinstance(span, list) or len(span) != 2 or any(type(x) is not int for x in span)
            or not 0 <= span[0] < span[1] <= len(raw) or raw[span[0]:span[1]] != fragment):
        raise ValueError('Source byte span differs from its fragment')
    if (hashlib.sha256(raw).hexdigest() != descriptor.get('file_sha256')
            or hashlib.sha256(fragment).hexdigest() != descriptor.get('source_sha256')
            or hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != descriptor.get('git_blob')):
        raise ValueError('Source snapshot identity differs')
    return raw


def prepare_probe(folder, expected_manifest_sha256, out_dir):
    """Reauthenticate authored teaching prompts, withholding their proposed labels."""
    folder, out = Path(folder).resolve(), Path(out_dir).resolve()
    manifest_path = _member(folder, 'manifest.json')
    if not expected_manifest_sha256 or _sha(manifest_path) != expected_manifest_sha256:
        raise ValueError('A development probe requires its pinned publication manifest')
    manifest = json.loads(manifest_path.read_bytes())
    if (manifest.get('schema') != 'security-teaching-publication/1'
            or manifest.get('state') != 'review_required' or manifest.get('admitted_views') != 0
            or manifest.get('independent_evaluation_eligible') is not False):
        raise ValueError('Development probes require an explicitly unadmitted teaching publication')
    files = manifest.get('files') or {}
    if 'candidate_views.jsonl' not in files or len(files) > 64:
        raise ValueError('Missing or over-budget teaching publication')
    total = 0
    for name, identity in files.items():
        path = _member(folder, name)
        total += path.stat().st_size
        if total > 16 << 20 or path.stat().st_size != identity.get('bytes') or _sha(path) != identity.get('sha256'):
            raise ValueError('Teaching publication member differs or exceeds budget')
    candidates = [json.loads(line) for line in (folder / 'candidate_views.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    if not candidates or len(candidates) > 1024:
        raise ValueError('Empty or over-budget teaching candidate inventory')
    records, views, seen = {}, [], set()
    for candidate in candidates:
        key, rid = candidate.get('view_id'), candidate.get('record_id')
        if (not isinstance(key, str) or not key or key in seen or not isinstance(rid, str) or not rid
                or candidate.get('eligible') is not False
                or candidate.get('review_status') != 'independent_review_required'):
            raise ValueError('Development probe identity or review gate differs')
        seen.add(key)
        context = candidate.get('context_text')
        if not isinstance(context, str) or hashlib.sha256(context.encode()).hexdigest() != candidate.get('context_sha256'):
            raise ValueError('Development prompt identity differs')
        descriptors = candidate.get('provenance', {}).get('sources')
        if not isinstance(descriptors, list) or not descriptors or len(descriptors) > 32:
            raise ValueError('Missing bounded authenticated source descriptors')
        snapshots, target = [], None
        for descriptor in descriptors:
            raw = _source(descriptor)
            if descriptor['text'] not in context:
                raise ValueError('Authenticated source fragment is absent from rendered context')
            name = 'probe_sources/' + hashlib.sha256(raw).hexdigest() + '.txt'
            path = out / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            snapshots.append({'path': name, 'sha256': hashlib.sha256(raw).hexdigest(),
                              'revision': descriptor['revision'], 'source_path': descriptor['path']})
            if descriptor.get('owner') == 'target':
                if target is not None:
                    raise ValueError('Ambiguous target ownership')
                target = descriptor
        if target is None:
            raise ValueError('Missing authenticated target')
        record = records.setdefault(rid, {'record_id': rid, 'label_state': 'unreviewed',
            'review_status': 'independent_review_required', 'source_snapshots': [],
            'relevant_parent_commit': target['revision'], 'repository_identity': candidate.get('repository_identity'),
            'language': candidate.get('language'), 'issue_group_id': candidate.get('issue_group_id')})
        for snapshot in snapshots:
            if snapshot not in record['source_snapshots']:
                record['source_snapshots'].append(snapshot)
        # The source selector accepts only these two revisions, never a guessed checkout.
        if target['revision'] != record['relevant_parent_commit']:
            if record.get('fix_commit', target['revision']) != target['revision']:
                raise ValueError('More than two revisions under one source record')
            record['fix_commit'] = target['revision']
        # Authored responses remain in the publication, never enter gold facts.
        views.append({**candidate, 'response_json': '{}', 'source_revision': target['revision'],
                      'allowed_patch_paths': [target['path']], 'eligible': False, 'weight': 0.0})
    return {'schema': 'security-development-probe/1', 'independent': False, 'accuracy_allowed': False,
            'manifest_sha256': expected_manifest_sha256, 'views': views, 'records': records, 'data_dir': str(out),
            'source_authentication': 'hash_blob_span_verified', 'label_state': 'unreviewed'}


def _cohort(views, tasks, max_views):
    supported = {'discovery', 'repair', 'condition_assessment', 'localization', 'insufficient_context',
                 'instruction_integrity', 'regression_test_design'}
    if not tasks or set(tasks) - supported or type(max_views) is not int or not 1 <= max_views <= 256:
        raise ValueError('Select structured security tasks with a bounded positive view cap')
    grouped = defaultdict(list)
    for view in views:
        if view['task'] in tasks:
            grouped[view['record_id']].append(view)
    chosen = []
    for key in sorted(grouped):
        group = sorted(grouped[key], key=lambda view: view['view_id'])
        if len(chosen) + len(group) <= max_views:
            chosen.extend(group)
    return chosen


def _evaluate_view(model, tokenizer, view, records, *, arm, limits, backend, cfg, generate_fn, log):
    """Reuse core inference and its exact serialization for additional probe tasks."""
    from training.finetune import infer, serialization, tokenization
    if view['task'] in infer.TRACK_OF_TASK:
        return infer.evaluate_views(model, tokenizer, [view], records, arm=arm, tasks=(view['task'],),
            max_new_tokens=limits, device=backend.device, dtype=backend.dtype, batch_size=1, head=None,
            generate_fn=generate_fn, log=log, context=cfg.context, protocol=cfg.model.serialization)
    task = view['task']
    prompt = serialization.serialize_prompt(task, view['context_text'], protocol=cfg.model.serialization, tokenizer=tokenizer).prompt
    total, reserve = tokenization.limits(task, context=cfg.context)
    if tokenization.token_count(tokenizer, prompt) > total - reserve:
        return [], {'raw_generations': [], 'excluded': {view['view_id']: 'prompt_over_budget'}}
    generated = generate_fn(model, tokenizer, [prompt], max_new_tokens=limits[task], device=backend.device, batch_size=1)
    if len(generated) != 1:
        raise RuntimeError('Generation count differs from selected cohort')
    generation = generated[0]
    raw = {'view_id': view['view_id'], 'arm': arm, 'task': task, 'text': generation.text,
           'new_tokens': generation.new_tokens, 'hit_limit': generation.hit_limit, 'seconds': generation.seconds,
           'generation_batch_size': getattr(generation, 'batch_size', None), 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()}
    row = infer._generated_row(task, view, records[view['record_id']], arm, generation, 'generated_response')
    return [row], {'raw_generations': [raw], 'excluded': {}}


def _load_base(cfg, backend, bundle, tiny):
    from training.finetune import runner, model, hub, trial
    if tiny:
        train = runner.load_views(bundle / 'data', 'train')
        heldout = [view for view in runner.load_views(bundle / 'data', 'val') if view['val_role'] == 'source_trial']
        tokenizer = runner.tiny_tokenizer([view['context_text'] + view['response_json'] for view in train + heldout])
        base = model.build_tiny_model(len(tokenizer), seed=cfg.training.pilot_seed)
        return base.to(backend.device), tokenizer
    return trial.load_base(cfg, backend), hub.load_tokenizer(cfg.model.id, cfg.model.tokenizer_revision)


def _attach_adapter(base, directory):
    """Require an exact load; PEFT's warning-only missing-key path is unsafe here."""
    from peft import PeftConfig, get_peft_model
    wrapped = get_peft_model(base, PeftConfig.from_pretrained(directory))
    loaded = wrapped.load_adapter(directory, adapter_name='default', is_trainable=False)
    if loaded.missing_keys or loaded.unexpected_keys:
        raise ValueError('Adapter load is incomplete: missing=' + str(loaded.missing_keys[:8])
                         + '; unexpected=' + str(loaded.unexpected_keys[:8]))
    wrapped.requires_grad_(False)
    return wrapped.eval()


def export_outputs(out_dir):
    """Export small reports only, excluding materialized weights and source bundles."""
    out = Path(out_dir)
    names = sorted(path.name for path in out.iterdir() if path.is_file() and path.suffix in ('.json', '.jsonl')
                   and path.name != 'security_outputs_manifest.json')
    manifest = {'schema': 'security-comparison-outputs/1', 'experimental': True,
                'counts_as_evaluation': False, 'counts_as_full_training': False,
                'files': {name: {'sha256': _sha(out / name), 'bytes': (out / name).stat().st_size} for name in names}}
    _write(out / 'security_outputs_manifest.json', manifest)
    target, temp = out / 'security_outputs.zip', out / 'security_outputs.zip.tmp'
    with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names + ['security_outputs_manifest.json']:
            archive.write(out / name, name)
    os.replace(temp, target)
    return str(target)


def run(*, bundle_dir, adapter_source, out_dir, evaluation_dir=None, evaluation_manifest_sha256=None,
        development_probe=None, probe_manifest_sha256=None, total_budget_minutes=30, deadline=None,
        tiny=False, tasks=None, max_views=24, generate_fn=None, clock=time.monotonic, log=print):
    from training.finetune.trial import Budget
    budget = Budget(total_budget_minutes, deadline=deadline, clock=clock)
    bundle, out = Path(bundle_dir).resolve(), Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError('Comparison requires a fresh output directory')
    report = {'schema': 'experimental-security-comparison/1', 'state': 'initializing', 'experimental': True,
              'software_test': tiny, 'counts_as_evaluation': False, 'counts_as_full_training': False,
              'decision_head': 'not_used', 'calibration': 'not_fabricated', 'security_metrics': None, 'arms': {},
              'total_budget_minutes': total_budget_minutes, 'deadline_monotonic': budget.deadline,
              'interpretation': 'No deployment-readiness or security-improvement claim from unreviewed probes or software fixtures.'}

    def snapshot(phase):
        report.update(phase=phase, elapsed_seconds=budget.elapsed, remaining_seconds=budget.remaining)
        _write(out / 'security_comparison.json', report)
        log(f'Security comparison {phase}; {budget.remaining:.1f}s remain')

    try:
        snapshot('data_preflight')
        from training.finetune import resolve, evaluation_data, trial_adapter
        cfg = resolve.read_resolved(bundle)
        if evaluation_dir is not None and development_probe is not None:
            raise ValueError('Choose one reviewed benchmark or one unreviewed development probe')
        if development_probe is not None:
            prepared = prepare_probe(development_probe, probe_manifest_sha256, out / 'probe')
            views, records, source = prepared['views'], prepared['records'], Path(prepared['data_dir'])
            report['data'] = {key: prepared[key] for key in ('schema', 'independent', 'accuracy_allowed', 'manifest_sha256', 'label_state')}
            independent = False
            tasks = tuple(sorted({view['task'] for view in views})) if tasks is None else tasks
        else:
            tasks = ('discovery', 'repair') if tasks is None else tasks
            ready = evaluation_data.preflight(bundle / 'data', cfg, evaluation_dir, evaluation_manifest_sha256, tasks=tasks)
            if not ready['independent']:
                raise evaluation_data.EvaluationBlocked('independent_benchmark_required', 'Supply an independently reviewed benchmark.')
            source = Path(evaluation_dir)
            import pyarrow.parquet as pq
            records = {row['record_id']: row for row in pq.read_table(source / 'records.parquet').to_pylist()}
            views, independent = ready['views'], True
            report['data'] = {key: ready[key] for key in ('independent', 'evaluation_dataset', 'training_dataset', 'overlap_audit')}
            report['data']['accuracy_allowed'] = True
        selected = _cohort(views, tasks, max_views)
        report['coverage'] = {'available_by_task': dict(Counter(v['task'] for v in views)),
            'selected_by_task': dict(Counter(v['task'] for v in selected)), 'selected_ids': [v['view_id'] for v in selected],
            'omitted_ids': sorted({v['view_id'] for v in views} - {v['view_id'] for v in selected}),
            'selection': 'deterministic whole-record groups; authored responses never used for selection'}
        if not selected:
            raise evaluation_data.EvaluationBlocked('empty_tasks', 'No whole record group fits the selected task/view budget.')
        snapshot('adapter_verification')
        adapter = trial_adapter.verify_trial_adapter(adapter_source, bundle_dir=bundle, allow_software=tiny)
        if adapter['software_test'] != tiny:
            raise ValueError('Software and pinned-model experiments must not be mixed')
        report['adapter'] = adapter
        if not tiny:
            root = Path(__file__).resolve().parents[2]
            if len(adapter['serialization_members']) != 4 or any(_sha(root / name) != sha for name, sha in adapter['serialization_members'].items()):
                raise ValueError('Runtime serialization changed from the original adapter bundle; explicit protocol migration is required')
        from training.finetune import accelerator, runtime_resources, config, runtime_compatibility
        backend = accelerator.select_backend('cpu' if tiny else 'auto')
        if backend.name != adapter['runtime_recipe']['backend']:
            raise evaluation_data.EvaluationBlocked('adapter_recipe_backend_mismatch',
                'This adapter requires its original quantization backend. Use a CUDA runtime for a CUDA NF4 adapter, or train a separate experimental TPU adapter.')
        cfg = config.ProjectConfig.model_validate(adapter['effective_config'])
        resources = ({'state': 'ready', 'software_test': True, 'resources': {'backend': 'cpu'}} if tiny
                     else runtime_resources.preflight(bundle, out, cfg, backend=backend))
        resources['purpose'] = 'comparison; conservative existing training resource bound'
        _write(out / 'resource_report.json', resources)
        report['resources'] = resources
        if resources['state'] != 'ready':
            raise evaluation_data.EvaluationBlocked('insufficient_resources', str(resources.get('reasons')))
        if not tiny:
            runtime_resources.apply_process_policy(resources)
            kernels = runtime_compatibility.validate_runtime(backend)
            _write(out / 'runtime_compatibility.json', kernels)
            if kernels['state'] != 'ready':
                raise evaluation_data.EvaluationBlocked('incompatible_runtime', str(kernels))
        if not budget.can_start(5, 120):
            report.update(state='partial', reason_code='setup_exhausted_budget')
            return report
        snapshot('loading_pinned_base')
        model, tokenizer = _load_base(cfg, backend, bundle, tiny)
        import torch
        from training.finetune import infer, attempts, generation_validation, repair_validation, sft_evaluation
        limits = {task: 1024 if task in ('repair', 'regression_test_design') else 512 for task in tasks}
        report['decoding'] = {'max_new_tokens': limits, 'do_sample': False, 'batch_size': 1,
                              'serialization': cfg.model.serialization, 'context': cfg.context.model_dump(mode='json')}
        reserve = 5 if tiny else 120
        base_deadline = clock() + max(0, budget.remaining - reserve) / 2

        def evaluate(arm, phase_deadline):
            rows, raw = [], []
            result = {'attempts': rows, 'raw_generations': raw, 'excluded': {}, 'state': 'partial'}
            report['arms'][arm] = result
            for view in selected:
                fact = attempts.facts_of(view, records[view['record_id']])
                track = infer.TRACK_OF_TASK.get(view['task'], 'generated_response')
                fallback = {'attempt_id': f"{arm}:{view['view_id']}:{track}", 'view_id': view['view_id'],
                            'arm': arm, 'track': track, **fact}
                if clock() >= phase_deadline:
                    answer, info = [], {'raw_generations': [], 'excluded': {view['view_id']: 'shared_deadline_reservation'}}
                else:
                    try:
                        answer, info = _evaluate_view(model, tokenizer, view, records, arm=arm, limits=limits,
                            backend=backend, cfg=cfg, generate_fn=generate_fn or infer.generate, log=log)
                    except (RuntimeError, TimeoutError, MemoryError) as error:
                        answer = [{**fallback, 'status': 'timeout' if isinstance(error, TimeoutError) else 'env_error',
                                   'prediction': None, 'detail': type(error).__name__ + ': ' + str(error)[:2000]}]
                        info = {'raw_generations': [], 'excluded': {}}
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                if not answer:
                    reason = info['excluded'].get(view['view_id'], 'generation_unavailable')
                    answer = [{**fallback, 'status': 'excluded', 'prediction': None, 'detail': reason}]
                for row in answer:
                    row.update(task=view['task'], record_id=view['record_id'])
                    if not independent:
                        row['truth'] = None
                rows.extend(answer)
                raw.extend(info['raw_generations'])
                result['excluded'].update(info.get('excluded', {}))
                _write(out / f'{arm}_responses.json', result)
                snapshot(arm + '_evaluation')
            result['state'] = 'finished' if all(row['status'] in ('answered', 'malformed', 'abstained') for row in rows) else 'partial'
            result['contract_and_evidence'] = generation_validation.measure(raw, selected, records, source)
            result['patch_applicability'] = repair_validation.measure(raw, selected, records, source)
            _write(out / f'{arm}_responses.json', result)
            return result

        base = evaluate('base', base_deadline)
        if not budget.can_start(1, reserve):
            # Preserve the full cohort, even when none of the second arm can run.
            adapted = evaluate('adapter', clock())
            report.update(state='partial', reason_code='adapter_evaluation_budget_exhausted')
        else:
            snapshot('attaching_verified_adapter')
            materialized = trial_adapter.materialize_trial_adapter(adapter_source, bundle_dir=bundle,
                out_dir=out / 'materialized_adapter', allow_software=tiny)
            model = _attach_adapter(model, materialized['adapter_dir'])
            report['adapter_load'] = {'state': 'exact', 'missing_keys': [], 'unexpected_keys': [], 'trainable': False}
            adapted = evaluate('adapter', budget.deadline - reserve)
            report['state'] = 'finished' if base['state'] == adapted['state'] == 'finished' else 'partial'
        # One-to-one raw prompt hashes are also checked after generation.
        hashes = [{row['view_id']: row['prompt_sha256'] for row in arm['raw_generations']} for arm in (base, adapted)]
        if any(hashes[0][key] != hashes[1][key] for key in hashes[0].keys() & hashes[1].keys()):
            raise ValueError('Paired prompt identities differ')
        report['paired_generated_views'] = len(hashes[0].keys() & hashes[1].keys())
        if independent and any(view['task'] == 'discovery' for view in selected):
            facts = {key: {**record, 'split': 'val'} for key, record in records.items()}
            report['security_metrics'] = sft_evaluation.compare(base['attempts'], adapted['attempts'], facts)
        if not independent:
            report['interpretation'] = ('Unreviewed development prompts only. Compare raw paired responses and static contract/source checks. '
                'Authored answers are not ground truth. No detection accuracy, semantic evidence correctness, functional repair correctness or security improvement is measured.')
        return report
    except Exception as error:
        from training.finetune.evaluation_data import EvaluationBlocked
        report.update(state='blocked' if isinstance(error, (EvaluationBlocked, ValueError)) else 'failed',
                      reason_code=getattr(error, 'code', type(error).__name__), detail=str(error))
        return report
    finally:
        if 'selected' in locals():
            for arm in ('base', 'adapter'):
                result = report['arms'].setdefault(arm, {'attempts': [], 'raw_generations': [], 'excluded': {}, 'state': 'partial'})
                attempted = {row['view_id'] for row in result['attempts']}
                for view in selected:
                    if view['view_id'] not in attempted:
                        reason = report.get('reason_code', 'phase_not_completed')
                        result['attempts'].append({'view_id': view['view_id'], 'record_id': view['record_id'],
                            'task': view['task'], 'arm': arm, 'status': 'excluded', 'detail': reason})
                        result['excluded'][view['view_id']] = reason
                _write(out / f'{arm}_responses.json', result)
            report['missing_phases'] = [arm + '_evaluation' for arm, result in report['arms'].items()
                                        if not result['raw_generations']]
        snapshot('export')
        report['outputs_zip'] = str(out / 'security_outputs.zip')
        _write(out / 'security_comparison.json', report)
        export_outputs(out)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle-dir', 'adapter-source', 'out-dir'):
        parser.add_argument('--' + name, required=True)
    for name in ('evaluation-dir', 'evaluation-manifest-sha256', 'development-probe', 'probe-manifest-sha256'):
        parser.add_argument('--' + name)
    parser.add_argument('--total-budget-minutes', type=float, default=30)
    parser.add_argument('--deadline', type=float)
    parser.add_argument('--tiny', action='store_true')
    parser.add_argument('--max-views', type=int, default=24)
    args = vars(parser.parse_args())
    report = run(**args)
    print(json.dumps({key: report.get(key) for key in ('state', 'reason_code', 'outputs_zip')}, indent=2))
    return 0 if report['state'] == 'finished' else 2


if __name__ == '__main__':
    raise SystemExit(main())
