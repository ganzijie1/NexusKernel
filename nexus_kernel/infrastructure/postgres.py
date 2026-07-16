"""Postgres pool adapter loaded only when its provider is started."""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Mapping, Sequence


class PostgresDatabase:
    def __init__(
        self,
        *,
        pool: Any | None = None,
        dsn: str | None = None,
        pool_options: Mapping[str, Any] | None = None,
    ) -> None:
        self._pool = pool
        self._dsn = dsn
        self._pool_options = dict(pool_options or {})
        self._connection: ContextVar[Any | None] = ContextVar(
            "nexus_kernel_postgres_connection", default=None
        )

    @classmethod
    def from_dsn(cls, dsn: str, **pool_options: Any) -> PostgresDatabase:
        return cls(dsn=dsn, pool_options=pool_options)

    async def start(self) -> None:
        if self._pool is None:
            if self._dsn is None:
                raise RuntimeError("Postgres DSN is not configured")
            asyncpg_module = importlib.import_module("asyncpg")
            self._pool = await asyncpg_module.create_pool(
                dsn=self._dsn,
                **self._pool_options,
            )

    async def health_check(self) -> bool:
        try:
            result = await self._require_pool().fetchval("SELECT 1")
        except Exception:
            return False
        return bool(result == 1)

    def _executor(self) -> Any:
        return self._connection.get() or self._require_pool()

    def _require_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("Postgres provider is not started")
        return self._pool

    async def execute(self, query: str, *args: Any) -> str:
        return str(await self._executor().execute(query, *args))

    async def fetch_one(self, query: str, *args: Any) -> Mapping[str, Any] | None:
        row = await self._executor().fetchrow(query, *args)
        return None if row is None else dict(row)

    async def fetch_all(self, query: str, *args: Any) -> Sequence[Mapping[str, Any]]:
        rows = await self._executor().fetch(query, *args)
        return [dict(row) for row in rows]

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction() as transaction:
                token = self._connection.set(connection)
                try:
                    yield transaction
                finally:
                    self._connection.reset(token)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
