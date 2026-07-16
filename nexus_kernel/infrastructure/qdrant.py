"""Qdrant vector-store implementation loaded only with the qdrant extra."""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from nexus_kernel.infrastructure.contracts import VectorMatch


class QdrantVectorStore:
    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str, *, api_key: str | None = None) -> QdrantVectorStore:
        client_type = getattr(
            importlib.import_module("qdrant_client"), "AsyncQdrantClient"
        )
        return cls(client_type(url=url, api_key=api_key))

    async def start(self) -> None:
        await self._client.get_collections()

    async def health_check(self) -> bool:
        try:
            await self._client.get_collections()
        except Exception:
            return False
        return True

    async def ensure_collection(
        self, collection: str, *, vector_size: int, distance: str = "Cosine"
    ) -> bool:
        if await self._client.collection_exists(collection_name=collection):
            return False
        models = importlib.import_module("qdrant_client.models")
        distance_type = getattr(models, "Distance")
        vector_params_type = getattr(models, "VectorParams")

        await self._client.create_collection(
            collection_name=collection,
            vectors_config=vector_params_type(
                size=vector_size,
                distance=distance_type(distance),
            ),
        )
        return True

    async def upsert(
        self,
        collection: str,
        item_id: int | str | UUID,
        vector: Sequence[float],
        payload: Mapping[str, Any],
    ) -> None:
        normalized_id = _normalize_item_id(item_id)
        point_type = getattr(
            importlib.import_module("qdrant_client.models"), "PointStruct"
        )

        await self._client.upsert(
            collection_name=collection,
            points=[
                point_type(
                    id=normalized_id,
                    vector=list(vector),
                    payload=dict(payload),
                )
            ],
        )

    async def search(
        self, collection: str, vector: Sequence[float], *, limit: int = 10
    ) -> Sequence[VectorMatch]:
        response = await self._client.query_points(
            collection_name=collection,
            query=list(vector),
            limit=limit,
            with_payload=True,
        )
        return [
            VectorMatch(
                id=str(point.id),
                score=float(point.score),
                payload=point.payload or {},
            )
            for point in response.points
        ]

    async def delete(self, collection: str, item_id: int | str | UUID) -> None:
        normalized_id = _normalize_item_id(item_id)
        selector_type = getattr(
            importlib.import_module("qdrant_client.models"), "PointIdsList"
        )

        await self._client.delete(
            collection_name=collection,
            points_selector=selector_type(points=[normalized_id]),
        )

    async def close(self) -> None:
        await self._client.close()


def _normalize_item_id(item_id: int | str | UUID) -> int | str:
    if isinstance(item_id, bool):
        raise ValueError("Qdrant item_id must be a uint64 or UUID")
    if isinstance(item_id, int):
        if 0 <= item_id <= (1 << 64) - 1:
            return item_id
        raise ValueError("Qdrant item_id must be a uint64 or UUID")
    try:
        return str(item_id if isinstance(item_id, UUID) else UUID(item_id))
    except (TypeError, ValueError, AttributeError):
        raise ValueError("Qdrant item_id must be a uint64 or UUID") from None
