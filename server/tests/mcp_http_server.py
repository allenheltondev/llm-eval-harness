"""A real MCP server over streamable HTTP, in-process, for the MCP tests.

FastMCP's ASGI app served by uvicorn on a free localhost port in a daemon
thread. Every request must carry ``X-Api-Key: <API_KEY>`` -- the stand-in for
a saved auth header -- or it gets a 401 before reaching MCP.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from mcp.server.fastmcp import FastMCP

API_KEY = "s3cret-key"
#: Requests that arrived by following /redirect -- must stay empty.
REDIRECT_TARGET_HITS: list[str] = []


def _app():
    server = FastMCP("nimbus-test", stateless_http=True, json_response=True)

    @server.tool()
    def echo(text: str) -> str:
        """Echo the text back, reversed."""
        return text[::-1]

    @server.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    inner = server.streamable_http_app()

    async def guarded(scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/redirect":
            # A public endpoint bouncing the client somewhere else (the SSRF
            # redirect case). It targets the real endpoint, so following it
            # would visibly succeed.
            REDIRECT_TARGET_HITS.clear()
            await send(
                {
                    "type": "http.response.start",
                    "status": 307,
                    "headers": [(b"location", b"/mcp?via=redirect")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        if scope["type"] == "http" and b"via=redirect" in scope.get("query_string", b""):
            REDIRECT_TARGET_HITS.append(scope["path"])
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if headers.get(b"x-api-key") != API_KEY.encode():
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await inner(scope, receive, send)

    return guarded


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def running_mcp_server() -> Iterator[str]:
    """Yield the server's MCP URL while it runs."""
    port = _free_port()
    config = uvicorn.Config(_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover - environment failure
            raise RuntimeError("MCP test server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
