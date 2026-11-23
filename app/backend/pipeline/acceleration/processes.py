"""Terminate only a subprocess we own, including its observed descendants."""
import psutil
import os
import subprocess


def spawn(args,**kwargs):
    """Assign ownership before the process can start creating descendants."""
    if os.name!='nt':
        return subprocess.Popen(args,**kwargs)
    from .windows_job import Job
    job = Job()
    kwargs['creationflags'] = kwargs.get('creationflags',0)|0x4  # CREATE_SUSPENDED
    process = None
    try:
        process = subprocess.Popen(args,**kwargs)
        process._owned_job = job
        job.assign_and_resume(process)
        return process
    except BaseException:
        job.terminate(10)
        if process is not None:
            if process.poll() is None: process.kill()
            process.wait(timeout=10)
        raise


def terminate(process, timeout=10):
    if job := getattr(process,'_owned_job',None):
        job.terminate(timeout)
        process.wait(timeout=timeout)
        return
    owned = list(getattr(process, '_cleanup_owned', []))
    if process.poll() is None:
        try:
            root = psutil.Process(process.pid)
            owned = list(dict.fromkeys([*owned, *root.children(recursive=True), root]))
            process._cleanup_owned = owned
        except psutil.NoSuchProcess:
            pass
    # psutil.Process remembers creation time, protecting against PID reuse.
    for item in reversed(owned):
        try: item.kill()
        except psutil.NoSuchProcess: pass
    _, alive = psutil.wait_procs(owned, timeout=timeout)
    if alive:
        raise OSError('Could not confirm owned process termination: ' + ', '.join(str(p.pid) for p in alive))
    process.wait(timeout=timeout)
