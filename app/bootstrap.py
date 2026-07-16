"""Application-owned assembly of framework, modules, and optional AI runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from app.modules.chat import ChatModule, ChatModuleConfig, create_ai_runtime
from app.providers import load_provider_settings, register_infrastructure_providers
from app.routes import create_chat_router
from app.services import AppChatService, ChatService, TrustedIdentityResolver
from nexus_kernel.ai import AdkSessionStore
from nexus_kernel.config import (
    ApplicationSettings,
    load_application_settings,
    load_module_definitions,
)
from nexus_kernel.errors import ConfigError, WorkerError
from nexus_kernel.gateway import create_app
from nexus_kernel.kernel import Kernel, load_modules
from nexus_kernel.observability import MetricsRegistry, configure_logging
from nexus_kernel.providers import ProviderRegistry, ProviderSpec
from nexus_kernel.runtime import WorkerState, WorkerStatus, WorkerSupervisor


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(slots=True)
class ApplicationRuntime:
    settings: ApplicationSettings
    kernel: Kernel
    chat_service: ChatService
    workers: WorkerSupervisor
    metrics: MetricsRegistry
    chat_enabled: bool = True
    _workers_registered: bool = False

    async def _stop_components(self) -> tuple[str, ...]:
        errors: list[str] = []
        if self.settings.runtime.role == "worker":
            try:
                errors.extend(await self.workers.stop_all())
            except asyncio.CancelledError as cancellation:
                try:
                    await self.kernel.stop()
                except BaseException:
                    pass
                raise cancellation
            except Exception:
                errors.append("worker_supervisor_stop_failed")
        try:
            errors.extend(await self.kernel.stop())
        except asyncio.CancelledError:
            raise
        except Exception:
            errors.append("kernel_stop_failed")
        return tuple(errors)

    async def start(self) -> None:
        await self.kernel.start()
        if self.settings.runtime.role != "worker":
            return
        try:
            if not self._workers_registered:
                for spec in self.kernel.worker_specs():
                    self.workers.register(spec)
                self._workers_registered = True
            await self.workers.start_all()
        except asyncio.CancelledError:
            await self._stop_components()
            raise
        except Exception:
            cleanup_errors = await self._stop_components()
            raise WorkerError(
                "Worker runtime startup failed", errors=cleanup_errors
            ) from None

    async def stop(self) -> None:
        errors = await self._stop_components()
        if errors:
            raise WorkerError("Runtime shutdown failed", errors=errors)

    def health_status(self) -> dict[str, object]:
        kernel_health = self.kernel.health_status()
        if self.settings.runtime.role != "worker":
            return kernel_health
        worker_health = self.workers.health_status()
        return {
            **kernel_health,
            "status": (
                "degraded"
                if worker_health["status"] != "healthy"
                or kernel_health["status"] == "degraded"
                else "healthy"
            ),
            "workers": worker_health["workers"],
        }


def _project_root() -> Path:
    working_root = Path.cwd()
    if (working_root / "config" / "application.yaml").is_file():
        return working_root
    return _PROJECT_ROOT


def build_runtime(
    *,
    application_config: str | Path | None = None,
    modules_config: str | Path | None = None,
    providers_config: str | Path | None = None,
    trusted_identity_resolver: TrustedIdentityResolver | None = None,
    adk_session_store: AdkSessionStore | None = None,
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
    providers_path = (
        Path(providers_config)
        if providers_config
        else (project_root / "config" / "providers.yaml")
    )
    settings = load_application_settings(application_path)
    definitions = load_module_definitions(modules_path)
    provider_settings = load_provider_settings(providers_path)
    loaded_modules = load_modules(definitions)
    loaded_chat = next(
        (entry.module for entry in loaded_modules if entry.module.name == "chat"), None
    )
    chat_enabled = loaded_chat is not None
    registry = ProviderRegistry()
    register_infrastructure_providers(registry, provider_settings)

    if loaded_chat is None:
        chat_config = ChatModuleConfig()
    else:
        if not isinstance(loaded_chat, ChatModule):
            raise ConfigError("Configured chat module has an invalid type")
        chat_config = loaded_chat.config
        if chat_config.timeout_seconds >= settings.server.request_timeout_seconds:
            raise ConfigError("AI timeout must be smaller than request timeout")
        registry.register(
            ProviderSpec(
                "ai_runtime",
                lambda: create_ai_runtime(chat_config, session_store=adk_session_store),
                required=False,
                timeout_seconds=chat_config.timeout_seconds,
            )
        )

    kernel = Kernel(loaded_modules, providers=registry)
    metrics = MetricsRegistry(
        settings.monitoring.metrics_prefix,
        cache_ttl_seconds=settings.monitoring.metrics_cache_ttl_seconds,
    )

    def worker_status_listener(status: WorkerStatus) -> None:
        metrics.set_component_health(
            "worker", status.name, healthy=status.state is not WorkerState.FAILED
        )

    workers = WorkerSupervisor(status_listener=worker_status_listener)
    service_token = (
        chat_config.service_bearer_token.get_secret_value()
        if chat_config.service_bearer_token is not None
        else None
    )
    chat_service = AppChatService(
        registry,
        service_token=service_token if trusted_identity_resolver is None else None,
        identity_resolver=trusted_identity_resolver,
    )
    return ApplicationRuntime(
        settings,
        kernel,
        chat_service,
        workers,
        metrics,
        chat_enabled=chat_enabled,
    )


def create_application(
    *,
    application_config: str | Path | None = None,
    modules_config: str | Path | None = None,
    providers_config: str | Path | None = None,
    trusted_identity_resolver: TrustedIdentityResolver | None = None,
    adk_session_store: AdkSessionStore | None = None,
) -> FastAPI:
    runtime = build_runtime(
        application_config=application_config,
        modules_config=modules_config,
        providers_config=providers_config,
        trusted_identity_resolver=trusted_identity_resolver,
        adk_session_store=adk_session_store,
    )
    configure_logging(
        level=runtime.settings.logging.level,
        serialize=runtime.settings.logging.serialize,
    )
    routers = (
        (create_chat_router(runtime.chat_service),) if runtime.chat_enabled else ()
    )
    return create_app(
        settings=runtime.settings,
        kernel=runtime.kernel,
        routers=routers,
        metrics=runtime.metrics,
    )
