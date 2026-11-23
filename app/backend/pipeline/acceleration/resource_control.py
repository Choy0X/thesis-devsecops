"""Adaptive host-memory budgeting. Memory pressure is a wait, not a campaign stop."""
from contextlib import contextmanager
import os
import ctypes as ct
import threading
import time

CAP_BYTES = 3_000_000_000
HEADROOM_BYTES = 64 << 20
GROWTH_BYTES = 256 << 20
STAGE_BYTES = {'acquire': 32 << 20, 'parse': 32 << 20, 'context': 32 << 20, 'publish': 16 << 20}


class AdmissionDeferred(Exception):
    """A growth estimate was not admitted; no allocation failure occurred."""
    def __init__(self, message, *, resource=None, required_bytes=0):
        super().__init__(message)
        self.resource, self.required_bytes = resource, required_bytes


class _MemoryStatus(ct.Structure):
    _fields_ = [('length',ct.c_uint32),('load',ct.c_uint32),
                *[(name,ct.c_uint64) for name in ('total_physical','available_physical',
                  'total_commit','available_commit','total_virtual','available_virtual','extended_virtual')]]

_memory_api = None
_memory_api_lock = threading.Lock()


def system_commit_available():
    """Uncommitted Windows backing, including existing paging capacity, never VRAM."""
    global _memory_api
    if os.name != 'nt':return None
    try:
        if _memory_api is None:
            with _memory_api_lock:
                if _memory_api is None:
                    api=ct.WinDLL('kernel32',use_last_error=True).GlobalMemoryStatusEx
                    api.argtypes=[ct.POINTER(_MemoryStatus)];api.restype=ct.c_int
                    _memory_api=api
        status=_MemoryStatus();status.length=ct.sizeof(status)
        return int(status.available_commit) if _memory_api(ct.byref(status)) else None
    except (OSError,AttributeError):
        return None

def process_memory():
    import psutil
    private = rss = 0
    try:
        root = psutil.Process()
        for process in [root, *root.children(recursive=True)]:
            try:
                info = process.memory_info()
                value = getattr(info, 'private', None)
                if value is None:
                    full = process.memory_full_info()
                    value = getattr(full, 'uss', None)
                if value is None:
                    return {'private_bytes': None, 'rss_bytes': None, 'available_bytes': psutil.virtual_memory().available}
                private += value
                rss += info.rss
            except psutil.NoSuchProcess:
                continue
    except (OSError, psutil.AccessDenied):
        private = rss = None
    if private is None:
        # A process of the tree could not be read (access denied): use this process's own resident size, never a wait that cannot end. It undercounts children, so
        # the Governor's budget (which follows the system's free memory) stays the limit that matters.
        try:
            private = rss = psutil.Process().memory_info().rss
        except (OSError, psutil.Error):
            private = rss = None
    return {'private_bytes': private, 'rss_bytes': rss, 'available_bytes': psutil.virtual_memory().available,
            'commit_available_bytes':system_commit_available()}

