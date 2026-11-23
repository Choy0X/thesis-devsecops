"""Docker sandbox manager (Chapter 4.7).

Isolated Docker containers for safe testing and patch validation, managed from the
UI. Replaces the Rust tool's QEMU micro-VM entirely. The framing is defensive: these
are isolated test environments, with network off by default and conservative
resource limits. Every function degrades cleanly when no Docker daemon is present
(the API reports docker_available: false and the UI disables actions).

Only containers this app creates are managed; they carry the label below so the
manager never touches unrelated containers on the host.
"""
from __future__ import annotations
import io
import tarfile
import pathlib
import time
import re
from app.backend import sandbox_runtime

LABEL_KEY = "aivdf.sandbox"
PREFIX = "aivdf-"


def _client():
    import docker  # lazy: optional dependency / daemon
    return docker.from_env()


def available() -> bool:
    try:
        _client().ping()
        return True
    except Exception:  # ignored: Docker not reachable means not available
        return False


def hardening(pids_limit: int = 256) -> list[dict]:
    """The protections every sandbox gets whatever the person picks (the create dialog shows them as chips). Kept next to
    create() so the list and the settings applied there are read together."""
    return [
        {"id": "caps", "label": "No extra privileges", "detail": "Every Linux capability is dropped, so the code cannot mount disks, change the network or load modules."},
        {"id": "nonewpriv", "label": "No privilege gain", "detail": "The no-new-privileges flag stops programs from becoming more powerful than the user that started them."},
        {"id": "user", "label": "Not root", "detail": "The code runs as user 1000, an ordinary user without administrator rights."},
        {"id": "pids", "label": f"At most {pids_limit} processes", "detail": "A limit on processes stops a runaway program from filling the machine."},
        {"id": "memory", "label": "Memory capped, no swap", "detail": "The memory limit also covers swap, so the sandbox cannot spill onto the disk to get around it."},
    ]


def status() -> dict:
    if not available():
        return {"docker_available": False, "reason": "Docker daemon not reachable", "hardening": hardening()}
    c = _client()
    info = c.version()
    ours = c.containers.list(all=True, filters={"label": LABEL_KEY})
    return {"docker_available": True, "version": info.get("Version"),
            "api_version": info.get("ApiVersion"), "containers": len(ours), "hardening": hardening()}


def _view(ct) -> dict:
    ct.reload()
    from app.backend import sandbox_tasks
    return {
        "id": ct.id, "short_id": ct.short_id, "name": ct.name, "status": ct.status,
        "image": (ct.image.tags[0] if ct.image.tags else ct.image.short_id),
        "created": ct.attrs.get("Created", "")[:19],
        "network": ct.attrs.get("HostConfig", {}).get("NetworkMode"),
        "execution": sandbox_tasks.container_state(ct.id),
    }


def list_containers() -> list[dict]:
    if not available():
        return []
    from app.backend.sandbox_cleanup import owned
    scope = sandbox_runtime.cleanup_scope()
    return [_view(ct) for ct in _client().containers.list(all=True, filters={"label": LABEL_KEY}) if owned(ct, **scope)]


