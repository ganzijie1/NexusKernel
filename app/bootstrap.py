"""Application-owned assembly of framework, modules, and optional AI runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from pydantic import ValidationError

from app.modules.chat import ChatModuleConfig, create_ai_runtime
from app.services import AppChatService
from nexuskernel.config import (
    ApplicationSettings,
    load_application_settings,
    load_module_definitions,
)
from nexuskernel.errors import ConfigError
from nexuskernel.gateway import create_app
from nexuskernel.kernel import Kernel, load_modules
from nexuskernel.providers import ProviderRegistry, ProviderSpec


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class ApplicationRuntime:
    settings: ApplicationSettings
    kernel: Kernel
    chat_service: AppChatService


def _project_root() -> Path:
    working_root = Path.cwd()
    if (working_root / "config" / "application.yaml").is_file():
        return working_root
    return _PROJECT_ROOT


def build_runtime(
    *,
    application_config: str | Path | None = None,
    modules_config: str | Path | None = None,
) -> ApplicationRuntime:
    project_root = _project_root()
    application_path = (
        Path(application_config)
        if application_config
        else (project_root / "config" / "application.yaml")
    )
    modules_path = (
        Path(modules_config)
        if modules_config
        else (project_root / "config" / "modules.yaml")
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
    return ApplicationRuntime(settings, kernel, chat_service)


def create_application(
    *,
    application_config: str | Path | None = None,
    modules_config: str | Path | None = None,
) -> FastAPI:
    runtime = build_runtime(
        application_config=application_config,
        modules_config=modules_config,
    )
    return create_app(
        settings=runtime.settings,
        kernel=runtime.kernel,
        chat_service=runtime.chat_service,
    )
