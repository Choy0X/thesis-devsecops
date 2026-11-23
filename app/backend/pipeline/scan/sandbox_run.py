"""The runner that puts a copy of a project into a closed container and runs tools there (Scan Codebase, P3f.4.2).

What it promises, and the tests that hold it:

* the host folder is only read. The container has no bind mount, so nothing it does can reach the host's files; the project is copied in as a tar stream;
* the copy leaves out what must not travel: version-control data, virtual environments and caches, files that look like keys or credentials (only their names are reported, never their
  contents), links of every kind (symbolic links, hard links and, on Windows, directory junctions, which look like ordinary folders to tar; each item's real path must also stay inside the folder), anything that is not a plain file or folder, files above a size, and a copy above a total size or file
  count is refused instead of cut quietly;
* the container has no network at all (its own loopback still exists), every capability dropped, no new privileges, an ordinary user, a read-only root, a small scratch folder, a
  process limit, and a memory limit with swap equal to it; ``/work`` is an anonymous volume that is removed with the container;
* a tool runs under ``timeout``, so a tool that hangs is stopped inside the container, and the host waits a little longer than that before giving up on the container itself;
* the output of every tool is masked for credentials, stored as an evidence file with its SHA-256, and every run (and the creation and removal of the container) is one line of the project's
  audit log;
* a tool that was not found, was killed or timed out is reported as such (``state``), never as a run that found nothing.

The image is the one the owner approves and builds; its tag is read from ``data/sandbox/toolkit_image.json``, written by the build step. Until
that exists ``toolkit_image`` says so and nothing is started. This module has been exercised against a stand-in for Docker only; the first run on a real container is the first time the
anonymous ``/work`` volume and the copy into it are tried for real.
"""
from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import io
import json
import pathlib
import secrets
import tarfile
import time
from dataclasses import dataclass, field

from app.backend import sandbox_runtime, sandbox
from app.backend.pipeline import fsio, projects, util
from app.backend.pipeline.scan import code_rules, copyin
from app.backend.pipeline.scan.copyin import (  # re-exported: existing callers/tests import these from here
    CopyRefused, MAX_COPY_BYTES, MAX_COPY_FILES, MAX_FILE_BYTES, copy_stream, is_link, slug as _slug,
)

TOOLKIT_IMAGE = pathlib.Path("data/sandbox/toolkit_image.json")
SCANNER_UID = 10001
SCANNER = f"{SCANNER_UID}:{SCANNER_UID}"
KIND = "scan"
TMPFS = {"/tmp": "rw,noexec,nosuid,size=256m"}
LIMITS = {"memory_mb": 2048, "cpus": 2.0, "pids": 256}

MAX_EVIDENCE_BYTES = 5 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
MAX_STREAM_BYTES = 256 * 1024 * 1024         # a tool that writes more than this is stopped; the output is read as it comes and only the tail is kept, never all of it at once
HOST_GRACE_SECONDS = 30                      # how long past a tool's own timeout the host waits before it gives up on the container


class ToolkitMissing(RuntimeError):
    """The tool image has not been built (or its record is unreadable): a scan cannot start."""


def toolkit_image() -> dict:
    """``{"tag", "id", "built_at"}`` of the tool image, from the record the build step writes."""
    try:
        doc = json.loads(TOOLKIT_IMAGE.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc.get("tag"):
            return doc
    except (OSError, ValueError):
        pass
    raise ToolkitMissing("The tool image for Scan Codebase has not been built yet, so no scan can run. It is built from the Images tab after the toolkit is approved.")


def container_kwargs(image: str, name: str, limits: dict | None = None) -> dict:
    """The arguments of the container, as one pure function so a test can read every setting that makes it closed."""
    from docker.types import Mount
    lim = {**LIMITS, **(limits or {})}
    return {"image": image, "name": name, "command": ["sleep", "infinity"], "detach": True, "labels": {sandbox.LABEL_KEY: "1", "aivdf.kind": KIND},
            "network_mode": "none", "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges"], "user": SCANNER,
            "pids_limit": lim["pids"], "mem_limit": f"{lim['memory_mb']}m", "memswap_limit": f"{lim['memory_mb']}m", "nano_cpus": int(lim["cpus"] * 1e9),
            "working_dir": "/work", "tmpfs": dict(TMPFS), "mounts": [Mount(target="/work", source=None, type="volume")],              # an anonymous volume of the container's own, removed with it
            "environment": {"HOME": "/tmp", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "SEMGREP_SEND_METRICS": "off", "SEMGREP_ENABLE_VERSION_CHECK": "0"}}