class Governor:
    def __init__(self, *, sample=process_memory, clock=time.monotonic, sleep=time.sleep, poll_seconds=1):
        self.sample, self.clock, self.sleep = sample, clock, sleep
        self.poll_seconds = poll_seconds
        self.lock = threading.RLock()
        self.last_sample = float('-inf')
        self.budget = None
        self.stable = 0
        self.tickets = {}
        self._ticket_generations = {}
        self._reservation_generation = 0
        self._startup_loans = {}
        self.serial_tickets = set()
        self.serial_admissions = 0
        self.state = {}
        self.wait_seconds = 0.
        self.trimmers = []
        self.trim_requested = False
        self.blocked_amount = 0
        self.blocked_key = None
        self.waiting_startups = set()
        self.pressure_target = None
        self.pressure_workers = set()

    def refresh(self, *, force=False):
        with self.lock:
            now = self.clock()
            if not force and now-self.last_sample < self.poll_seconds:
                return self.snapshot()
            self.last_sample = now
            self.state = self.sample()
            used = self.state.get('private_bytes')
            try:
                from .metrics import current as _performance
                _performance.note_memory(used, self.state.get('rss_bytes'), self.state.get('available_bytes'))
            except Exception:  # ignored: peaks are a report, never a reason to stop
                pass
            available = self.state.get('available_bytes', 0)
            desired = min(CAP_BYTES, used + max(0, available-HEADROOM_BYTES)) if used is not None else 0
            previous = self.budget
            if previous is None or desired < previous:
                self.budget, self.stable = desired, 0
            elif desired > previous:
                self.stable += 1
                if self.stable >= 3:
                    self.budget, self.stable = min(desired, previous+GROWTH_BYTES), 0
            else:
                self.stable = 0
            if previous != self.budget:
                self.trim_requested = True
            # A disposable cache can recover while the aggregate budget stays
            # capped. Sampled physical headroom must still schedule its callback.
            for callback in self.trimmers:
                threshold=getattr(getattr(callback,'__self__',None),'trim_recovery_available_bytes',0)
                if type(threshold) is int and threshold>0 and available>=threshold:
                    self.trim_requested=True
                    break
            return self.snapshot()

    def startup_wait(self,ticket,waiting=True):
        with self.lock:
            if waiting and ticket in self.tickets:self.waiting_startups.add(ticket)
            else:self.waiting_startups.discard(ticket)

    def forget_blocked(self,key):
        with self.lock:
            if self.blocked_key==key:self.blocked_amount=0;self.blocked_key=None

    @contextmanager
    def _reservation_change(self):
        # Allocate rollback state before touching ownership. These maps contain
        # only live admissions/loans, never completed campaign items. Replacing
        # the references on failure requires no new map/set entries under OOM.
        previous=(dict(self.tickets),dict(self._ticket_generations),
                  dict(self._startup_loans),set(self.serial_tickets),
                  set(self.waiting_startups),self.blocked_key,self.blocked_amount)
        try:
            yield
        except BaseException:
            (self.tickets,self._ticket_generations,self._startup_loans,
             self.serial_tickets,self.waiting_startups,
             self.blocked_key,self.blocked_amount)=previous
            raise

    def retain(self,key,owned_key):
        """Transfer admission ownership so scheduler cleanup cannot release it."""
        with self.lock:
            if key!=owned_key and owned_key not in self.tickets:
                with self._reservation_change():
                    self.tickets[owned_key]=self.tickets.pop(key)
                    generation=self._ticket_generations.pop(key,None)
                    if generation is None:
                        self._reservation_generation+=1
                        generation=self._reservation_generation
                    self._ticket_generations[owned_key]=generation
                    if key in self._startup_loans:
                        self._startup_loans[owned_key]=self._startup_loans.pop(key)
                    for child,(parent,version,amount,serial) in list(self._startup_loans.items()):
                        if parent==key and version==generation:
                            self._startup_loans[child]=(owned_key,version,amount,serial)
                    if key in self.serial_tickets:
                        self.serial_tickets.remove(key);self.serial_tickets.add(owned_key)
                    self.waiting_startups.discard(key)
            return owned_key

    def borrow_startup(self,parent,owned_key,amount,*,expected_generation=None):
        """Move existing stage credit to a child until its ownership is settled."""
        if type(amount) is not int or amount<=0:raise ValueError('Startup credit must be positive bytes')
        with self.lock:
            if (parent==owned_key or parent not in self._ticket_generations
                    or (expected_generation is not None and self._ticket_generations.get(parent)!=expected_generation)
                    or parent in self._startup_loans or owned_key in self.tickets
                    or self.tickets.get(parent,0)<amount):return False
            state=self.refresh(force=True)
            used=state.get('private_bytes');commit=state.get('commit_available_bytes')
            total=None if used is None else used+state['reserved_bytes']
            serial=parent in self.serial_tickets
            if total is None or total>CAP_BYTES:return False
            if commit is not None and commit<amount+HEADROOM_BYTES:return False
            if total>state['budget_bytes'] and (not serial or commit is None):return False
            with self._reservation_change():
                self.tickets[parent]-=amount
                self.tickets[owned_key]=amount
                self._reservation_generation+=1
                self._ticket_generations[owned_key]=self._reservation_generation
                self._startup_loans[owned_key]=(parent,self._ticket_generations[parent],amount,serial)
                if serial:self.serial_tickets.add(owned_key)
            return True

    def retired(self,worker):
        with self.lock:
            self.pressure_workers.discard(worker)
            if not self.pressure_workers:self.pressure_target=None

    def reclaim_needed(self, budget=None, *, worker=None):
        with self.lock:
            state=self.refresh()
            used=state.get('private_bytes')
            if used is None:return False
            if self.pressure_target is not None and used<=self.pressure_target:
                self.pressure_target=None;self.pressure_workers.clear()
            if self.blocked_amount and self._admission(state,self.blocked_amount) is not None:
                self.blocked_amount=0;self.blocked_key=None
            limit=self.budget if budget is None else budget
            commit=state.get('commit_available_bytes')
            resident=limit is not None and used+(32<<20)>limit
            backing=commit is not None and commit<HEADROOM_BYTES+(32<<20)
            # Reservations for work already in flight can drain naturally. A
            # cold startup with no active ticket must be able to reclaim heaps.
            draining=set(self.tickets).difference(self.waiting_startups)
            blocked=bool(self.blocked_amount and not draining)
            episode=self.pressure_target is not None and (worker is None or worker in self.pressure_workers)
            return resident or backing or episode or blocked

    def trim(self):
        with self.lock:
            callbacks = list(self.trimmers) if self.trim_requested else []
            self.trim_requested = False
            workers=(getattr(self,'parser_pool',None),getattr(self,'publication_process',None))
        # Release disposable caches before retiring parser heaps, and preserve
        # the downstream writer until cheaper reclaim has been exhausted.
        def priority(callback):
            owner=getattr(callback,'__self__',None)
            if owner is not None and owner is workers[0]:return 1
            if owner is not None and owner is workers[1]:return 2
            return 0
        for callback in sorted(callbacks,key=priority):
            needed=self.reclaim_needed()
            changed=callback(self.budget)
            if changed or needed:self.refresh(force=True)

    def snapshot(self):
        with self.lock:
            return {**self.state, 'budget_bytes': self.budget or 0, 'cap_bytes': CAP_BYTES,
                    'reserved_bytes': sum(self.tickets.values()), 'memory_wait_seconds': self.wait_seconds,
                    'next_check_seconds': self.poll_seconds, 'serial_admission':bool(self.serial_tickets),
                    'serial_admissions':self.serial_admissions}

    def _admission(self, state, amount):
        used=state.get('private_bytes')
        if used is None or used+state['reserved_bytes']+amount>CAP_BYTES:return None
        commit=state.get('commit_available_bytes')
        if commit is not None and commit<amount+HEADROOM_BYTES:return None
        if used+state['reserved_bytes']+amount<=state['budget_bytes']:return 'parallel'
        # A single bounded operation may use OS-managed paging. More parallel work
        # still needs physical headroom. Unknown backing never grants this path.
        if not self.tickets and commit is not None:return 'serial'
        return None

    def fits(self, amount=0):
        with self.lock:
            return self._admission(self.refresh(),amount) is not None

    def reserve(self, key, amount):
        with self.lock:
            if key in self.tickets:
                return True
            mode=self._admission(self.refresh(),amount)
            if mode is None:
                # A denied concurrent ticket is not evidence of resident pressure.
                if not self.blocked_amount:self.blocked_amount=amount;self.blocked_key=key
                self.trim_requested = True
                return False
            with self._reservation_change():
                self.tickets[key] = amount
                self._reservation_generation += 1
                self._ticket_generations[key] = self._reservation_generation
                if amount>=self.blocked_amount:self.blocked_amount=0;self.blocked_key=None
                if mode=='serial':
                    self.serial_tickets.add(key)
                    self.serial_admissions+=1
            return True

    def release(self, key):
        with self.lock:
            with self._reservation_change():
                loan=self._startup_loans.pop(key,None)
                self.tickets.pop(key, None)
                self._ticket_generations.pop(key,None)
                self.serial_tickets.discard(key)
                self.waiting_startups.discard(key)
                self.forget_blocked(key)
                if loan is not None:
                    parent,generation,amount,serial=loan
                    if parent in self.tickets and self._ticket_generations.get(parent)==generation:
                        self.tickets[parent]+=amount
                        if serial:self.serial_tickets.add(parent)

    def idle(self, *, should_stop=lambda:False, progress=lambda **kw:None, kind="waiting_memory"):
        from app.backend.pipeline import stopping
        stopping.check(should_stop)
        self.trim()
        progress(state=kind, label='Campaign remains active: '+kind, resources=self.refresh())
        start = self.clock()
        self.sleep(self.poll_seconds)
        seconds = self.clock()-start
        with self.lock:
            if kind == "waiting_memory":self.wait_seconds += seconds
        from . import operations
        op = operations.current()
        if op is not None:
            op.deadline += seconds
            op.resource_wait_seconds = getattr(op, 'resource_wait_seconds', 0.) + seconds
        stopping.check(should_stop)
        self.refresh(force=True)

    def wait_for(self, amount, *, should_stop=lambda:False, progress=lambda **kw:None):
        while not self.fits(amount):
            self.idle(should_stop=should_stop, progress=progress)

    def pressure(self):
        import gc
        gc.collect()
        self.refresh(force=True)
        # Do not apply an unreachable baseline target to future retry workers.
        # Only heaps present in this pressure episode are eligible for it.
        workers=set()
        parser=getattr(self,'parser_pool',None)
        if parser is not None:
            with parser.lock:workers.update(id(item['pool']) for item in parser.entries if item['pool'] is not None)
        publisher=getattr(self,'publication_process',None)
        if publisher is not None:
            with publisher.state_lock:
                if publisher.process is not None:workers.add(id(publisher.process))
        with self.lock:
            used=self.state.get('private_bytes')
            self.pressure_workers=workers
            self.pressure_target=max(0,used-HEADROOM_BYTES) if used is not None and workers else None
            self.trim_requested=True
            self.stable=0
        self.trim()

    def stage_cost(self,stage):
        if stage == 'parse' and getattr(self,'parser_pool',None) is not None:
            return self.parser_pool.admission_cost()
        if stage == 'publish' and getattr(self,'publication_process',None) is not None:
            return self.publication_process.admission_cost()
        return STAGE_BYTES[stage]

    def stage_limits(self):
        from app.backend import compute
        from .mining_policy import acquisition_config
        from .memory_profile import current as profile
        limits={'acquire':8 if acquisition_config()['version']==2 else 4,
                'parse':max(1,min(5,compute.physical_cores()-1)), 'context':8, 'publish':1}
        p=profile()
        for stage,name in (('acquire','acquire_limit'),('parse','parse_workers'),('context','context_limit')):
            limits[stage]=p.capped(limits[stage],name)
        for stage,target in (('acquire',p.acquire_target),('parse',p.parse_target)):
            if target:limits[stage]=max(limits[stage],int(target))      # the large tier widens; no tier narrows below the old constants here
        return limits

