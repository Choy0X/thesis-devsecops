"""The egress proxy of the agent sandbox: the one way a sandbox with its network switched on reaches the internet.

A sandbox container is never given a network of its own to the outside. It is put on an internal Docker network that has nothing on it but this proxy, so it can reach this proxy and nothing else:
not the app's own address on the computer, not the database, not the other containers, not the router, not a cloud's metadata address. This program, which runs in a container of its own that is also on
a network with a way out, forwards a request only to a public address.

What it does, and nothing more:
* ``CONNECT host:port`` (HTTPS, and anything else wrapped in a tunnel) and ``GET http://host/...`` (plain HTTP, and the other methods in absolute form) are the only requests it understands.
* The port must be one of ``ALLOWED_PORTS`` (80 and 443 unless told otherwise).
* The host is resolved here, once, and the address that was checked is the address that is dialled (a name that answers one thing to the check and another to the connection cannot get through).
  Every address it resolves to must be public: a name with one private address among its answers is refused.
* No loopback, private, link-local (a cloud's metadata address is one), shared (100.64.0.0/10), multicast, reserved or unspecified address, in IPv4 or IPv6 or as an IPv4 address written in IPv6.
* An address with a name or password in front of the host is refused, and so is an address that is not a plain host name or number.
* Limits: how many connections at once, how long a request line and its headers may be, how long they may take, how long a connection may be silent.
Each decision is one line on the container's output (``ALLOW`` or ``DENY`` with the reason), which the app can show.

It uses nothing but the standard library, so it runs in the agent image with nothing added to it.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
import sys
import time

ALLOWED_PORTS = {int(p) for p in os.environ.get("EGRESS_PORTS", "80,443").split(",") if p.strip().isdigit()}
MAX_CONNECTIONS = int(os.environ.get("EGRESS_MAX_CONNECTIONS", "64"))
HEADER_BYTES = 16 * 1024
HEADER_SECONDS = 15.0
CONNECT_SECONDS = 10.0
IDLE_SECONDS = float(os.environ.get("EGRESS_IDLE_SECONDS", "120"))
RELAY_CHUNK = 64 * 1024
_HOST = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*|\[[0-9A-Fa-f:.]+\])$")


class Denied(Exception):
    """A request the proxy does not forward, with the reason in words."""


# ranges that must never be reached whatever the standard library calls them: site-local addresses (still routed inside some networks), the two NAT64 prefixes (an IPv4 address, private ones too, in the
# last 32 bits), the shared and anycast-relay ranges, unique-local, the protocol-assignment, documentation and segment-routing ranges
_DENY = [ipaddress.ip_network(n) for n in ("64:ff9b::/96", "64:ff9b:1::/48", "fec0::/10", "100.64.0.0/10", "192.88.99.0/24", "5f00::/16", "fc00::/7", "2001::/23", "2001:db8::/32", "3fff::/20")]


def is_public(address: str) -> bool:
    """Whether ``address`` is one a sandbox may reach: only what the standard library calls global, after an IPv4 address written in IPv6 is read as the IPv4 address it is, and never a range of ``_DENY``."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped                                          # ::ffff:10.0.0.1 is 10.0.0.1
    if isinstance(ip, ipaddress.IPv6Address) and (ip.sixtofour is not None or ip.teredo is not None):
        return False                                                 # tunnels that carry an IPv4 address inside: not trusted to say which
    if ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_private or not ip.is_global:
        return False
    return not any(ip in net for net in _DENY if net.version == ip.version)


def split_host_port(target: str, default_port: int | None = None) -> tuple[str, int]:
    """``(host, port)`` from ``host:port`` (an IPv6 address in square brackets), refused when it is not a plain host."""
    target = target.strip()
    if not target or "@" in target or any(c in target for c in " \t\r\n/\\?#"):
        raise Denied("the address is not a plain host and port")
    if target.startswith("["):
        host, _, rest = target.partition("]")
        host += "]"
        port_text = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port_text = target.rpartition(":") if target.count(":") == 1 else (target, "", "")
        if not host:
            host, port_text = target, ""
    if not _HOST.match(host):
        raise Denied("the host is not a plain name or number")
    if port_text:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise Denied("the port is not a number")
        port = int(port_text)
    elif default_port is not None:
        port = default_port
    else:
        raise Denied("the port is missing")
    return host.strip("[]"), port


