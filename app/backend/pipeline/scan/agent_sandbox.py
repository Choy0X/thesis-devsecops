"""One long-lived container per project (agentic mode, Part B): the project's own copy, reused across a whole agent session rather than recreated per command, so Manual mode's
checkpoints (plain ``git commit``\\ s inside it, added once ``agent_loop.py`` needs them) build a real history and Auto mode's many small steps do not each pay a fresh container's
cost. Composes ``scan/copyin.py``'s already-hardened copy -- the same one ``ScanSandbox`` uses, reused rather than a second, independently-reasoned-about implementation -- with the
same closed-container hardening ``scan/sandbox_run.py`` already established (no network, every capability dropped, no new privileges, a read-only root, a process and memory limit).
The container survives on disk between tasks, but runs only while a claimed chat execution owns it. The last task stops it without deleting the project's copy or checkpoint
history. A durable, fenced execution receipt protects concurrent owners and retryable cleanup; the watchdog also reconciles interrupted tasks.

Nothing here is reachable from a model's own tools (``agent_tools.py``, Part C): a session's write/exec tools call ``ensure()`` themselves, never the model directly.
"""
from __future__ import annotations

import json
import pathlib
import re
import shlex
import threading
import time
from typing import Callable

from app.backend import sandbox, sandbox_runtime
from app.backend.pipeline import fsio, projects, util
from app.backend.pipeline.scan import agent_image, agent_network, copyin

RECORDS = pathlib.Path("data/sandbox/agent")
UID = 10001
USER = f"{UID}:{UID}"
KIND = "agent"
TMPFS = {"/tmp": "rw,noexec,nosuid,size=256m"}
LIMITS = {"memory_mb": 2048, "cpus": 2.0, "pids": 256}
DEFAULT_IDLE_MINUTES = 30.0
BASE_TAG = "aivdf-base"                    # the commit that holds the project as it was when it was copied (or last refreshed from the project): what every change is measured against
# what running a project's own tools leaves behind: never part of a change the owner reviews (a project's own .gitignore still applies on top of this)
IGNORED = ["__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".coverage", "node_modules/", ".venv/", "venv/", ".DS_Store", "*.egg-info/"]
COPY_IN_GIT = ("git init -q && printf '%s\\n' " + " ".join(shlex.quote(p) for p in IGNORED) + " >> .git/info/exclude && git add -A && git commit -q -m 'Initial copy' --allow-empty && "
               f"git tag -f {BASE_TAG} > /dev/null")
BASE_COMMAND = f"git rev-parse --verify -q {BASE_TAG}^{{commit}} || git rev-list --max-parents=0 HEAD | tail -n 1"


class AgentSandboxError(ValueError):
    """What is wrong with a project's agent sandbox, in a sentence, with the HTTP status a route answers with (mirrors ``ChatStoreError``)."""

    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.status, self.code = status, code


def _path(project_id: str) -> pathlib.Path:
    return RECORDS / f"{project_id}.json"


def _load(project_id: str) -> dict | None:
    try:
        doc = json.loads(_path(project_id).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) and doc.get("container") else None
    except (OSError, ValueError):
        return None


def _save(project_id: str, doc: dict) -> None:
    fsio.write_json(_path(project_id), doc, indent=2)


def container_kwargs(image: str, name: str, network: str = "off", volume: str | None = None, project_id: str | None = None) -> dict:
    """Arguments of the long-lived container: the same hardening the scan sandbox already uses (no network, every capability dropped, no new privileges, a read-only root, process
    and memory limits), as one pure function a test can read every setting from. ``network="proxy"`` puts it on its project's agent network instead of none (``agent_network.py``: nothing
    there but the egress proxy) with the variables that make a program use that proxy; ``volume`` is the volume of the project's copy to mount again when the container is made anew."""
    from docker.types import Mount
    from app.backend import sandbox_runtime
    kw = {"image": image, "name": name, "command": ["sleep", "infinity"], "detach": True, "labels": sandbox_runtime.labels(KIND), 'restart_policy': {'Name': 'no'},
          "network_mode": "none", "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges"], "user": USER,
          "pids_limit": LIMITS["pids"], "mem_limit": f"{LIMITS['memory_mb']}m", "memswap_limit": f"{LIMITS['memory_mb']}m", "nano_cpus": int(LIMITS["cpus"] * 1e9),
          "working_dir": "/work", "tmpfs": dict(TMPFS), "mounts": [Mount(target="/work", source=volume, type="volume")],
          "environment": {"HOME": "/tmp", "LANG": "C.UTF-8", "GIT_AUTHOR_NAME": "agent", "GIT_AUTHOR_EMAIL": "agent@localhost", "GIT_COMMITTER_NAME": "agent", "GIT_COMMITTER_EMAIL": "agent@localhost"}}
    if network == agent_network.PROXY:
        kw.pop("network_mode")
        kw["network"] = agent_network.network_name(project_id or name)
        kw["environment"] = {**kw["environment"], **agent_network.environment()}
    return kw


