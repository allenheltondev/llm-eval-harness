"""The HTTP client every MCP connection goes through.

Two rules, enforced where the connection is made rather than in a preflight
check, because a preflight only proves what a name resolved to *then*:

* **No redirects, anywhere.** A saved URL is the endpoint. Following a
  redirect would let a public server bounce the connection -- with the saved
  auth headers, which httpx only strips from ``Authorization`` -- to a host
  nobody validated. A redirect response fails the connection with the URL it
  pointed at, so the fix (save that URL) is obvious.
* **Deployed, every socket goes to a public address.** :class:`PublicOnlyBackend`
  resolves the host itself, refuses unless *every* address is public, and then
  connects to the address it checked -- so there is no second lookup for a
  rebinding name to answer differently. TLS still verifies the certificate
  against the hostname: httpcore sends the origin host as SNI, not the address
  it connected to.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterable
from typing import Any

import anyio
import httpcore
import httpx

from nimbus.mcp.urls import is_public_address

#: MCP's own defaults: 30s for requests, 5 minutes for a streamed response.
DEFAULT_TIMEOUT = httpx.Timeout(30.0, read=300.0)

Resolver = Callable[[str, int], list[str]]


def _resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """A network backend that only ever opens sockets to public addresses."""

    def __init__(
        self,
        inner: httpcore.AsyncNetworkBackend | None = None,
        resolve: Resolver = _resolve,
    ) -> None:
        self._inner = inner or httpcore.AnyIOBackend()
        self._resolve = resolve

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await anyio.to_thread.run_sync(self._resolve, host, port)
        except OSError as exc:
            raise httpcore.ConnectError(f"Cannot resolve {host}: {exc}") from exc
        if not addresses:
            raise httpcore.ConnectError(f"Cannot resolve {host}")
        blocked = [address for address in addresses if not is_public_address(address)]
        if blocked:
            raise httpcore.ConnectError(
                f"Refusing to connect to {host}: it resolves to a non-public address "
                f"({', '.join(blocked)})"
            )
        failure: httpcore.ConnectError | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as exc:
                failure = exc
        assert failure is not None
        raise failure

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix sockets are not allowed for MCP servers")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _PublicOnlyTransport(httpx.AsyncHTTPTransport):
    """httpx's own transport, with its connection pool on :class:`PublicOnlyBackend`.

    httpx has no public hook for a network backend, so the pool it builds is
    replaced with an equivalent one that has it. ``tests/test_mcp.py`` checks
    that connections really go through the backend, so an httpx upgrade that
    renames the attribute fails loudly instead of silently dropping the guard.
    """

    def __init__(self, backend: httpcore.AsyncNetworkBackend) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            network_backend=backend,
        )


async def _refuse_redirects(response: httpx.Response) -> None:
    if response.is_redirect:
        location = response.headers.get("location", "?")
        raise httpx.HTTPStatusError(
            f"The MCP server answered {response.status_code} redirecting to {location}; "
            "redirects are not followed -- save the final URL instead",
            request=response.request,
            response=response,
        )


def build_http_client(
    headers: dict[str, str],
    *,
    deployed: bool,
    backend: httpcore.AsyncNetworkBackend | None = None,
) -> httpx.AsyncClient:
    """The client an MCP connection uses: no redirects; deployed, public sockets only."""
    kwargs: dict[str, Any] = {
        "headers": headers,
        "timeout": DEFAULT_TIMEOUT,
        "follow_redirects": False,
        "event_hooks": {"response": [_refuse_redirects]},
    }
    if deployed:
        # No proxies from the environment either: a proxy would make the
        # connection on our behalf, to whatever it resolves the host to.
        kwargs["transport"] = _PublicOnlyTransport(backend or PublicOnlyBackend())
        kwargs["trust_env"] = False
    return httpx.AsyncClient(**kwargs)
