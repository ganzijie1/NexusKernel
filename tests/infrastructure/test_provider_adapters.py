from __future__ import annotations

import importlib
import importlib.util
import sys
from enum import Enum
from types import ModuleType
from typing import Any

import pytest

from nexus_kernel._validation import require_positive_integer
from nexus_kernel.infrastructure import (
    MongoDocumentStore,
    QdrantVectorStore,
    RedisStreamQueue,
    StoredDocument,
)


class _MongoCollection:
    def __init__(self) -> None:
        self.item: dict[str, Any] | None = None
        self.sessions: list[Any] = []

    async def find_one(
        self, query: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any] | None:
        self.sessions.append(kwargs.get("session"))
        return self.item

    async def replace_one(
        self,
        query: dict[str, Any],
        item: dict[str, Any],
        *,
        upsert: bool,
        **kwargs: Any,
    ) -> None:
        self.sessions.append(kwargs.get("session"))
        self.item = item

    async def delete_one(self, query: dict[str, Any], **kwargs: Any) -> Any:
        self.sessions.append(kwargs.get("session"))
        deleted = int(self.item is not None)
        self.item = None
        return type("DeleteResult", (), {"deleted_count": deleted})()


class _MongoDatabase:
    def __init__(self) -> None:
        self.collection = _MongoCollection()
        self.commands: list[str] = []

    def __getitem__(self, name: str) -> _MongoCollection:
        return self.collection

    async def command(self, command: str) -> dict[str, int]:
        self.commands.append(command)
        return {"ok": 1}


class _MongoSession:
    def __init__(self) -> None:
        self.events: list[str] = []

    def start_transaction(self) -> None:
        self.events.append("start")

    async def commit_transaction(self) -> None:
        self.events.append("commit")

    async def abort_transaction(self) -> None:
        self.events.append("abort")

    async def end_session(self) -> None:
        self.events.append("end")


class _MongoClient:
    def __init__(self, session: _MongoSession) -> None:
        self.session = session
        self.closed = False

    async def start_session(self) -> _MongoSession:
        return self.session

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_mongo_lifecycle_health_and_transaction_scope() -> None:
    database = _MongoDatabase()
    session = _MongoSession()
    client = _MongoClient(session)
    store = MongoDocumentStore(database, client=client)

    await store.start()
    assert await store.health_check() is True
    async with store.transaction():
        await store.put("items", StoredDocument("one", {"value": 1}))
        assert (await store.get("items", "one")) is not None
    await store.close()

    assert database.commands == ["ping", "ping"]
    assert database.collection.sessions == [session, session]
    assert session.events == ["start", "commit", "end"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_mongo_transaction_rolls_back_on_error() -> None:
    database = _MongoDatabase()
    session = _MongoSession()
    store = MongoDocumentStore(database, client=_MongoClient(session))

    with pytest.raises(RuntimeError, match="boom"):
        async with store.transaction():
            raise RuntimeError("boom")

    assert session.events == ["start", "abort", "end"]


@pytest.mark.asyncio
async def test_mongo_transaction_start_failure_still_ends_session() -> None:
    class FailingSession(_MongoSession):
        def start_transaction(self) -> None:
            self.events.append("start")
            raise RuntimeError("transaction start failed")

    database = _MongoDatabase()
    session = FailingSession()
    store = MongoDocumentStore(database, client=_MongoClient(session))

    with pytest.raises(RuntimeError, match="transaction start failed"):
        async with store.transaction():
            pass

    assert session.events == ["start", "end"]


class _RedisClient:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.groups: list[tuple[str, str]] = []
        self.closed = False
        self.io_calls: list[str] = []

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, **kwargs: Any) -> bool:
        self.io_calls.append("set")
        self.values[key] = value
        self.last_set = (key, value, kwargs)
        return True

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

    async def ttl(self, key: str) -> int:
        return 30 if key in self.values else -2

    async def xgroup_create(
        self, topic: str, group: str, *, id: str, mkstream: bool
    ) -> bool:
        self.groups.append((topic, group))
        return True

    async def xadd(self, topic: str, fields: dict[str, str]) -> str:
        self.published = (topic, fields)
        return "1-0"

    async def xreadgroup(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.io_calls.append("xreadgroup")
        return [("events", [("1-0", {"payload": '{"ok": true}'})])]

    async def xack(self, topic: str, group: str, message_id: str) -> int:
        self.acked = (topic, group, message_id)
        return 1

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_redis_lifecycle_kv_ttl_and_stream_group() -> None:
    client = _RedisClient()
    queue = RedisStreamQueue(client)

    await queue.start()
    assert await queue.health_check() is True
    assert await queue.set("key", "value", ttl_seconds=30) is True
    assert await queue.get("key") == "value"
    assert await queue.ttl("key") == 30
    await queue.ensure_consumer_group("events", "workers")
    assert await queue.publish("events", {"ok": True}) == "1-0"
    message = await anext(queue.consume("events", group="workers", consumer="one"))
    await queue.acknowledge("events", "workers", message.id)
    assert await queue.delete("key") is True
    await queue.close()

    assert client.groups == [("events", "workers")]
    assert message.payload == {"ok": True}
    assert client.closed is True


@pytest.mark.parametrize(
    "value",
    [True, False, 0, -1, 1.0, float("nan"), float("inf"), float("-inf"), "1", None],
)
def test_positive_integer_validator_rejects_invalid_values_without_echoing(
    value: object,
) -> None:
    with pytest.raises(ValueError) as captured:
        require_positive_integer(value, "redis value")

    assert str(captured.value) == "redis value must be a positive integer"


@pytest.mark.parametrize(
    "block_milliseconds",
    [True, False, 0, -1, 1.0, float("nan"), float("inf"), float("-inf"), "1", None],
)
def test_redis_rejects_invalid_block_interval_before_client_io(
    block_milliseconds: Any,
) -> None:
    client = _RedisClient()

    with pytest.raises(
        ValueError, match="^block_milliseconds must be a positive integer$"
    ):
        RedisStreamQueue(client, block_milliseconds=block_milliseconds)

    assert client.io_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ttl_seconds",
    [True, False, 0, -1, 1.0, float("nan"), float("inf"), float("-inf"), "1"],
)
async def test_redis_rejects_invalid_ttl_before_client_io(ttl_seconds: Any) -> None:
    client = _RedisClient()
    queue = RedisStreamQueue(client)

    with pytest.raises(ValueError, match="^ttl_seconds must be a positive integer$"):
        await queue.set("key", "value", ttl_seconds=ttl_seconds)

    assert client.io_calls == []
    assert client.values == {}


