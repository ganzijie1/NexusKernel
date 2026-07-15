"""Redis Stream queue implementation loaded only with the redis extra."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from nexuskernel.infrastructure.contracts import QueueMessage


class RedisStreamQueue:
    def __init__(self, client: Any, *, block_milliseconds: int = 1000) -> None:
        self._client = client
        self._block_milliseconds = block_milliseconds

    @classmethod
    def from_url(cls, url: str) -> RedisStreamQueue:
        from redis.asyncio import Redis  # type: ignore[import-not-found]

        return cls(Redis.from_url(url, decode_responses=True))

    async def publish(self, topic: str, payload: Mapping[str, Any]) -> str:
        message_id = await self._client.xadd(
            topic, {"payload": json.dumps(dict(payload), ensure_ascii=True)}
        )
        return str(message_id)

    async def consume(
        self, topic: str, *, group: str, consumer: str
    ) -> AsyncIterator[QueueMessage]:
        while True:
            batches = await self._client.xreadgroup(
                group,
                consumer,
                {topic: ">"},
                count=1,
                block=self._block_milliseconds,
            )
            for _, messages in batches:
                for message_id, fields in messages:
                    payload = json.loads(fields["payload"])
                    if isinstance(payload, dict):
                        yield QueueMessage(id=str(message_id), payload=payload)

    async def acknowledge(self, topic: str, group: str, message_id: str) -> None:
        await self._client.xack(topic, group, message_id)

    async def close(self) -> None:
        await self._client.aclose()