def copy_in(project: dict, ct) -> list[tuple[str, str]]:
    """Copy every folder of the project into the container (the scan sandbox's own hardened copy) and start a git history in each, so the first checkpoint has something to diff
    against (``.git`` is already excluded by the copy, so there is never real history to preserve). Only ever called once per container, at creation: calling it again would
    overwrite whatever the session has since written. Returns ``(root, host folder)`` pairs: the mapping ``agent_apply.py`` needs to know, for something changed at a root, which
    real folder on the host it would go back to."""
    projects.require_authorized(project)
    folders = [pathlib.Path(f["path"]) for f in project.get("folders", [])]
    if not folders:
        raise AgentSandboxError("The project has no source folder.")
    pairs = []
    for i, folder in enumerate(folders):
        arc = f"{i + 1}-{copyin.slug(folder.name)}" if len(folders) > 1 else copyin.slug(folder.name)
        data, summary = copyin.copy_stream(folder, arc, uid=UID)
        if not ct.put_archive("/work", data):
            raise AgentSandboxError(f"Docker did not accept the copy of {folder}.")
        root = f"/work/{arc}"
        pairs.append((root, str(folder)))
        projects.audit(project["id"], tool="agent sandbox copy", target=str(folder), summary=f"{summary['files']} files, {summary['bytes']:,} bytes copied to {root}; skipped {summary['skipped']}")
        res = ct.exec_run(["bash", "-lc", COPY_IN_GIT], workdir=root)
        if res.exit_code != 0:
            raise AgentSandboxError(f"git could not start history in {root}: {(res.output or b'').decode('utf-8', 'replace')[:300]}")
    return pairs


_NETWORK_LOCK = threading.RLock()        # taken by ensure while it reads the network setting and attaches a container, and by revoke_network: a session that read "proxy" just before the owner turned it off
                                         # finishes attaching first and is then cut off, and one that starts after reads "off"


def revoke_network() -> list[str]:
    """Take the way out away from every agent sandbox right now (the owner switched the network off): each is disconnected from its network and the proxy goes, even while a session is running in it.
    The container is made again, with no network at all, the next time a session starts in it. Best effort and silent when Docker is not running (then nothing is running either). The names cut off."""
    try:
        if not sandbox.available():
            return []
        with _NETWORK_LOCK:
            cut = agent_network.revoke(sandbox._client())
    except Exception:  # ignored: Docker is not reachable, so no container has a network to take away
        return []
    from app.backend import sandbox_history
    for name in cut:
        sandbox_history.record(name, "network revoked", {"reason": "the network setting was turned off"})
        pid = name[len(f"{sandbox.PREFIX}agent-"):] if name.startswith(f"{sandbox.PREFIX}agent-") else ""
        if pid:
            try:
                projects.audit(pid, tool="agent sandbox", target=name, summary="the way out through the egress proxy was taken away at once (the network setting was turned off)")
            except Exception:  # ignored: an audit line for a project that is gone is not worth failing the revoke
                pass
    return cut


def _work_volume(ct) -> str | None:
    """The name of the volume that holds the project's copy (``/work``) in a container."""
    for m in (getattr(ct, "attrs", None) or {}).get("Mounts") or []:
        if m.get("Destination") == "/work" and m.get("Name"):
            return m["Name"]
    return None


