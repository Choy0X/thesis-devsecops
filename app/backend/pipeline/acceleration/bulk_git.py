"""Explicit bulk hydration of immutable Git blobs, followed by local-only reads."""
from pathlib import Path
import re
import threading
import time

from .metrics import current
from . import operations

class FetchSlots:
    """One shared, cancellable Git transport limit with bounded host footprint."""
    def __init__(self):
        self.condition = threading.Condition()
        self.active = 0

    def limit(self):
        from . import mining_policy
        if mining_policy.current().get('version') != 4:
            return 2
        from .resource_control import active, CAP_BYTES, HEADROOM_BYTES
        resources = active()
        if resources is None:
            return 2
        state = resources.refresh()
        private = state.get('private_bytes')
        if private is None:
            return 1
        room = min(state.get('available_bytes', 0),
                   max(0, CAP_BYTES-private-state.get('reserved_bytes', 0)))
        if state.get('commit_available_bytes') is not None:
            room = min(room, state['commit_available_bytes'])
        from .memory_profile import current as memory_profile
        return max(1, min(memory_profile().network_target or 8, (room-HEADROOM_BYTES)//(32 << 20)))

    def acquire(self, blocking=True, timeout=-1):
        deadline = None if timeout is None or timeout < 0 else time.monotonic()+timeout
        with self.condition:
            while self.active >= self.limit():
                if not blocking:
                    return False
                left = None if deadline is None else deadline-time.monotonic()
                if left is not None and left <= 0:
                    return False
                self.condition.wait(min(.2, left) if left is not None else .2)
                operations.check()
            self.active += 1
            current.maximum('git_network_peak_active', self.active)
            return True

    def release(self):
        with self.condition:
            if self.active <= 0:
                raise ValueError('No Git fetch slot to release')
            self.active -= 1
            self.condition.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


_network = FetchSlots()
_revision = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?\Z')


def _path(value):
    from app.backend.pipeline.security_snapshots import logical_path as safe_path
    if not isinstance(value, str) or '\n' in value or '\r' in value:
        raise ValueError('Invalid Git object path')
    return safe_path(value)


class Repository:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self._trees, self._available = {}, set()
        from . import mining_policy
        self.shared = None
        if mining_policy.current().get('version') in (3, 4):
            from .repository_service import service
            self.shared = service(self.path)

    def _run(self, *args, input_data=None, text=True, timeout=180):
        from app.backend.pipeline.ingest.codemine import _git
        return _git(['-C', str(self.path), *args], text=text, timeout=timeout,
                    input_data=input_data, no_lazy=True)

    def tree(self, revision):
        if self.shared is not None:
            from .repository_service import CompatibilityFallback
            try:
                if revision not in self._trees:self._trees[revision]=self.shared.tree(revision)
                return self._trees[revision]
            except CompatibilityFallback:
                current.count('git_tree_reference_fallbacks')
                self.shared = None
        if not isinstance(revision, str) or not _revision.fullmatch(revision):
            raise ValueError('An exact Git commit is required')
        if revision not in self._trees:
            from app.backend.pipeline.security_snapshots import logical_path as safe_path
            with current.timer('git_tree_index'):
                raw = self._run('ls-tree', '-r', '-z', revision, text=False)
            paths = {}
            for entry in raw.split(b'\0'):
                if not entry:
                    continue
                metadata, path = entry.split(b'\t', 1)
                mode, kind, oid = metadata.decode('ascii').split()
                if mode in ('100644', '100755') and kind == 'blob':
                    try: decoded = path.decode('utf-8')
                    except UnicodeError:
                        current.count('git_unrepresentable_paths')
                        continue
                    paths[safe_path(decoded)] = oid
            self._trees[revision] = paths
        return self._trees[revision]

    def prefetch(self, refs):
        from app.backend.pipeline.ingest.codemine import _repo_lock, FetchError
        refs = list(refs)
        stats = {'requested_paths': len(refs), 'unique_objects': 0,
                 'missing_objects': 0, 'network_batches': 0}
        def resolve():
            trees,wanted={},{}
            for revision,path in refs:
                if not isinstance(revision,str):raise ValueError('An exact Git commit is required')
                if revision not in trees:trees[revision]=self.tree(revision)
                oid=trees[revision].get(_path(path))
                if oid is not None:wanted[oid]=None
            return wanted
        wanted=None
        if self.shared is not None:
            wanted = resolve()
            if set(wanted).issubset(self._available):
                stats['unique_objects'] = len(wanted)
                current.count('git_verified_prefetch_hits')
                for name,value in stats.items():current.count('git_prefetch_'+name,value)
                return stats
        with _repo_lock(self.path), current.timer('git_prefetch'):
            if wanted is None:wanted=resolve()
            self._prefetch_locked(wanted,stats)
        for name, value in stats.items():
            current.count('git_prefetch_' + name, value)
        return stats

    def prefetch_oids(self,oids):
        """Hydrate blobs already bound by an exact raw Git snapshot diff.

        This low-level entry does not establish path provenance. The metadata
        caller must retain its snapshot proof, raw paths, modes and revisions.
        """
        from app.backend.pipeline.ingest.codemine import _repo_lock
        oids=list(oids)
        if any(not isinstance(oid,str) or not _revision.fullmatch(oid) or not oid.strip('0') for oid in oids):
            raise ValueError('Exact non-null Git blob identifiers are required')
        stats={'requested_paths':len(oids),'unique_objects':len(set(oids)),
               'missing_objects':0,'network_batches':0}
        wanted=dict.fromkeys(oids)
        if set(wanted).issubset(self._available):current.count('git_verified_prefetch_hits')
        else:
            with _repo_lock(self.path),current.timer('git_prefetch'):
                self._prefetch_locked(wanted,stats)
        for name,value in stats.items():current.count('git_prefetch_'+name,value)
        return stats

    def _prefetch_locked(self,wanted,stats):
        from app.backend.pipeline.ingest.codemine import FetchError
        stats['unique_objects']=len(wanted)
        pending=[oid for oid in wanted if oid not in self._available]
        if not pending:return
        rows=self._inventory(pending)
        if len(rows)!=len(pending):raise FetchError('Incomplete Git object inventory')
        missing=[]
        for oid,row in zip(pending,rows):
            if row==oid+' missing':missing.append(oid)
            elif row.split()[:2]==[oid,'blob']:self._available.add(oid)
            else:raise FetchError('Invalid Git blob inventory')
        stats['missing_objects']=len(missing)
        for start in range(0,len(missing),256):
            batch=missing[start:start+256]
            with current.timer('git_network_fetch'):
                self._run('-c','fetch.negotiationAlgorithm=noop','fetch','origin',
                    '--no-tags','--no-write-fetch-head','--recurse-submodules=no',
                    '--no-auto-maintenance','--filter=blob:none','--stdin',
                    input_data=('\n'.join(batch)+'\n').encode())
            stats['network_batches']+=1
            if self.shared:self.shared.invalidate_availability()
            rows=self._inventory(batch)
            if len(rows)!=len(batch) or any(row.split()[:2]!=[oid,'blob'] for oid,row in zip(batch,rows)):
                raise FetchError('The remote did not supply the requested Git blobs')
            self._available.update(batch)

    def _inventory(self,oids):
        if self.shared:
            return [oid+' missing' if row is None else ' '.join(map(str,row)) for oid,row in zip(oids,self.shared.info_many(oids))]
        return self._run('cat-file','--batch-check',input_data=('\n'.join(oids)+'\n').encode()).splitlines()

    def read(self, revision, path, *, limit=2**63-1):
        if self.shared is not None:
            from app.backend.pipeline.security_snapshots import MAX_BLOB_BYTES
            path=_path(path)
            tree=self.tree(revision)
            # This Repository already owns the validated exact snapshot binding.
            # tree() can switch to reference Git when interpretation is unusual.
            if self.shared is not None:
                oid=tree.get(path)
                return self.shared._read_blob(oid,limit=min(limit,MAX_BLOB_BYTES)) if oid is not None else None
        if _path(path) not in self.tree(revision):
            return None
        from .git_objects import read
        return read(self.path, revision, path, limit=limit)
