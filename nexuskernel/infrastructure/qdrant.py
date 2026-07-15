"""Qdrant vector-store implementation loaded only with the qdrant extra."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from nexuskernel.infrastructure.contracts import VectorMatch


class QdrantVectorStore:
    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str, *, api_key: str | None = None) -> QdrantVectorStore:
        from qdrant_client import AsyncQdrantClient  # type: ignore[import-not-found]

        return cls(AsyncQdrantClient(url=url, api_key=api_key))

    async def upsert(
        self,
        collection: str,
        item_id: str,
        vector: Sequence[float],
        payload: Mapping[str, Any],
    ) -> None:
        from qdrant_client.models import PointStruct  # type: ignore[import-not-found]

        await self._client.upsert(
            collection_name=collection,
            points=[
                PointStruct(
                    id=item_id,
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

    async def delete(self, collection: str, item_id: str) -> None:
        from qdrant_client.models import PointIdsList

        await self._client.delete(
            collection_name=collection,
            points_selector=PointIdsList(points=[item_id]),
        )

    async def close(self) -> None:
        await self._client.close()
