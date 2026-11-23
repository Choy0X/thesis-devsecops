"""Exact immutable Git objects through a persistent, locally bounded command pipe."""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import datetime
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import weakref

from . import operations
from .metrics import current


class CompatibilityFallback(RuntimeError):
    pass


class ByteCache:
    def __init__(self, limit):
        self.limit,self.size=int(limit),0
        self.values=OrderedDict()
        self.lock=threading.RLock()

    def resize(self,limit):
        with self.lock:
            self.limit=max(0,int(limit))
            while self.values and self.size>self.limit:
                _,(_,size)=self.values.popitem(last=False)
                self.size-=size

    def get(self,key):
        with self.lock:
            item=self.values.pop(key,None)
            if item is not None:self.values[key]=item;return item[0]

    def put(self,key,value,size):
        with self.lock:
            if old:=self.values.pop(key,None):self.size-=old[1]
            if size>self.limit:return
            while self.values and self.size+size>self.limit:
                _,(_,n)=self.values.popitem(last=False);self.size-=n
            self.values[key]=(value,size);self.size+=size


class SnapshotTree(dict):
    """Weakly reusable while a snapshot or loader still owns this exact index."""
    pass


_object_info=ByteCache(4<<20)
_blobs=ByteCache(64<<20)
_trees=ByteCache(16<<20)
_entries=ByteCache(96<<20)      # parsed directory objects by (repository, object id); see Service._parse_entries
_revision=re.compile(r"[0-9a-f]{40}\Z")
_drive=re.compile(r"[A-Za-z]:")


_live_readers=set()
_reader_guard=threading.RLock()
_lease_resources=ContextVar('git_reader_lease_resources',default=None)
_READER_STARTUP_BYTES=32<<20


