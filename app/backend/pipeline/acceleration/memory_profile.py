"""One memory profile that sizes the collection campaign from the memory it may use.

The campaign used to run with fixed caches, worker counts and tickets that assume about 2 GB or more. ``choose`` turns the usable memory into one of three tiers; ``activate`` applies it
to the caches that already have a setter (module limits read at use time, ``ByteCache.resize``) and the rest of the code asks ``current()`` for the numbers it used to hard-code.

The ``normal`` tier carries exactly the values the code had before, so a machine with room and every test that does not activate a profile behave as before.
Smaller tiers only ever shrink a number (a smaller budget never gets a larger cache or more workers), and memory pressure stays a wait: below the floor the campaign pauses and is
resumed by itself, it is never killed.
"""
from dataclasses import dataclass, replace
import threading

MIB = 1 << 20
GIB = 1 << 30
CAP_NORMAL = 3_000_000_000      # the campaign's memory cap before this module (resource_control.CAP_BYTES)
FLOOR_BYTES = 350 * MIB         # under this much usable memory nothing is started: the campaign waits
TIGHT_BELOW = 900 * MIB
SMALL_BELOW = 1800 * MIB
LARGE_FROM = 6 * GIB          # from this much usable memory the campaign may run wider (only reachable through ZTSA_CAMPAIGN_MEMORY_MB or a cap above the old 3 GB)
RESERVE_MIN = 150 * MIB         # what stays free for the system in the smaller tiers
GIT_LIMITS = ('-c', 'pack.windowMemory=32m', '-c', 'pack.deltaCacheSize=16m', '-c', 'core.packedGitLimit=64m',
              '-c', 'core.packedGitWindowSize=16m', '-c', 'core.deltaBaseCacheLimit=16m', '-c', 'core.bigFileThreshold=1m')


# Numerical libraries reserve buffers for every CPU thread when they are imported: on a 12-core machine importing numpy alone commits about 360 MB, and each parser or publication child
# pays it again. The campaign does no heavy linear algebra, so one thread costs nothing in speed and takes the import stack from about 430 MB to about 70 MB (measured, tools/memory_floor_bench).
SINGLE_THREAD_ENV = {name: '1' for name in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'BLIS_NUM_THREADS', 'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS')}


def limit_threads(environ):
    """Set the one-thread variables in ``environ`` (a dict or ``os.environ``) unless a person already chose a value. Must run before numpy is imported in that process; children inherit it."""
    for name, value in SINGLE_THREAD_ENV.items():
        environ.setdefault(name, value)
    return environ


@dataclass(frozen=True)
class Profile:
    tier: str
    budget_bytes: int = 0           # usable memory the tier was chosen from (0 = not measured)
    cap_bytes: int = CAP_NORMAL     # the Governor's upper bound for the campaign's private memory
    viable: bool = True             # False below the floor: wait for memory
    reserve_bytes: int | None = None   # kept free for the system; None = the campaign admission reserve (a share of total RAM)
    ceiling_bytes: int = 2 * GIB    # private memory above which the pipeline pauses to let memory recover
    resume_needs_gb: float = 2.0    # free memory in GB (absolute, the reserve included) at which a memory pause is retried
    slot_gb: float = .25            # memory one reader slot is assumed to need
    io_max: int = 4                 # most readers at once
    ingest_workers: int | None = None      # caps (None = no cap, the old behaviour)
    parse_workers: int | None = None
    acquire_limit: int | None = None
    context_limit: int | None = None
    reader_slots: int | None = None
    tree_cache: int | None = None
    mine_threads: int | None = None
    recover_threads: int | None = None
    deps_threads: int | None = None
    parse_cache_bytes: int = 48 * MIB
    parse_cache_entries: int = 1024
    stream_bytes: int = 16 * MIB
    stream_entries: int = 4096
    tree_memo_bytes: int = 600_000
    blob_cache: int = 64 * MIB
    tree_object_cache: int = 16 * MIB
    info_cache: int = 4 * MIB
    sqlite_cache_kib: int = 8192
    osv_checkpoint: int = 64 * MIB
    parser_ticket: int = 160 * MIB
    parser_cost_idle: int = 32 * MIB
    parser_cost_busy: int = 192 * MIB
    publish_ticket: int = 256 * MIB
    git_flags: tuple = ()
    acquire_target: int | None = None      # raise (never lower) the stage widths and the git network width; None = the old constants
    parse_target: int | None = None
    network_target: int | None = None

    def capped(self, value, name):
        """``value`` limited by this profile's cap for ``name`` (a worker or slot count)."""
        cap = getattr(self, name)
        return value if cap is None else max(1, min(int(value), cap))

    def snapshot(self):
        return {'tier': self.tier, 'budget_mb': round(self.budget_bytes / MIB), 'cap_mb': round(self.cap_bytes / MIB), 'viable': self.viable,
                'io_max': self.io_max, 'ingest_workers': self.ingest_workers, 'parse_workers': self.parse_workers, 'reader_slots': self.reader_slots,
                'resume_needs_gb': self.resume_needs_gb}


