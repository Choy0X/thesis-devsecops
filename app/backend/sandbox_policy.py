"""The resource and network policy of a sandbox container: read what it has now, change what Docker lets a running container change, and recreate it for what it does not.

A running container's CPU share, memory (with the same figure for swap, so it still cannot swap) and process limit can be changed in place. Its network (none or bridge) and its read-only
root filesystem cannot: Docker refuses to move a container out of the ``none`` network and fixes the root filesystem when the container is created, so those two change by creating the
container again from a commit of its filesystem, with the same limits, and removing the old one. What lived in memory (running processes) is lost then, and the answer says so. Every figure is
checked against bounds before anything is sent, and a change that opens the network is returned with a warning in words. Containers of a verification run are not editable: a run states its
hardening in its receipt, and a sandbox container is only ever one this app labelled as its own (``sandbox._ours``).
"""
from __future__ import annotations

import os

from app.backend import sandbox_runtime, sandbox

MIN_MEMORY_MB, MIN_PIDS, MAX_PIDS = 64, 16, 4096
MIN_CPUS = 0.1
VERIFICATION_LABEL = "aivdf.kind"
VERIFICATION = "verification"
RECREATE_TAG = "aivdf-recreate"


class PolicyError(ValueError):
    """A change that cannot be made, with the reason in words."""


def machine_bounds() -> dict:
    """The most a container may be given on this computer: its processors and the memory Docker has."""
    cpus = float(os.cpu_count() or 1)
    memory = None
    try:
        memory = int(sandbox._client().info().get("MemTotal") or 0) // (1024 * 1024) or None
    except Exception:  # ignored: Docker's memory is only an upper bound for the form; without it the form does not cap the memory
        pass
    return {"max_cpus": cpus, "max_memory_mb": memory, "min_cpus": MIN_CPUS, "min_memory_mb": MIN_MEMORY_MB, "min_pids": MIN_PIDS, "max_pids": MAX_PIDS}


def current(ct) -> dict:
    """What a container has now, from Docker's own record of it."""
    host = ct.attrs.get("HostConfig", {})
    nano = host.get("NanoCpus") or 0
    memory = host.get("Memory") or 0
    return {"cpus": round(nano / 1e9, 2) if nano else None, "memory_mb": round(memory / (1024 * 1024)) if memory else None,
            "swap_mb": round(host.get("MemorySwap", 0) / (1024 * 1024)) if host.get("MemorySwap", 0) > 0 else None, "pids": host.get("PidsLimit") or None,
            "network": host.get("NetworkMode"), "read_only": bool(host.get("ReadonlyRootfs")), "editable": editable(ct)[0], "reason": editable(ct)[1]}


def editable(ct) -> tuple[bool, str | None]:
    labels = ct.labels or {}
    if labels.get(VERIFICATION_LABEL) == VERIFICATION:
        return False, "This container belongs to a verification run, whose hardening is stated in its receipt and cannot be changed."
    return True, None


def validate(cpus=None, memory_mb=None, pids=None, bounds: dict | None = None) -> dict:
    """The figures of a change, checked; ``PolicyError`` names the first that is out of bounds."""
    b = bounds or machine_bounds()
    out = {}
    if cpus is not None:
        if not isinstance(cpus, (int, float)) or isinstance(cpus, bool) or not (b["min_cpus"] <= float(cpus) <= b["max_cpus"]):
            raise PolicyError(f"CPUs must be between {b['min_cpus']} and {b['max_cpus']:g} (the processors of this computer).")
        out["cpus"] = float(cpus)
    if memory_mb is not None:
        top = b.get("max_memory_mb")
        if not isinstance(memory_mb, int) or isinstance(memory_mb, bool) or memory_mb < b["min_memory_mb"] or (top and memory_mb > top):
            raise PolicyError(f"Memory must be a whole number of MB from {b['min_memory_mb']}" + (f" to {top} (what Docker has)." if top else "."))
        out["memory_mb"] = memory_mb
    if pids is not None:
        if not isinstance(pids, int) or isinstance(pids, bool) or not (b["min_pids"] <= pids <= b["max_pids"]):
            raise PolicyError(f"The process limit must be a whole number from {b['min_pids']} to {b['max_pids']}.")
        out["pids"] = pids
    return out


