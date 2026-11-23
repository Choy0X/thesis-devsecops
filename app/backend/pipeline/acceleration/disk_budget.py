"""Shared additional-cache budget. Historical bytes are never evicted."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid

_active=ContextVar('mining_disk_budget',default=None)


def size_of(root):
    total=0
    pending=[root]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():continue
                        if entry.is_dir(follow_symlinks=False):pending.append(entry.path)
                        else:total+=entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:pass
        except (FileNotFoundError,NotADirectoryError):pass
    return total


def count_used(root, baselines):
    return size_of(root)+sum(max(0,size_of(Path(path))-baseline) for path,baseline in baselines.items())


LIMIT_ENV='ZTSA_CAMPAIGN_CACHE_LIMIT_GB'
AUTO_MARGIN=10_000_000_000      # beyond the reserve, this much of the drive is never planned for the cache


def adaptive_limit(floor,used,free,reserve,margin=AUTO_MARGIN):
    """The cache cap for a drive with ``free`` bytes left while the cache already holds ``used``: everything the cache and the free space add up to, less the reserve and a margin,
    never below ``floor`` (the old fixed 20 GB). Pure, so it can be tested without a drive."""
    return max(int(floor),int(used)+int(free)-int(reserve)-int(margin))


class Budget:
    def __init__(self,root,*,additional=(),limit=20_000_000_000,reserve=15_000_000_000,headroom=256<<20,scanner_factory=None,auto_limit=False):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.limit,self.reserve,self.headroom=int(limit),int(reserve),int(headroom)
        self.auto_limit=bool(auto_limit)
        self.lock=threading.RLock();self.at=float("-inf");self.used=0;self.free=0;self.reclaim=None
        self.scanner_factory=scanner_factory;self.scanner=None
        self.scanner_lock=threading.RLock();self.closed=False
        self.baselines={}
        # A durable baseline excludes pre-existing source material, never later growth.
        from app.backend.pipeline import oslock
        with oslock.FileLock(self.root/'budget.lock',note=False):
            marker=self.root/'budget-baseline.json'
            if marker.exists():self.baselines=json.loads(marker.read_text(encoding='utf-8'))
            for path in additional:
                path=Path(path).resolve()
                if path.is_relative_to(self.root):continue
                if str(path) not in self.baselines:self.baselines[str(path)]=size_of(path)
            temporary=marker.with_name(marker.name+'.'+uuid.uuid4().hex+'.tmp')
            try:
                temporary.write_text(json.dumps(self.baselines,sort_keys=True),encoding='utf-8')
                os.replace(temporary,marker)
            finally:temporary.unlink(missing_ok=True)
        explicit=os.environ.get(LIMIT_ENV,'').strip()
        if explicit:
            try:self.limit=int(float(explicit)*1_000_000_000)      # a person's number is used as given
            except ValueError:pass
        elif self.auto_limit:
            # The 20 GB default was sized for one ecosystem; a campaign that mines two fills it while the drive still has room. Follow the drive instead (the reserve below still
            # guarantees free space).
            try:
                self.limit=adaptive_limit(self.limit,count_used(self.root,self.baselines),shutil.disk_usage(self.root).free,self.reserve)
            except OSError:pass

    @contextmanager
    def _observation(self):
        # Operation.check is deliberately direct: operations.check would recurse
        # into this Budget while it is already waiting for a fresh observation.
        from .operations import current
        operation=current()
        while True:
            if operation is not None:operation.check()
            if self.closed:raise RuntimeError('Disk budget is closed')
            if self.lock.acquire(timeout=.05):break
        try:
            if operation is not None:operation.check()
            if self.closed:raise RuntimeError('Disk budget is closed')
            yield
        finally:self.lock.release()

    def snapshot(self,force=False,*,fresh=False):
        with self._observation():
            if force or time.monotonic()-self.at>=1:
                if self.scanner_factory is None:
                    used=count_used(self.root,self.baselines)
                else:
                    with self.scanner_lock:
                        if self.closed:raise RuntimeError('Disk budget is closed')
                        if self.scanner is None:
                            self.scanner=self.scanner_factory(self.root,dict(self.baselines))
                        scanner=self.scanner
                    used=scanner.scan()
                    if type(used) is not int or used<0:
                        raise ValueError('Invalid disk scanner byte total')
                free=shutil.disk_usage(self.root).free
                # Do not install partial observations after a failed recount or
                # volume query, and start the same one-second cadence at the end.
                self.used,self.free=used,free
                self.at=time.monotonic()
            elif fresh:
                self.free=shutil.disk_usage(self.root).free
            return {'bytes':self.used,'limit_bytes':self.limit,'headroom_bytes':self.headroom,
                    'free_bytes':self.free,'reserve_bytes':self.reserve}

    def close(self):
        # Independent of the observation lock: close must interrupt a scan that
        # owns it. Keep the scanner reference if termination cannot be confirmed.
        with self.scanner_lock:
            self.closed=True
            scanner=self.scanner
        if scanner is not None:scanner.close()

    def check(self,needed=0,*,force=False,fresh=False):
        from .pipeline_spool import DiskPressure
        # Cooperative cancellation checks need one observation per second, not
        # one Windows volume query per object/lock. Data writes and subprocess
        # admission still check current free space before consuming more disk.
        value=self.snapshot(force,fresh=fresh or needed>0)
        shortage=max(value['bytes']+needed+self.headroom-self.limit,self.reserve+self.headroom+needed-value['free_bytes'])
        if shortage>0 and self.reclaim:
            self.reclaim(shortage+(64<<10))
            value=self.snapshot(force=True)
        if value['bytes']+needed+self.headroom>self.limit or value['free_bytes']-needed<self.reserve+self.headroom:
            raise DiskPressure('Additional SSD cache budget or disk reserve reached; retained source and active checkpoints preserved')
        return value


@contextmanager
def use(budget):
    token=_active.set(budget)
    try:yield budget
    finally:_active.reset(token)


def check(needed=0,*,fresh=False):
    if budget:=_active.get():return budget.check(needed,fresh=fresh)
