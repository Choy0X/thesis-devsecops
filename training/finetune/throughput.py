"""Portable online tuning plus compatible legacy A100 throughput profiles."""
from __future__ import annotations

import gc
import json
import math
import time

import torch

RUNTIME_PATCH = None


def _checkpointing(model, enabled):
    if enabled:
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
    else:
        model.gradient_checkpointing_disable()


def _release():
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def choose_plan(effective_batch, benchmark, *, budget_bytes, seconds=120, clock=time.monotonic, progress=lambda row: None):
    """Choose measured tokens/s among fitting divisors of the effective batch."""
    began, rows, blocked = clock(), [], set()
    sizes = [n for n in (1, 2, 4, 8, 16, 32) if n <= effective_batch and effective_batch % n == 0]
    for size in sizes:
        for checkpointing in (True, False):
            if checkpointing in blocked or (rows and clock() - began >= seconds):
                continue
            row = {'micro_batch_size': size, 'gradient_checkpointing': checkpointing}
            try:
                measured = benchmark(size, checkpointing)
                if (not math.isfinite(measured['seconds']) or measured['seconds'] <= 0
                        or measured['tokens'] <= 0 or measured['peak_allocated_bytes'] < 0):
                    raise ValueError('Invalid A100 throughput measurement')
                row.update(measured)
                if row['peak_allocated_bytes'] > budget_bytes:
                    row['state'] = 'insufficient_headroom'
                    blocked.add(checkpointing)
                else:
                    row.update(state='passed', tokens_per_second=row['tokens'] / row['seconds'])
            except torch.cuda.OutOfMemoryError:
                row['state'] = 'out_of_memory'
                blocked.add(checkpointing)
            if row['state'] == 'out_of_memory':
                # Outside the handler: the failed forward's traceback no longer
                # owns tensors while the allocator cache is cleared.
                _release()
            rows.append(row)
            progress(row)
    passed = [row for row in rows if row['state'] == 'passed']
    if not passed:
        raise RuntimeError('A100 probes could not fit one example with optimizer memory headroom')
    chosen = max(passed, key=lambda row: (row['tokens_per_second'], -row['peak_allocated_bytes']))
    return {'schema': 'a100-throughput/1', 'selected': chosen, 'measurements': rows,
            'effective_sequence_batch_size': effective_batch, 'budget_bytes': budget_bytes,
            'benchmark_seconds': clock() - began, 'optimizer_updates': 0, 'sampler_draws': 0}


def probe(trainer, examples, *, checkpointing):
    """Time one forward/backward, restoring RNG, modes and checkpointing even on failure."""
    if any(p.grad is not None for p in trainer.params):
        raise RuntimeError('Throughput probes require an optimizer boundary with cleared gradients')
    rng = trainer.backend.rng_state()
    was_training, head_training = trainer.model.training, trainer.head.training
    was_checkpointing = bool(getattr(trainer.model, 'is_gradient_checkpointing', False))
    cuda = trainer.backend.name == 'cuda'
    loss = parts = None
    try:
        _checkpointing(trainer.model, checkpointing)
        trainer.model.train()
        trainer.head.train()
        if cuda:
            torch.cuda.reset_peak_memory_stats(trainer.device)
        trainer.backend.synchronize()
        began = time.perf_counter()
        loss, parts = trainer.compute_loss(examples)
        loss.backward()
        trainer.backend.synchronize()
        return {'seconds': time.perf_counter() - began,
                'tokens': sum(len(e.encoded.input_ids) for e in examples),
                'peak_allocated_bytes': torch.cuda.max_memory_allocated(trainer.device) if cuda else 0}
    finally:
        trainer.optimizer.zero_grad(set_to_none=True)
        loss = parts = None
        _checkpointing(trainer.model, was_checkpointing)
        trainer.model.train(was_training)
        trainer.head.train(head_training)
        trainer.backend.restore_rng(rng)
        _release()


