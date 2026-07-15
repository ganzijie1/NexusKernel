"""Optional-infrastructure contracts kept independent from concrete SDKs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncContextManager, Protocol


@dataclass(frozen=True, slots=True)
class StoredDocument:
    id: str
    body: Mapping[str, Any]
    version: int = 1


class DocumentStore(Protocol):
    async def get(self, collection: str, document_id: str) -> StoredDocument | None: ...

    async def put(self, collection: str, document: StoredDocument) -> None: ...

    async def delete(self, collection: str, document_id: str) -> bool: ...


class Transaction(Protocol):
    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class TransactionManager(Protocol):
    def transaction(self) -> AsyncContextManager[Transaction]: ...


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    id: str
    topic: str
    payload: Mapping[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class OutboxStore(Protocol):
    async def add(self, message: OutboxMessage) -> None: ...

    async def pending(self, limit: int = 100) -> Sequence[OutboxMessage]: ...

    async def mark_published(self, message_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class QueueMessage:
    id: str
    payload: Mapping[str, Any]


class MessageQueue(Protocol):
    async def publish(self, topic: str, payload: Mapping[str, Any]) -> str: ...

    def consume(
        self, topic: str, *, group: str, consumer: str
    ) -> AsyncIterator[QueueMessage]: ...

    async def acknowledge(self, topic: str, group: str, message_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class VectorMatch:
    id: str
    score: float
    payload: Mapping[str, Any]


class VectorStore(Protocol):
    async def upsert(
        self,
        collection: str,
        item_id: str,
        vector: Sequence[float],
        payload: Mapping[str, Any],
    ) -> None: ...

    async def search(
        self, collection: str, vector: Sequence[float], *, limit: int = 10
    ) -> Sequence[VectorMatch]: ...

    async def delete(self, collection: str, item_id: str) -> None: ...


class Span(Protocol):
    def set_attribute(self, key: str, value: str | int | float | bool) -> None: ...


class Tracer(Protocol):
    def start_span(self, name: str) -> AsyncContextManager[Span]: ...