def check_port(port: int) -> None:
    if port not in ALLOWED_PORTS:
        raise Denied(f"port {port} is not one of {', '.join(str(p) for p in sorted(ALLOWED_PORTS))}")


async def resolve_public(host: str, port: int, resolver=None) -> str:
    """The address to dial for ``host``: resolved here, every answer public, the first one returned. A host that is already an address is checked as it is."""
    try:
        literal = ipaddress.ip_address(host)
        addresses = [str(literal)]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            infos = await (resolver or loop.getaddrinfo)(host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise Denied("the name does not resolve") from None
        addresses = [info[4][0] for info in infos]
    if not addresses:
        raise Denied("the name does not resolve")
    for a in addresses:
        if not is_public(a):
            raise Denied(f"{host} is, or resolves to, an address that is not public ({a})")
    return addresses[0]


def log(line: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)


async def _read_head(reader: asyncio.StreamReader) -> list[bytes]:
    """The request line and the headers of a request, as lines, within the limits."""
    try:
        data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_SECONDS)
    except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, asyncio.TimeoutError):
        raise Denied("the request head is too long, too slow or not complete") from None
    if len(data) > HEADER_BYTES:
        raise Denied("the request head is too long")
    return data[:-4].split(b"\r\n")


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(RELAY_CHUNK), IDLE_SECONDS)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # ignored: a stream that is already closed needs nothing more
            pass


async def _refuse(writer: asyncio.StreamWriter, why: str, status: str = "403 Forbidden") -> None:
    body = f"Refused by the sandbox's egress proxy: {why}.\n".encode()
    writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    writer.close()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, resolver=None, connect=None) -> None:
    """One client of the proxy. ``resolver`` and ``connect`` are for tests; they are what the program itself does."""
    peer = writer.get_extra_info("peername")
    target_text = "?"
    try:
        lines = await _read_head(reader)
        try:
            method, target, version = lines[0].decode("latin-1").split(" ", 2)
        except ValueError:
            raise Denied("not a request") from None
        if not version.startswith("HTTP/1."):
            raise Denied("not an HTTP/1 request")
        headers = [l for l in lines[1:] if l]
        if method.upper() == "CONNECT":
            host, port = split_host_port(target)
            rest = b""
        else:
            if not target.lower().startswith("http://"):
                raise Denied("only requests for an http:// address, and CONNECT, are forwarded")
            after = target[7:]
            authority, slash, path = after.partition("/")
            host, port = split_host_port(authority, 80)
            rest = ("/" + path).encode("latin-1", "replace")
        target_text = f"{host}:{port}"
        check_port(port)
        address = await resolve_public(host, port, resolver)
        try:
            up_reader, up_writer = await asyncio.wait_for((connect or asyncio.open_connection)(address, port), CONNECT_SECONDS)
        except (OSError, asyncio.TimeoutError):
            log(f"FAIL {target_text} -> {address} (could not connect)")
            await _refuse(writer, "the address did not answer", "502 Bad Gateway")
            return
        log(f"ALLOW {method.upper()} {target_text} -> {address} (for {peer[0] if peer else '?'})")
        if method.upper() == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            keep = [h for h in headers if h.split(b":", 1)[0].strip().lower() not in (b"proxy-connection", b"proxy-authorization", b"connection")]
            up_writer.write(f"{method} ".encode() + rest + f" {version}\r\n".encode() + b"\r\n".join(keep) + b"\r\nConnection: close\r\n\r\n")
            await up_writer.drain()
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
    except Denied as e:
        log(f"DENY {target_text} ({e})")
        await _refuse(writer, str(e))
    except (ConnectionError, OSError):
        try:
            writer.close()
        except Exception:  # ignored: the client went away
            pass


async def serve(host: str = "0.0.0.0", port: int = 3128):
    slots = asyncio.Semaphore(MAX_CONNECTIONS)

    async def limited(reader, writer):
        if slots.locked():
            await _refuse(writer, "too many connections at once", "503 Service Unavailable")
            return
        async with slots:
            await handle(reader, writer)

    server = await asyncio.start_server(limited, host, port, limit=HEADER_BYTES)
    log(f"egress proxy listening on {host}:{port}; ports {sorted(ALLOWED_PORTS)}; public addresses only")
    return server


def main() -> int:
    port = int(os.environ.get("EGRESS_PORT", "3128"))

    async def run():
        server = await serve(port=port)
        async with server:
            await server.serve_forever()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