@pytest.mark.asyncio
async def test_redis_consume_propagates_cancellation() -> None:
    class BlockingRedisClient(_RedisClient):
        def __init__(self) -> None:
            super().__init__()
            self.started = pytest.importorskip("asyncio").Event()
            self.cancelled = False

        async def xreadgroup(self, *args: Any, **kwargs: Any) -> list[Any]:
            self.started.set()
            try:
                await pytest.importorskip("asyncio").Event().wait()
            except pytest.importorskip("asyncio").CancelledError:
                self.cancelled = True
                raise

    client = BlockingRedisClient()
    queue = RedisStreamQueue(client)
    messages = queue.consume("events", group="workers", consumer="one")
    task = pytest.importorskip("asyncio").create_task(anext(messages))
    await client.started.wait()

    task.cancel()
    with pytest.raises(pytest.importorskip("asyncio").CancelledError):
        await task

    assert client.cancelled is True
    await messages.aclose()


class _QdrantClient:
    def __init__(self) -> None:
        self.exists = False
        self.created: list[dict[str, Any]] = []
        self.closed = False

    async def get_collections(self) -> object:
        return object()

    async def collection_exists(self, collection_name: str) -> bool:
        return self.exists

    async def create_collection(self, **kwargs: Any) -> None:
        self.created.append(kwargs)
        self.exists = True

    async def close(self) -> None:
        self.closed = True


class _Distance(str, Enum):
    COSINE = "Cosine"


class _VectorParams:
    def __init__(self, *, size: int, distance: _Distance) -> None:
        self.size = size
        self.distance = distance


class _PointStruct:
    def __init__(self, **values: Any) -> None:
        self.values = values


class _PointIdsList:
    def __init__(self, *, points: list[int | str]) -> None:
        self.points = points


def _install_qdrant_models(monkeypatch: pytest.MonkeyPatch) -> None:
    package = ModuleType("qdrant_client")
    models = ModuleType("qdrant_client.models")
    models.Distance = _Distance
    models.VectorParams = _VectorParams
    models.PointStruct = _PointStruct
    models.PointIdsList = _PointIdsList
    package.models = models
    monkeypatch.setitem(sys.modules, "qdrant_client", package)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", models)