NORMAL = Profile('normal')
SMALL = replace(NORMAL, tier='small', cap_bytes=SMALL_BELOW, ceiling_bytes=SMALL_BELOW, reserve_bytes=256 * MIB, resume_needs_gb=.9, slot_gb=.15, io_max=3,
                ingest_workers=2, parse_workers=2, acquire_limit=4, context_limit=4, reader_slots=4, tree_cache=8 * MIB, mine_threads=3, recover_threads=4, deps_threads=8,
                parse_cache_bytes=24 * MIB, parse_cache_entries=512, stream_bytes=8 * MIB, stream_entries=2048, tree_memo_bytes=400_000,
                blob_cache=24 * MIB, tree_object_cache=8 * MIB, info_cache=2 * MIB, sqlite_cache_kib=4096, osv_checkpoint=24 * MIB,
                parser_ticket=128 * MIB, parser_cost_idle=24 * MIB, parser_cost_busy=160 * MIB, publish_ticket=200 * MIB, git_flags=GIT_LIMITS)
TIGHT = replace(SMALL, tier='tight', cap_bytes=TIGHT_BELOW, ceiling_bytes=TIGHT_BELOW, reserve_bytes=RESERVE_MIN, resume_needs_gb=.45, slot_gb=.08, io_max=2,
                ingest_workers=1, parse_workers=1, acquire_limit=2, context_limit=2, reader_slots=2, tree_cache=4 * MIB, mine_threads=2, recover_threads=2, deps_threads=4,
                parse_cache_bytes=8 * MIB, parse_cache_entries=256, stream_bytes=4 * MIB, stream_entries=1024, tree_memo_bytes=200_000,
                blob_cache=8 * MIB, tree_object_cache=4 * MIB, info_cache=1 * MIB, sqlite_cache_kib=2048, osv_checkpoint=8 * MIB,
                parser_ticket=112 * MIB, parser_cost_idle=16 * MIB, parser_cost_busy=128 * MIB, publish_ticket=160 * MIB)
# A machine with room to spare (the 12 GB server): the pipeline was measured queue-bound on its fetch slots and its single parser while using under 2 GB, so this tier only widens those
# and enlarges the caches. Every number is at least the normal tier's, so choose() stays monotone.
LARGE = replace(NORMAL, tier='large', cap_bytes=6 * GIB, ceiling_bytes=9 * GIB // 2, resume_needs_gb=3.0, io_max=8, acquire_target=16, parse_target=2, network_target=16,
                parse_cache_bytes=192 * MIB, parse_cache_entries=4096, stream_bytes=64 * MIB, stream_entries=16384, tree_memo_bytes=2_400_000, blob_cache=256 * MIB,
                tree_object_cache=64 * MIB, info_cache=16 * MIB, sqlite_cache_kib=262144, osv_checkpoint=256 * MIB)
TIERS = {'tight': TIGHT, 'small': SMALL, 'normal': NORMAL, 'large': LARGE}


