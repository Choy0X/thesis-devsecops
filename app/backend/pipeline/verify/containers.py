"""The hardened container a verification step runs in. No docker import: this builds the arguments, the runner starts the container.

Isolated (no network), non-root (nobody), a read-only root filesystem with a small tmpfs, every capability dropped, no new privileges, a
process limit, a memory limit without swap, a cpu limit, no host environment, and a wall-clock timeout enforced by the runner. The
editable tree is mounted read-write at ``/work``; the acceptance tests are mounted **read-only** at ``/acceptance`` and must live outside
the editable tree, so a patch can neither edit nor see a way around the verifier. ``limits_record`` is what a receipt stores about it.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

from app.backend import sandbox

NOBODY = "65534:65534"
TMPFS = {"/tmp": "rw,noexec,nosuid,size=64m"}


@dataclass(frozen=True)
class ContainerSpec:
    image: str
    command: list
    workspace: pathlib.Path
    acceptance: pathlib.Path
    env: dict = field(default_factory=dict)
    memory_mb: int = 512
    cpus: float = 1.0
    pids_limit: int = 128
    timeout: int = 900                     # the project default, not an official benchmark budget
    user: str = NOBODY


def _inside(child: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        child, parent = child.resolve(), parent.resolve()
    except OSError:
        return False
    return child == parent or parent in child.parents


def container_kwargs(spec: ContainerSpec) -> dict:
    from app.backend import sandbox_runtime
    if _inside(spec.acceptance, spec.workspace) or _inside(spec.workspace, spec.acceptance):
        raise ValueError("the acceptance tests must be outside the editable tree (and the tree outside them)")
    env = {"PYTHONPATH": "/work/src", "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp", "LANG": "C.UTF-8", **spec.env}
    return {"image": spec.image, "command": list(spec.command), "detach": True, "labels": sandbox_runtime.labels('verification'), 'restart_policy': {'Name': 'no'},
            "network_mode": "none", "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges"], "user": spec.user,
            "pids_limit": spec.pids_limit, "mem_limit": f"{spec.memory_mb}m", "memswap_limit": f"{spec.memory_mb}m",
            "nano_cpus": int(spec.cpus * 1e9), "working_dir": "/work", "tmpfs": dict(TMPFS), "environment": env,
            "volumes": {str(spec.workspace): {"bind": "/work", "mode": "rw"},
                        str(spec.acceptance): {"bind": "/acceptance", "mode": "ro"}}}


def limits_record(spec: ContainerSpec) -> dict:
    """What a receipt records about the container: the hardening, the limits and the timeout it was run with."""
    return {"network": "none", "user": spec.user, "non_root": not spec.user.startswith(("0:", "root")), "read_only_root": True,
            "cap_drop": ["ALL"], "no_new_privileges": True, "pids_limit": spec.pids_limit, "memory_mb": spec.memory_mb, "cpus": spec.cpus,
            "timeout_seconds": spec.timeout, "acceptance_read_only": True, "tmpfs": sorted(TMPFS)}
