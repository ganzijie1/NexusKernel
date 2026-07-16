"""Mongo document-store implementation loaded only with the mongo extra."""

from __future__ import annotations

import copy
import importlib
import inspect
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator

from nexus_kernel.infrastructure.contracts import StoredDocument


class MongoDocumentStore:
    def __init__(self, database: Any, *, client: Any | None = None) -> None:
        self._database = database
        self._client = client
        self._session: ContextVar[Any | None] = ContextVar(
            "nexus_kernel_mongo_session", default=None
        )

    @classmethod
    def from_uri(cls, uri: str, database: str) -> MongoDocumentStore:
        client_type = getattr(importlib.import_module("pymongo"), "AsyncMongoClient")
        client = client_type(uri)
        return cls(client[database], client=client)

    async def start(self) -> None:
        await self._database.command("ping")

    async def health_check(self) -> bool:
        try:
            result = await self._database.command("ping")
        except Exception:
            return False
        return bool(result.get("ok", 1)) if isinstance(result, dict) else True

    def _session_kwargs(self) -> dict[str, Any]:
        session = self._session.get()
        return {} if session is None else {"session": session}

    async def get(self, collection: str, document_id: str) -> StoredDocument | None:
        item = await self._database[collection].find_one(
            {"_id": document_id}, **self._session_kwargs()
        )
        if item is None:
            return None
        body = copy.deepcopy(item.get("body", {}))
        return StoredDocument(
            id=str(item["_id"]), body=body, version=int(item.get("version", 1))
        )

    async def put(self, collection: str, document: StoredDocument) -> None:
        await self._database[collection].replace_one(
            {"_id": document.id},
            {
                "_id": document.id,
                "body": copy.deepcopy(dict(document.body)),
                "version": document.version,
            },
            upsert=True,
            **self._session_kwargs(),
        )

    async def delete(self, collection: str, document_id: str) -> bool:
        result = await self._database[collection].delete_one(
            {"_id": document_id}, **self._session_kwargs()
        )
        return bool(result.deleted_count)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[_MongoTransaction]:
        if self._client is None:
            raise RuntimeError("Mongo transactions require an initialized client")
        session = await _await_if_needed(self._client.start_session())
        transaction = _MongoTransaction(session)
        try:
            await _await_if_needed(session.start_transaction())
            token = self._session.set(session)
            try:
                yield transaction
            except BaseException:
                await transaction.rollback()
                raise
            else:
                await transaction.commit()
            finally:
                self._session.reset(token)
        finally:
            await _await_if_needed(session.end_session())

    async def close(self) -> None:
        client = getattr(self, "_client", None)
        if client is not None:
            await _await_if_needed(client.close())


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class _MongoTransaction:
    def __init__(self, session: Any) -> None:
        self._session = session
        self._completed = False

    async def commit(self) -> None:
        if not self._completed:
            await _await_if_needed(self._session.commit_transaction())
            self._completed = True

    async def rollback(self) -> None:
        if not self._completed:
            await _await_if_needed(self._session.abort_transaction())
            self._completed = True