def limits_record(limits: dict | None = None) -> dict:
    lim = {**LIMITS, **(limits or {})}
    return {"network": "none", "user": SCANNER, "non_root": True, "read_only_root": True, "cap_drop": ["ALL"], "no_new_privileges": True, "pids_limit": lim["pids"],
            "memory_mb": lim["memory_mb"], "cpus": lim["cpus"], "host_mounts": [], "tmpfs": sorted(TMPFS)}


# -- the copy ----------------------------------------------------------------------------------------------------------------------------------------------------

# -- one tool run ------------------------------------------------------------------------------------------------------------------------------------------------

@dataclass
class ToolRun:
    tool: str
    argv: list
    state: str                               # completed | timed_out | missing | killed | error
    exit_code: int | None
    seconds: float
    output: str                              # what the tool wrote to standard output, masked for credentials, for showing and storing
    evidence: str | None                     # path relative to the project folder
    sha256: str | None
    bytes: int = 0
    note: str = ""
    raw: str = field(default="", repr=False) # the same standard output as it came, for the adapters to parse: masking is for what is shown, and must not change what is read
    stderr: str = ""                         # standard error, masked: log lines and complaints, never parsed as a report
    cut: bool = False                        # only the tail of standard output was kept, so a report in it is not whole

    @property
    def ran(self) -> bool:
        """The tool ran to its end (whatever it said). Anything else must be reported as an environment problem, never as 'no finding'."""
        return self.state == "completed"


def classify(exit_code: int | None, timeout: int) -> tuple[str, str]:
    """``(state, note)`` of a finished exec from its exit status under ``timeout -k``."""
    if exit_code is None:
        return "error", "Docker gave no exit status."
    if exit_code == 124:
        return "timed_out", f"stopped after {timeout} seconds"
    if exit_code in (126, 127):
        return "missing", "the tool is not in the image or cannot be started"
    if exit_code in (137, 143):
        return "killed", "the tool was killed, most likely for using more memory than the container has"
    return "completed", ""


def _evidence_text(raw: bytes, total: int | None = None) -> tuple[str, int]:
    """The output as masked text, and the number of bytes the tool wrote (``total``, which may be more than ``raw`` when only the tail was kept); above the cap only the tail is kept, and it says so."""
    n = len(raw) if total is None else total
    raw = raw[-MAX_EVIDENCE_BYTES:]
    cut = f"[the first {n - len(raw):,} bytes of the output are not kept]\n" if n > len(raw) else ""
    return cut + code_rules.mask(raw.decode("utf-8", "replace"), config=True), n


class _Tail:
    """The last ``cap`` bytes of a stream that is read block by block, and how many bytes went by in all."""

    def __init__(self, cap: int):
        self.cap, self.blocks, self.size, self.total = cap, [], 0, 0

    def add(self, block: bytes) -> None:
        self.total += len(block)
        self.blocks.append(block)
        self.size += len(block)
        while self.blocks and self.size - len(self.blocks[0]) >= self.cap:                # the oldest blocks that the cap no longer needs
            self.size -= len(self.blocks.pop(0))

    def value(self) -> bytes:
        return b"".join(self.blocks)[-self.cap:]