def budget(*, available, used=0, total=None, cap=None):
    """Usable bytes: what this process already holds plus what the system has free, less the reserve, never above ``cap`` (default the old 3 GB cap)."""
    if available is None:
        return 0
    reserve = max(RESERVE_MIN, int(.05 * total)) if total else RESERVE_MIN
    return max(0, min(int(cap) if cap else CAP_NORMAL, int(used or 0) + int(available) - reserve))


def choose(usable_bytes):
    """The tier for ``usable_bytes`` of memory. Pure and monotone: less memory never gives a bigger value."""
    usable = max(0, int(usable_bytes or 0))
    base = TIGHT if usable < TIGHT_BELOW else SMALL if usable < SMALL_BELOW else LARGE if usable >= LARGE_FROM else NORMAL
    return replace(base, budget_bytes=usable, viable=usable >= FLOOR_BYTES)


def measure(*, cap=None):
    """The profile for the memory this machine has free right now (``NORMAL`` unchanged when memory cannot be read: a guess must never shrink or grow the campaign)."""
    import os
    forced = os.environ.get('ZTSA_CAMPAIGN_MEMORY_MB', '').strip().lower()
    if forced not in ('', 'auto'):
        try:
            return choose(int(float(forced) * MIB))           # a person's number is used as given, even above what is free now
        except ValueError:
            pass
    try:
        import psutil
        from app.backend.pipeline.acceleration.resource_control import process_memory
        vm = psutil.virtual_memory()
        used = process_memory().get('private_bytes') or 0
        return choose(budget(available=vm.available, used=used, total=vm.total, cap=cap))
    except Exception:                                   # ignored: psutil missing or memory unreadable; the documented fallback is NORMAL, never a guess
        return NORMAL


def campaign_ram_cap() -> int:
    """The memory cap handed to the campaign worker: the profile's cap for the memory this machine may use (never below the old 3 GB, so small machines behave as before; more only for the large tier)."""
    try:
        return max(CAP_NORMAL, int(measure().cap_bytes))
    except Exception:                                   # ignored: an unmeasurable profile keeps the old 3 GB cap (CAP_NORMAL)
        return CAP_NORMAL


_lock = threading.RLock()
_active = None
_saved = None


def current():
    return _active or NORMAL


def active():
    return _active


def _setters(profile):
    """(owner, attribute, value) for every number that already lives in a module global read at use time."""
    from app.backend.pipeline import funcsplit
    from app.backend.pipeline.ingest import osv_read
    from app.backend.pipeline.acceleration import resource_control
    return [(funcsplit, 'CACHE_BYTES', profile.parse_cache_bytes), (funcsplit, 'CACHE_ENTRIES', profile.parse_cache_entries),
            (funcsplit, 'STREAM_BYTES', profile.stream_bytes), (funcsplit, 'STREAM_ENTRIES', profile.stream_entries),
            (funcsplit, 'TREE_MEMO_BYTES', profile.tree_memo_bytes), (osv_read, 'CHECKPOINT_BYTES', profile.osv_checkpoint),
            (resource_control, 'CAP_BYTES', profile.cap_bytes)]


def activate(profile):
    """Make ``profile`` the one every consumer sees and resize the existing caches. Returns the profile. ``deactivate`` restores what was there."""
    global _active, _saved
    from app.backend.pipeline.acceleration import repository_service
    with _lock:
        rows = _setters(profile)
        if _saved is None:
            _saved = {'attrs': [(owner, name, getattr(owner, name)) for owner, name, _ in rows],
                      'caches': [(repository_service._blobs, repository_service._blobs.limit), (repository_service._trees, repository_service._trees.limit),
                                 (repository_service._object_info, repository_service._object_info.limit)]}
        for owner, name, value in rows:
            setattr(owner, name, value)
        repository_service._blobs.resize(profile.blob_cache)
        repository_service._trees.resize(profile.tree_object_cache)
        repository_service._object_info.resize(profile.info_cache)
        _active = profile
        return profile


def deactivate():
    global _active, _saved
    with _lock:
        if _saved is not None:
            for owner, name, value in _saved['attrs']:
                setattr(owner, name, value)
            for cache, limit in _saved['caches']:
                cache.resize(limit)
        _active, _saved = None, None