def _reconfigure(project: dict, doc: dict, ct, want: str, session_id: str | None) -> tuple[dict, object]:
    """The container made anew with the network the settings now say, on the same volume (the project's copy and its history are in it and stay). The old container is kept under another name until
    the new one is running, so a failure leaves the sandbox as it was. Refused while another session of the project is running (it would lose its container under it); a checkpoint is made first."""
    from app.backend.pipeline import agent_sessions
    pid = project["id"]
    others = agent_sessions.running_sessions(pid) - {session_id} if session_id else agent_sessions.running_sessions(pid)
    if others:
        raise AgentSandboxError("The sandbox's network was changed in Settings, but another session of this project is running in it. Stop that session first; the change is made when the next one starts.")
    volume = _work_volume(ct)
    if not volume:
        raise AgentSandboxError("The sandbox's network could not be changed: the volume that holds the project's copy was not found, and recreating the sandbox without it would lose the copy.")
    client = sandbox._client()
    image = agent_image.image_record()["tag"]
    if want == agent_network.PROXY:
        try:
            agent_network.ensure_proxy(client, image, agent_network.network_name(pid))
        except agent_network.AgentNetworkError as e:
            raise AgentSandboxError(str(e)) from None
    if ct.status != "running":
        sandbox_runtime.start_container(ct)
    checkpoint(pid, "(auto) before the sandbox's network changed")
    name = doc.get("name") or f"{sandbox.PREFIX}agent-{pid}"
    ct.stop(timeout=5)
    ct.rename(f"{name}-old")
    try:
        new = sandbox_runtime.run_container(client, **container_kwargs(image, name, want, volume, pid))
    except BaseException:
        ct.rename(name)                                                   # the new one could not be made: the old one is the sandbox again
        sandbox_runtime.start_container(ct)
        raise
    ct.remove(force=True, v=False)                                        # the volume is the new container's now
    from app.backend import sandbox_history
    sandbox_history.record(new.name, "reconfigured", {"network": want, "volume": volume})
    projects.audit(pid, tool="agent sandbox", target=name, summary=f"made again on the same volume with network {want}" + (": a way out through the egress proxy, public web addresses only" if want == agent_network.PROXY else ": no network"))
    doc = {**doc, "container": new.id, "touched_at": time.time()}
    _save(pid, doc)
    if want == agent_network.OFF:
        agent_network.release(client, agent_network.network_name(pid))    # this project's network goes, and the proxy with the last one (best effort)
    return doc, new


def ensure(project: dict, session_id: str | None = None) -> dict:
    from app.backend import sandbox_tasks
    with sandbox_tasks.provision(project['id'], session_id) as attach:
        doc = _ensure(project, session_id)
        attach(doc['container'])
        return doc


def _ensure(project: dict, session_id: str | None = None) -> dict:
    """The project's agent sandbox: created and seeded the first time, started again if it had been stopped idle, left alone if it is already running. Never recreated once it
    exists (that would discard whatever the session has written) and never copied into a second time. The one change: when the network setting no longer matches the container's, it is made again
    on the same volume (``_reconfigure``), and nothing of the copy is lost. ``session_id`` is the session asking, which is not another one that is running."""
    with _NETWORK_LOCK:                                                # the setting is read and acted on with no revoke able to run between the two (see revoke_network)
        doc = _load(project["id"])
        # A new sandbox needs a built image before Docker can do anything for it.
        # Existing containers remain usable without the original image record.
        image = agent_image.image_record()["tag"] if not doc else None
        try:
            from docker.errors import DockerException
            client = sandbox._client()
        except ImportError as error:
            raise AgentSandboxError("Docker support is not installed, so the agent sandbox cannot start.",
                                    503, "docker_unavailable") from error
        except DockerException as error:
            raise AgentSandboxError("The agent sandbox could not connect to Docker. Start Docker and try again.",
                                    503, "docker_unavailable") from error
        ct = None
        if doc:
            try:
                ct = client.containers.get(doc["container"])
            except Exception:  # ignored: the record outlived its container, so the sandbox is made again below
                doc, ct = None, None                                       # the record outlived the container (removed by hand, or the host was reset): start over
        if doc and ct is not None:
            ct.reload()
            want = agent_network.mode()
            if agent_network.container_mode(ct) != want:
                doc, ct = _reconfigure(project, doc, ct, want, session_id)
            if ct.status != "running":
                sandbox_runtime.start_container(ct)
                from app.backend import sandbox_history
                sandbox_history.record(ct.name, "started")
            touch(project["id"])
            return {**doc, "created": False}
        if image is None:
            image = agent_image.image_record()["tag"]
        name = f"{sandbox.PREFIX}agent-{project['id']}"
        want = agent_network.mode()
        if want == agent_network.PROXY:
            try:
                agent_network.ensure_proxy(client, image, agent_network.network_name(project["id"]))
            except agent_network.AgentNetworkError as e:
                raise AgentSandboxError(str(e)) from None
        ct = sandbox_runtime.run_container(client, **container_kwargs(image, name, want, None, project["id"]))
    try:
        pairs = copy_in(project, ct)
    except BaseException:
        ct.remove(force=True, v=True)
        raise
    from app.backend import sandbox_history
    sandbox_history.record(ct.name, "created", {"image": image, "kind": KIND, "project": project["id"]})
    lim = LIMITS
    projects.audit(project["id"], tool="agent sandbox", target=name,
                   summary=f"closed container from {image}: {'a way out through the egress proxy (public web addresses only)' if want == agent_network.PROXY else 'no network'}, no host mounts, all capabilities dropped, {lim['memory_mb']} MB, {lim['cpus']} CPUs, {lim['pids']} processes")
    now = time.time()
    doc = {"project_id": project["id"], "container": ct.id, "name": name, "roots": [r for r, _ in pairs], "root_folders": dict(pairs), "created_at": now, "touched_at": now}
    _save(project["id"], doc)
    return {**doc, "created": True}


