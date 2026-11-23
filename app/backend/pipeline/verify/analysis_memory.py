"""Release reconstructible, already loaded analysis caches in this worker.

Call only at an analysis-to-execution handoff, outside active parsing work.
This is best-effort cleanup, not resource admission: the caller must still run
the unchanged resource budget check afterwards. No optional module is imported.
"""

import gc
import os
import sys


def _private_bytes(errors):
    module = sys.modules.get('psutil')
    if module is None:
        return None, None
    try:
        process = module.Process(os.getpid())
        value = getattr(process.memory_info(), 'private', None)
        if isinstance(value, int) and value >= 0:
            return value, 'private'
        value = getattr(process.memory_full_info(), 'uss', None)
        if isinstance(value, int) and value >= 0:
            return value, 'uss'
        errors.append('Process private memory measurement unavailable')
    except Exception as exc:
        errors.append('Process private memory measurement failed: ' + str(exc))
    return None, None


def _clear(name, cache, report, *, public=False):
    entry = {'status': 'failed', 'entries_evicted': 0}
    report['caches'][name] = entry
    try:
        before = cache.cache_info()
        entry['entries_before'] = before['entries'] if public else before.currsize
        if public:
            entry['bytes_before'] = before['bytes']
            cache.clear_cache()
        else:
            cache.cache_clear()
        after = cache.cache_info()
        entry['entries_after'] = after['entries'] if public else after.currsize
        if public:
            entry['bytes_after'] = after['bytes']
        # Counts describe the observed reduction; concurrent repopulation can
        # reduce it. They are not a claim about reclaimed allocator bytes.
        entry['entries_evicted'] = max(0, entry['entries_before'] - entry['entries_after'])
        report['entries_evicted'] += entry['entries_evicted']
        entry['status'] = 'cleared'
    except Exception as exc:
        report['errors'].append(name + ': ' + str(exc))


def release_analysis_caches():
    """Return measured memory and cache reductions, without granting admission.

    funcsplit.clear_cache is the public cache API; its recent parser trees are
    thread-local, so only the calling thread's recent trees can be released.
    Process memory may stay flat or increase after cleanup. Missing private
    memory telemetry is reported as None and is never replaced by RSS.
    """
    report = {
        'schema': 'analysis-memory-release/1', 'pid': os.getpid(),
        'caches': {}, 'entries_evicted': 0, 'errors': [],
        'arrow_pool_release': 'not_loaded', 'gc_collected': None, 'owned_readers': {},
    }
    before, before_kind = _private_bytes(report['errors'])
    report['before_private_bytes'] = before
    report['before_measurement'] = before_kind

    module = sys.modules.get('app.backend.pipeline.context_builder')
    if module is not None:
        for attribute in ('_refs_cached', '_parsed_units'):
            cache = getattr(module, attribute, None)
            if cache is not None:
                _clear('app.backend.pipeline.context_builder.' + attribute, cache, report)
    module = sys.modules.get('app.backend.pipeline.security_snapshots')
    if module is not None:
        cls = getattr(module, 'GitSnapshot', None)
        cache = getattr(cls, 'read', None)
        if cache is not None:
            _clear('app.backend.pipeline.security_snapshots.GitSnapshot.read', cache, report)
        cache = getattr(module, '_git_snapshot', None)
        if cache is not None:
            _clear('app.backend.pipeline.security_snapshots._git_snapshot', cache, report)
    module = sys.modules.get('app.backend.pipeline.funcsplit')
    if module is not None:
        _clear('app.backend.pipeline.funcsplit', module, report, public=True)

    # This function runs at a quiescent phase boundary, never during parsing or
    # acquisition. Public pool cleanup confirms termination of this process's
    # retained Git children; it cannot stop another worker's readers.
    for name in ('app.backend.pipeline.acceleration.git_objects',
                 'app.backend.pipeline.acceleration.repository_service'):
        module = sys.modules.get(name)
        if module is None:
            continue
        try:
            module.close_all()
            report['owned_readers'][name] = {'status': 'closed'}
        except Exception as exc:
            report['owned_readers'][name] = {'status': 'failed'}
            report['errors'].append(name + ': ' + str(exc))

    try:
        report['gc_collected'] = gc.collect()
    except Exception as exc:
        report['errors'].append('Garbage collection failed: ' + str(exc))
    arrow = sys.modules.get('pyarrow')
    if arrow is not None:
        try:
            arrow.default_memory_pool().release_unused()
            report['arrow_pool_release'] = 'completed'
        except Exception as exc:
            report['arrow_pool_release'] = 'failed'
            report['errors'].append('Arrow pool release failed: ' + str(exc))

    after, after_kind = _private_bytes(report['errors'])
    report['after_private_bytes'] = after
    report['after_measurement'] = after_kind
    report['private_bytes_delta'] = (
        after - before if before is not None and after is not None and before_kind == after_kind else None
    )
    return report
