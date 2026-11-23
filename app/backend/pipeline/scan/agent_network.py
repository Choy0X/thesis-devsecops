"""The network of the agent sandbox: closed by default, and when the owner switches it on, a way out through a guarded proxy and no other.

``off`` (the default): the sandbox container has no network at all.

``proxy``: the container is put on an internal Docker network of its own project (``internal`` means Docker gives it no route to anywhere: not the host, not the other containers of the computer, not the
internet; one network for each project, so the sandboxes of two projects cannot reach each other) that has exactly one other member, the egress proxy (``scan/agent_sandbox_image/egress_proxy.py``) in a container of
its own, shared by all of them and also on the default network, so it can reach the outside. A program in the
sandbox that honours ``HTTP_PROXY`` and ``HTTPS_PROXY`` (pip, npm, git, curl, apt, most others do) goes out through it; one that does not honour them reaches nothing. The proxy forwards only to public
addresses on the web ports (its file says exactly what it refuses).

The network has no address for the host. A Docker bridge network normally gives the computer an address of its own on it (the gateway), and "internal" does not stop a container from talking to the
computer through that address: whatever listens on the computer for all addresses (a published port, a service) would answer. The network is therefore made with the bridge's own IPv4 address inhibited
(``com.docker.network.bridge.inhibit_ipv4``), so there is no such address and nothing of the computer to reach; containers on it still reach each other (the sandbox reaches the proxy). A Docker that
cannot make such a network is refused with a sentence, never worked around with a plain internal one.

What this does not make safe, and the owner is told so when they switch it on: the project's code runs in the sandbox, and with a way out it can send what is in the sandbox (the project's copy, and what
it reads from its tools) to any public address on port 80 or 443. The proxy keeps the sandbox from the owner's computer and network; it does not keep the project's code from the internet.
"""
from __future__ import annotations

import pathlib
import re

from app.backend import sandbox_runtime, sandbox
from app.backend.config import Settings

OFF, PROXY = "off", "proxy"
NETWORK = "aivdf-agent-net"                  # the prefix of the internal networks: one for each project, with its sandbox and the proxy, nothing else
PROXY_NAME = "aivdf-agent-egress"
PROXY_PORT = 3128
PROXY_URL = f"http://{PROXY_NAME}:{PROXY_PORT}"
PROXY_SCRIPT = pathlib.Path(__file__).resolve().parent / "agent_sandbox_image" / "egress_proxy.py"
PROXY_LIMITS = {"memory_mb": 128, "pids": 128, "cpus": 0.5}
LABEL = "aivdf.kind"
NO_HOST_ADDRESS = "com.docker.network.bridge.inhibit_ipv4"       # the bridge gets no IPv4 address of its own: the computer has no address on the network


class AgentNetworkError(RuntimeError):
    """The network of the sandbox could not be set up, as a sentence for the owner."""


def mode() -> str:
    """What the settings say: ``proxy`` or (anything else, the default) ``off``."""
    value = str(getattr(Settings.load(), "agent_sandbox_network", "") or "").strip().lower()
    return PROXY if value == PROXY else OFF


def environment() -> dict:
    """The variables that make a program in the sandbox use the proxy (both spellings, since tools differ), and none for what must not go through it (the sandbox's own address)."""
    return {"HTTP_PROXY": PROXY_URL, "HTTPS_PROXY": PROXY_URL, "http_proxy": PROXY_URL, "https_proxy": PROXY_URL, "ALL_PROXY": PROXY_URL, "all_proxy": PROXY_URL,
            "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1"}


def network_name(project_id: str) -> str:
    """The internal network of one project's sandbox (a Docker name: letters, digits, underscore, dot, hyphen)."""
    return f"{NETWORK}-{re.sub(r'[^A-Za-z0-9_.-]', '-', str(project_id))}"[:60]


def _networks_of(ct) -> dict:
    try:
        return (ct.attrs.get("NetworkSettings") or {}).get("Networks") or {}
    except AttributeError:
        return {}


