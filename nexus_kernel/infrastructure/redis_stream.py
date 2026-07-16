"""Redis Stream queue implementation loaded only with the redis extra."""

from __future__ import annotations

import importlib
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from nexus_kernel.infrastructure.contracts import QueueMessage


class RedisStreamQueue:
    def __init__(self, client: Any, *, block_milliseconds: int = 1000) -> None:
        self._client = client
        self._block_milliseconds = block_milliseconds

    @classmethod
    def from_url(cls, url: str) -> RedisStreamQueue:
        redis_type = getattr(importlib.import_module("redis.asyncio"), "Redis")
        return cls(redis_type.from_url(url, decode_responses=True))

    async def start(self) -> None:
        await self._client.ping()

    async def health_check(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def get(self, key: str) -> str | None:
        value = await self._client.get(key)
        return None if value is None else str(value)

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> bool:
        options = {} if ttl_seconds is None else {"ex": ttl_seconds}
        return bool(await self._client.set(key, value, **options))

    async def delete(self, key: str) -> bool:
        return bool(await self._client.delete(key))

    async def ttl(self, key: str) -> int:
        return int(await self._client.ttl(key))

    async def ensure_consumer_group(self, topic: str, group: str) -> bool:
        try:
            await self._client.xgroup_create(topic, group, id="0", mkstream=True)
        except Exception as exc:
            if (
                "BUSYGROUP" in type(exc).__name__.upper()
                or "BUSYGROUP" in str(exc).upper()
            ):
                return False
            raise
        return True

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
