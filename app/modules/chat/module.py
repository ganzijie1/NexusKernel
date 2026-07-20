"""Chat module lifecycle, private configuration, and AI runtime assembly."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from nexus_kernel.ai import (
    AdkSessionStore,
    GoogleAdkRuntime,
    LiteLLMClient,
    OpenAICompatibleClient,
)
from nexus_kernel.errors import ConfigError
from nexus_kernel.kernel import Module, ModuleConfig
from nexus_kernel.providers import Provider


class ChatModuleConfig(ModuleConfig):
    provider_id: Literal["openai_compatible", "litellm", "google_adk"] = (
        "openai_compatible"
    )
    model: str = Field(default="gpt-4.1-mini", min_length=1)
    service_bearer_token: SecretStr | None = None
    ai_api_key: SecretStr | None = None
    base_url: str | None = None
    timeout_seconds: float = Field(default=25.0, gt=0, allow_inf_nan=False)
    tool_timeout_seconds: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    max_tool_rounds: int = Field(default=4, ge=1, le=16)
    max_tool_calls: int = Field(default=16, ge=1, le=128)
    max_tool_argument_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    max_tool_result_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    adk_app_name: str = Field(default="nexus_kernel", pattern=r"^[a-z][a-z0-9_]*$")
    adk_session_ttl_seconds: float = Field(default=1800.0, gt=0, allow_inf_nan=False)
    adk_session_max_entries: int = Field(default=256, ge=1, le=10_000)
    adk_session_max_events: int = Field(default=200, ge=2, le=2000)
    adk_session_cleanup_timeout_seconds: float = Field(
        default=2.0, gt=0, le=30, allow_inf_nan=False
    )

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

    @model_validator(mode="after")
    def validate_deadlines(self) -> ChatModuleConfig:
        if self.tool_timeout_seconds > self.timeout_seconds:
            raise ValueError("tool timeout must not exceed the AI timeout")
        if self.adk_session_cleanup_timeout_seconds > self.timeout_seconds:
            raise ValueError("session cleanup timeout must not exceed the AI timeout")
        return self


class ChatModule(Module):
    name = "chat"
    required_provider_names = ("ai_runtime",)
    config_schema = ChatModuleConfig

    @property
    def config(self) -> ChatModuleConfig:
        configured = super().config
        if not isinstance(configured, ChatModuleConfig):
            raise ConfigError("Chat module configuration is invalid")
        return configured


def create_ai_runtime(
    config: ChatModuleConfig, *, session_store: AdkSessionStore | None = None
) -> Provider:
    if config.ai_api_key is None:
        raise ConfigError("AI provider credential is not configured")
    api_key = config.ai_api_key.get_secret_value()
    if config.provider_id == "openai_compatible":
        return OpenAICompatibleClient(
            api_key=api_key,
            model=config.model,
            base_url=config.base_url,
            timeout_seconds=config.timeout_seconds,
            tool_timeout_seconds=config.tool_timeout_seconds,
            max_tool_rounds=config.max_tool_rounds,
            max_tool_calls=config.max_tool_calls,
            max_tool_argument_bytes=config.max_tool_argument_bytes,
            max_tool_result_bytes=config.max_tool_result_bytes,
        )
    if config.provider_id == "litellm":
        return LiteLLMClient(
            api_key=api_key,
            model=config.model,
            base_url=config.base_url,
            timeout_seconds=config.timeout_seconds,
            tool_timeout_seconds=config.tool_timeout_seconds,
            max_tool_rounds=config.max_tool_rounds,
            max_tool_calls=config.max_tool_calls,
            max_tool_argument_bytes=config.max_tool_argument_bytes,
            max_tool_result_bytes=config.max_tool_result_bytes,
        )
    return GoogleAdkRuntime(
        api_key=api_key,
        model=config.model,
        base_url=config.base_url,
        timeout_seconds=config.timeout_seconds,
        tool_timeout_seconds=config.tool_timeout_seconds,
        max_tool_rounds=config.max_tool_rounds,
        max_tool_calls=config.max_tool_calls,
        max_tool_argument_bytes=config.max_tool_argument_bytes,
        max_tool_result_bytes=config.max_tool_result_bytes,
        adk_app_name=config.adk_app_name,
        session_ttl_seconds=config.adk_session_ttl_seconds,
        session_max_entries=config.adk_session_max_entries,
        session_max_events=config.adk_session_max_events,
        session_cleanup_timeout_seconds=(config.adk_session_cleanup_timeout_seconds),
        session_store=session_store,
    )
