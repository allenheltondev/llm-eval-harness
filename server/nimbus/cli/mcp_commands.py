"""``nimbus mcp``: save, change, test and remove remote MCP servers.

Like ``tools``, it works wherever the CLI is pointed: the signed-in stack
(through its API, so the server encrypts the headers) or, with ``--local`` or
no login, this machine's own store.

Header values are secrets. ``--header 'Name: value'`` takes one inline;
``--header Name`` asks for the value without echo, which keeps it out of your
shell history. They are never printed back -- only their names.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
from typing import Any, TextIO

from nimbus.cli import remote, render
from nimbus.config import Settings
from nimbus.errors import BadRequestError
from nimbus.mcp import service
from nimbus.mcp.schemas import McpServerCreate, McpServerUpdate
from nimbus.mcp.store import build_mcp_store

EXIT_OK = 0
EXIT_FAILED = 1


def _write(stream: TextIO, text: str) -> None:
    stream.write(text)
    stream.flush()


def parse_headers(specs: list[str] | None, err: TextIO) -> dict[str, str]:
    """``Name: value`` pairs; a bare ``Name`` prompts for its value, unechoed."""
    headers: dict[str, str] = {}
    for spec in specs or []:
        name, sep, value = spec.partition(":")
        name = name.strip()
        if not name:
            raise BadRequestError(f"--header {spec!r}: expected 'Name: value' or 'Name'")
        if not sep:
            value = getpass.getpass(f"{name}: ", stream=err)
        headers[name] = value.strip()
    return headers


class _Local:
    """The same operations as the API, against this machine's store."""

    def __init__(self, settings: Settings) -> None:
        self._store = build_mcp_store(settings)

    async def list(self) -> list[dict[str, Any]]:
        return [s.public().model_dump(mode="json") for s in self._store.list()]

    async def create(self, body: dict[str, Any]) -> dict[str, Any]:
        server = service.create(self._store, McpServerCreate(**body))
        return server.public().model_dump(mode="json")

    async def update(self, server_id: str, body: dict[str, Any]) -> dict[str, Any]:
        server = service.update(self._store, server_id, McpServerUpdate(**body))
        return server.public().model_dump(mode="json")

    async def remove(self, server_id: str) -> None:
        self._store.delete(server_id)

    async def test(self, server_id: str) -> dict[str, Any]:
        result = await asyncio.to_thread(service.test, self._store, server_id)
        return result.model_dump(mode="json")


class _Remote:
    def __init__(self, api: remote.RemoteApi) -> None:
        self._api = api

    async def list(self) -> list[dict[str, Any]]:
        return (await self._api.get("/mcp-servers")).get("servers") or []

    async def create(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._api.post("/mcp-servers", body)

    async def update(self, server_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._api.put(f"/mcp-servers/{server_id}", body)

    async def remove(self, server_id: str) -> None:
        await self._api.delete(f"/mcp-servers/{server_id}")

    async def test(self, server_id: str) -> dict[str, Any]:
        return await self._api.post(f"/mcp-servers/{server_id}/test")


def _headers_cell(server: dict[str, Any]) -> str:
    return ", ".join(server.get("header_names") or []) or "-"


def _first_line(text: str | None) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0] if lines else ""


def _saved(out: TextIO, err: TextIO, server: dict[str, Any], as_json: bool, verb: str) -> None:
    if as_json:
        _write(out, render.dumps(server) + "\n")
        return
    # The id alone on stdout, so `id=$(nimbus mcp add ...)` works.
    _write(out, server["id"] + "\n")
    _write(err, f"| {verb} {server['name']} ({server['url']}), headers: {_headers_cell(server)}\n")


async def _dispatch(backend: Any, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    action = args.mcp_command
    if action == "list":
        servers = await backend.list()
        if args.json:
            _write(out, render.dumps({"servers": servers}) + "\n")
        elif servers:
            rows = [[s["id"], s["name"], s["url"], _headers_cell(s)] for s in servers]
            _write(out, render.table(rows, ["ID", "NAME", "URL", "HEADERS"]) + "\n")
        else:
            _write(err, "| no MCP servers saved: add one with `nimbus mcp add NAME URL`\n")
        return EXIT_OK

    if action == "add":
        body = {"name": args.name, "url": args.url, "headers": parse_headers(args.header, err)}
        _saved(out, err, await backend.create(body), args.json, "saved")
        return EXIT_OK

    if action == "update":
        patch: dict[str, str | None] = dict(parse_headers(args.header, err))
        for name in args.remove_header or []:
            if name in patch:
                raise BadRequestError(f"header {name!r} is both set and removed")
            patch[name] = None
        body: dict[str, Any] = {
            key: value for key, value in (("name", args.name), ("url", args.url)) if value
        }
        if patch:
            body["headers"] = patch
        if not body:
            raise BadRequestError(
                "nothing to change: pass --name, --url, --header or --remove-header"
            )
        _saved(out, err, await backend.update(args.id, body), args.json, "updated")
        return EXIT_OK

    if action == "remove":
        await backend.remove(args.id)
        _write(err, f"| removed {args.id}\n")
        return EXIT_OK

    # test
    result = await backend.test(args.id)
    if args.json:
        _write(out, render.dumps(result) + "\n")
    elif result.get("ok"):
        tools = result.get("tools") or []
        _write(err, f"| connected: {len(tools)} tool(s)\n")
        rows = [[t["name"], _first_line(t.get("description"))] for t in tools]
        if rows:
            _write(out, render.table(rows, ["TOOL", "DESCRIPTION"]) + "\n")
    else:
        _write(err, f"| could not connect: {result.get('error')}\n")
    return EXIT_OK if result.get("ok") else EXIT_FAILED


async def mcp(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Manage saved MCP servers on the target (the signed-in stack, or here)."""
    target: remote.Login | None = getattr(args, "target", None)
    if target is None:
        return await _dispatch(_Local(settings), args, out, err)
    who = f" as {target.email}" if target.email else ""
    _write(err, f"| on {target.url}{who}\n")
    async with remote.http_client() as http:
        return await _dispatch(_Remote(remote.RemoteApi(http, target)), args, out, err)
