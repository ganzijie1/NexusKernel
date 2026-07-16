"""Explicit application assembly for supported infrastructure adapters."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import SecretStr

from app.providers.config import InfrastructureProviderSettings
from nexus_kernel.infrastructure import (
    MongoDocumentStore,
    PostgresDatabase,
    QdrantVectorStore,
    RedisStreamQueue,
)
from nexus_kernel.providers import Provider, ProviderRegistry, ProviderSpec


def _required_secret(value: SecretStr | None, provider: str) -> str:
    if value is None:
        raise RuntimeError(f"{provider} provider is not configured")
    return value.get_secret_value()


def register_infrastructure_providers(
    registry: ProviderRegistry, settings: InfrastructureProviderSettings
) -> None:
    providers = settings.providers
    mongo = providers.mongo
    redis = providers.redis
    postgres = providers.postgres
    qdrant = providers.qdrant

    factories: tuple[tuple[str, Callable[[], Provider], object], ...] = (
        (
            "mongo",
            lambda: MongoDocumentStore.from_uri(
                _required_secret(mongo.dsn, "mongo"),
                mongo.database or "",
            ),
            mongo,
        ),
        (
            "redis",
            lambda: RedisStreamQueue.from_url(_required_secret(redis.dsn, "redis")),
            redis,
        ),
        (
            "postgres",
            lambda: PostgresDatabase.from_dsn(
                _required_secret(postgres.dsn, "postgres"),
                min_size=postgres.min_pool_size,
                max_size=postgres.max_pool_size,
            ),
            postgres,
        ),
        (
            "qdrant",
            lambda: QdrantVectorStore.from_url(
                _required_secret(qdrant.url, "qdrant"),
                api_key=(
                    qdrant.api_key.get_secret_value()
                    if qdrant.api_key is not None
                    else None
                ),
            ),
            qdrant,
        ),
    )

    for name, factory, config in factories:
        registry.register(
            ProviderSpec(
                name,
                factory,
                required=config.required,  # type: ignore[attr-defined]
                enabled=config.enabled,  # type: ignore[attr-defined]
                timeout_seconds=config.timeout_seconds,  # type: ignore[attr-defined]
            )
        )
