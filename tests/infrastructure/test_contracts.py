from __future__ import annotations

from typing import Any

import pytest

from nexuskernel.infrastructure import MongoDocumentStore, StoredDocument


class FakeCollection:
    def __init__(self) -> None:
        self.item: dict[str, Any] | None = None

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        return self.item

    async def replace_one(
        self, query: dict[str, Any], item: dict[str, Any], *, upsert: bool
    ) -> None:
        self.item = item

    async def delete_one(self, query: dict[str, Any]) -> Any:
        deleted = int(self.item is not None)
        self.item = None
        return type("DeleteResult", (), {"deleted_count": deleted})()


class FakeDatabase:
    def __init__(self) -> None:
        self.collection = FakeCollection()

    def __getitem__(self, name: str) -> FakeCollection:
        return self.collection


@pytest.mark.asyncio
async def test_mongo_document_store_uses_provider_neutral_documents() -> None:
    database = FakeDatabase()
    store = MongoDocumentStore(database)
    source = {"nested": {"value": 1}}

    await store.put("items", StoredDocument("one", source, version=2))
    source["nested"]["value"] = 9
    loaded = await store.get("items", "one")

    assert loaded is not None
    assert loaded.id == "one"
    assert loaded.version == 2
    assert loaded.body == {"nested": {"value": 1}}
    assert await store.delete("items", "one") is True
    assert await store.get("items", "one") is None
