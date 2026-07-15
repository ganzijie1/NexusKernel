"""Application-owned assembly of framework, modules, and optional AI runtime."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from pydantic import ValidationError

from app.modules.chat import ChatModuleConfig, create_ai_runtime
from app.services import AppChatService
from nexuskernel.config import (
    load_application_settings,
    load_module_definitions,
)
from nexuskernel.errors import ConfigError
from nexuskernel.gateway import create_app
from nexuskernel.kernel import Kernel, load_modules
from nexuskernel.providers import ProviderRegistry, ProviderSpec


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def create_application(
    *,
    application_config: str | Path | None = None,
    modules_config: str | Path | None = None,
) -> FastAPI:
    application_path = (
        Path(application_config)
        if application_config
        else (_PROJECT_ROOT / "config" / "application.yaml")
    )
    modules_path = (
        Path(modules_config)
        if modules_config
        else (_PROJECT_ROOT / "config" / "modules.yaml")
    )
    settings = load_application_settings(application_path)
    definitions = load_module_definitions(modules_path)
    chat_definition = definitions.modules.get("chat")
    registry = ProviderRegistry()

    if chat_definition is None or not chat_definition.enabled:
        chat_config = ChatModuleConfig()
    else:
        try:
            chat_config = ChatModuleConfig.model_validate(chat_definition.config)
        except ValidationError:
            raise ConfigError("Chat module configuration is invalid") from None
        registry.register(
            ProviderSpec(
                "ai_runtime",
                lambda: create_ai_runtime(chat_config),
                required=False,
                timeout_seconds=chat_config.timeout_seconds,
            )
        )

    kernel = Kernel(load_modules(definitions), providers=registry)
    service_token = (
        chat_config.service_bearer_token.get_secret_value()
        if chat_config.service_bearer_token is not None
        else None
    )
    chat_service = AppChatService(registry, service_token=service_token)
    return create_app(
        settings=settings,
        kernel=kernel,
        chat_service=chat_service,
    )
