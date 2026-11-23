"""A private Windows job keeps descendants owned after a wrapper exits."""
import ctypes as ct
from ctypes import wintypes as w
import time
import threading


class Limits(ct.Structure):
    _fields_ = [('process_time',ct.c_longlong),('job_time',ct.c_longlong),('flags',w.DWORD),
                ('minimum',ct.c_size_t),('maximum',ct.c_size_t),('active',w.DWORD),
                ('affinity',ct.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]


class Extended(ct.Structure):
    _fields_ = [('basic',Limits),('io',ct.c_ulonglong*6),('process_memory',ct.c_size_t),
                ('job_memory',ct.c_size_t),('peak_process',ct.c_size_t),('peak_job',ct.c_size_t)]


class Accounting(ct.Structure):
    _fields_ = [('times',ct.c_longlong*4),('faults',w.DWORD),('total',w.DWORD),
                ('active',w.DWORD),('terminated',w.DWORD)]


class ThreadEntry(ct.Structure):
    _fields_ = [('dwSize',w.DWORD),('cntUsage',w.DWORD),('th32ThreadID',w.DWORD),
                ('th32OwnerProcessID',w.DWORD),('tpBasePri',w.LONG),
                ('tpDeltaPri',w.LONG),('dwFlags',w.DWORD)]


class PssThreadEntry(ct.Structure):
    _fields_ = [('ExitStatus',w.DWORD),('TebBaseAddress',ct.c_void_p),
                ('ProcessId',w.DWORD),('ThreadId',w.DWORD),('AffinityMask',ct.c_size_t),
                ('Priority',ct.c_int),('BasePriority',ct.c_int),
                ('LastSyscallFirstArgument',ct.c_void_p),('LastSyscallNumber',w.WORD),
                ('CreateTime',w.FILETIME),('ExitTime',w.FILETIME),
                ('KernelTime',w.FILETIME),('UserTime',w.FILETIME),
                ('Win32StartAddress',ct.c_void_p),('CaptureTime',w.FILETIME),
                ('Flags',w.DWORD),('SuspendCount',w.WORD),
                ('SizeOfContextRecord',w.WORD),('ContextRecord',ct.c_void_p)]


class Job:
    def __init__(self, memory_limit=None, *, allow_breakaway=False):
        self.lock = threading.RLock()
        self.api = ct.WinDLL('kernel32',use_last_error=True)
        signatures = {'CreateJobObjectW':([ct.c_void_p,w.LPCWSTR],w.HANDLE),
            'SetInformationJobObject':([w.HANDLE,ct.c_int,ct.c_void_p,w.DWORD],w.BOOL),
            'AssignProcessToJobObject':([w.HANDLE,w.HANDLE],w.BOOL),
            'TerminateJobObject':([w.HANDLE,w.UINT],w.BOOL),
            'QueryInformationJobObject':([w.HANDLE,ct.c_int,ct.c_void_p,w.DWORD,ct.c_void_p],w.BOOL),
            'CreateToolhelp32Snapshot':([w.DWORD,w.DWORD],w.HANDLE),
            'Thread32First':([w.HANDLE,ct.POINTER(ThreadEntry)],w.BOOL),
            'Thread32Next':([w.HANDLE,ct.POINTER(ThreadEntry)],w.BOOL),
            'OpenThread':([w.DWORD,w.BOOL,w.DWORD],w.HANDLE),
            'GetProcessIdOfThread':([w.HANDLE],w.DWORD),
            'ResumeThread':([w.HANDLE],w.DWORD),'CloseHandle':([w.HANDLE],w.BOOL)}
        pss_signatures = {'PssCaptureSnapshot':([w.HANDLE,w.DWORD,w.DWORD,ct.POINTER(w.HANDLE)],w.DWORD),
            'PssWalkMarkerCreate':([ct.c_void_p,ct.POINTER(w.HANDLE)],w.DWORD),
            'PssWalkSnapshot':([w.HANDLE,ct.c_int,w.HANDLE,ct.c_void_p,w.DWORD],w.DWORD),
            'PssWalkMarkerFree':([w.HANDLE],w.DWORD),
            'PssFreeSnapshot':([w.HANDLE,w.HANDLE],w.DWORD),
            'GetCurrentProcess':([],w.HANDLE)}
        self.pss_available=all(hasattr(self.api,name) for name in pss_signatures)
        if self.pss_available:signatures.update(pss_signatures)
        for name,(args,result) in signatures.items():
            function = getattr(self.api,name); function.argtypes, function.restype = args,result
        self.handle = self.api.CreateJobObjectW(None,None)
        self.check(self.handle)
        limits = Extended(); limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway.
        if allow_breakaway:
            # Only a child explicitly requesting CREATE_BREAKAWAY_FROM_JOB can
            # escape. Ordinary Git/parser children still inherit this job.
            limits.basic.flags |= 0x800  # JOB_OBJECT_LIMIT_BREAKAWAY_OK
        if memory_limit is not None:
            if not 0 < memory_limit <= 3_000_000_000:raise ValueError('Invalid campaign memory cap')
            limits.basic.flags |= 0x200  # JOB_OBJECT_LIMIT_JOB_MEMORY
            limits.job_memory = memory_limit
        try: self.check(self.api.SetInformationJobObject(self.handle,9,ct.byref(limits),ct.sizeof(limits)))
        except BaseException:
            self.api.CloseHandle(self.handle); self.handle = None
            raise

    @staticmethod
    def check(value):
        if not value: raise ct.WinError(ct.get_last_error())

    def _initial_thread_id(self,pid):
        # Toolhelp copies thread identities without querying per-thread CPU times.
        # TH32CS_SNAPTHREAD is system-wide; filter the exact owned process ID.
        for attempt in range(3):
            snapshot=self.api.CreateToolhelp32Snapshot(0x4,0)
            if snapshot!=ct.c_void_p(-1).value:break
            error=ct.get_last_error()
            if error!=24 or attempt==2:raise ct.WinError(error)  # ERROR_BAD_LENGTH
        try:
            entry=ThreadEntry();entry.dwSize=ct.sizeof(entry)
            found=None;count=0
            available=self.api.Thread32First(snapshot,ct.byref(entry))
            while available:
                if entry.dwSize<ThreadEntry.th32OwnerProcessID.offset+ct.sizeof(w.DWORD):
                    raise OSError('Incomplete native thread entry')
                if entry.th32OwnerProcessID==pid:
                    found=entry.th32ThreadID;count+=1
                entry.dwSize=ct.sizeof(entry)
                available=self.api.Thread32Next(snapshot,ct.byref(entry))
            error=ct.get_last_error()
            if error!=18:raise ct.WinError(error)  # ERROR_NO_MORE_FILES
            if count!=1:raise OSError('Expected one initial suspended thread')
            return found
        finally:self.check(self.api.CloseHandle(snapshot))

    @staticmethod
    def _status(code):
        if code:raise ct.WinError(code)

    def _pss_thread_id(self,process):
        if not self.pss_available:return None
        snapshot=w.HANDLE()
        # Capture identities only. No VA clone, contexts, handles or breakaway.
        code=self.api.PssCaptureSnapshot(int(process._handle),0x80,0,ct.byref(snapshot))
        if code in (50,120):return None  # API unsupported / not implemented.
        self._status(code)
        try:
            marker=w.HANDLE()
            self._status(self.api.PssWalkMarkerCreate(None,ct.byref(marker)))
            try:
                count=0;found=None
                while True:
                    entry=PssThreadEntry()
                    code=self.api.PssWalkSnapshot(snapshot,3,marker,ct.byref(entry),ct.sizeof(entry))
                    if code==259:break  # ERROR_NO_MORE_ITEMS, not a partial walk.
                    self._status(code)
                    if entry.ProcessId!=process.pid:
                        raise OSError('Initial thread does not belong to the owned process')
                    if entry.SuspendCount!=1:raise OSError('Expected one initial suspended thread')
                    count+=1;found=entry.ThreadId
                if count!=1:raise OSError('Expected one initial suspended thread')
                return found
            finally:self._status(self.api.PssWalkMarkerFree(marker))
        finally:
            # PssCaptureSnapshot creates the descriptor in this process, even
            # though the captured target is a different (suspended) process.
            self._status(self.api.PssFreeSnapshot(self.api.GetCurrentProcess(),snapshot))

    def assign_and_resume(self,process):
        self.check(self.api.AssignProcessToJobObject(self.handle,int(process._handle)))
        tid=self._pss_thread_id(process)
        if tid is None:tid=self._initial_thread_id(process.pid)
        handle=self.api.OpenThread(0x802,False,tid)  # SUSPEND_RESUME | QUERY_LIMITED_INFORMATION
        self.check(handle)
        try:
            owner=self.api.GetProcessIdOfThread(handle)
            self.check(owner)
            if owner!=process.pid:raise OSError('Initial thread no longer belongs to the owned process')
            if self.api.ResumeThread(handle)!=1:raise OSError('Could not resume owned process')
        finally:self.check(self.api.CloseHandle(handle))

    def terminate(self,timeout):
        with self.lock:
            self._terminate(timeout)

    def _terminate(self,timeout):
        if self.handle is None: return
        self.check(self.api.TerminateJobObject(self.handle,1))
        deadline = time.monotonic()+timeout
        while True:
            accounting = Accounting()
            self.check(self.api.QueryInformationJobObject(self.handle,1,ct.byref(accounting),ct.sizeof(accounting),None))
            if not accounting.active: break
            if time.monotonic() >= deadline: raise OSError('Windows job still has active owned processes')
            time.sleep(.02)
        self.check(self.api.CloseHandle(self.handle)); self.handle = None


_campaign_job = None
_verification_job = None

def install_campaign_limit(memory_limit=3_000_000_000):
    # Keep this handle until worker exit. Closing a containing job would kill us.
    global _campaign_job
    import os
    if os.name != 'nt':
        from . import posix_limit                      # no job object here: a watchdog in the worker stops the tree when it overshoots (see posix_limit)
        posix_limit.install(int(memory_limit))
        return
    if _campaign_job is not None:
        if not 0 <= memory_limit-current_memory_limit() < 4096:raise RuntimeError('Campaign memory cap changed')
        return
    job = Job(memory_limit=memory_limit)
    job.api.GetCurrentProcess.argtypes = []
    job.api.GetCurrentProcess.restype = w.HANDLE
    try:
        job.check(job.api.AssignProcessToJobObject(job.handle,job.api.GetCurrentProcess()))
    except BaseException:
        job.api.CloseHandle(job.handle)
        job.handle = None
        raise
    _campaign_job = job

def current_memory_limit():
    if _campaign_job is None and _verification_job is None:
        from . import posix_limit
        return posix_limit.current_limit()
    info = Extended()
    job = _verification_job or _campaign_job
    job.check(job.api.QueryInformationJobObject(job.handle,9,ct.byref(info),ct.sizeof(info),None))
    return int(info.job_memory) if info.basic.flags & 0x200 else None


def install_verification_limit(memory_limit=3_000_000_000):
    """Own ordinary worker descendants, allowing explicit trusted engine escape.

    The shared Docker Desktop must be launched with CREATE_BREAKAWAY_FROM_JOB.
    Campaign policy remains unchanged and never grants breakaway.
    """
    global _verification_job
    import os
    if os.name != 'nt':
        raise RuntimeError('Verification aggregate host enforcement requires Windows')
    if _campaign_job is not None:
        raise RuntimeError('A campaign job cannot also own shared-runtime preparation')
    if _verification_job is not None:
        if not 0 <= memory_limit-current_memory_limit() < 4096:
            raise RuntimeError('Verification host memory cap changed')
        return
    job = Job(memory_limit=memory_limit, allow_breakaway=True)
    job.api.GetCurrentProcess.argtypes = []
    job.api.GetCurrentProcess.restype = w.HANDLE
    try:
        job.check(job.api.AssignProcessToJobObject(job.handle, job.api.GetCurrentProcess()))
    except BaseException:
        job.api.CloseHandle(job.handle)
        job.handle = None
        raise
    _verification_job = job
