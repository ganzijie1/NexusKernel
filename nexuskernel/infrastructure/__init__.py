"""Optional storage, queue, vector, transaction, outbox, and tracing APIs."""

from nexuskernel.infrastructure.contracts import (
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
from nexuskernel.infrastructure.mongo import MongoDocumentStore
from nexuskernel.infrastructure.otel import OpenTelemetryTracer
from nexuskernel.infrastructure.qdrant import QdrantVectorStore
from nexuskernel.infrastructure.redis_stream import RedisStreamQueue

__all__ = [
    "DocumentStore",
    "MessageQueue",
    "MongoDocumentStore",
    "OpenTelemetryTracer",
    "OutboxMessage",
    "OutboxStore",
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