def record(project_id: str) -> dict:
    """The project's agent sandbox record (``container``, ``roots``, ``root_folders``, ...), for a reader that must not create one by itself (``agent_apply.py``'s diff, unlike a
    session's own tools, which call ``ensure()`` and may)."""
    doc = _load(project_id)
    if not doc:
        raise AgentSandboxError("This project has no agent sandbox yet: nothing has run in Manual or Auto mode.", 404, "no_sandbox")
    return doc


def checkpoint(project_id: str, message: str = "Checkpoint") -> dict:
    """Commit the current state of every root as a checkpoint (a real ``git commit``) -- the human button's equivalent of ``agent_tools.checkpoint``, for the same sandbox a
    session's own tool calls land in. Symmetric on purpose: either the model or the person watching may add one."""
    doc = record(project_id)
    cid = doc["container"]
    message = str(message or "").strip() or "Checkpoint"
    commits = {}
    for root in doc["roots"]:
        res = sandbox.exec_run(cid, f"git add -A && git commit -q -m {shlex.quote(message)} --allow-empty && git rev-parse --short HEAD", workdir=root, timeout=60)
        if res["exit_code"] != 0:
            raise AgentSandboxError(f"Could not checkpoint {root}: {res['output'].strip()[:300]}")
        commits[root] = res["output"].strip()
    touch(project_id)
    return {"message": message, "commits": commits}


def checkpoints(project_id: str) -> list[dict]:
    """Every checkpoint of the project's sandbox, newest first, across every root: its short hash, when (seconds since the epoch) and its message. A commit hash is only ever
    valid within the root it was made in, which is why each entry names its own."""
    doc = record(project_id)
    cid = doc["container"]
    out = []
    for root in doc["roots"]:
        res = sandbox.exec_run(cid, "git log --format=%h%x09%ct%x09%s", workdir=root, timeout=30)
        if res["exit_code"] != 0:
            continue                                                     # a root with no history yet (should not happen past ensure(), but never worth failing the whole list over)
        for line in res["output"].splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[1].isdigit():
                out.append({"root": root, "hash": parts[0], "at": int(parts[1]), "message": parts[2]})
    out.sort(key=lambda c: c["at"], reverse=True)
    return out


def rollback(project_id: str, root: str, commit_hash: str) -> dict:
    """Reset one root of the sandbox to the state it had at ``commit_hash``, discarding anything written since. Human-only (there is no model tool for this): an agent silently
    discarding its own prior work would be un-auditable."""
    if not re.fullmatch(r"[0-9a-f]{4,64}", str(commit_hash or "")):
        raise AgentSandboxError("That is not the hash of a checkpoint.")
    doc = record(project_id)
    cid = doc["container"]
    if root not in doc["roots"]:
        raise AgentSandboxError("That is not a root of this project's sandbox.")
    check = sandbox.exec_run(cid, f"git cat-file -t {shlex.quote(commit_hash)}", workdir=root, timeout=15)
    if check["exit_code"] != 0 or check["output"].strip() != "commit":
        raise AgentSandboxError("That is not a checkpoint of this root.")
    res = sandbox.exec_run(cid, f"git reset --hard {shlex.quote(commit_hash)}", workdir=root, timeout=60)
    if res["exit_code"] != 0:
        raise AgentSandboxError(f"Could not roll back {root}: {res['output'].strip()[:300]}")
    touch(project_id)
    return {"root": root, "hash": commit_hash}


