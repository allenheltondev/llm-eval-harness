"""Where saved MCP server definitions live.

Two stores behind one protocol, chosen the way the history backend is: a
DynamoDB table when one is configured (``NIMBUS_MCP_TABLE`` -- every deployed
stack), the local SQLite history file otherwise. The deployed store keeps
the auth header *values* out of the table entirely: they are SSM Parameter
Store ``SecureString`` parameters, so a table read alone never yields a secret.

Item shape (single-table, alongside runs and evaluations)::

    pk=MCP#{id}  sk=META  GSI1PK=MCP  GSI1SK={created_at}
    name, url, created_at, updated_at, header_names
    -> values: SecureString {NIMBUS_MCP_SSM_PREFIX}/{id}, JSON {name: value}
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from fastapi import Depends, status
from sqlmodel import Session, select

from nimbus import deployment
from nimbus.config import Settings, get_settings
from nimbus.errors import AppError, NotFoundError
from nimbus.mcp.schemas import McpServer

logger = logging.getLogger(__name__)

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
# DynamoDB + SSM Parameter Store
# --------------------------------------------------------------------------- #


class HeaderSecrets(Protocol):
    """Where a deployed server's header values live, keyed by server id."""

    def put(self, server_id: str, headers: dict[str, str]) -> None: ...

    def get(self, server_id: str) -> dict[str, str]: ...

    def delete(self, server_id: str) -> None: ...