class ScanSandbox:
    """``with ScanSandbox(project, scan_id) as box:`` a closed container with the project copied into ``/work``; ``box.run(tool, argv, timeout=...)`` runs a tool in it. The container is
    removed (with its volume) when the block ends, whatever happened."""

    def __init__(self, project: dict, scan_id: str, *, image: str | None = None, limits: dict | None = None):
        self.project, self.scan_id, self.image, self.limits = project, scan_id, image, limits
        self.ct = None
        self.roots: list[str] = []
        self.copy: list[dict] = []
        self._n = 0
        self.folder = projects.ROOT / project["id"] / "scans" / scan_id

    def __enter__(self) -> "ScanSandbox":
        projects.require_authorized(self.project)
        image = self.image or toolkit_image()["tag"]
        name = f"{sandbox.PREFIX}scan-{self.scan_id}"
        folders = [pathlib.Path(f["path"]) for f in self.project.get("folders", [])]
        if not folders:
            raise CopyRefused("The project has no source folder.")
        streams = []
        for i, folder in enumerate(folders):                                      # the copies are made before the container exists: a folder that is too large costs nothing
            arc = f"{i + 1}-{_slug(folder.name)}" if len(folders) > 1 else _slug(folder.name)
            data, summary = copy_stream(folder, arc, uid=SCANNER_UID, max_file_bytes=MAX_FILE_BYTES, max_files=MAX_COPY_FILES, max_bytes=MAX_COPY_BYTES)
            streams.append((arc, data, {"folder": str(folder), "as": f"/work/{arc}", **summary}))
        self.ct = sandbox_runtime.run_container(sandbox._client(), **container_kwargs(image, name, self.limits))
        lim = limits_record(self.limits)
        projects.audit(self.project["id"], tool="sandbox", target=name, summary=f"closed container from {image}: no network, no host mounts, all capabilities dropped, {lim['memory_mb']} MB, {lim['cpus']} CPUs, {lim['pids_limit']} processes")
        try:
            for arc, data, info in streams:
                if not self.ct.put_archive("/work", data):
                    raise RuntimeError(f"Docker did not accept the copy of {info['folder']}.")
                self.roots.append(info["as"])
                self.copy.append(info)
                projects.audit(self.project["id"], tool="sandbox copy", target=info["folder"], summary=f"{info['files']} files, {info['bytes']:,} bytes copied to {info['as']}; skipped {info['skipped']}")
        except BaseException:
            self._remove()
            raise
        return self

    def run(self, tool: str, argv: list, *, timeout: int = 300, workdir: str | None = None, env: dict | None = None) -> ToolRun:
        """Run ``argv`` in the container under ``timeout`` and store its output as evidence. ``env`` adds variables to the environment of this one run (the packages an update trial installed)."""
        assert self.ct is not None, "run() is for use inside the with block"
        self._n += 1
        started, t0 = util.now_zulu(), time.monotonic()
        wrapped = ["timeout", "-k", "5", str(int(timeout)), *[str(a) for a in argv]]
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            raw, err, total, code, state, note = b"", b"", 0, None, "error", ""
            try:
                raw, err, total, code, flooded = pool.submit(self._exec, wrapped, workdir or (self.roots[0] if self.roots else "/work"), env).result(timeout=timeout + HOST_GRACE_SECONDS)
                if flooded:
                    state, note = "killed", f"the tool wrote more than {MAX_STREAM_BYTES // 1048576} MB of output and was stopped"
                    self._kill()
                else:
                    state, note = classify(code, timeout)
            except concurrent.futures.TimeoutError:
                note = "the container did not answer in time and was stopped"
                self._kill()
            except Exception as e:
                note = f"Docker refused the run: {type(e).__name__}: {str(e)[:200]}"
        finally:
            pool.shutdown(wait=False)
        text, size = _evidence_text(raw, total)
        errors = code_rules.mask(err.decode("utf-8", "replace"), config=True)
        stored = text + (f"\n--- standard error ---\n{errors}" if errors.strip() else "")
        seconds = round(time.monotonic() - t0, 2)
        rel = f"scans/{self.scan_id}/evidence/{self._n:02d}-{_slug(tool)}.txt"
        path = projects.ROOT / self.project["id"] / rel
        digest = hashlib.sha256(stored.encode("utf-8")).hexdigest()
        fsio.write_text(path, stored)
        shown = " ".join(code_rules.mask(str(a), config=True) for a in argv)
        projects.audit(self.project["id"], tool=tool, summary=f"{shown} [{state}{': ' + note if note else ''}]", target=self.roots[0] if self.roots else "/work", started=started, ended=util.now_zulu(),
                       exit_status=code if code is not None else state, evidence=rel)
        return ToolRun(tool=tool, argv=[str(a) for a in argv], state=state, exit_code=code, seconds=seconds, output=text, evidence=rel, sha256=digest, bytes=size, note=note,
                       raw=raw.decode("utf-8", "replace"), stderr=errors, cut=size > len(raw))

    def put_file(self, path: str, content: bytes) -> None:
        """Put one file into the copy that is already in the container, in place of the one that is there, owned by the scanner user. The path must lie under one of the copies."""
        assert self.ct is not None, "put_file() is for use inside the with block"
        if ".." in path.split("/") or not any(path.startswith(r + "/") for r in self.roots):
            raise ValueError("That path is not inside the copy of the project.")
        folder, name = path.rsplit("/", 1)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name)
            info.size, info.uid, info.gid, info.mode = len(content), SCANNER_UID, SCANNER_UID, 0o644
            tar.addfile(info, io.BytesIO(content))
        if not self.ct.put_archive(folder, buf.getvalue()):
            raise RuntimeError(f"Docker did not accept the file {path}.")
        projects.audit(self.project["id"], tool="sandbox file", target=path, summary=f"{len(content):,} bytes put in the copy in place of the file that was there")

    def put_tree(self, host_dir, dest: str, *, max_bytes: int = 300 * 1024 * 1024) -> int:
        """Copy the files of a flat folder on this computer (the wheels of an update trial) into the container as ``dest``, a new folder under ``/work`` owned by the scanner user. Returns the bytes."""
        assert self.ct is not None, "put_tree() is for use inside the with block"
        if not dest.startswith("/work/") or ".." in dest.split("/"):
            raise ValueError("That folder is not under /work in the container.")
        files = [p for p in sorted(pathlib.Path(host_dir).iterdir()) if p.is_file() and not is_link(p)]
        total = sum(p.stat().st_size for p in files)
        if total > max_bytes:
            raise CopyRefused(f"The files are {total // 1048576} MB, more than the {max_bytes // 1048576} MB that are copied into a container.")
        parent, name = dest.rsplit("/", 1)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            top = tarfile.TarInfo(name)
            top.type, top.mode, top.uid, top.gid = tarfile.DIRTYPE, 0o755, SCANNER_UID, SCANNER_UID
            tar.addfile(top)
            for p in files:
                info = tarfile.TarInfo(f"{name}/{p.name}")
                info.size, info.mode, info.uid, info.gid = p.stat().st_size, 0o644, SCANNER_UID, SCANNER_UID
                with p.open("rb") as fh:
                    tar.addfile(info, fh)
        if not self.ct.put_archive(parent, buf.getvalue()):
            raise RuntimeError(f"Docker did not accept the folder {dest}.")
        projects.audit(self.project["id"], tool="sandbox files", target=dest, summary=f"{len(files)} files, {total:,} bytes copied in from this computer")
        return total

    def _exec(self, argv: list, workdir: str, env: dict | None = None) -> tuple[bytes, bytes, int, int | None, bool]:
        """Run ``argv`` and read its two streams as they arrive: ``(the last MAX_EVIDENCE_BYTES of standard output, the last MAX_STDERR_BYTES of standard error, the size of standard output,
        the exit status, whether it was a flood)``. Neither stream is ever held whole, and they are kept apart so that a log line can never be read as part of a report."""
        api = sandbox._client().api
        exec_id = api.exec_create(self.ct.id, argv, workdir=workdir, stdout=True, stderr=True, **({"environment": dict(env)} if env else {}))["Id"]
        out, err, total_out, total = _Tail(MAX_EVIDENCE_BYTES), _Tail(MAX_STDERR_BYTES), 0, 0
        for frame in api.exec_start(exec_id, stream=True, demux=True):
            o, e = frame if isinstance(frame, tuple) else (frame, None)
            total += len(o or b"") + len(e or b"")
            if total > MAX_STREAM_BYTES:
                return out.value(), err.value(), out.total, None, True
            if o:
                out.add(o)
            if e:
                err.add(e)
        return out.value(), err.value(), out.total, api.exec_inspect(exec_id).get("ExitCode"), False

    def _kill(self) -> None:
        try:
            self.ct.kill()
        except Exception:  # ignored: the container is removed when the block ends; this only tries to free the CPU now
            pass

    def _remove(self) -> None:
        ct, self.ct = self.ct, None
        if ct is None:
            return
        try:
            ct.remove(force=True, v=True)
            projects.audit(self.project["id"], tool="sandbox", target=ct.name, summary="container and its volume removed")
        except Exception as e:  # ignored: a container that could not be removed is listed on the Containers tab, where it can be deleted by hand
            projects.audit(self.project["id"], tool="sandbox", target=getattr(ct, "name", "?"), summary=f"the container could not be removed ({type(e).__name__}); delete it on the Containers tab")

    def __exit__(self, *exc) -> None:
        self._remove()


def new_scan_id() -> str:
    return f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
