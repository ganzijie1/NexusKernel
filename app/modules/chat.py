"""Chat module lifecycle and private AI configuration schema."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from nexuskernel.ai import GoogleAdkRuntime, LiteLLMClient, OpenAICompatibleClient
from nexuskernel.errors import ConfigError
from nexuskernel.kernel import Module
from nexuskernel.providers import Provider


class ChatModuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: Literal["openai_compatible", "litellm", "google_adk"] = (
        "openai_compatible"
    )
    model: str = Field(default="gpt-4.1-mini", min_length=1)
    service_bearer_token: SecretStr | None = None
    ai_api_key: SecretStr | None = None
    base_url: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0)
    session_ttl_seconds: float = Field(default=1800.0, gt=0)

    @field_validator("service_bearer_token", "ai_api_key", mode="before")
    @classmethod
    def empty_secrets_are_unconfigured(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("base_url", mode="before")
    @classmethod
    def validate_base_url(cls, value: object) -> object:
        if value in {None, ""}:
            return None
        if not isinstance(value, str):
            raise ValueError("base_url must be a string")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an HTTP URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain credentials")
        return value.rstrip("/")


class ChatModule(Module):
    name = "chat"
    required_provider_names = ("ai_runtime",)


def create_ai_runtime(config: ChatModuleConfig) -> Provider:
    if config.ai_api_key is None:
        raise ConfigError("AI provider credential is not configured")
    api_key = config.ai_api_key.get_secret_value()
    if config.provider_id == "openai_compatible":
        return OpenAICompatibleClient(
            api_key=api_key,
            model=config.model,
            base_url=config.base_url,
            timeout_seconds=config.timeout_seconds,
        )
    if config.provider_id == "litellm":
        return LiteLLMClient(
            api_key=api_key,
            model=config.model,
            base_url=config.base_url,
            timeout_seconds=config.timeout_seconds,
        )
    return GoogleAdkRuntime(
        api_key=api_key,
        model=config.model,
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
        session_ttl_seconds=config.session_ttl_seconds,
    )