class SsmHeaderSecrets:
    """One ``SecureString`` parameter per server: ``{prefix}/{server_id}``.

    Encrypted with the account's AWS-managed ``aws/ssm`` key, so there is no
    key to create or grant. Standard-tier parameters hold at most 4 KB, which
    :data:`nimbus.mcp.schemas.MAX_HEADERS_BYTES` keeps a server's headers under.
    """

    def __init__(self, prefix: str, region_name: str, client: Any | None = None) -> None:
        self._prefix = prefix.rstrip("/")
        self._region_name = region_name
        self._client = client

    def _ssm(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("ssm", region_name=self._region_name)
        return self._client

    def _name(self, server_id: str) -> str:
        return f"{self._prefix}/{server_id}"

    def put(self, server_id: str, headers: dict[str, str]) -> None:
        self._ssm().put_parameter(
            Name=self._name(server_id),
            Value=json.dumps(headers),
            Type="SecureString",
            Overwrite=True,
            Description="Nimbus: a saved MCP server's auth headers",
        )

    def get(self, server_id: str) -> dict[str, str]:
        client = self._ssm()
        try:
            response = client.get_parameter(Name=self._name(server_id), WithDecryption=True)
        except client.exceptions.ParameterNotFound as exc:
            # The item says it has headers and they are gone: connecting
            # without them would fail anyway, and less legibly.
            raise McpStoreUnavailableError(
                f"The saved headers for MCP server {server_id!r} are missing; "
                "remove the server and add it again",
                detail={"mcp_server_id": server_id},
            ) from exc
        return json.loads(response["Parameter"]["Value"])

    def delete(self, server_id: str) -> None:
        client = self._ssm()
        with suppress(client.exceptions.ParameterNotFound):
            client.delete_parameter(Name=self._name(server_id))


class DynamoMcpStore:
    """The deployed store: definitions in the stack's table, header values in SSM.

    The item carries the header *names* only, so listing -- and anyone who can
    read the table -- never sees a value.
    """

    def __init__(self, table: Any, secrets: HeaderSecrets) -> None:
        self._table = table
        self._secrets = secrets

    @staticmethod
    def _item(server: McpServer) -> dict[str, Any]:
        return {
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

    def _model(self, item: dict[str, Any], *, with_headers: bool = True) -> McpServer:
        server_id = str(item["pk"])[len(PK_PREFIX) :]
        names = list(item.get("header_names") or [])
        if names and with_headers:
            headers = self._secrets.get(server_id)
        else:
            # Listing never needs the values: names only, no SSM call.
            headers = {name: "" for name in names}
        return McpServer(
            id=server_id,
            name=str(item["name"]),
            url=str(item["url"]),
            headers=headers,
            created_at=datetime.fromisoformat(str(item["created_at"])),
            updated_at=datetime.fromisoformat(str(item["updated_at"])),
        )

    def _write(
        self,
        server: McpServer,
        *,
        previous_item: dict[str, Any] | None,
        previous_headers: dict[str, str],
    ) -> None:
        """Write the secret, then the item; undo the secret only if the item didn't land.

        Secret first, so an item that names headers never points at a
        parameter that was not written. A failed ``put_item`` is *ambiguous*,
        though: a timeout can arrive after DynamoDB committed. So the outcome
        is read back (strongly consistent) before anything is undone:

        * the new item is there -- the write landed; nothing to undo.
        * the previous item (or, for a create, none) is there -- it did not;
          the secret goes back to ``previous_headers``, the headers that item
          names, so the pair stays consistent and a create strands nothing.
        * the read fails too, or finds something else -- the outcome is
          unknown; nothing is undone (undoing could itself break a pair that
          landed) and the error is raised.
        """
        attempted = self._item(server)
        self._put_secret(server.id, server.headers)
        try:
            self._table.put_item(Item=attempted)
        except Exception:
            try:
                current = self._read(server.id, consistent=True)
            except Exception:  # pragma: no cover - logged below; the write error wins
                logger.warning("MCP server %s: outcome of a failed write unknown", server.id)
                raise
            if current == attempted:
                logger.info("MCP server %s: write reported failure but landed", server.id)
                return
            if current == previous_item:
                try:
                    self._put_secret(server.id, previous_headers)
                except Exception:  # pragma: no cover - the original failure matters more
                    logger.warning("could not restore headers for MCP server %s", server.id)
            else:  # pragma: no cover - a concurrent writer; leave its state alone
                logger.warning("MCP server %s changed during a failed write", server.id)
            raise

    def _put_secret(self, server_id: str, headers: dict[str, str]) -> None:
        if headers:
            self._secrets.put(server_id, headers)
        else:
            self._secrets.delete(server_id)

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

    def _read(self, server_id: str, *, consistent: bool = False) -> dict[str, Any] | None:
        key = {"pk": PK_PREFIX + server_id, "sk": META_SK}
        if consistent:
            response = self._table.get_item(Key=key, ConsistentRead=True)
        else:
            response = self._table.get_item(Key=key)
        return response.get("Item") or None

    def _get_item(self, server_id: str) -> dict[str, Any]:
        item = self._read(server_id)
        if not item:
            raise _not_found(server_id)
        return item

    def get(self, server_id: str) -> McpServer:
        return self._model(self._get_item(server_id))

    def create(self, *, name: str, url: str, headers: dict[str, str]) -> McpServer:
        now = _now()
        server = McpServer(
            id=uuid4().hex, name=name, url=url, headers=headers, created_at=now, updated_at=now
        )
        self._write(server, previous_item=None, previous_headers={})
        return server

    def save(self, server: McpServer) -> McpServer:
        previous_item = self._get_item(server.id)
        previous_headers = self._model(previous_item).headers
        saved = server.model_copy(update={"updated_at": _now()})
        self._write(saved, previous_item=previous_item, previous_headers=previous_headers)
        return saved

    def delete(self, server_id: str) -> None:
        """Secret first, then the item, so a failure part-way can be retried.

        The other order strands the parameter if SSM fails after the item is
        gone: the retry finds no server, and nothing names the parameter any
        more. This way a failed delete leaves the server in place to delete
        again, and a parameter already gone (an earlier attempt) is fine.
        """
        self._get_item(server_id)
        self._secrets.delete(server_id)
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
            SsmHeaderSecrets(settings.mcp_ssm_prefix, settings.aws_region),
        )
    if deployment.in_lambda():
        raise McpStoreUnavailableError(
            "Saved MCP servers are not configured on this deployment (NIMBUS_MCP_TABLE)"
        )
    return SqliteMcpStore()


def get_mcp_store(settings: Settings = Depends(get_settings)) -> McpServerStore:
    """FastAPI dependency: the active MCP server store."""
    return build_mcp_store(settings)
