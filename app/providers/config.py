"""Application-owned configuration for optional infrastructure providers."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from nexus_kernel.config import load_config_model


def _optional_text(value: object) -> object:
    return None if value == "" else value


def _validate_secret_url(
    value: SecretStr | None, *, schemes: frozenset[str], label: str
) -> SecretStr | None:
    if value is None:
        return None
    parsed = urlsplit(value.get_secret_value())
    if parsed.scheme.casefold() not in schemes or not parsed.hostname:
        raise ValueError(f"{label} must use a supported URL scheme and host")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ProviderConfig(_StrictModel):
    required: bool = False
    timeout_seconds: float = Field(default=5.0, gt=0)
    _schemes: ClassVar[frozenset[str]]

    @property
    def enabled(self) -> bool:
        raise NotImplementedError

    @model_validator(mode="after")
    def required_provider_must_be_configured(self) -> _ProviderConfig:
        if self.required and not self.enabled:
            raise ValueError("required provider must be configured")
        return self


class MongoProviderConfig(_ProviderConfig):
    _schemes = frozenset({"mongodb", "mongodb+srv"})

    dsn: SecretStr | None = None
    database: str | None = None

    @field_validator("dsn", "database", mode="before")
    @classmethod
    def empty_values_are_unconfigured(cls, value: object) -> object:
        return _optional_text(value)

    @field_validator("dsn")
    @classmethod
    def validate_dsn(cls, value: SecretStr | None) -> SecretStr | None:
        return _validate_secret_url(value, schemes=cls._schemes, label="Mongo DSN")

    @model_validator(mode="after")
    def validate_pair(self) -> MongoProviderConfig:
        if (self.dsn is None) != (self.database is None):
            raise ValueError("Mongo DSN and database must be configured together")
        return self

    @property
    def enabled(self) -> bool:
        return self.dsn is not None


class RedisProviderConfig(_ProviderConfig):
    _schemes = frozenset({"redis", "rediss"})

    dsn: SecretStr | None = None

    @field_validator("dsn", mode="before")
    @classmethod
    def empty_dsn_is_unconfigured(cls, value: object) -> object:
        return _optional_text(value)

    @field_validator("dsn")
    @classmethod
    def validate_dsn(cls, value: SecretStr | None) -> SecretStr | None:
        return _validate_secret_url(value, schemes=cls._schemes, label="Redis DSN")

    @property
    def enabled(self) -> bool:
        return self.dsn is not None


class PostgresProviderConfig(_ProviderConfig):
    _schemes = frozenset({"postgres", "postgresql"})

    dsn: SecretStr | None = None
    min_pool_size: int = Field(default=1, ge=1)
    max_pool_size: int = Field(default=10, ge=1)

    @field_validator("dsn", mode="before")
    @classmethod
    def empty_dsn_is_unconfigured(cls, value: object) -> object:
        return _optional_text(value)

    @field_validator("dsn")
    @classmethod
    def validate_dsn(cls, value: SecretStr | None) -> SecretStr | None:
        return _validate_secret_url(value, schemes=cls._schemes, label="Postgres DSN")

    @model_validator(mode="after")
    def validate_pool_range(self) -> PostgresProviderConfig:
        if self.max_pool_size < self.min_pool_size:
            raise ValueError(
                "Postgres max_pool_size must not be less than min_pool_size"
            )
        return self

    @property
    def enabled(self) -> bool:
        return self.dsn is not None


class QdrantProviderConfig(_ProviderConfig):
    _schemes = frozenset({"http", "https"})

    url: SecretStr | None = None
    api_key: SecretStr | None = None

    @field_validator("url", "api_key", mode="before")
    @classmethod
    def empty_values_are_unconfigured(cls, value: object) -> object:
        return _optional_text(value)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: SecretStr | None) -> SecretStr | None:
        return _validate_secret_url(value, schemes=cls._schemes, label="Qdrant URL")

    @model_validator(mode="after")
    def validate_api_key_dependency(self) -> QdrantProviderConfig:
        if self.url is None and self.api_key is not None:
            raise ValueError("Qdrant API key requires a URL")
        return self

    @property
    def enabled(self) -> bool:
        return self.url is not None


class ProviderConfigurations(_StrictModel):
    mongo: MongoProviderConfig = Field(default_factory=MongoProviderConfig)
    redis: RedisProviderConfig = Field(default_factory=RedisProviderConfig)
    postgres: PostgresProviderConfig = Field(default_factory=PostgresProviderConfig)
    qdrant: QdrantProviderConfig = Field(default_factory=QdrantProviderConfig)


class InfrastructureProviderSettings(_StrictModel):
    providers: ProviderConfigurations = Field(default_factory=ProviderConfigurations)


def load_provider_settings(path: str | Path) -> InfrastructureProviderSettings:
    return load_config_model(Path(path), InfrastructureProviderSettings)
