"""API and storage shapes for saved MCP servers."""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: HTTP header names (RFC 9110 token characters).
_HEADER_NAME_CHARS = frozenset(
    "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)
#: Headers a caller may not set: the transport owns them.
RESERVED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "accept",
        "connection",
        "transfer-encoding",
        "mcp-session-id",
        "mcp-protocol-version",
    }
)
MAX_HEADERS = 10
MAX_HEADER_VALUE_LENGTH = 2048
#: A server's headers, serialized, must fit one standard-tier (free) SSM
#: parameter, which holds 4 KB; see :class:`nimbus.mcp.store.SsmHeaderSecrets`.
MAX_HEADERS_BYTES = 4000


def _check_total(headers: dict[str, str]) -> None:
    if len(json.dumps(headers).encode()) > MAX_HEADERS_BYTES:
        raise ValueError(f"a server's headers are limited to {MAX_HEADERS_BYTES} bytes in total")


def _check_header_name(name: str) -> str:
    if not name or len(name) > 128 or any(c not in _HEADER_NAME_CHARS for c in name):
        raise ValueError(f"invalid header name {name!r}")
    if name.lower() in RESERVED_HEADERS:
        raise ValueError(f"header {name!r} is set by the MCP transport and cannot be overridden")
    return name


def _check_header_value(value: str) -> str:
    if len(value) > MAX_HEADER_VALUE_LENGTH:
        raise ValueError(f"header values are limited to {MAX_HEADER_VALUE_LENGTH} characters")
    if any(c in value for c in "\r\n\0"):
        raise ValueError("header values cannot contain line breaks")
    return value


class McpServerCreate(BaseModel):
    """Body of ``POST /mcp-servers``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=2048)
    #: Sent on every request to the server (e.g. ``Authorization``). Secret:
    #: write-only through the API.
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @field_validator("headers")
    @classmethod
    def _check_headers(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_HEADERS:
            raise ValueError(f"at most {MAX_HEADERS} headers")
        _check_unique(value)
        checked = {_check_header_name(k): _check_header_value(v) for k, v in value.items()}
        _check_total(checked)
        return checked


class McpServerUpdate(BaseModel):
    """Body of ``PUT /mcp-servers/{id}``: every field optional.

    ``headers`` is a patch because the client never sees the stored values: a
    string sets (or replaces) that header, ``null`` removes it, and a header
    left out is kept as it is.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    headers: dict[str, str | None] | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @field_validator("headers")
    @classmethod
    def _check_headers(cls, value: dict[str, str | None] | None) -> dict[str, str | None] | None:
        if value is None:
            return None
        _check_unique(value)
        return {
            _check_header_name(k): (None if v is None else _check_header_value(v))
            for k, v in value.items()
        }

    def apply_headers(self, current: dict[str, str]) -> dict[str, str]:
        """``current`` with this patch applied (names compared case-insensitively)."""
        if self.headers is None:
            return dict(current)
        merged = dict(current)
        for name, value in self.headers.items():
            for existing in [k for k in merged if k.lower() == name.lower()]:
                del merged[existing]
            if value is not None:
                merged[name] = value
        if len(merged) > MAX_HEADERS:
            raise ValueError(f"at most {MAX_HEADERS} headers")
        _check_total(merged)
        return merged


def _check_unique(headers: dict[str, object]) -> None:
    lowered = [k.lower() for k in headers]
    if len(set(lowered)) != len(lowered):
        raise ValueError("header names must be unique (case-insensitive)")


class McpServer(BaseModel):
    """A stored definition, secrets included. Never serialized to a client."""

    id: str
    name: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    def public(self) -> McpServerSummary:
        return McpServerSummary(
            id=self.id,
            name=self.name,
            url=self.url,
            header_names=sorted(self.headers),
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class McpServerSummary(BaseModel):
    """What the API returns: the header *names*, never their values."""

    id: str
    name: str
    url: str
    header_names: list[str]
    created_at: datetime
    updated_at: datetime


class McpToolInfo(BaseModel):
    name: str
    description: str | None = None


class McpTestResult(BaseModel):
    """``POST /mcp-servers/{id}/test``: did it connect, and what does it offer."""

    ok: bool
    tools: list[McpToolInfo] = Field(default_factory=list)
    error: str | None = None