def active():
    from . import runtime
    return getattr(runtime._active, 'resources', None)

class AdaptiveParserPool:
    """Independent one-process executors can retire without killing busy siblings."""
    def __init__(self, resources, *, should_stop=lambda:False, progress=lambda **kw:None):
        self.resources, self.should_stop, self.progress = resources, should_stop, progress
        self.lock = threading.RLock()
        self.entries = []
        self._pool = self
        self.closed = False
        self.resources.trimmers.append(self.trim)
        self.resources.parser_pool = self
        self.serial = 0
        self.last_budget = self.resources.budget
        self.retire_pending = False

    def __enter__(self):
        return self

    def _start_entry(self,entry,prepaid):
        from app.backend import compute
        from . import operations
        guard=threading.RLock()
        def cleanup():
            with guard:
                pool=entry['pool']
                try:
                    if pool is not None:pool.__exit__(None,None,None)
                except BaseException:
                    with self.lock:
                        entry['ticket']=self.resources.retain(entry['ticket'],'parser-unconfirmed-'+str(id(entry)))
                        entry['cleanup']=cleanup
                    raise
                with self.lock:
                    if entry in self.entries:self.entries.remove(entry)
                if pool is not None:self.resources.retired(id(pool))
                if entry['ticket']!=prepaid:self.resources.release(entry['ticket'])
                entry.pop('cleanup',None)
        operation=operations.current()
        try:
            with operations.resource(cleanup):
                from .parser_tasks import initialize
                pool=compute.Pool(1,force_process=True,initializer=initialize)
                with guard:
                    entry['pool']=pool
                    cancelled=operation is not None and (operation.cancelled.is_set() or time.monotonic()>=operation.deadline)
                    if not cancelled:pool.__enter__()
                # Operation.check can run cancellation callbacks, so keep it
                # outside the startup/cleanup guard and the parser registry lock.
                if cancelled:raise operations.DeadlineExpired('Parser startup was cancelled')
                operations.check()
        except BaseException:
            if operation is None:cleanup()
            raise

    def submit(self, fn, *args):
        from . import operations
        entry=None;ticket=None;start=False
        op=operations.current();prepaid=getattr(op,'resource_ticket',None)
        try:
            while entry is None:
                operations.check()
                self.resources.trim()
                with self.lock:
                    if self.closed:raise RuntimeError('Parser pool is closed')
                    entry=next((item for item in self.entries if not item['busy']),None)
                    if entry is not None:
                        entry['busy']=True
                    elif len(self.entries)<self.resources.stage_limits()['parse']:
                        if ticket is None:
                            self.serial+=1;ticket='parser-start-'+str(self.serial)
                        from .memory_profile import current as profile
                        start_bytes=profile().parser_ticket
                        if prepaid is not None and self.resources.tickets.get(prepaid,0)>=start_bytes:
                            ticket=prepaid;admitted=True
                        else:admitted=self.resources.reserve(ticket,start_bytes)
                        if admitted:
                            entry={'pool':None,'busy':True,'ticket':ticket}
                            self.entries.append(entry);start=True
                        else:self.resources.startup_wait(prepaid)
                if entry is None:
                    self.resources.idle(should_stop=self.should_stop,progress=self.progress)
            if start:self._start_entry(entry,prepaid)
        finally:
            self.resources.startup_wait(prepaid,False)
            self.resources.forget_blocked(ticket)
        try:
            if fn.__module__=='app.backend.pipeline.ingest.codemine' and fn.__name__=='_analyze_files':
                from .parser_tasks import analyze_files
                fn=analyze_files
            return entry['pool'].submit(fn,*args)
        finally:
            with self.lock:
                ticket=entry.pop('ticket',None)
                if ticket!=prepaid:
                    if ticket is not None:self.resources.refresh(force=True)
                    self.resources.release(ticket)
                entry['busy']=False
                if getattr(entry['pool'],'_aborted',False):
                    entry['pool'].__exit__(None,None,None)
                    self.entries.remove(entry)
                    self.resources.retired(id(entry['pool']))
                self._retire_idle()

    def admission_cost(self):
        with self.lock:
            from .memory_profile import current as profile
            p=profile()
            return p.parser_cost_idle if any(not item['busy'] for item in self.entries) else p.parser_cost_busy

    def trim(self, budget):
        with self.lock:
            self.last_budget=budget
            self.retire_pending=self.resources.reclaim_needed(budget)
            return self._retire_idle()

    def _retire_idle(self):
        if not self.retire_pending:return False
        changed=False
        self.resources.refresh(force=True)
        for item in list(self.entries):
            # Growth, completed work or an earlier cache/worker retirement can
            # satisfy the old request before a busy parser becomes idle.
            if not self.resources.reclaim_needed():break
            if not item['busy'] and self.resources.reclaim_needed(worker=id(item['pool'])):
                item['pool'].__exit__(None,None,None)
                self.entries.remove(item)
                self.resources.retired(id(item['pool']))
                changed=True
                self.resources.refresh(force=True)
        self.retire_pending=self.resources.reclaim_needed()
        return changed

    @property
    def worker_count(self):
        with self.lock:
            return len(self.entries)

    def __exit__(self, *exc):
        self.closed = True
        self.resources.trimmers.remove(self.trim)
        for item in list(self.entries):
            if 'cleanup' in item:item['cleanup']()
            else:
                item['pool'].__exit__(*exc)
                self.resources.retired(id(item['pool']))
                self.resources.release(item.get('ticket'))
        self.entries.clear()



