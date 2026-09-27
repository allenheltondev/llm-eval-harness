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


def _is_public(address: str) -> bool:
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
        if literal is not None and not _is_public(host):
            raise McpUrlError("MCP server URLs must be publicly reachable", detail={"url": url})
    return url.strip()


def check_reachable(url: str, *, deployed: bool) -> None:
    """Re-check a saved URL just before connecting.

    Deployed, the host is resolved and every address it resolves to must be
    public -- a name that pointed somewhere public when it was saved can point
    somewhere else now.
    """
    validate_url(url, deployed=deployed)
    if not deployed:
        return
    _, host, port = _parse(url)
    try:
        infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise McpUrlError(f"Cannot resolve {host}: {exc}", detail={"url": url}) from exc
    if not infos or not all(_is_public(str(info[4][0])) for info in infos):
        raise McpUrlError("MCP server URLs must be publicly reachable", detail={"url": url})