def choose_bucket_plan(effective_batch, buckets, benchmark, *, budget_bytes, baseline_size=None,
                       seconds=120, clock=time.monotonic, progress=lambda row: None):
    """Measure worst shapes per length bucket, keeping the effective batch fixed."""
    began, measurements, schedule = clock(), [], []
    sizes = [n for n in (1, 2, 4, 8, 16, 32) if effective_batch % n == 0]
    longest = max(length for length, _ in buckets)
    def measured(length, size):
        row = {'max_sequence_tokens': length, 'micro_batch_size': size, 'gradient_checkpointing': True}
        try:
            values = benchmark(length, size)
            if (not math.isfinite(values['seconds']) or values['seconds'] <= 0
                    or values['tokens'] <= 0 or values['peak_allocated_bytes'] < 0):
                raise ValueError('Invalid length-bucket throughput measurement')
            row.update(values)
            row['state'] = 'passed' if row['peak_allocated_bytes'] <= budget_bytes else 'insufficient_headroom'
            row['tokens_per_second'] = row['tokens'] / row['seconds']
        except torch.cuda.OutOfMemoryError:
            row['state'] = 'out_of_memory'
        if row['state'] == 'out_of_memory':
            _release()
        measurements.append(row)
        progress(row)
        return row
    long_passed = []
    long_sizes = ([n for n in reversed(sizes) if n <= baseline_size] if baseline_size else sizes)
    for size in long_sizes:
        row = measured(longest, size)
        if row['state'] == 'passed':
            long_passed.append(row)
            if baseline_size:
                break
        elif not baseline_size:
            break
        if long_passed and clock() - began >= seconds:
            break
    if not long_passed:
        raise RuntimeError('One longest example does not fit with optimizer memory headroom')
    selected = max(long_passed, key=lambda row: (row['tokens_per_second'], -row['peak_allocated_bytes']))
    schedule.append(dict(selected))
    # Prioritize common lengths within the paid-runtime benchmark budget.
    for length, count in sorted(buckets, key=lambda pair: -pair[1]):
        if length == longest:
            continue
        fallback = {**selected, 'max_sequence_tokens': length, 'state': 'baseline_fallback',
                    'tokens_per_second': None, 'scope': 'safe longest-example batch retained'}
        passed = []
        for size in (n for n in sizes if n >= selected['micro_batch_size']):
            if clock() - began >= seconds:
                break
            row = measured(length, size)
            if row['state'] != 'passed':
                break
            passed.append(row)
        choice = max(passed, key=lambda row: (row['tokens_per_second'], -row['peak_allocated_bytes'])) if passed else fallback
        schedule.append({**choice, 'views_in_bucket': count})
    return {'schema': 'a100-throughput/2', 'selected': selected,
            'batch_schedule': sorted(schedule, key=lambda row: row['max_sequence_tokens']),
            'measurements': measurements, 'effective_sequence_batch_size': effective_batch,
            'budget_bytes': budget_bytes, 'benchmark_seconds': clock() - began,
            'optimizer_updates': 0, 'sampler_draws': 0}


def _apply(trainer, report):
    trainer.batch_schedule = report['batch_schedule']
    trainer.micro_batch_size = max(row['micro_batch_size'] for row in trainer.batch_schedule)
    _checkpointing(trainer.model, True)


def _save(path, report):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, indent=2), encoding='utf-8')
    temporary.replace(path)


def _optimizer_initialized_bytes(optimizer):
    return sum(value.numel() * value.element_size()
               for state in optimizer.state.values() for value in state.values()
               if isinstance(value, torch.Tensor) and value.device.type == 'cuda')


