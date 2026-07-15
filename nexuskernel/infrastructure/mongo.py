"""Mongo document-store implementation loaded only with the mongo extra."""

from __future__ import annotations

import copy
from typing import Any

from nexuskernel.infrastructure.contracts import StoredDocument


class MongoDocumentStore:
    def __init__(self, database: Any) -> None:
        self._database = database
        self._client: Any | None = None

    @classmethod
    def from_uri(cls, uri: str, database: str) -> MongoDocumentStore:
        from pymongo import AsyncMongoClient  # type: ignore[import-not-found]

        client = AsyncMongoClient(uri)
        instance = cls(client[database])
        instance._client = client
        return instance

    async def get(self, collection: str, document_id: str) -> StoredDocument | None:
        item = await self._database[collection].find_one({"_id": document_id})
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
        )

    async def delete(self, collection: str, document_id: str) -> bool:
        result = await self._database[collection].delete_one({"_id": document_id})
        return bool(result.deleted_count)

    async def close(self) -> None:
        client = getattr(self, "_client", None)
        if client is not None:
            await client.close()