def base_commit(cid: str, root: str) -> str:
    """The commit the project's copy is measured against in ``root``: the baseline tag, or the first commit of a sandbox that was made before there was one."""
    res = sandbox.exec_run(cid, BASE_COMMAND, workdir=root, timeout=30)
    sha = res["output"].strip().splitlines()[-1].strip() if res["output"].strip() else ""
    if res["exit_code"] != 0 or not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        raise AgentSandboxError(f"{root} has no checkpoint history to measure against yet.")
    return sha


def discard_changes(project_id: str) -> dict:
    """Put every root of the sandbox back to the project as it was when it was copied: what the session changed, added or removed goes. Nothing is lost for good: the state before is
    committed as a checkpoint first, and what is put back is committed too, so the history still holds both and a rollback can reach either. Human-only, like a rollback."""
    doc = record(project_id)
    cid = doc["container"]
    checkpoint(project_id, "Before discarding the session's changes")
    done = {}
    for root in doc["roots"]:
        base = base_commit(cid, root)
        res = sandbox.exec_run(cid, f"git read-tree -u --reset {shlex.quote(base)} && git clean -fdq && git commit -q -m {shlex.quote('Discarded the changes of the session')} --allow-empty && git rev-parse --short HEAD",
                               workdir=root, timeout=120)
        if res["exit_code"] != 0:
            raise AgentSandboxError(f"Could not put {root} back: {res['output'].strip()[:300]}")
        done[root] = res["output"].strip().splitlines()[-1] if res["output"].strip() else ""
    touch(project_id)
    return {"roots": done, "summary": "The sandbox copy is back to the project as it was when it was copied. The earlier state is kept as a checkpoint."}


_TOUCH_LOCK = threading.Lock()


def touch(project_id: str) -> None:
    """Record that the sandbox was used just now, so the idle watchdog leaves it running. Called on every tool call, so one at a time (a read, a change and a write of the record)."""
    with _TOUCH_LOCK:
        doc = _load(project_id)
        if doc:
            doc["touched_at"] = time.time()
            _save(project_id, doc)


def idle_minutes() -> float:
    from app.backend.config import Settings
    try:
        return max(0.0, float(getattr(Settings.load(), "agent_sandbox_idle_minutes", "") or DEFAULT_IDLE_MINUTES))
    except ValueError:
        return DEFAULT_IDLE_MINUTES


def stop_idle(*, now: Callable[[], float] = time.time) -> list[str]:
    """Stop (never delete) every running agent sandbox whose project has not touched it for longer than the configured idle minutes (0 = never). Returns the project ids stopped."""
    minutes = idle_minutes()
    if minutes <= 0 or not RECORDS.is_dir():
        return []
    limit, moment = minutes * 60.0, now()
    client = sandbox._client()
    stopped = []
    from app.backend.pipeline import agent_sessions
    for f in sorted(RECORDS.glob("*.json")):
        project_id = f.stem
        if agent_sessions.project_busy(project_id):          # a run is working in it: never stopped under it, however long ago it was last touched
            continue
        doc = _load(project_id)
        if not doc or moment - float(doc.get("touched_at") or 0) < limit:
            continue
        try:
            ct = client.containers.get(doc["container"])
            ct.reload()
            if ct.status == "running":
                ct.stop(timeout=10)
                from app.backend import sandbox_history
                sandbox_history.record(ct.name, "stopped", {"reason": "idle"})
                stopped.append(project_id)
        except Exception:  # ignored: a container already gone or a daemon briefly away is not an error here
            continue                                                   # the next tick looks again; a container already gone is not an error here
    return stopped


def start_idle_watchdog(interval: float = 5.0) -> Callable[[], None]:
    """Keep an eye on every project's agent sandbox from a thread of the app, the way ``campaigns.control.start_watchdog`` already watches campaigns: every ``interval`` seconds,
    stop whichever have been idle longer than the configured minutes. Returns the function that stops it."""
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            try:
                from app.backend import sandbox_tasks
                sandbox_tasks.reconcile()
                stop_idle()
            except Exception:  # ignored: a store or daemon that is briefly unreachable is looked at again at the next tick
                pass
    threading.Thread(target=loop, daemon=True, name="agent-sandbox-watchdog").start()
    return stop.set
