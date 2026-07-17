"""Application-owned assembly of framework, modules, and optional AI runtime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from fastapi import FastAPI

from app.modules.chat import ChatModule, ChatModuleConfig, create_ai_runtime
from app.providers import load_provider_settings, register_infrastructure_providers
from app.routes.chat import create_chat_router
from app.services.chat import AppChatService, ChatService
from app.services.identity import TrustedIdentityResolver
from nexus_kernel.ai import (
    AdkApplicationRegistry,
    AdkApplicationSpec,
    AdkSessionStore,
    AgentToolSpec,
    WorkflowRuntime,
)
from nexus_kernel.config import (
    ApplicationSettings,
    load_application_settings,
    load_module_definitions,
)
from nexus_kernel.errors import ConfigError, WorkerError
from nexus_kernel.gateway import create_app
from nexus_kernel.kernel import Kernel, load_modules
from nexus_kernel.observability import MetricsRegistry, configure_logging
from nexus_kernel.providers import Provider, ProviderRegistry, ProviderSpec
from nexus_kernel.runtime import WorkerState, WorkerStatus, WorkerSupervisor

if TYPE_CHECKING:
    from app.agent_applications import AgentRunsRegistration
    from app.services.agent_runs import AgentAuthorizer, AppAgentRunsService


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class _ShutdownResult:
    errors: tuple[str, ...]
    retryable: bool = False


@dataclass(slots=True)
class ApplicationRuntime:
    settings: ApplicationSettings
    kernel: Kernel
    chat_service: ChatService
    workers: WorkerSupervisor
    metrics: MetricsRegistry
    chat_enabled: bool = True
    agent_runs_service: AppAgentRunsService | None = None
    agent_application_registry: AdkApplicationRegistry | None = None
    agent_runs_enabled: bool = False
    _workers_registered: bool = False
    _agent_runs_started: bool = False
    _shutdown_task: asyncio.Task[_ShutdownResult] | None = field(
        default=None, init=False, repr=False
    )

    async def _stop_components_in_order(self) -> _ShutdownResult:
        errors: list[str] = []
        if (
            self.settings.runtime.role == "api"
            and self.agent_runs_service is not None
            and self._agent_runs_started
        ):
            try:
                await self.agent_runs_service.close()
            except asyncio.CancelledError:
                errors.append("agent_runs_service_stop_failed")
                return _ShutdownResult(tuple(errors), retryable=True)
            except Exception:
                errors.append("agent_runs_service_stop_failed")
                return _ShutdownResult(tuple(errors), retryable=True)
            self._agent_runs_started = False
        if self.settings.runtime.role == "worker":
            try:
                errors.extend(await self.workers.stop_all())
            except asyncio.CancelledError:
                errors.append("worker_supervisor_stop_failed")
                return _ShutdownResult(tuple(errors), retryable=True)
            except Exception:
                errors.append("worker_supervisor_stop_failed")
            if not self.workers.shutdown_complete:
                return _ShutdownResult(tuple(errors), retryable=True)
        try:
            errors.extend(await self.kernel.stop())
        except asyncio.CancelledError:
            errors.append("kernel_stop_failed")
            return _ShutdownResult(tuple(errors), retryable=True)
        except Exception:
            errors.append("kernel_stop_failed")
            return _ShutdownResult(tuple(errors), retryable=True)
        return _ShutdownResult(
            tuple(errors), retryable=not self.kernel.shutdown_complete
        )

    async def _stop_components(self) -> tuple[str, ...]:
        cleanup = self._shutdown_task
        if cleanup is None:
            cleanup = asyncio.create_task(
                self._stop_components_in_order(),
                name="nexus_kernel_runtime_shutdown",
            )
            self._shutdown_task = cleanup
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                result = await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError as exc:
                if cleanup.done():
                    if cleanup.cancelled():
                        if self._shutdown_task is cleanup:
                            self._shutdown_task = None
                        raise
                    result = cleanup.result()
                    cancellation = cancellation or exc
                    break
                cancellation = cancellation or exc
        if result.retryable and self._shutdown_task is cleanup:
            self._shutdown_task = None
        if cancellation is not None:
            raise cancellation
        return result.errors

    async def start(self) -> None:
        await self.kernel.start()
        if self.settings.runtime.role == "api":
            service = self.agent_runs_service
            if service is None or not self._module_is_operational("agent_runs"):
                return
            self._agent_runs_started = True
            try:
                await service.start()
            except asyncio.CancelledError:
                await self._stop_components()
                raise
            except Exception:
                cleanup_errors = await self._stop_components()
                raise WorkerError(
                    "Agent Runs runtime startup failed", errors=cleanup_errors
                ) from None
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

    def _module_is_operational(self, name: str) -> bool:
        return any(
            status["name"] == name and status["state"] in {"ready", "degraded"}
            for status in self.kernel.module_statuses()
        )

    async def stop(self) -> None:
        errors = await self._stop_components()
        if errors:
            raise WorkerError("Runtime shutdown failed", errors=errors)

    def health_status(self) -> dict[str, object]:
        kernel_health = cast(dict[str, object], self.kernel.health_status())
        if self.settings.runtime.role == "api":
            service = self.agent_runs_service
            if not self.agent_runs_enabled or service is None:
                return kernel_health
            stats = service.runtime_stats
            self.metrics.set_agent_runs_runtime(
                queue_depth=stats.queue_depth,
                active_phases=stats.active_phases,
                waiting_actions=stats.waiting_actions,
                cleanup_debt=stats.cleanup_debt,
                runs_by_status=stats.runs_by_status,
            )
            agent_runs_healthy = stats.healthy
            lifecycle = (
                "draining"
                if stats.draining
                else "accepting"
                if stats.accepting
                else "stopped"
            )
            return {
                **kernel_health,
                "status": (
                    "healthy"
                    if agent_runs_healthy and kernel_health["status"] == "healthy"
                    else "degraded"
                ),
                "agent_runs": {
                    "status": "healthy" if agent_runs_healthy else "degraded",
                    "lifecycle": lifecycle,
                },
            }
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


def _resolve_agent_runs_registration(
    registration_factory: Callable[[], AgentRunsRegistration] | None,
    *,
    applications: Sequence[AdkApplicationSpec] | None,
    authorizer: AgentAuthorizer | None,
    tools: Sequence[AgentToolSpec],
    identity_resolver: TrustedIdentityResolver | None,
) -> tuple[
    Sequence[AdkApplicationSpec] | None,
    AgentAuthorizer | None,
    Sequence[AgentToolSpec],
    TrustedIdentityResolver | None,
]:
    if registration_factory is None:
        return applications, authorizer, tools, identity_resolver
    if applications is not None or authorizer is not None or tools:
        raise ConfigError(
            "Agent Runs registration factory conflicts with explicit injection"
        )
    registration = registration_factory()
    registered_resolver = registration.identity_resolver
    if (
        registered_resolver is not None
        and identity_resolver is not None
        and identity_resolver is not registered_resolver
    ):
        raise ConfigError("Agent Runs identity resolver is configured twice")
    return (
        registration.applications,
        registration.authorizer,
        registration.tools,
        registered_resolver or identity_resolver,
    )


def build_runtime(
    *,
    application_config: str | Path | None = None,
    modules_config: str | Path | None = None,
    providers_config: str | Path | None = None,
    trusted_identity_resolver: TrustedIdentityResolver | None = None,
    adk_session_store: AdkSessionStore | None = None,
    agent_applications: Sequence[AdkApplicationSpec] | None = None,
    agent_runs_authorizer: AgentAuthorizer | None = None,
    agent_runs_tools: Sequence[AgentToolSpec] = (),
    agent_runs_registration: Callable[[], AgentRunsRegistration] | None = None,
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
    metrics = MetricsRegistry(
        settings.monitoring.metrics_prefix,
        cache_ttl_seconds=settings.monitoring.metrics_cache_ttl_seconds,
    )
    loaded_modules = load_modules(definitions)
    loaded_chat = next(
        (entry.module for entry in loaded_modules if entry.module.name == "chat"), None
    )
    loaded_agent_runs = next(
        (entry.module for entry in loaded_modules if entry.module.name == "agent_runs"),
        None,
    )
    if settings.runtime.role == "worker":
        # Agent Runs owns API-process state and has no WorkerSpec in v1. Keeping the
        # module in a worker Kernel would incorrectly require API credentials and
        # an application registry that the worker never consumes.
        loaded_modules = [
            entry for entry in loaded_modules if entry.module.name != "agent_runs"
        ]
        loaded_agent_runs = None
    chat_enabled = loaded_chat is not None
    agent_runs_enabled = loaded_agent_runs is not None
    registry = ProviderRegistry()
    register_infrastructure_providers(registry, provider_settings)
    ai_runtime: Provider | None = None

    if loaded_chat is None:
        chat_config = ChatModuleConfig()
    else:
        if not isinstance(loaded_chat, ChatModule):
            raise ConfigError("Configured chat module has an invalid type")
        chat_config = loaded_chat.config
        if chat_config.timeout_seconds >= settings.server.request_timeout_seconds:
            raise ConfigError("AI timeout must be smaller than request timeout")
        if chat_config.ai_api_key is not None:
            configured_ai_runtime = create_ai_runtime(
                chat_config, session_store=adk_session_store
            )
            ai_runtime = configured_ai_runtime

            def provide_ai_runtime() -> Provider:
                return configured_ai_runtime

            runtime_factory = provide_ai_runtime
        else:

            def create_unconfigured_ai_runtime() -> Provider:
                return create_ai_runtime(chat_config, session_store=adk_session_store)

            runtime_factory = create_unconfigured_ai_runtime
        registry.register(
            ProviderSpec(
                "ai_runtime",
                runtime_factory,
                required=False,
                timeout_seconds=chat_config.timeout_seconds,
            )
        )

    agent_runs_service: AppAgentRunsService | None = None
    application_registry: AdkApplicationRegistry | None = None
    if loaded_agent_runs is not None:
        (
            agent_applications,
            agent_runs_authorizer,
            agent_runs_tools,
            trusted_identity_resolver,
        ) = _resolve_agent_runs_registration(
            agent_runs_registration,
            applications=agent_applications,
            authorizer=agent_runs_authorizer,
            tools=agent_runs_tools,
            identity_resolver=trusted_identity_resolver,
        )
        from app.modules.agent_runs import AgentRunsModule, AgentRunsModuleConfig
        from app.services.agent_runs import AppAgentRunsService

        if not isinstance(loaded_agent_runs, AgentRunsModule):
            raise ConfigError("Configured Agent Runs module has an invalid type")
        if loaded_chat is None or chat_config.provider_id != "google_adk":
            raise ConfigError("Agent Runs requires the google_adk AI provider")
        if (
            ai_runtime is None
            or getattr(ai_runtime, "provider_id", None) != "google_adk"
        ):
            raise ConfigError("Agent Runs requires configured Google ADK credentials")
        if agent_applications is None:
            raise ConfigError("Agent Runs application registry must be injected")
        applications = tuple(agent_applications)
        if not applications:
            raise ConfigError("Agent Runs requires at least one application")
        try:
            application_registry = AdkApplicationRegistry(applications)
        except (TypeError, ValueError):
            raise ConfigError("Agent Runs application registry is invalid") from None
        registered_tools = tuple(agent_runs_tools)
        registered_tool_names = {tool.name for tool in registered_tools}
        if len(registered_tool_names) != len(registered_tools) or any(
            not set(application.tool_names).issubset(registered_tool_names)
            for application in applications
        ):
            raise ConfigError("Agent Runs tool registry is invalid")
        agent_config = loaded_agent_runs.config
        if not isinstance(agent_config, AgentRunsModuleConfig):
            raise ConfigError("Configured Agent Runs module has invalid config")
        agent_runs_service = AppAgentRunsService(
            cast(WorkflowRuntime, ai_runtime),
            application_registry,
            agent_config,
            service_token=(
                chat_config.service_bearer_token.get_secret_value()
                if chat_config.service_bearer_token is not None
                and trusted_identity_resolver is None
                else None
            ),
            identity_resolver=trusted_identity_resolver,
            authorizer=agent_runs_authorizer,
            tools=registered_tools,
            session_ttl_seconds=chat_config.adk_session_ttl_seconds,
            observer=metrics,
        )

    kernel = Kernel(
        loaded_modules,
        providers=registry,
        module_cleanup_timeout_seconds=(
            settings.runtime.module_cleanup_timeout_seconds
        ),
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
        settings=settings,
        kernel=kernel,
        chat_service=chat_service,
        workers=workers,
        metrics=metrics,
        chat_enabled=chat_enabled,
        agent_runs_service=agent_runs_service,
        agent_application_registry=application_registry,
        agent_runs_enabled=agent_runs_enabled,
    )


def create_runtime_application(runtime: ApplicationRuntime) -> FastAPI:
    routers = []
    if runtime.chat_enabled:
        routers.append(create_chat_router(runtime.chat_service))
    if runtime.agent_runs_enabled and runtime.agent_runs_service is not None:
        from app.routes.agent_runs import create_agent_runs_router

        routers.append(create_agent_runs_router(runtime.agent_runs_service))
    application = create_app(
        settings=runtime.settings,
        kernel=runtime.kernel,
        routers=tuple(routers),
        metrics=runtime.metrics,
        health_provider=runtime.health_status,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            try:
                await runtime.stop()
            except asyncio.CancelledError:
                raise
            except WorkerError as error:
                app.state.shutdown_errors = error.errors
                raise RuntimeError("application_runtime_shutdown_failed") from None
            else:
                app.state.shutdown_errors = ()

    application.router.lifespan_context = lifespan
    application.state.runtime = runtime
    return application


def create_application(
    *,
    application_config: str | Path | None = None,
    modules_config: str | Path | None = None,
    providers_config: str | Path | None = None,
    trusted_identity_resolver: TrustedIdentityResolver | None = None,
    adk_session_store: AdkSessionStore | None = None,
    agent_applications: Sequence[AdkApplicationSpec] | None = None,
    agent_runs_authorizer: AgentAuthorizer | None = None,
    agent_runs_tools: Sequence[AgentToolSpec] = (),
    agent_runs_registration: Callable[[], AgentRunsRegistration] | None = None,
) -> FastAPI:
    runtime = build_runtime(
        application_config=application_config,
        modules_config=modules_config,
        providers_config=providers_config,
        trusted_identity_resolver=trusted_identity_resolver,
        adk_session_store=adk_session_store,
        agent_applications=agent_applications,
        agent_runs_authorizer=agent_runs_authorizer,
        agent_runs_tools=agent_runs_tools,
        agent_runs_registration=agent_runs_registration,
    )
    configure_logging(
        level=runtime.settings.logging.level,
        serialize=runtime.settings.logging.serialize,
    )
    return create_runtime_application(runtime)
