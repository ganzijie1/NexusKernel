"""Optional storage, queue, vector, transaction, outbox, and tracing APIs."""

from nexus_kernel.infrastructure.contracts import (
    DocumentStore,
    MessageQueue,
    OutboxMessage,
    OutboxStore,
    QueueMessage,
    Span,
    StoredDocument,
    Tracer,
    Transaction,
    TransactionManager,
    VectorMatch,
    VectorStore,
)
from nexus_kernel.infrastructure.mongo import MongoDocumentStore
from nexus_kernel.infrastructure.otel import OpenTelemetryTracer
from nexus_kernel.infrastructure.postgres import PostgresDatabase
from nexus_kernel.infrastructure.qdrant import QdrantVectorStore
from nexus_kernel.infrastructure.redis_stream import RedisStreamQueue

__all__ = [
    "DocumentStore",
    "MessageQueue",
    "MongoDocumentStore",
    "OpenTelemetryTracer",
    "OutboxMessage",
    "OutboxStore",
    "PostgresDatabase",
    "QdrantVectorStore",
    "QueueMessage",
    "RedisStreamQueue",
    "Span",
    "StoredDocument",
    "Tracer",
    "Transaction",
    "TransactionManager",
    "VectorMatch",
    "VectorStore",
]
