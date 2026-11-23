"""Transport/parser policy is independent of CPU versus CUDA arithmetic."""


def normalize(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {'version', 'parser_workers', 'acquisition_version', 'hydrate_context'}:
        raise ValueError('Invalid mining policy fields')
    hydrate = value.get('hydrate_context', True)
    if type(hydrate) is not bool:
        raise ValueError('hydrate_context must be true or false')
    keep_hydrate = {} if hydrate else {'hydrate_context': False}      # a plan that never mentioned it keeps its stored form (and hash)
    version = value.get('version', 2)
    if type(version) is not int or version not in (1, 2, 3, 4):
        raise ValueError('Choose mining policy version 1, 2, experimental 3 or adaptive 4')
    acquisition = value.get('acquisition_version', 1)
    if 'acquisition_version' in value and (version != 4 or type(acquisition) is not int or acquisition not in (1, 2)):
        raise ValueError('Acquisition version must be 1 or 2 and requires mining policy 4')
    if version == 1:
        return {'version': 1, **keep_hydrate}
    parsers = value.get('parser_workers', 1)
    if type(parsers) is not int or not 1 <= parsers <= 16:
        raise ValueError('Parser workers must be an integer from 1 to 16')
    if version == 3 and parsers != 1:
        raise ValueError('Experimental pipeline uses one persistent parser')
    return {'version': version, 'parser_workers': parsers,
            **({'acquisition_version': acquisition} if 'acquisition_version' in value else {}), **keep_hydrate}


def acquisition_config(policy=None):
    """Missing versions retain historical behavior, including resumed campaigns."""
    policy = current() if policy is None else policy
    version = policy.get('acquisition_version', 1) if policy.get('version') == 4 else 1
    if type(version) is not int or version not in (1, 2):
        raise ValueError('Unknown acquisition version')
    from .memory_profile import current as memory_profile
    profile = memory_profile()
    tree_cache = (64 if version == 2 else 16) << 20
    return {'version': version, 'reader_slots': profile.capped(16 if version == 2 else 8, 'reader_slots'),
            'initial_blob_bytes': 131072 if version == 2 else 0,
            'tree_cache_bytes': tree_cache if profile.tree_cache is None else min(tree_cache, profile.tree_cache)}


def hydrate_context(policy=None):
    """Whether a mined commit also fetches the other files of its JS/TS context bundle (snapshot hydration). Measured on the npm campaign it was 38 percent of all mining
    time (3.3 s per commit). The grounded corpus never reads those bundles, so a plan for it may switch hydration off (policy field hydrate_context: false)."""
    policy = current() if policy is None else policy
    return policy.get('hydrate_context', True) is not False


def current():
    from .runtime import current_policy
    return current_policy().get('mining') or {'version': 1}


def rolling_enabled(policy=None):
    """Explicit experimental admission policy; never upgrades a stored plan."""
    import os
    value=os.environ.get('ZTSA_EXPERIMENTAL_ROLLING_ACQUISITION','')
    if value not in ('','1'):raise ValueError('ZTSA_EXPERIMENTAL_ROLLING_ACQUISITION must be empty or 1')
    if not value:return False
    policy=current() if policy is None else policy
    if policy.get('version')!=4 or acquisition_config(policy)['version']!=2:
        raise ValueError('Rolling acquisition requires explicit acquisition version2 on mining policy4')
    return True


def optimized():
    return current().get('version') in (2, 3, 4)


def cumulative_progress(attempt, outcomes, *, pairs, minimum_total=0):
    counts = outcomes.get('by_status') or {}
    processed = sum(counts.get(k,0) for k in ('mined','no_code','skipped','failed'))
    previous = max(0,processed-attempt.get('processed',0))
    total = max(minimum_total,outcomes['commits'],attempt.get('total',0)+previous)
    return {**attempt, 'attempt':{k:attempt[k] for k in ('processed','total','counts') if k in attempt},
            'processed':processed, 'total':total,
            'counts':{**counts,'pairs':pairs,'pending':max(0,total-processed)}}


def io_width(ceiling):
    from app.backend import compute
    from app.backend.pipeline.campaigns.admission import _reserve_of
    from .memory_profile import current as memory_profile
    profile = memory_profile()
    total, free = compute.memory_gb()
    if total is None or free is None: return 0
    reserve = _reserve_of(total) if profile.reserve_bytes is None else profile.reserve_bytes / 2**30
    return max(0,min(profile.io_max,int(ceiling),int(max(0,free-reserve)/profile.slot_gb)))