def configure(trainer, *, log=lambda message: None):
    """Opt-in only. Reuse a bound result on resume instead of repeating probes."""
    if getattr(trainer.training, 'micro_batch_tuning', 'off') == 'auto':
        from training.finetune.online_training import configure as configure_online
        if getattr(trainer, 'controller', None) is not None:
            return trainer.controller.state_dict()
        return configure_online(trainer, mode='auto', log=log)
    if (getattr(trainer.training, 'micro_batch_tuning', 'off') != 'a100'
            or trainer.backend.name != 'cuda'):
        return None
    properties = torch.cuda.get_device_properties(trainer.device)
    if 'A100' not in properties.name:
        log('A100 throughput tuning skipped on ' + properties.name + '; using configured fixed batch.')
        return None
    if trainer.pack or trainer.loss_cfg.pair_enabled or trainer.loss_cfg.invariance_enabled:
        raise ValueError('A100 automatic tuning currently supports unpacked language-model supervision')
    identity = {**{key: trainer.meta.get(key) for key in (
                    'base_commit', 'tokenizer_revision', 'serialization_hash', 'dataset_manifest_sha256',
                    'config_sha256', 'stage', 'seed')},
                'gpu': properties.name, 'capacity_bytes': properties.total_memory,
                'lm_loss_chunk_tokens': trainer.chunk,
                'effective_sequence_batch_size': trainer.training.effective_sequence_batch_size}
    path = trainer.run_dir / 'throughput_profile.json'
    previous = None
    if path.exists() or getattr(trainer, '_throughput_checkpoint', None):
        report = (json.loads(path.read_text(encoding='utf-8')) if path.exists()
                  else trainer._throughput_checkpoint)
        selected = report['selected']
        size = selected['micro_batch_size']
        if (report.get('identity') != identity or type(size) is not int or size < 1
                or trainer.training.effective_sequence_batch_size % size
                or type(selected['gradient_checkpointing']) is not bool
                or selected.get('state') != 'passed'):
            raise ValueError('Saved A100 throughput profile does not match this run and GPU')
        if report.get('schema') == 'a100-throughput/2':
            schedule = report.get('batch_schedule') or []
            bounds = [row['max_sequence_tokens'] for row in schedule]
            if (not schedule or bounds != sorted(set(bounds)) or bounds[0] < 1
                    or bounds[-1] != report['longest_sequence_tokens']
                    or any(type(row['micro_batch_size']) is not int or row['micro_batch_size'] < 1
                           or trainer.training.effective_sequence_batch_size % row['micro_batch_size']
                           or row['gradient_checkpointing'] is not True for row in schedule)):
                raise ValueError('Invalid saved length-bucket batch schedule')
            _apply(trainer, report)
            trainer.meta['throughput_profile'] = report
            if not path.exists():
                _save(path, report)
            log('Reusing verified A100 length-bucket schedule: ' +
                ', '.join(f"<={row['max_sequence_tokens']} tokens: batch {row['micro_batch_size']}" for row in schedule))
            return report
        if report.get('schema') != 'a100-throughput/1':
            raise ValueError('Unknown saved A100 throughput schema')
        previous = report
        log('Upgrading fixed A100 batches without changing checkpoint identities or sampler progress.')
    representatives, counts = {}, {}
    for vid in trainer.dataset.ids():
        ex = trainer.dataset.get(vid)
        length = len(ex.encoded.input_ids)
        bucket = next((cap for cap in (256, 512, 1024, 2048) if length <= cap), 4096)
        if bucket not in representatives or length > len(representatives[bucket].encoded.input_ids):
            representatives[bucket] = ex
        counts[bucket] = counts.get(bucket, 0) + 1
    if not representatives:
        raise ValueError('A100 throughput tuning requires training examples')
    by_length = {len(ex.encoded.input_ids): ex for ex in representatives.values()}
    buckets = [(len(ex.encoded.input_ids), counts[cap]) for cap, ex in representatives.items()]
    free, total = torch.cuda.mem_get_info(trainer.device)
    outside = max(0, total - free - torch.cuda.memory_reserved(trainer.device))
    optimizer_bytes = 8 * sum(p.numel() for p in trainer.params)
    initialized_bytes = _optimizer_initialized_bytes(trainer.optimizer)
    reserve = max(3 * 1024**3, int(.10 * total)) + max(0, optimizer_bytes - initialized_bytes)
    budget = total - outside - reserve
    log('A100 length-bucket warmup: checkpointing remains enabled; no weight updates or sampler draws.')
    probe(trainer, [by_length[min(by_length)]], checkpointing=True)
    def benchmark(length, size):
        log(f'A100 probe: sequence={length}, batch={size}, checkpointing=True')
        return probe(trainer, [by_length[length]] * size, checkpointing=True)
    report = choose_bucket_plan(trainer.training.effective_sequence_batch_size, buckets, benchmark,
                               budget_bytes=budget, baseline_size=previous['selected']['micro_batch_size'] if previous else None,
                               progress=lambda row: log(f"A100 length={row['max_sequence_tokens']} batch={row['micro_batch_size']}: {row['state']}"))
    report.update(identity=identity, optimizer_reserved_bytes=optimizer_bytes,
                  optimizer_initialized_bytes=initialized_bytes,
                  longest_sequence_tokens=max(by_length), runtime_patch=RUNTIME_PATCH,
                  previous_fixed_selection=previous['selected'] if previous else None,
                  scope='forward/backward probes, not measured end-to-end training speedup')
    if previous:
        history = path.with_name('throughput_profile.fixed.json')
        if not history.exists():
            _save(history, previous)
    _save(path, report)
    _apply(trainer, report)
    trainer.meta['throughput_profile'] = report
    log('A100 adaptive batches selected: ' + ', '.join(
        f"<={row['max_sequence_tokens']} tokens: batch {row['micro_batch_size']}" for row in report['batch_schedule']) +
        '; effective batch unchanged.')
    return report