@pytest.mark.asyncio
async def test_qdrant_lifecycle_health_and_collection_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_qdrant_models(monkeypatch)
    client = _QdrantClient()
    store = QdrantVectorStore(client)

    await store.start()
    assert await store.health_check() is True
    assert (
        await store.ensure_collection("memory", vector_size=3, distance="Cosine")
        is True
    )
    assert (
        await store.ensure_collection("memory", vector_size=3, distance="Cosine")
        is False
    )
    await store.close()

    assert len(client.created) == 1
    assert client.created[0]["collection_name"] == "memory"
    vectors = client.created[0]["vectors_config"]
    assert isinstance(vectors, _VectorParams)
    assert vectors.size == 3
    assert vectors.distance is _Distance.COSINE
    assert client.closed is True


@pytest.mark.asyncio
async def test_qdrant_rejects_item_ids_outside_uint64_and_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_qdrant_models(monkeypatch)
    store = QdrantVectorStore(_QdrantClient())

    with pytest.raises(ValueError, match="uint64 or UUID"):
        await store.upsert("memory", "not-an-id", [1.0], {})


def test_postgres_adapter_module_exists() -> None:
    assert importlib.util.find_spec("nexus_kernel.infrastructure.postgres") is not None


class _PostgresTransaction:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> _PostgresTransaction:
        self._events.append("transaction:start")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        self._events.append(
            "transaction:rollback" if exc_type else "transaction:commit"
        )


class _PostgresConnection:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def transaction(self) -> _PostgresTransaction:
        return _PostgresTransaction(self._events)

    async def execute(self, query: str, *args: Any) -> str:
        self._events.append(f"execute:{query}")
        return "UPDATE 1"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        return {"value": 1}

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return [{"value": 1}, {"value": 2}]


class _PostgresAcquire:
    def __init__(self, connection: _PostgresConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _PostgresConnection:
        return self._connection

    async def __aexit__(self, *args: Any) -> None:
        return None


class _PostgresPool:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.connection = _PostgresConnection(self.events)
        self.closed = False

    async def fetchval(self, query: str) -> int:
        return 1

    async def execute(self, query: str, *args: Any) -> str:
        self.events.append(f"pool-execute:{query}")
        return "INSERT 0 1"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        return {"value": 1}

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return [{"value": 1}, {"value": 2}]

    def acquire(self) -> _PostgresAcquire:
        return _PostgresAcquire(self.connection)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_postgres_pool_queries_transaction_and_close() -> None:
    module = importlib.import_module("nexus_kernel.infrastructure.postgres")
    database = module.PostgresDatabase(pool=_PostgresPool())

    await database.start()
    assert await database.health_check() is True
    assert await database.execute("INSERT") == "INSERT 0 1"
    assert await database.fetch_one("SELECT one") == {"value": 1}
    assert await database.fetch_all("SELECT all") == [
        {"value": 1},
        {"value": 2},
    ]
    async with database.transaction():
        assert await database.execute("UPDATE") == "UPDATE 1"
    pool = database._pool
    await database.close()

    assert pool.events == [
        "pool-execute:INSERT",
        "transaction:start",
        "execute:UPDATE",
        "transaction:commit",
    ]
    assert pool.closed is True


@pytest.mark.asyncio
async def test_postgres_transaction_rolls_back_on_error() -> None:
    module = importlib.import_module("nexus_kernel.infrastructure.postgres")
    pool = _PostgresPool()
    database = module.PostgresDatabase(pool=pool)

    with pytest.raises(RuntimeError, match="rollback"):
        async with database.transaction():
            await database.execute("UPDATE")
            raise RuntimeError("rollback")

    assert pool.events == [
        "transaction:start",
        "execute:UPDATE",
        "transaction:rollback",
    ]


@pytest.mark.asyncio
async def test_postgres_health_failure_returns_false() -> None:
    class UnhealthyPool(_PostgresPool):
        async def fetchval(self, query: str) -> int:
            raise RuntimeError("database unavailable")

    module = importlib.import_module("nexus_kernel.infrastructure.postgres")
    database = module.PostgresDatabase(pool=UnhealthyPool())

    assert await database.health_check() is False


def test_postgres_from_dsn_does_not_import_asyncpg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("nexus_kernel.infrastructure.postgres")
    original_import = __import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "asyncpg":
            raise AssertionError("asyncpg imported before start")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)

    database = module.PostgresDatabase.from_dsn("postgresql://database.invalid/app")

    assert database._pool is None
