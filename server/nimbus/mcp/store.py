"""Where saved MCP server definitions live.

Two stores behind one protocol, chosen the way the history backend is: a
DynamoDB table when one is configured (``NIMBUS_MCP_TABLE`` -- every deployed
stack), the local SQLite history file otherwise. The deployed store encrypts
the auth headers with KMS before they are written, so a table read alone
never yields a secret.

Item shape (single-table, alongside runs and evaluations)::

    pk=MCP#{id}  sk=META  GSI1PK=MCP  GSI1SK={created_at}
    name, url, created_at, updated_at, headers_enc (base64 KMS ciphertext)
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from fastapi import Depends, status
from sqlmodel import Session, select

from nimbus import deployment
from nimbus.config import Settings, get_settings
from nimbus.errors import AppError, NotFoundError
from nimbus.mcp.schemas import McpServer

PK_PREFIX = "MCP#"
GSI1_PARTITION = "MCP"
META_SK = "META"


class McpStoreUnavailableError(AppError):
    """This process has nowhere durable to keep MCP server definitions."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "mcp_store_unavailable"


def _now() -> datetime:
    return datetime.now(UTC)


def _not_found(server_id: str) -> NotFoundError:
    return NotFoundError(
        f"MCP server {server_id!r} not found",
        detail={"mcp_server_id": server_id},
        code="unknown_mcp_server",
    )


class McpServerStore(Protocol):
    def list(self) -> list[McpServer]: ...

    def get(self, server_id: str) -> McpServer:
        """Raises :class:`NotFoundError` (code ``unknown_mcp_server``)."""
        ...

    def create(self, *, name: str, url: str, headers: dict[str, str]) -> McpServer: ...

    def save(self, server: McpServer) -> McpServer:
        """Replace an existing definition; stamps ``updated_at``."""
        ...

    def delete(self, server_id: str) -> None: ...


# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #


def _aware(value: datetime) -> datetime:
    # SQLite hands datetimes back naive; they were written as UTC.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class SqliteMcpStore:
    """The local store: a table in the history database."""

    def _session(self) -> Session:
        from nimbus.store.db import get_engine

        return Session(get_engine())

    @staticmethod
    def _model(row: Any) -> McpServer:
        return McpServer(
            id=row.id,
            name=row.name,
            url=row.url,
            headers=json.loads(row.headers or "{}"),
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    def list(self) -> list[McpServer]:
        from nimbus.store.models import McpServerRow

        with self._session() as session:
            rows = session.exec(select(McpServerRow).order_by(McpServerRow.created_at)).all()
            return [self._model(row) for row in rows]

    def get(self, server_id: str) -> McpServer:
        from nimbus.store.models import McpServerRow

        with self._session() as session:
            row = session.get(McpServerRow, server_id)
            if row is None:
                raise _not_found(server_id)
            return self._model(row)

    def create(self, *, name: str, url: str, headers: dict[str, str]) -> McpServer:
        from nimbus.store.models import McpServerRow

        now = _now()
        row = McpServerRow(
            id=uuid4().hex,
            name=name,
            url=url,
            headers=json.dumps(headers),
            created_at=now,
            updated_at=now,
        )
        with self._session() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._model(row)

    def save(self, server: McpServer) -> McpServer:
        from nimbus.store.models import McpServerRow

        with self._session() as session:
            row = session.get(McpServerRow, server.id)
            if row is None:
                raise _not_found(server.id)
            row.name = server.name
            row.url = server.url
            row.headers = json.dumps(server.headers)
            row.updated_at = _now()
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._model(row)

    def delete(self, server_id: str) -> None:
        from nimbus.store.models import McpServerRow

        with self._session() as session:
            row = session.get(McpServerRow, server_id)
            if row is None:
                raise _not_found(server_id)
            session.delete(row)
            session.commit()


# --------------------------------------------------------------------------- #
# DynamoDB + KMS
# --------------------------------------------------------------------------- #


class HeaderCipher(Protocol):
    def encrypt(self, headers: dict[str, str], server_id: str) -> str: ...

    def decrypt(self, blob: str, server_id: str) -> dict[str, str]: ...


class KmsHeaderCipher:
    """KMS envelope for a server's headers, bound to that server's id.

    The encryption context ties each ciphertext to its item, so a blob copied
    onto another server's item does not decrypt there.
    """

    def __init__(self, key_id: str | None, region_name: str, client: Any | None = None) -> None:
        self._key_id = key_id
        self._region_name = region_name
        self._client = client

    def _kms(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("kms", region_name=self._region_name)
        return self._client

    @staticmethod
    def _context(server_id: str) -> dict[str, str]:
        return {"nimbus:mcp-server": server_id}

    def encrypt(self, headers: dict[str, str], server_id: str) -> str:
        if not self._key_id:
            raise McpStoreUnavailableError(
                "Saving MCP server headers needs a KMS key: set NIMBUS_MCP_KMS_KEY_ID"
            )
        response = self._kms().encrypt(
            KeyId=self._key_id,
            Plaintext=json.dumps(headers).encode(),
            EncryptionContext=self._context(server_id),
        )
        return base64.b64encode(response["CiphertextBlob"]).decode()

    def decrypt(self, blob: str, server_id: str) -> dict[str, str]:
        response = self._kms().decrypt(
            CiphertextBlob=base64.b64decode(blob),
            EncryptionContext=self._context(server_id),
        )
        return json.loads(response["Plaintext"])


class DynamoMcpStore:
    """The deployed store: items in the stack's table, headers encrypted."""

    def __init__(self, table: Any, cipher: HeaderCipher) -> None:
        self._table = table
        self._cipher = cipher

    def _item(self, server: McpServer) -> dict[str, Any]:
        item: dict[str, Any] = {
            "pk": PK_PREFIX + server.id,
            "sk": META_SK,
            "GSI1PK": GSI1_PARTITION,
            "GSI1SK": server.created_at.isoformat(),
            "name": server.name,
            "url": server.url,
            "created_at": server.created_at.isoformat(),
            "updated_at": server.updated_at.isoformat(),
            "header_names": sorted(server.headers),
        }
        if server.headers:
            item["headers_enc"] = self._cipher.encrypt(server.headers, server.id)
        return item

    def _model(self, item: dict[str, Any], *, with_headers: bool = True) -> McpServer:
        server_id = str(item["pk"])[len(PK_PREFIX) :]
        blob = item.get("headers_enc")
        if blob and with_headers:
            headers = self._cipher.decrypt(str(blob), server_id)
        else:
            # Listing never needs the values: names only, no KMS call.
            headers = {name: "" for name in item.get("header_names") or []}
        return McpServer(
            id=server_id,
            name=str(item["name"]),
            url=str(item["url"]),
            headers=headers,
            created_at=datetime.fromisoformat(str(item["created_at"])),
            updated_at=datetime.fromisoformat(str(item["updated_at"])),
        )

    def list(self) -> list[McpServer]:
        from boto3.dynamodb.conditions import Key

        from nimbus.store.ddb_items import GSI1_INDEX_NAME

        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "IndexName": GSI1_INDEX_NAME,
            "KeyConditionExpression": Key("GSI1PK").eq(GSI1_PARTITION),
            "ScanIndexForward": True,
        }
        while True:
            response = self._table.query(**kwargs)
            items.extend(response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
        return [self._model(item, with_headers=False) for item in items]

    def get(self, server_id: str) -> McpServer:
        response = self._table.get_item(Key={"pk": PK_PREFIX + server_id, "sk": META_SK})
        item = response.get("Item")
        if not item:
            raise _not_found(server_id)
        return self._model(item)

    def create(self, *, name: str, url: str, headers: dict[str, str]) -> McpServer:
        now = _now()
        server = McpServer(
            id=uuid4().hex, name=name, url=url, headers=headers, created_at=now, updated_at=now
        )
        self._table.put_item(Item=self._item(server))
        return server

    def save(self, server: McpServer) -> McpServer:
        self.get(server.id)
        saved = server.model_copy(update={"updated_at": _now()})
        self._table.put_item(Item=self._item(saved))
        return saved

    def delete(self, server_id: str) -> None:
        self.get(server_id)
        self._table.delete_item(Key={"pk": PK_PREFIX + server_id, "sk": META_SK})


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def build_mcp_store(settings: Settings) -> McpServerStore:
    """The store these settings resolve to.

    Raises:
        McpStoreUnavailableError: inside Lambda with no table configured --
            a Lambda's SQLite file does not outlive the invocation.
    """
    if settings.mcp_table:
        from nimbus.evals import ddb_reader

        return DynamoMcpStore(
            ddb_reader.build_table(settings.mcp_table, settings.aws_region),
            KmsHeaderCipher(settings.mcp_kms_key_id, settings.aws_region),
        )
    if deployment.in_lambda():
        raise McpStoreUnavailableError(
            "Saved MCP servers are not configured on this deployment (NIMBUS_MCP_TABLE)"
        )
    return SqliteMcpStore()


def get_mcp_store(settings: Settings = Depends(get_settings)) -> McpServerStore:
    """FastAPI dependency: the active MCP server store."""
    return build_mcp_store(settings)