@sandbox_runtime.creation_guard()
def create(image: str = "python:3.11-slim", name: str | None = None,
           network: bool = False, cpus: float = 1.0, memory_mb: int = 512,
           read_only: bool = False, command: str | None = None, pids_limit: int = 256) -> dict:
    """Create and start a sandbox container with conservative, isolated defaults: no network unless asked, non-root, every
    capability dropped, no new privileges, a process limit and a memory limit without swap."""
    c = _client()
    kwargs = dict(
        image=image,
        name=(PREFIX + name) if name else None,
        hostname=re.sub(r'[^A-Za-z0-9_.-]', '-', name or 'sandbox'),
        command=command or "sleep infinity",
        detach=True, tty=True,
        labels=sandbox_runtime.labels(),
        restart_policy={'Name': 'no'},
        network_mode="none" if not network else "bridge",
        read_only=read_only,
        mem_limit=f"{memory_mb}m",
        memswap_limit=f"{memory_mb}m",
        nano_cpus=int(cpus * 1e9),
        pids_limit=pids_limit,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
        tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"} if read_only else None,
        user="1000:1000",
        working_dir="/work",
    )
    from docker.errors import ImageNotFound
    try:
        ct = c.containers.run(**{k: v for k, v in kwargs.items() if v is not None})
    except ImageNotFound:
        # Image not present locally: pull then retry (needs network on the host).
        c.images.pull(image)
        ct = c.containers.run(**{k: v for k, v in kwargs.items() if v is not None})
    sandbox_runtime.register(ct)
    if not read_only:
        _own_workdir(ct)
        try:
            ct.exec_run(['sh', '-c', "grep -q '^[^:]*:[^:]*:1000:' /etc/passwd || printf '%s\\n' 'sandbox:x:1000:1000:Sandbox:/work:/bin/sh' >> /etc/passwd; grep -q '^[^:]*:[^:]*:1000:' /etc/group || printf '%s\\n' 'sandbox:x:1000:' >> /etc/group"], user='0')
        except Exception:  # ignored: minimal images may lack writable account files; the PTY still sets its named prompt.
            pass  # The PTY still uses an explicit named prompt on minimal/read-only images.
    from app.backend import sandbox_history
    sandbox_history.record(ct.name, "created", {"image": image, "network": "bridge" if network else "none", "cpus": cpus, "memory_mb": memory_mb, "read_only": read_only})
    return _view(ct)


def _own_workdir(ct) -> None:
    """Docker makes the working folder as root, and the container runs as user 1000 with every capability dropped, so nothing in it could write to ``/work`` (found by the first real run of
    the page's operations: ``echo > /work/a.txt`` was refused). The folder is handed to user 1000 through the archive route, which needs no capability inside the container. A container with
    a read-only root keeps ``/work`` read-only on purpose."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("work")
        info.type, info.mode, info.uid, info.gid, info.mtime = tarfile.DIRTYPE, 0o755, 1000, 1000, int(time.time())
        tar.addfile(info)
    try:
        ct.put_archive("/", buf.getvalue())
    except Exception:  # ignored: without the hand-over the container is still created and isolated; its working folder is then read-only for the user, which the person sees at the first write
        pass


def _ours(container_id: str):
    """The container, if this app created it (label); any other container on the
    host - the user's own work - is never touched."""
    ct = _client().containers.get(container_id)
    from app.backend.sandbox_cleanup import owned
    if not owned(ct, **sandbox_runtime.cleanup_scope()):
        raise PermissionError("not a sandbox container of this app")
    return ct


def action(container_id: str, act: str) -> dict:
    ct = _ours(container_id)
    from app.backend import sandbox_history
    if act in ('start', 'restart'):
        with sandbox_runtime.creation_guard():
            if ct.status == 'paused': ct.unpause()
            ct.start() if act == 'start' else ct.restart(timeout=5)
            sandbox_runtime.register(ct)
    elif act == "stop":
        if ct.status == 'paused': ct.unpause()
        ct.stop(timeout=5)
    elif act == "delete":
        name = ct.name
        from app.backend import sandbox_cleanup
        result = sandbox_cleanup.delete_owned(ct.id, _client(), **sandbox_runtime.cleanup_scope())
        if result['deleted']:
            sandbox_runtime.forget(ct.id)
            sandbox_history.record(name, 'deleted')
            image = result['image']
            if image.get('status') == 'removed' and image.get('id'):
                sandbox_runtime.update(lambda state: state['pending_images'].pop(image['id'], None))
            if image.get('status') in ('failed', 'retained_in_use'):
                sandbox_runtime.update(lambda state: state['pending_images'].update({image['id']: image}))
        return result
    else:
        raise ValueError(f"unknown action: {act}")
    sandbox_history.record(ct.name, act)
    return _view(ct)