STALE = "stale"


def container_mode(ct) -> str:
    """What a sandbox container is set up for: ``proxy`` when it is attached to an agent network, ``stale`` when it is not attached but was made to be (its way out was taken away: it has no network
    now, and a restart would look for a network that is gone), ``off`` when it never had one."""
    try:
        host = (ct.attrs.get("HostConfig") or {}).get("NetworkMode") or ""
    except AttributeError:
        return OFF
    if any(n.startswith(NETWORK) for n in _networks_of(ct)):
        return PROXY
    return STALE if host.startswith(NETWORK) else OFF


def _is_isolated(net) -> bool:
    """Internal, with the computer's IPv4 address inhibited, with IPv6 off (an IPv6 address of the computer would be a way to it that the IPv4 option does not close) and with no gateway in any address range."""
    attrs = net.attrs or {}
    options = attrs.get("Options") or {}
    if not attrs.get("Internal") or str(options.get(NO_HOST_ADDRESS, "")).lower() != "true":
        return False
    if attrs.get("EnableIPv6") or str(options.get("com.docker.network.enable_ipv6", "false")).lower() == "true":
        return False
    return not any((c or {}).get("Gateway") for c in (attrs.get("IPAM") or {}).get("Config") or [])


def ensure_network(client, name: str):
    """The internal network of a project, made if it is not there. It is internal (nothing on it has a route to anything else) and has no address for the computer on it (see the top of this file): that
    is what keeps a sandbox on it from reaching anything but the proxy. One that exists and is not both is refused, never used."""
    try:
        net = client.networks.get(name)
        net.reload()
        if not _is_isolated(net):
            raise AgentNetworkError(f"The Docker network {name} exists but is not isolated (internal, with no address for this computer on it), so it would give a sandbox a way to the computer. Remove it (docker network rm {name}) and try again.")
        return net
    except AgentNetworkError:
        raise
    except Exception:  # ignored: not there yet (the client names it as its own NotFound); made below
        pass
    try:
        net = client.networks.create(name, driver="bridge", internal=True, enable_ipv6=False, options={NO_HOST_ADDRESS: "true"}, labels={sandbox.LABEL_KEY: "1", LABEL: "agent-network"}, check_duplicate=True)
    except Exception as e:
        raise AgentNetworkError(f"Docker could not make an isolated network for the sandbox ({str(e)[:240]}). This needs a recent Docker Engine; the sandbox keeps no network until it can.") from None
    net.reload()
    if not _is_isolated(net):                                                  # an engine that took the option and ignored it
        net.remove()
        raise AgentNetworkError("Docker made the network but did not apply its isolation, so it was removed. The sandbox keeps no network.")
    return net


def proxy_kwargs(image: str, network: str) -> dict:
    """Arguments of the proxy's container: hardened like the sandbox (every capability dropped, no new privileges, a read-only root, process and memory limits, not root), on the internal network of the
    first project that needs it, reaching the outside through the default one. The program is passed as the text of the command, so nothing has to be added to the image."""
    script = PROXY_SCRIPT.read_text(encoding="utf-8")
    return {"image": image, "name": PROXY_NAME, "command": ["python3", "-I", "-c", script], "detach": True, "labels": {sandbox.LABEL_KEY: "1", LABEL: "agent-egress"}, "network": network,
            "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges"], "user": "10001:10001", "pids_limit": PROXY_LIMITS["pids"],
            "mem_limit": f"{PROXY_LIMITS['memory_mb']}m", "memswap_limit": f"{PROXY_LIMITS['memory_mb']}m", "nano_cpus": int(PROXY_LIMITS["cpus"] * 1e9),
            "tmpfs": {"/tmp": "rw,noexec,nosuid,size=16m"}, "restart_policy": {"Name": "unless-stopped"}, "environment": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"}}