class CommandReader:
    def __init__(self,repository,*,isolated=False):
        from .processes import spawn
        self.lock=threading.RLock()
        self.process=self.executor=None
        self.closed=False
        env=dict(os.environ)
        if isolated:
            env={key:value for key,value in env.items() if not key.upper().startswith('GIT_')}
            env.update(GIT_NO_REPLACE_OBJECTS='1',GIT_CONFIG_NOSYSTEM='1',
                       GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_SYSTEM=os.devnull)
        env.update(GIT_NO_LAZY_FETCH='1',GIT_TERMINAL_PROMPT='0')
        location=['--git-dir',str(repository)] if isolated else ['-C',str(repository)]
        from .memory_profile import current as memory_profile
        self.process=spawn(["git",*memory_profile().git_flags,*location,"cat-file","--batch-command","--buffer"],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            env=env,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        with _reader_guard:_live_readers.add(self)
        self.executor=ThreadPoolExecutor(1,thread_name_prefix="git-command")

    def _exchange(self,commands,limit):
        p=self.process
        p.stdin.write(b"".join((command+"\n").encode("ascii") for command in commands)+b"flush\n")
        p.stdin.flush()
        out=[]
        for command in commands:
            header=p.stdout.readline(4096)
            if header.endswith(b" missing\n") or header.endswith(b" ambiguous\n"):
                out.append(None);continue
            fields=header.rstrip(b"\n").split()
            if len(fields)!=3 or not fields[2].isdigit():raise OSError("Invalid Git command response")
            oid,kind,size=fields[0].decode(),fields[1].decode(),int(fields[2])
            body=None
            if command.startswith("contents "):
                if size>limit:raise OSError("Git object exceeds bounded read limit")
                chunks=[];remaining=size
                while remaining:
                    data=p.stdout.read(min(remaining,65536))
                    if not data:raise OSError("Truncated Git object")
                    chunks.append(data);remaining-=len(data)
                body=b"".join(chunks)
                if p.stdout.read(1)!=b"\n":raise OSError("Invalid Git object terminator")
            out.append((oid,kind,size,body))
        return out

    def exchange(self,commands,limit=64<<20):
        with operations.acquire(self.lock,disk=False):
            if self.closed:raise OSError("Git command reader closed")
            def batches():
                out=[]
                for start in range(0,len(commands),64):out.extend(self._exchange(commands[start:start+64],limit))
                return out
            try:
                with operations.resource(self.close):
                    from .submission import submit_owned
                    future=submit_owned(self.executor,batches)
                    deadline=time.monotonic()+operations.timeout(30,disk=False)
                    while True:
                        operations.check(disk=False)
                        left=deadline-time.monotonic()
                        if left<=0:raise TimeoutError()
                        try:return future.result(timeout=min(.2,left))
                        except TimeoutError:continue
            except BaseException:
                self.close();raise

    def close(self):
        if self.closed:return
        from .processes import terminate
        if self.process is not None:
            terminate(self.process)
        if self.executor is not None:self.executor.shutdown(wait=True,cancel_futures=True)
        if self.process is not None:
            self.process.stdin.close();self.process.stdout.close()
        self.closed=True
        with _reader_guard:_live_readers.discard(self)


class Service:
    def __init__(self,repository):
        self.path=Path(repository).resolve()
        self.gitdir=self.path/".git" if (self.path/".git").is_dir() else self.path
        self.lock=threading.RLock()
        self.reader=None
        self.diff_readers={}
        self.generation=0
        self.metadata_cache={}
        self.active_trees=weakref.WeakValueDictionary()
        self.last_used=time.monotonic()
        self._lease_users=0
        self._retiring=self._starting=False
        self._governor=None
        self._startup_tickets={}
        self._pending_startup=None
        self._pending_startup_admitted=False

    @contextmanager
    def reader_lease(self, *, resources=None):
        """Pin one local Git stage burst; waiting never owns the registry guard.

        Callers enter before repository/service locks and release before remote
        registry work. Retained loader handles do not own these leases.
        """
        token=_lease_resources.set(resources if resources is not None else _lease_resources.get())
        try:
            _touch_reader(self,pin=True)
            try:
                yield self
            finally:
                with _guard:self._lease_users-=1
        finally:
            _lease_resources.reset(token)

    def _release_startup(self,ticket,governor):
        try:
            governor.release(ticket)
        except BaseException as cleanup:
            _cleanup_failed(self,cleanup)
            raise
        self._startup_tickets.pop(ticket,None)
        pending=self._pending_startup
        if pending is not None and pending[0] is ticket:
            self._pending_startup_admitted=False
            self._pending_startup=None

    def _start_reader(self,factory,args,assign):
        from .resource_control import active, AdmissionDeferred
        operation=operations.current()
        startup_credit=getattr(operation,'_reader_startup_credit',None)
        governor=startup_credit[0] if startup_credit is not None else (_lease_resources.get() or active())
        ticket=object();attached=False
        # Allocate the retry identity before any credit moves. The existing
        # attribute slots remain usable if insertion into the ownership map fails.
        pending=(ticket,governor) if governor is not None else None
        with _guard:self._starting=True
        try:
            if self._pending_startup_admitted or self._startup_tickets:
                error=MemoryError('Git reader admission cleanup remains unconfirmed')
                _cleanup_failed(self,error)
                raise error
            if governor is not None:
                self._pending_startup=pending
                parent=startup_credit[1] if startup_credit is not None else getattr(operation,'resource_ticket',None)
                borrow=getattr(governor,'borrow_startup',None)
                if startup_credit is not None:
                    prepaid=borrow is not None and borrow(parent,ticket,_READER_STARTUP_BYTES,
                        expected_generation=startup_credit[2])
                else:
                    prepaid=borrow is not None and borrow(parent,ticket,_READER_STARTUP_BYTES)
                if not prepaid and not governor.reserve(ticket,_READER_STARTUP_BYTES):
                    current.count('git_reader_startup_denied')
                    stage=(parent[1] if isinstance(parent,tuple) and len(parent)==2
                           and parent[1] in ('acquire','parse','context','publish') else 'unowned')
                    current.count('git_reader_startup_denied_'+stage)
                    current.count('git_reader_startup_denied_bytes',_READER_STARTUP_BYTES)
                    governor.forget_blocked(ticket)
                    raise AdmissionDeferred('Git reader startup was not admitted',
                        resource='git_reader',required_bytes=_READER_STARTUP_BYTES)
                self._pending_startup_admitted=True
                self._startup_tickets[ticket]=governor
                self._pending_startup_admitted=False
                self._pending_startup=None
                if prepaid:current.count('git_reader_startup_prepaid')
                if self._governor is None:
                    self._governor=governor
                    governor.trimmers.append(self.trim)
            # Attach before initialization so even a partially constructed child
            # stays owned if its constructor or subsequent cleanup fails.
            reader=factory.__new__(factory)
            reader.process=reader.executor=None
            reader.lock=threading.RLock()
            reader.closed=False
            reader._service_initialized=False
            assign(reader);attached=True
            factory.__init__(reader,*args)
            reader._service_initialized=True
            if governor is not None:governor.refresh(force=True)
        except BaseException:
            if attached:
                try:self.close()
                except BaseException as cleanup:
                    _cleanup_failed(self,cleanup)
                    raise
            elif ticket in self._startup_tickets or (self._pending_startup is pending
                    and self._pending_startup_admitted):
                self._release_startup(ticket,governor)
                if self.reader is None and not self.diff_readers and self._governor is governor:
                    if self.trim in governor.trimmers:governor.trimmers.remove(self.trim)
                    self._governor=None
            raise
        else:
            if governor is not None:self._release_startup(ticket,governor)
            return reader
        finally:
            if self._pending_startup is pending and not self._pending_startup_admitted:
                self._pending_startup=None
            with _guard:self._starting=False

    def _reader(self):
        with self.lock:
            self.last_used=time.monotonic()
            _touch_reader(self)
            if self.reader is None or self.reader.closed:
                # Starting a child retains synchronous admission. Reusing its
                # no-lazy-fetch pipe only checks cancellation and deadlines.
                operations.check()
                self._start_reader(CommandReader,(self.path,),lambda reader:setattr(self,'reader',reader))
                current.count('git_reader_starts')
            return self.reader

    def trim(self,budget):
        governor=self._governor
        worker=id(getattr(self.reader,'process',None))
        if governor is None or not governor.reclaim_needed(budget,worker=worker):return False
        with _guard:
            victim=_retirement(self)
        if victim is None:return False
        _retire(victim,reason='pressure')
        governor.refresh(force=True)
        return True

    def _exchange(self,commands,limit=64<<20):
        # Keep reader selection and use atomic with idle-service retirement.
        # Returning a reader before acquiring its lock permits LRU eviction to
        # close it in the handoff gap even though this operation is active.
        with operations.acquire(self.lock,disk=False):
            return self._reader().exchange(commands,limit)

    def diff(self, metadata, preset):
        """The same guarded service owns both fixed diff formats and cat-file."""
        from .diff_reader import DiffReader, PRESETS, interpretation
        if preset not in PRESETS:
            raise ValueError('Unsupported persistent diff preset')
        with operations.acquire(self.lock):
            revision, parent = metadata.get('sha'), metadata.get('parent')
            self._exact(revision)
            if parent is not None:
                self._exact(parent, allow_shallow=True)
            identifiers = [revision, parent] if parent is not None else [revision]
            infos = self.info_many(identifiers)
            if any(info is None or info[1] != 'commit' for info in infos):
                raise CompatibilityFallback('Persistent diff exact commit or parent is unavailable')
            actual = self._metadata(revision)
            if actual['parent'] != parent:
                raise CompatibilityFallback('Persistent diff parent does not match exact commit')
            signature = interpretation(self.path)
            reader = self.diff_readers.get(preset)
            if reader is not None and not reader.closed and reader.signature != signature:
                reader.close()
            if reader is None or reader.closed:
                _touch_reader(self)
                reader = self._start_reader(DiffReader,(self.path,preset),
                    lambda value:self.diff_readers.__setitem__(preset,value))
                current.count('git_persistent_diff_reader_starts')
            _touch_reader(self)
            result = reader.exchange(revision, parent)
            if interpretation(self.path) != signature:
                reader.close()
                raise CompatibilityFallback('Git interpretation changed during persistent diff')
            current.count('git_persistent_diff_requests')
            current.count('git_persistent_diff_bytes', len(result))
            return result

    @property
    def worker_pid(self):return self._reader().process.pid

    def _exact(self,revision,*,allow_shallow=False):
        if not isinstance(revision,str) or not _revision.fullmatch(revision):
            raise CompatibilityFallback("Persistent metadata currently requires an exact SHA-1 revision")
        if (self.gitdir/"info/grafts").exists() or any((self.gitdir/"refs/replace").glob("*")):
            raise CompatibilityFallback("Replacement refs or grafts require reference Git interpretation")
        packed=self.gitdir/"packed-refs"
        if packed.exists() and "refs/replace/" in packed.read_text(encoding="ascii",errors="replace"):
            raise CompatibilityFallback("Packed replacement refs require reference Git interpretation")
        shallow=self.gitdir/"shallow"
        if not allow_shallow and shallow.exists() and revision in shallow.read_text().splitlines():
            raise CompatibilityFallback("Selected commit is a shallow boundary")

    def info_many(self,oids):
        if any(not _revision.fullmatch(oid) for oid in oids):raise ValueError("Invalid object identifier")
        if not oids:return []
        current.count("git_service_info_items",len(oids))
        results=[None if row is None else row[:3] for row in self._exchange(["info "+oid for oid in oids])]
        for oid,row in zip(oids,results):
            if row is not None:_object_info.put((str(self.path),oid),row,160)
        return results

    def _object(self,oid,limit=64<<20):
        if not _revision.fullmatch(oid):raise ValueError("Invalid object identifier")
        row=self._exchange(["contents "+oid],limit)[0]
        return row

    def metadata(self,revision):
        with self.lock:
            return self._metadata(revision)

    def _metadata(self,revision,*,allow_shallow=False):
        self._exact(revision,allow_shallow=allow_shallow)
        if revision in self.metadata_cache:return dict(self.metadata_cache[revision])
        row=self._object(revision,1<<20)
        if row is None or row[1]!="commit":raise CompatibilityFallback("Commit object unavailable")
        header,separator,body=row[3].partition(b"\n\n")
        if not separator or b"\nencoding " in b"\n"+header:
            raise CompatibilityFallback("Commit encoding requires reference Git formatting")
        values={};parents=[]
        for line in header.splitlines():
            if line.startswith(b"parent "):parents.append(line[7:].decode("ascii"))
            elif line.startswith(b"tree "):values["tree"]=line[5:].decode("ascii")
            elif line.startswith(b"committer "):
                stamp=re.search(rb"> (-?\d+) ([+-]\d{4})$",line)
                if stamp is None:raise CompatibilityFallback("Unsupported commit timestamp")
                zone=stamp[2].decode();minutes=int(zone[1:3])*60+int(zone[3:])
                tz=datetime.timezone(datetime.timedelta(minutes=minutes if zone[0]=="+" else -minutes))
                values["date"]=datetime.datetime.fromtimestamp(int(stamp[1]),tz).isoformat(timespec="seconds")
        values.update(sha=revision,parents=parents,parent=parents[0] if parents else None,message=body.decode("utf-8","replace"))
        if "tree" not in values or "date" not in values:raise CompatibilityFallback("Incomplete commit object")
        self.metadata_cache[revision]=values
        current.count("git_service_metadata_items")
        return dict(values)

    def snapshot_metadata(self,revision,*,owned_root):
        """Prove an exact policy-4 snapshot independently of shallow history.

        This does not change generic parent traversal. Proof is never cached:
        all commit and directory objects must still be present on every call.
        """
        with operations.acquire(self.lock):
            self._exact(revision,allow_shallow=True)
            shallow=self.gitdir/'shallow'
            if not shallow.exists() or revision not in shallow.read_text().splitlines():
                return self._metadata(revision)
            from . import mining_policy
            if mining_policy.current().get('version')!=4:
                raise CompatibilityFallback('Selected commit is a shallow boundary')
            overrides=('GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE','GIT_COMMON_DIR',
                'GIT_OBJECT_DIRECTORY','GIT_ALTERNATE_OBJECT_DIRECTORIES','GIT_REPLACE_REF_BASE',
                'GIT_NO_REPLACE_OBJECTS','GIT_SHALLOW_FILE','GIT_GRAFT_FILE','GIT_CONFIG_PARAMETERS')
            if any(os.environ.get(name) for name in overrides) or os.environ.get('GIT_CONFIG_COUNT','0') not in ('','0'):
                raise CompatibilityFallback('Snapshot environment requires reference interpretation')
            root=Path(owned_root).resolve()
            try:relative=self.path.relative_to(root)
            except ValueError:
                raise CompatibilityFallback('Shallow snapshot requires an owned bare cache') from None
            if self.gitdir!=self.path or len(relative.parts)!=3 or self.path.suffix!='.git':
                raise CompatibilityFallback('Shallow snapshot requires an owned bare cache')
            import configparser
            try:
                config_path=self.path/'config'
                if config_path.stat().st_size>1<<20:
                    raise CompatibilityFallback('Owned snapshot configuration exceeds metadata bound')
                config=configparser.RawConfigParser(strict=False)
                config.read_string(config_path.read_text(encoding='utf-8-sig'))
                if not config.getboolean('core','bare'):raise ValueError('Not bare')
            except (OSError,UnicodeError,configparser.Error,ValueError) as error:
                raise CompatibilityFallback('Shallow snapshot requires an owned bare cache') from error
            budget=16<<20
            info=self.info_many([revision])[0]
            if info is None or info[1]!='commit' or info[2]>1<<20:
                raise CompatibilityFallback('Exact snapshot commit is unavailable or exceeds metadata bound')
            budget-=info[2]
            metadata=self._metadata(revision,allow_shallow=True)
            parents=metadata['parents']
            if any(not _revision.fullmatch(parent) for parent in parents):
                raise CompatibilityFallback('Invalid exact snapshot parent')
            roots=[metadata['tree']]
            for parent,info in zip(parents,self.info_many(parents)):
                operations.check()
                self._exact(parent,allow_shallow=True)
                if info is None or info[1]!='commit':
                    raise CompatibilityFallback('Exact snapshot parent is unavailable locally')
                if info[2]>1<<20 or info[2]>budget:
                    raise CompatibilityFallback('Snapshot parent exceeds metadata budget')
                budget-=info[2]
                row=self._object(parent,1<<20)
                if row is None or row[1]!='commit':
                    raise CompatibilityFallback('Exact snapshot parent is unavailable locally')
                match=re.search(rb'\Atree ([0-9a-f]{40})\n',row[3])
                if match is None:raise CompatibilityFallback('Invalid snapshot parent tree')
                roots.append(match[1].decode('ascii'))
            self._snapshot_tree_proof(roots,budget)
            current.count('git_shallow_snapshot_proofs')
            return dict(metadata)

    def _snapshot_tree_proof(self,roots,budget):
        # Unlike path indexes, this proof follows raw directory OIDs even when
        # entry names are not UTF-8. No path cache can mask a missing subtree.
        pending=list(dict.fromkeys(roots));seen=set(pending)
        if any(not _revision.fullmatch(oid) for oid in pending):
            raise CompatibilityFallback('Invalid snapshot tree')
        while pending:
            operations.check(disk=False)
            frontier=pending[:64];del pending[:64]
            infos=self.info_many(frontier)
            if any(info is None or info[1]!='tree' for info in infos):
                raise CompatibilityFallback('Exact snapshot tree is unavailable locally')
            size=sum(info[2] for info in infos)
            if size>budget:raise CompatibilityFallback('Snapshot trees exceed metadata budget')
            budget-=size
            for row in self._exchange(['contents '+oid for oid in frontier],16<<20):
                operations.check(disk=False)
                if row is None or row[1]!='tree':
                    raise CompatibilityFallback('Exact snapshot tree is unavailable locally')
                raw=row[3];offset=0
                while offset<len(raw):
                    space=raw.find(b' ',offset);end=raw.find(b'\0',space+1)
                    if space<offset or end<space+2 or end+21>len(raw):
                        raise CompatibilityFallback('Invalid exact snapshot tree')
                    mode=raw[offset:space];name=raw[space+1:end];child=raw[end+1:end+21].hex()
                    offset=end+21
                    if b'/' in name or mode not in (b'40000',b'100644',b'100755',b'120000',b'160000'):
                        raise CompatibilityFallback('Unsupported exact snapshot tree')
                    if mode==b'40000' and child not in seen:
                        seen.add(child);pending.append(child)
                        if len(seen)>65536:raise CompatibilityFallback('Snapshot tree object count exceeds metadata bound')

    def tree(self,revision):
        with self.lock:
            return self._tree(revision)

    def _tree(self,revision):
        self._exact(revision,allow_shallow=True)
        active=self.active_trees.get(revision)
        if active is not None:return active
        key=(str(self.path),revision)
        cached=_trees.get(key)
        if cached is not None:
            current.count("git_service_tree_hits")
            self.active_trees[revision]=cached
            return cached
        commit=self._object(revision,1<<20)
        if commit is None or commit[1]!='commit':raise OSError('Missing exact snapshot commit')
        header=commit[3].partition(b'\n\n')[0]
        match=re.search(rb'(?:^|\n)tree ([0-9a-f]{40})(?:\n|$)',header)
        if match is None:raise OSError('Invalid snapshot commit tree')
        root=match[1].decode('ascii')
        paths=SnapshotTree();queue=[("",root)];repo=str(self.path)
        while queue:
            frontier=queue[:64];del queue[:64]
            missing=list(dict.fromkeys(oid for _,oid in frontier if _entries.get((repo,oid)) is None))
            if missing:                                    # a directory object is parsed once per repository and reused by every revision that shares it
                objects=self._exchange(['contents '+oid for oid in missing])
                for oid,row in zip(missing,objects):
                    if row is None or row[1]!="tree":raise OSError("Missing exact snapshot tree")
                    entries=self._parse_entries(row[3])
                    _entries.put((repo,oid),entries,sum(len(n)+88 for n,_ in entries[0])+sum(len(n)+88 for n,_ in entries[1])+120)
            for prefix,oid in frontier:
                files,dirs=_entries.get((repo,oid)) or self._parse_entries_of(oid)
                if not prefix and any(_drive.match(name) for name,_ in (*files,*dirs)):
                    raise ValueError("invalid snapshot path")            # a drive prefix is refused on the first component only
                for name,blob in files:
                    paths[prefix+name]=blob
                for name,child in dirs:
                    queue.append((prefix+name+"/",child))
        _trees.put(key,paths,sum(len(k.encode())+len(v)+144 for k,v in paths.items()))
        self.active_trees[revision]=paths
        return paths

    def _parse_entries_of(self,oid):
        row=self._exchange(['contents '+oid])[0]
        if row is None or row[1]!="tree":raise OSError("Missing exact snapshot tree")
        return self._parse_entries(row[3])

    @staticmethod
    def _parse_entries(raw):
        """(files, dirs) of one directory object: [(name, blob id)] and [(name, child directory id)], names validated as a single path component. Cached by object id, because
        a directory object is the same in every revision that contains it (the Linux tree has about 5,000 of them and a commit changes a handful)."""
        files=[];dirs=[];offset=0
        while offset<len(raw):
            space=raw.index(b" ",offset);end=raw.index(b"\0",space)
            mode=raw[offset:space];name=raw[space+1:end];child=raw[end+1:end+21]
            if len(child)!=20:raise OSError("Truncated Git tree")
            offset=end+21
            try:name=name.decode("utf-8")
            except UnicodeError:
                current.count("git_unrepresentable_paths");continue
            if mode not in (b"40000",b"100644",b"100755"):continue
            if name in ("","."):continue                   # logical_path drops these components
            if name==".." or any(c in name for c in "\\\0\r\n"):raise ValueError("invalid snapshot path")
            (dirs if mode==b"40000" else files).append((name,child.hex()))
        return (tuple(files),tuple(dirs))

    def _parse_tree(self,prefix,raw,paths,queue):
        files,dirs=self._parse_entries(raw)
        for name,blob in files:paths[prefix+name]=blob
        for name,child in dirs:queue.append((prefix+name+"/",child))

    def read(self,revision,path,*,limit=1<<20):
        from app.backend.pipeline.security_snapshots import logical_path as safe_path
        if not isinstance(path,str) or any(c in path for c in "\\\0\r\n"):raise ValueError("Invalid Git source path")
        path=safe_path(path)
        oid=self.tree(revision).get(path)
        if oid is None:return None
        return self._read_blob(oid,limit=limit)

    def _read_blob(self,oid,*,limit=1<<20):
        """Read an immutable blob selected from an already validated exact tree."""
        cached=_blobs.get(oid)
        if cached is not None:return cached if len(cached)<=limit else None
        info=_object_info.get((str(self.path),oid))
        if info is None:info=self.info_many([oid])[0]
        if info is None or info[1]!="blob" or info[2]>limit:return None
        row=self._object(oid,limit)
        if row is None or row[1]!="blob":return None
        data=row[3]
        _blobs.put(oid,data,len(data));current.count("git_blob_bytes",len(data))
        return data

    def invalidate_availability(self):self.generation+=1

    def close(self):
        with self.lock:
            failure = None
            for reader in [self.reader, *self.diff_readers.values()]:
                if reader is None:
                    continue
                try:
                    if not getattr(reader,'_service_initialized',True) and reader.process is None:
                        reader.closed=True  # Initialization never created a child.
                    elif not getattr(reader,'_service_initialized',True) and reader.executor is None:
                        from .processes import terminate
                        terminate(reader.process)
                        for name in ('stdin','stdout','stderr'):
                            pipe=getattr(reader.process,name,None)
                            if pipe is not None:pipe.close()
                        reader.closed=True
                        with _reader_guard:_live_readers.discard(reader)
                    else:
                        reader.close()
                except Exception as error:
                    if failure is None:
                        failure = error
            self.diff_readers = {key: reader for key, reader in self.diff_readers.items() if not reader.closed}
            if failure is not None:
                raise failure
            pending=self._pending_startup
            if pending is not None:
                if self._pending_startup_admitted:
                    ticket,governor=pending
                    governor.release(ticket)
                    self._startup_tickets.pop(ticket,None)
                self._pending_startup_admitted=False
                self._pending_startup=None
            for ticket,governor in list(self._startup_tickets.items()):
                governor.release(ticket)
                del self._startup_tickets[ticket]
            if self._governor is not None:
                if self.trim in self._governor.trimmers:self._governor.trimmers.remove(self.trim)
                self._governor=None
            with _guard:_open_services.pop(id(self),None)

    def __enter__(self):return self
    def __exit__(self,*args):self.close()


# Canonical handles stay reusable while loaders own them. Only open pipes have
# strong LRU ownership, so a retained handle cannot reopen an untracked process.
_services=weakref.WeakValueDictionary()
_open_services=OrderedDict()
_guard=threading.RLock()


def _cleanup_failed(item,error):
    # Failed cleanup is fatal to an active stage, and its retry closer stays
    # registered. Never run callback-producing cancellation under cache locks.
    operation=operations.current()
    if operation is not None:
        token=('git-reader-cleanup',id(item))
        with operation.guard:
            operation.resources[token]=item.close
            operation.failed_resources.add(token)
            operation.cleanup_errors.append(str(error))
        operation.cancelled.set()


def _retirement(item):
    """Select under _guard, retaining capacity until synchronous close confirms."""
    if item._lease_users or item._retiring or item._starting or not item.lock.acquire(blocking=False):return None
    reader=item.reader
    if reader is not None and not reader.lock.acquire(blocking=False):
        item.lock.release()
        return None
    item._retiring=True
    return item,reader


def _retire(victim,*,reason='lru'):
    item,reader=victim
    try:
        item.close()
        current.count('git_reader_'+reason+'_retirements')
    except BaseException as error:
        _cleanup_failed(item,error)
        raise
    finally:
        with _guard:item._retiring=False
        if reader is not None:reader.lock.release()
        item.lock.release()


def _reader_capacity():
    from . import mining_policy
    from .resource_control import active
    # Additional pipes still require the existing per-startup reservation and
    # sampled process-tree budget. Unowned use retains the original limit.
    return mining_policy.acquisition_config()['reader_slots'] if (_lease_resources.get() or active()) is not None else 8


def _touch_reader(result,*,pin=False):
    if not pin:
        with _guard:
            if id(result) in _open_services and not result._retiring:
                _open_services.move_to_end(id(result))
                return
    deadline=time.monotonic()+30
    operation=operations.current()
    while True:
        operations.check()
        victim=None
        with _guard:
            if not result._retiring:
                if id(result) in _open_services or len(_open_services)<_reader_capacity():
                    _open_services.pop(id(result),None)
                    _open_services[id(result)]=result
                    if pin:result._lease_users+=1
                    return
                for item in list(_open_services.values()):
                    if item is result:continue
                    victim=_retirement(item)
                    if victim is not None:break
        if victim is not None:
            # No registry lock is held during termination, pipe cleanup or join.
            _retire(victim)
            continue
        if operation is None and time.monotonic()>=deadline:
            raise TimeoutError('Git reader slots remained occupied for 30 seconds')
        pause=operations.timeout(.05)
        started=time.monotonic()
        try:
            time.sleep(pause)
        finally:
            waited=max(0.,time.monotonic()-started)
            if operation is not None:
                # Reader admission is queueing, like existing resource waits.
                # Extend only the idle wait; cooperative checks still enforce
                # cancellation and the remaining active-service allowance.
                with operation.guard:
                    operation.deadline+=waited
                    operation.resource_wait_seconds=getattr(operation,'resource_wait_seconds',0.)+waited
                    operation.reader_slot_queue_seconds=getattr(operation,'reader_slot_queue_seconds',0.)+waited
            current.count('git_reader_slot_queue_wait_seconds',waited)


def service(repository):
    key=str(Path(repository).resolve())
    with _guard:
        result=_services.get(key)
        if result is None:
            result=Service(key);_services[key]=result
        return result


def close_all():
    # Do not hold the registry guard while waiting on a service: reader access
    # holds that service lock before touching the registry.
    with _guard:items=list(dict.fromkeys([*_services.values(),*_open_services.values()]))
    for item in items:item.close()
    with _reader_guard:readers=list(_live_readers)
    for reader in readers:reader.close()


def reference_commit(fetcher,ptr,path):
    """Consolidate metadata only when raw objects preserve reference Git semantics."""
    from app.backend.pipeline.ingest import codemine as cm
    shared=service(path)
    sha=ptr['sha']
    try:
        shared._exact(sha)
        info=shared.info_many([sha])[0]
        if info is None:
            cm._git(['-C',str(path),'-c','protocol.version=2','fetch','-q','--depth=2',
                     '--filter=blob:none','origin',sha],timeout=240)
            shared.invalidate_availability()
        metadata=shared.metadata(sha)
        parent=metadata['parent']
        if parent and shared.info_many([parent])[0] is None:
            raise CompatibilityFallback('First parent is not available locally')
        files=[]
        if parent:
            raw=cm._git(['-C',str(path),'diff-tree','-r','-M','--no-commit-id','--name-status',parent,sha])
            for line in raw.splitlines():
                parts=line.split('\t');code=parts[0]
                if code.startswith('R') and len(parts)==3:files.append(cm.FileChange(parts[2],parts[1],'renamed'))
                elif len(parts)>=2:files.append(cm.FileChange(parts[1],parts[1],{'A':'added','D':'removed'}.get(code[:1],'modified')))
        tree_equal=None
        if len(metadata['parents'])==2:
            try:tree_equal=metadata['tree']==shared.metadata(metadata['parents'][1])['tree']
            except CompatibilityFallback:tree_equal=cm.tree_equal_of(path,sha)
        return cm.Commit(sha,parent,metadata['message']+'\n',metadata['date'],files,cm._GitLoader(path),metadata['parents'],tree_equal)
    except CompatibilityFallback as error:
        current.count('git_metadata_reference_fallbacks')
        print('Persistent Git metadata requires reference interpretation: '+str(error))
        return None