def exec_run(container_id: str, command: str, workdir: str = "/work", timeout: int | None = 900) -> dict:
    """Run a command in a sandbox container. ``timeout`` (seconds, 900 by default: a project default, not an official benchmark budget)
    kills it afterwards; exit code 124 with a timeout means it was killed."""
    ct = _ours(container_id)
    argv = ["bash", "-lc", command]
    if timeout:
        argv = ["timeout", "-k", "5", str(int(timeout)), *argv]
    began = time.monotonic()
    res = ct.exec_run(argv, workdir=workdir, demux=False)
    out = res.output.decode("utf-8", "replace") if res.output else ""
    timed_out = bool(timeout) and res.exit_code == 124
    from app.backend import sandbox_history
    sandbox_history.record_command(ct.name, command, res.exit_code, time.monotonic() - began, out, timed_out)
    return {"exit_code": res.exit_code, "output": out, "timed_out": timed_out}


def exec_stream(container_id: str, argv: list[str], workdir: str = "/work", *, timeout: int | None = None, environment: dict | None = None, user: str | None = None):
    """Run ``argv`` (no shell: the program and its arguments) in a sandbox container and give back what it prints as it prints it: ``{"stream": "stdout" | "stderr", "data": bytes}`` for each
    piece, then one ``{"exit_code": int | None, "timed_out": bool}``. ``timeout`` (seconds) is enforced inside the container (``timeout -k 5``: the program is told to stop, and ended five seconds
    later if it does not), so a run that is not read to its end is still bounded. Not recorded in the command history: the caller, which knows what the command was for, does that."""
    ct = _ours(container_id)
    api = _client().api
    cmd = ["timeout", "-k", "5", str(int(timeout)), *argv] if timeout else list(argv)
    opts = {k: v for k, v in (("environment", environment), ("user", user)) if v}
    exec_id = api.exec_create(ct.id, cmd, workdir=workdir, stdout=True, stderr=True, **opts)["Id"]
    for frame in api.exec_start(exec_id, stream=True, demux=True):
        out, err = frame if isinstance(frame, tuple) else (frame, None)
        if out:
            yield {"stream": "stdout", "data": out}
        if err:
            yield {"stream": "stderr", "data": err}
    code = api.exec_inspect(exec_id).get("ExitCode")
    yield {"exit_code": code, "timed_out": bool(timeout) and code == 124}


def exec_with_stdin(container_id: str, argv: list[str], data: bytes, workdir: str = "/tmp", *, environment: dict | None = None, user: str | None = None):
    """Start ``argv`` (no shell) in a sandbox container with ``data`` on its standard input, and leave it running. What is sent never appears in the program's environment or command line. The connection
    that carried it is given back and the caller closes it when the program has what it needs (closing it does not stop the program). For a program meant to outlive the call (the MCP bridge)."""
    ct = _ours(container_id)
    api = _client().api
    opts = {k: v for k, v in (("environment", environment), ("user", user)) if v}
    exec_id = api.exec_create(ct.id, list(argv), workdir=workdir, stdin=True, stdout=True, stderr=True, **opts)["Id"]
    link = api.exec_start(exec_id, socket=True)
    getattr(link, "_sock", link).sendall(data)
    return link


def logs(container_id: str, tail: int = 200) -> str:
    ct = _ours(container_id)
    return ct.logs(tail=tail).decode("utf-8", "replace")


def stats(container_id: str) -> dict:
    ct = _ours(container_id)
    s = ct.stats(stream=False)
    try:
        cpu = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
        sys = s["cpu_stats"]["system_cpu_usage"] - s["precpu_stats"]["system_cpu_usage"]
        ncpu = s["cpu_stats"].get("online_cpus", 1) or 1
        cpu_pct = (cpu / sys) * ncpu * 100 if sys > 0 else 0.0
    except Exception:  # ignored: stats are optional; 0.0 when the container reports none
        cpu_pct = 0.0
    mem = s.get("memory_stats", {})
    sample = {"cpu_pct": round(cpu_pct, 1),
              "mem_mb": round(mem.get("usage", 0) / 1e6, 1),
              "mem_limit_mb": round(mem.get("limit", 0) / 1e6, 1)}
    from app.backend import sandbox_history
    sandbox_history.record_stats(ct.name, sample)
    return sample


