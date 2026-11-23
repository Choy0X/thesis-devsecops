"""A Windows job object so that nothing the app started outlives it.

The launcher puts itself into a job object whose limit is "kill everything in the job when the last handle to it closes". Every process the launcher starts
afterwards (the job workers, git, a model server) belongs to the same job, so when the launcher ends, however it ends (a crash, the task manager, a power
button held down), the operating system ends them too. The handle is kept open for the life of the process and is never closed by us: closing it is what
the operating system does at exit, and that is the signal. (A restart does not need an exception to this: the next copy is started by the watcher in app/restart.py after this
one has ended.)

On other systems this does nothing and says so; process groups already cover the common cases there.
"""
from __future__ import annotations

import sys

_handle = None


def supported() -> bool:
    return sys.platform == "win32"


def _api():
    """The kernel functions and the structure of the job's limits (Windows only)."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount",
                                                    "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    return k32, ExtendedLimits


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x800  # Only helpers explicitly requesting CREATE_BREAKAWAY_FROM_JOB escape.
EXTENDED_LIMIT_INFORMATION = 9


def _set_flags(job, flags: int) -> bool:
    import ctypes
    k32, limits = _api()
    info = limits()
    info.BasicLimitInformation.LimitFlags = flags
    return bool(k32.SetInformationJobObject(job, EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)))


def contain_current_process() -> bool:
    """Put this process (and so its future children) in a kill-on-close job. True when it worked; False elsewhere or when Windows refused (for example
    a job that does not allow breakaway on an old system)."""
    global _handle
    if not supported():
        return False
    if _handle is not None:
        return True
    k32, _limits = _api()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return False
    if not _set_flags(job, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK):
        k32.CloseHandle(job)
        return False
    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        k32.CloseHandle(job)
        return False
    _handle = job                                             # never closed by us: the operating system closes it when this process ends
    return True


def limit_flags() -> int | None:
    """The limit flags the job has now (None without a job); for a test and for a person reading the log."""
    if _handle is None:
        return None
    import ctypes
    from ctypes import wintypes
    k32, limits = _api()
    k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID]
    info = limits()
    if not k32.QueryInformationJobObject(_handle, EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info), None):
        return None
    return int(info.BasicLimitInformation.LimitFlags)