def _proxy(client):
    try:
        return client.containers.get(PROXY_NAME)
    except Exception:  # ignored: not there (the client reports that as its own NotFound)
        return None


def ensure_proxy(client, image: str, network: str):
    """The egress proxy, running and on ``network`` (a project's internal network, made if it is not there): created if it does not exist, connected if it is not on that network yet, started if it was
    stopped. A container of that name that this app did not make is never touched: the owner is told. One of ours without its way out (the default network) is made again."""
    ensure_network(client, network)
    ct = _proxy(client)
    if ct is not None:
        ct.reload()
        labels = ct.labels or {}
        if labels.get(sandbox.LABEL_KEY) != "1" or labels.get(LABEL) != "agent-egress":
            raise AgentNetworkError(f"A container named {PROXY_NAME} exists that this app did not make, so it is not used. Rename or remove it (docker rm -f {PROXY_NAME}) and try again.")
        if "bridge" not in _networks_of(ct):
            ct.remove(force=True)                       # a proxy with no way out is not one: made again
            ct = None
    try:
        if ct is None:
            ct = sandbox_runtime.run_container(client, create_only=True, **{k: v for k, v in proxy_kwargs(image, network).items() if k != "detach"})
            client.networks.get("bridge").connect(ct)   # the way out: the sandboxes are never on it
        elif network not in _networks_of(ct):
            client.networks.get(network).connect(ct)
        if ct.status != "running":
            sandbox_runtime.start_container(ct)
        return ct
    except Exception as e:
        raise AgentNetworkError(f"The egress proxy could not be started: {str(e)[:300]}") from None


def release(client, network: str) -> None:
    """A project's network when its sandbox no longer needs it (best effort): the proxy leaves it and it is removed, unless a container other than the proxy is still on it; the proxy itself is removed
    when no project's network is left."""
    proxy = _proxy(client)
    try:
        users = [c for c in client.containers.list(all=True) if c.name != PROXY_NAME and network in _networks_of(c)]
        if not users:
            net = client.networks.get(network)
            if proxy is not None:
                try:
                    net.disconnect(proxy)
                except Exception:  # ignored: it was not on it
                    pass
            net.remove()
    except Exception:  # ignored: already gone, or a sandbox is still using it
        pass
    if proxy is not None:
        try:
            proxy.reload()
            if not any(n.startswith(NETWORK) for n in _networks_of(proxy)):
                proxy.remove(force=True)
        except Exception:  # ignored: already gone
            pass


def revoke(client) -> list[str]:
    """Take the way out away from every sandbox now, whatever is running in it: each container is disconnected from its agent network, the proxy is removed and then the networks. The names of the
    sandboxes that were cut off. A session in the middle of a command loses its network at once; the container is made again with none when the next session starts (``container_mode`` is ``stale``)."""
    cut = []
    try:
        everything = client.containers.list(all=True)
    except Exception:  # ignored: the daemon could not list them; remove_all below still takes the proxy away
        everything = []
    for ct in everything:
        if ct.name == PROXY_NAME:
            continue
        for n in [n for n in _networks_of(ct) if n.startswith(NETWORK)]:
            try:
                client.networks.get(n).disconnect(ct, force=True)
                cut.append(ct.name)
            except Exception:  # ignored: it left the network meanwhile
                pass
    remove_all(client)
    return sorted(set(cut))


def remove_all(client) -> None:
    """The proxy and every agent network, whatever is on them (for a cleanup after a test and for the day the owner wants none of it left; a sandbox on one keeps it, since Docker refuses)."""
    proxy = _proxy(client)
    if proxy is not None:
        try:
            proxy.remove(force=True)
        except Exception:  # ignored: already gone
            pass
    try:
        nets = [n for n in client.networks.list() if n.name.startswith(NETWORK)]
    except Exception:  # ignored: the daemon could not list them
        nets = []
    for n in nets:
        try:
            n.remove()
        except Exception:  # ignored: a sandbox is still on it
            pass