def commit(container_id: str, repository: str, tag: str = "latest") -> dict:
    ct = _ours(container_id)
    img = ct.commit(repository=repository, tag=tag)
    return {"image": f"{repository}:{tag}", "id": img.short_id}


def images() -> list[dict]:
    if not available():
        return []
    out = []
    for im in _client().images.list():
        if im.tags:
            out.append({"id": im.short_id, "tags": im.tags,
                        "size_mb": round(im.attrs.get("Size", 0) / 1e6, 1)})
    return out


def pull(image: str) -> dict:
    _client().images.pull(image)
    return {"pulled": image}


# never copied into a container: secrets, environments, caches, mined repositories
_EXCLUDE_NAMES = {".env", "settings.json", ".api_token", ".venv", "venv", "node_modules",
                  ".git", "__pycache__", ".chrome-profile", "repos", "chroma"}
_EXCLUDE_SUFFIX = (".db", ".db-wal", ".db-shm", ".zip", ".pem", ".key")


def _put_dir(ct, src: pathlib.Path, dest: str = "/work"):
    """Copy a host directory into the container at dest (tar stream), without
    secrets, virtual environments, VCS data or large caches."""
    def keep(info: tarfile.TarInfo):
        name = pathlib.PurePosixPath(info.name).name
        if name in _EXCLUDE_NAMES or name.endswith(_EXCLUDE_SUFFIX) or name.startswith(".env"):
            return None
        return info
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(src), arcname=".", filter=keep)
    buf.seek(0)
    ct.put_archive(dest, buf.read())


def scan_target(container_id: str, host_path: str) -> dict:
    """Copy a host project into the container and run the scanners there."""
    ct = _ours(container_id)
    src = pathlib.Path(host_path).expanduser().resolve()
    if not src.is_dir():
        raise ValueError(f"not a folder: {host_path}")
    _put_dir(ct, src)
    dep = ct.exec_run(["bash", "-lc", "pip show -f . >/dev/null 2>&1; ls requirements.txt 2>/dev/null"], workdir="/work")
    code = ct.exec_run(["bash", "-lc",
        "python - <<'PY'\nimport re,glob\nhits=0\nfor f in glob.glob('**/*.py',recursive=True):\n try:\n  t=open(f,errors='ignore').read()\n except: continue\n if re.search(r'\\b(eval|exec|os.system|pickle.loads)\\b',t): hits+=1\nprint('flagged files:',hits)\nPY"], workdir="/work")
    return {"copied": host_path,
            "deps": dep.output.decode("utf-8", "replace"),
            "codebase": code.output.decode("utf-8", "replace")}


def exec_pty(container_id: str):
    """Open an interactive PTY exec in the container. Returns (exec_id, raw_socket)."""
    ct = _ours(container_id)
    api = _client().api
    label = re.sub(r'[^A-Za-z0-9_.-]', '-', ct.name.removeprefix(PREFIX))
    ex = api.exec_create(ct.id, ['/bin/bash', '--noprofile', '--norc', '-i'],
                         environment={'HOME': '/work', 'PS1': 'sandbox@' + label + ':\\w\\$ '},
                         tty=True, stdin=True, stdout=True, stderr=True)
    sock = api.exec_start(ex["Id"], tty=True, socket=True, demux=False)
    raw = getattr(sock, "_sock", sock)  # docker-py wraps the raw socket
    return ex["Id"], raw


def exec_resize(exec_id: str, rows: int, cols: int) -> None:
    try:
        _client().api.exec_resize(exec_id, height=rows, width=cols)
    except Exception:  # ignored: a failed resize must not close the terminal
        pass
