"""Which MCP server URLs this process may connect to.

Locally, anything goes: an MCP server on ``http://localhost`` is the normal
development setup, and the only network being reached is the developer's own.

Deployed, the server and worker are Lambda functions that hold AWS
credentials, so a saved URL must not become a way to reach anything that is not
a public HTTPS endpoint: loopback (the Lambda runtime API lives there),
private, link-local (instance metadata) and other reserved ranges are refused,
both as written in the URL and as the host resolves when the run connects.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from nimbus.errors import BadRequestError


class McpUrlError(BadRequestError):
    code = "invalid_mcp_url"


def _parse(url: str) -> tuple[str, str, int | None]:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise McpUrlError(f"Not a valid URL: {exc}", detail={"url": url}) from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise McpUrlError("MCP server URLs must be http(s) URLs with a host", detail={"url": url})
    if parts.username or parts.password:
        raise McpUrlError(
            "Put credentials in a header, not in the URL", detail={"url": url.split("@")[-1]}
        )
    return parts.scheme, parts.hostname, port


def is_public_address(address: str) -> bool:
    """Whether an IP address is globally routable (IPv4-mapped IPv6 unwrapped).

    A scoped IPv6 address (``fe80::1%eth0``) is link-local by definition.
    """
    if "%" in address:
        return False
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


def validate_url(url: str, *, deployed: bool) -> str:
    """Check a URL as saved; returns it stripped.

    Raises:
        McpUrlError: when it is not an http(s) URL, or -- deployed -- not an
            https URL whose host could be public.
    """
    scheme, host, _ = _parse(url)
    if deployed:
        if scheme != "https":
            raise McpUrlError("MCP server URLs must use https", detail={"url": url})
        lowered = host.lower().rstrip(".")
        if lowered == "localhost" or lowered.endswith((".localhost", ".internal", ".local")):
            raise McpUrlError("MCP server URLs must be publicly reachable", detail={"url": url})
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not is_public_address(host):
            raise McpUrlError("MCP server URLs must be publicly reachable", detail={"url": url})
    return url.strip()


def check_reachable(url: str, *, deployed: bool) -> None:
    """Re-check a saved URL just before connecting, for an early, legible error.

    This is *not* the guard: a name can resolve differently between this lookup
    and the connection. :class:`nimbus.mcp.transport.PublicOnlyBackend`
    enforces the rule on every socket the MCP client actually opens.
    """
    validate_url(url, deployed=deployed)
    if not deployed:
        return
    _, host, port = _parse(url)
    try:
        infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise McpUrlError(f"Cannot resolve {host}: {exc}", detail={"url": url}) from exc
    if not infos or not all(is_public_address(str(info[4][0])) for info in infos):
        raise McpUrlError("MCP server URLs must be publicly reachable", detail={"url": url})