def update_limits(container_id: str, *, cpus=None, memory_mb=None, pids=None, bounds: dict | None = None) -> dict:
    """Change the CPU share, memory and process limit of a running container in place. Memory and swap are set to the same figure, so the container still cannot swap."""
    ct = sandbox._ours(container_id)
    ok, why = editable(ct)
    if not ok:
        raise PolicyError(why)
    figures = validate(cpus, memory_mb, pids, bounds)
    if not figures:
        raise PolicyError("Nothing to change: give a CPU share, a memory limit or a process limit.")
    body = {}
    if "cpus" in figures:
        body["NanoCpus"] = int(figures["cpus"] * 1e9)
    if "memory_mb" in figures:
        body["Memory"] = body["MemorySwap"] = figures["memory_mb"] * 1024 * 1024
    if "pids" in figures:
        body["PidsLimit"] = figures["pids"]
    client = sandbox._client()
    client.api._result(client.api._post_json(client.api._url("/containers/{0}/update", ct.id), data=body), True)        # docker-py's update() has no NanoCpus or PidsLimit
    ct.reload()
    from app.backend import sandbox_history
    sandbox_history.record(ct.name, "limits", {"changed": figures})
    return {**current(ct), "changed": figures, "warnings": []}


def recreate(container_id: str, *, network: bool | None = None, read_only: bool | None = None) -> dict:
    """The container again with another network (none or bridge) or another read-only setting: its filesystem is committed to an image and a new container with the same name,
    limits and hardening starts from it; the old one is then removed. What was running in memory is lost."""
    ct = sandbox._ours(container_id)
    ok, why = editable(ct)
    if not ok:
        raise PolicyError(why)
    if network is None and read_only is None:
        raise PolicyError("Nothing to change: choose the network or the read-only setting.")
    host = ct.attrs.get("HostConfig", {})
    name = ct.name
    wants_network = (host.get("NetworkMode") != "none") if network is None else bool(network)
    wants_read_only = bool(host.get("ReadonlyRootfs")) if read_only is None else bool(read_only)
    if wants_network == (host.get("NetworkMode") != "none") and wants_read_only == bool(host.get("ReadonlyRootfs")):
        raise PolicyError("The container already has that network and that read-only setting.")
    client = sandbox._client()
    image = ct.commit(repository=RECREATE_TAG, tag=name.removeprefix(sandbox.PREFIX) or "container")
    tag = f"{RECREATE_TAG}:{name.removeprefix(sandbox.PREFIX) or 'container'}"
    kwargs = dict(image=tag, name=name + "-new", command=ct.attrs.get("Config", {}).get("Cmd") or "sleep infinity", detach=True, tty=True, labels=dict(ct.labels or {}),
                  network_mode="bridge" if wants_network else "none", read_only=wants_read_only, mem_limit=host.get("Memory") or None, memswap_limit=host.get("MemorySwap") or None,
                  nano_cpus=host.get("NanoCpus") or None, pids_limit=host.get("PidsLimit") or None, cap_drop=["ALL"], security_opt=["no-new-privileges"],
                  tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"} if wants_read_only else None, user=ct.attrs.get("Config", {}).get("User") or "1000:1000",
                  working_dir=ct.attrs.get("Config", {}).get("WorkingDir") or "/work")
    new = sandbox_runtime.run_container(client, **{k: v for k, v in kwargs.items() if v is not None})
    try:
        ct.remove(force=True)
        new.rename(name)
    finally:
        try:
            client.images.remove(tag, force=True, noprune=True)         # the tag only (force, because the new container uses the image and Docker refuses to untag it otherwise; noprune, so the layers stay)
        except Exception:  # ignored: a tag that cannot be removed is a leftover image the Images tab lists and can prune
            pass
    new.reload()
    from app.backend import sandbox_history
    sandbox_history.record(name, "recreated", {"network": "bridge" if wants_network else "none", "read_only": wants_read_only})
    warn = ["The container was created again from a commit of its filesystem: what was running in memory is gone."]
    if wants_network:
        warn.append("This container can now reach the network. Code that runs in it can reach other computers; give it only what you trust to be on the network.")
    return {**sandbox._view(new), **current(new), "warnings": warn}


def isolation_warnings(policy: dict) -> list[str]:
    """What a person should know about a container's policy, in words: an open network, a writable root, a very large limit."""
    out = []
    if policy.get("network") not in (None, "none"):
        out.append("The network is open: code in this container can reach other computers.")
    if policy.get("read_only") is False:
        out.append("The root filesystem is writable: a program can change the container's system files (not your computer's).")
    if policy.get("memory_mb") is None:
        out.append("There is no memory limit.")
    if policy.get("pids") is None:
        out.append("There is no process limit.")
    return out
