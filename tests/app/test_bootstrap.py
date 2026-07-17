from __future__ import annotations

import asyncio
import sys
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agent_applications import AgentRunsRegistration
from app.bootstrap import (
    ApplicationRuntime,
    build_runtime,
    create_application,
    create_runtime_application,
)
from app.modules.agent_runs.definitions import default_adk_applications
from app.routes.agent_runs import create_agent_runs_router
from app.modules.chat import ChatModuleConfig, create_ai_runtime
from app.services import AppAgentRunsService
from app.services.agent_runs import AgentRunsRuntimeStats
from app.services.chat import DisabledChatService
from nexus_kernel.ai import (
    AdkApplicationBuildContext,
    AdkApplicationSpec,
    AgentToolSpec,
    GoogleAdkRuntime,
)
from nexus_kernel.config import ApplicationSettings
from nexus_kernel.errors import ConfigError, WorkerError
from nexus_kernel.kernel import Kernel
from nexus_kernel.observability import MetricsRegistry
from nexus_kernel.runtime import WorkerSpec, WorkerState, WorkerSupervisor
from tests.project_contract import load_project_contract


PROJECT = load_project_contract()


async def _module_worker(stop_event) -> None:
    await stop_event.wait()


def _agent_runs_modules_config(tmp_path, *, provider_id: str = "google_adk"):
    modules = tmp_path / f"modules-{provider_id}.yaml"
    modules.write_text(
        "modules:\n"
        "  chat:\n"
        "    class: app.modules.chat.module.ChatModule\n"
        "    enabled: true\n"
        "    required: false\n"
        "    config:\n"
        f"      provider_id: {provider_id}\n"
        "      ai_api_key: test-api-key # security-test-fixture\n"
        "  agent_runs:\n"
        "    class: app.modules.agent_runs.module.AgentRunsModule\n"
        "    enabled: true\n"
        "    required: false\n"
        "    config: {}\n",
        encoding="utf-8",
    )
    return modules


def test_build_runtime_defers_module_worker_registration_until_start() -> None:
    runtime = build_runtime()

    assert isinstance(runtime.workers, WorkerSupervisor)
    assert runtime.workers.statuses() == []


def test_build_runtime_registers_disabled_infrastructure_providers() -> None:
    runtime = build_runtime()

    statuses = {
        item["name"]: item["state"] for item in runtime.kernel.providers.statuses()
    }

    assert statuses["mongo"] == "disabled"
    assert statuses["redis"] == "disabled"
    assert statuses["postgres"] == "disabled"
    assert statuses["qdrant"] == "disabled"


def test_build_runtime_accepts_explicit_provider_config(tmp_path) -> None:
    config = tmp_path / "providers.yaml"
    config.write_text(
        "providers:\n  mongo: {}\n  redis: {}\n  postgres: {}\n  qdrant: {}\n",
        encoding="utf-8",
    )

    runtime = build_runtime(providers_config=config)

    assert [item["name"] for item in runtime.kernel.providers.statuses()[:4]] == [
        "mongo",
        "redis",
        "postgres",
        "qdrant",
    ]


def test_default_application_starts_degraded_without_credentials(
    monkeypatch,
) -> None:
    for name in (
        PROJECT.environment_name("SERVICE_BEARER_TOKEN"),
        PROJECT.environment_name("AI_API_KEY"),
    ):
        monkeypatch.delenv(name, raising=False)
    app = create_application()

    with TestClient(app, raise_server_exceptions=False) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        chat = client.post(
            "/api/v1/chat",
            json={"messages": [{"role": "user", "content": "private"}]},
        )

    assert live.status_code == 200
    assert ready.status_code == 200
    assert ready.json()["data"]["status"] == "degraded"
    assert chat.status_code == 503
    assert chat.json()["error"]["code"] == "auth_not_configured"
    assert "private" not in chat.text


def test_disabled_chat_module_is_not_exposed_by_application(tmp_path) -> None:
    modules = tmp_path / "modules.yaml"
    modules.write_text(
        "modules:\n"
        "  chat:\n"
        "    class: app.modules.chat.module.ChatModule\n"
        "    enabled: false\n",
        encoding="utf-8",
    )

    runtime = build_runtime(modules_config=modules)
    app = create_application(modules_config=modules)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/v1/chat", json={})

    assert runtime.chat_enabled is False
    assert response.status_code == 404
    assert "/api/v1/chat" not in app.openapi()["paths"]
    assert "/api/v1/agents/{agent_id}/runs" not in app.openapi()["paths"]


def test_agent_runs_rejects_non_adk_provider(tmp_path) -> None:
    modules = _agent_runs_modules_config(tmp_path, provider_id="openai_compatible")

    with pytest.raises(ConfigError, match="google_adk"):
        build_runtime(modules_config=modules)


def test_agent_runs_rejects_explicit_empty_application_registry(tmp_path) -> None:
    modules = _agent_runs_modules_config(tmp_path)

    with pytest.raises(ConfigError, match="at least one application"):
        build_runtime(modules_config=modules, agent_applications=())


def test_agent_runs_rejects_invalid_module_config(tmp_path) -> None:
    modules = _agent_runs_modules_config(tmp_path)
    configured = modules.read_text(encoding="utf-8").replace(
        "    config: {}\n",
        "    config:\n"
        "      phase_timeout_seconds: 10\n"
        "      overall_lifetime_seconds: 1\n",
    )
    modules.write_text(configured, encoding="utf-8")

    with pytest.raises(ConfigError, match="agent_runs"):
        build_runtime(modules_config=modules)


def test_agent_runs_requires_an_injected_application_registry(tmp_path) -> None:
    modules = _agent_runs_modules_config(tmp_path)

    with pytest.raises(ConfigError, match="must be injected"):
        build_runtime(modules_config=modules)


def test_agent_runs_samples_are_explicit_and_do_not_import_adk(tmp_path) -> None:
    modules = _agent_runs_modules_config(tmp_path)
    imported_before = {name for name in sys.modules if name.startswith("google.adk")}

    runtime = build_runtime(
        modules_config=modules,
        agent_applications=default_adk_applications(),
    )

    imported_after = {name for name in sys.modules if name.startswith("google.adk")}
    assert imported_after == imported_before
    assert runtime.agent_runs_enabled is True
    assert runtime.agent_runs_service is not None
    assert runtime.agent_application_registry is not None
    assert runtime.agent_application_registry.resolve("sample_graph") is not None
    assert runtime.agent_application_registry.resolve("sample_team") is not None


def test_api_agent_runs_registration_factory_is_resolved_once(tmp_path) -> None:
    modules = _agent_runs_modules_config(tmp_path)
    calls = 0
    applications = default_adk_applications()

    def registration_factory() -> AgentRunsRegistration:
        nonlocal calls
        calls += 1
        return AgentRunsRegistration(applications=applications)

    runtime = build_runtime(
        modules_config=modules,
        agent_runs_registration=registration_factory,
    )

    assert calls == 1
    assert runtime.agent_application_registry is not None
    assert runtime.agent_application_registry.resolve("sample_graph") is applications[0]


def test_agent_runs_freezes_injected_application_definitions(tmp_path) -> None:
    modules = _agent_runs_modules_config(tmp_path)
    application = AdkApplicationSpec(
        "custom_agent", "v2", "team", lambda context: object()
    )

    runtime = build_runtime(modules_config=modules, agent_applications=(application,))

    assert runtime.agent_application_registry is not None
    assert runtime.agent_application_registry.resolve("custom_agent") is application
    assert runtime.agent_application_registry.resolve("sample_team") is None


def test_agent_runs_validates_tool_registry_at_startup(tmp_path) -> None:
    modules = _agent_runs_modules_config(tmp_path)
    application = AdkApplicationSpec(
        "tool_agent",
        "v1",
        "team",
        lambda context: object(),
        tool_names=("lookup",),
    )

    with pytest.raises(ConfigError, match="tool registry"):
        build_runtime(modules_config=modules, agent_applications=(application,))

    tool = AgentToolSpec(
        name="lookup",
        description="lookup",
        parameters={"type": "object"},
        handler=lambda arguments, identity: {"ok": True},
    )
    runtime = build_runtime(
        modules_config=modules,
        agent_applications=(application,),
        agent_runs_tools=(tool,),
    )
    assert runtime.agent_runs_service is not None


def test_agent_runs_default_factories_construct_adk_24_apps() -> None:
    pytest.importorskip("google.adk.workflow")
    applications = default_adk_applications()
    context = AdkApplicationBuildContext(model="gemini-test", tools={})

    graph = applications[0].factory(context)
    team = applications[1].factory(context)

    assert graph.root_agent.name == "sample_graph"
    assert graph.root_agent.max_concurrency == 2
    assert team.root_agent.name == "coordinator"
    assert [agent.mode for agent in team.root_agent.sub_agents] == [
        "task",
        "single_turn",
    ]


def test_agent_runs_routes_are_exposed_only_when_enabled(tmp_path) -> None:
    modules = _agent_runs_modules_config(tmp_path)

    app = create_application(
        modules_config=modules,
        agent_applications=default_adk_applications(),
    )

    paths = app.openapi()["paths"]
    assert "/api/v1/chat" in paths
    assert "/api/v1/agents/{agent_id}/runs" in paths
    assert "/api/v1/agent-runs/{run_id}" in paths


def test_agent_runs_openapi_declares_error_envelopes_and_response_headers() -> None:
    app = FastAPI()
    app.include_router(create_agent_runs_router(cast(object, object())))  # type: ignore[arg-type]

    schema = app.openapi()
    create = schema["paths"]["/api/v1/agents/{agent_id}/runs"]["post"]
    get = schema["paths"]["/api/v1/agent-runs/{run_id}"]["get"]
    resume = schema["paths"]["/api/v1/agent-runs/{run_id}/resume"]["post"]
    cancel = schema["paths"]["/api/v1/agent-runs/{run_id}"]["delete"]
    delete_session = schema["paths"]["/api/v1/agents/{agent_id}/sessions/{session_id}"][
        "delete"
    ]

    for operation in (create, get, resume, cancel, delete_session):
        assert operation["responses"]["401"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/ErrorEnvelope")
        assert operation["responses"]["404"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/ErrorEnvelope")
        assert operation["responses"]["500"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/ErrorEnvelope")
        assert operation["responses"]["503"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/ErrorEnvelope")

    for operation, status in ((create, "202"), (resume, "202")):
        assert set(operation["responses"][status]["headers"]) == {
            "Location",
            "Cache-Control",
        }
    assert set(get["responses"]["200"]["headers"]) == {
        "Retry-After",
        "Cache-Control",
    }
    assert set(cancel["responses"]["200"]["headers"]) == {
        "Location",
        "Cache-Control",
    }
    assert set(cancel["responses"]["202"]["headers"]) == {
        "Location",
        "Cache-Control",
    }
    assert set(delete_session["responses"]["200"]["headers"]) == {"Cache-Control"}


def test_worker_role_ignores_api_only_agent_runs_registration(
    tmp_path, monkeypatch
) -> None:
    modules = _agent_runs_modules_config(tmp_path)
    modules.write_text(
        modules.read_text(encoding="utf-8").replace(
            "      ai_api_key: test-api-key # security-test-fixture\n", ""
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(PROJECT.environment_name("RUNTIME_ROLE"), "worker")
    registration_called = False

    def unexpected_registration() -> object:
        nonlocal registration_called
        registration_called = True
        raise AssertionError("worker must not load the Agent Runs registry")

    runtime = build_runtime(
        modules_config=modules,
        agent_runs_registration=unexpected_registration,  # type: ignore[arg-type]
    )

    assert registration_called is False
    assert runtime.agent_runs_enabled is False
    assert runtime.agent_runs_service is None
    assert runtime.agent_application_registry is None
    assert all(
        status["name"] != "agent_runs" for status in runtime.kernel.module_statuses()
    )


class _FakeAdkProvider:
    provider_id = "google_adk"

    def __init__(self) -> None:
        self.events: list[str] = []

    async def start(self) -> None:
        self.events.append("provider:start")

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        self.events.append("provider:close")


@pytest.mark.asyncio
async def test_agent_runs_and_chat_share_one_lazy_runtime_candidate(
    tmp_path, monkeypatch
) -> None:
    modules = _agent_runs_modules_config(tmp_path)
    provider = _FakeAdkProvider()
    monkeypatch.setattr(
        "app.bootstrap.create_ai_runtime",
        lambda config, *, session_store=None: provider,
    )

    runtime = build_runtime(
        modules_config=modules,
        agent_applications=default_adk_applications(),
    )
    assert runtime.agent_runs_service is not None
    assert runtime.agent_runs_service._runtime is provider
    assert runtime.agent_runs_service._observer is runtime.metrics

    await runtime.start()
    try:
        assert runtime.kernel.providers.get("ai_runtime") is provider
        assert provider.events == ["provider:start"]
    finally:
        await runtime.stop()

    assert provider.events == ["provider:start", "provider:close"]


def test_chat_config_defaults_provider_and_redacts_secrets() -> None:
    config = ChatModuleConfig.model_validate(
        {
            "service_bearer_token": "service-secret",  # security-test-fixture
            "ai_api_key": "test-api-key-secret",  # security-test-fixture
        }
    )

    assert config.provider_id == "openai_compatible"
    assert config.max_tool_argument_bytes == 65_536
    assert config.adk_session_cleanup_timeout_seconds == 2.0
    assert "service-secret" not in repr(config)
    assert "test-api-key-secret" not in repr(config)


def test_chat_config_rejects_credential_bearing_base_url() -> None:
    try:
        ChatModuleConfig.model_validate(
            {
                "base_url": "https://user:password@models.example/v1"  # security-test-fixture
            }
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("credential-bearing URL must be rejected")


def test_create_adk_runtime_propagates_session_cleanup_timeout() -> None:
    config = ChatModuleConfig.model_validate(
        {
            "provider_id": "google_adk",
            "ai_api_key": "test-api-key",  # security-test-fixture
            "max_tool_argument_bytes": 4096,
            "adk_session_cleanup_timeout_seconds": 1.5,
        }
    )

    runtime = create_ai_runtime(config)

    assert isinstance(runtime, GoogleAdkRuntime)
    assert runtime.max_tool_argument_bytes == 4096
    assert runtime.session_cleanup_timeout_seconds == 1.5


def test_chat_config_rejects_cleanup_timeout_above_request_timeout() -> None:
    with pytest.raises(ValidationError, match="cleanup timeout"):
        ChatModuleConfig.model_validate(
            {
                "timeout_seconds": 1,
                "tool_timeout_seconds": 1,
                "adk_session_cleanup_timeout_seconds": 1.1,
            }
        )


def test_create_application_configures_process_logging(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "app.bootstrap.configure_logging", lambda **values: calls.append(values)
    )

    create_application()

    assert calls == [{"level": "INFO", "serialize": False}]


class _RecordingKernel:
    def __init__(
        self, events: list[str], *, stop_errors: list[str] | None = None
    ) -> None:
        self.events = events
        self.stop_errors = stop_errors or []
        self.shutdown_complete = True

    async def start(self) -> None:
        self.events.append("kernel:start")

    async def stop(self) -> list[str]:
        self.events.append("kernel:stop")
        return self.stop_errors

    def worker_specs(self) -> list[WorkerSpec]:
        return [WorkerSpec("module_worker", _module_worker)]

    def health_status(self) -> dict[str, object]:
        return {"status": "healthy", "modules": [], "providers": []}


class _RecordingWorkers:
    def __init__(
        self,
        events: list[str],
        *,
        start_error: Exception | None = None,
        stop_errors: list[str] | None = None,
        stop_error: Exception | None = None,
        shutdown_complete: bool = True,
    ) -> None:
        self.events = events
        self.start_error = start_error
        self.stop_errors = stop_errors or []
        self.stop_error = stop_error
        self.shutdown_complete = shutdown_complete

    def register(self, spec: WorkerSpec) -> None:
        self.events.append(f"workers:register:{spec.name}")

    async def start_all(self) -> None:
        self.events.append("workers:start")
        if self.start_error is not None:
            raise self.start_error

    async def stop_all(self) -> list[str]:
        self.events.append("workers:stop")
        if self.stop_error is not None:
            raise self.stop_error
        return self.stop_errors


def _runtime(
    role: str,
    kernel: _RecordingKernel,
    workers: _RecordingWorkers,
) -> ApplicationRuntime:
    return ApplicationRuntime(
        settings=ApplicationSettings.model_validate({"runtime": {"role": role}}),
        kernel=cast(Kernel, kernel),
        chat_service=DisabledChatService(),
        workers=cast(WorkerSupervisor, workers),
        metrics=MetricsRegistry("test"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (
            "worker",
            [
                "kernel:start",
                "workers:register:module_worker",
                "workers:start",
                "workers:stop",
                "kernel:stop",
            ],
        ),
        ("api", ["kernel:start", "kernel:stop"]),
    ],
)
async def test_runtime_respects_role_lifecycle_order(
    role: str, expected: list[str]
) -> None:
    events: list[str] = []
    runtime = _runtime(role, _RecordingKernel(events), _RecordingWorkers(events))

    await runtime.start()
    await runtime.stop()

    assert events == expected


@pytest.mark.asyncio
async def test_worker_start_failure_rolls_back_and_redacts_error() -> None:
    events: list[str] = []
    runtime = _runtime(
        "worker",
        _RecordingKernel(events),
        _RecordingWorkers(events, start_error=RuntimeError("password=must-not-leak")),
    )

    with pytest.raises(WorkerError) as captured:
        await runtime.start()

    assert events == [
        "kernel:start",
        "workers:register:module_worker",
        "workers:start",
        "workers:stop",
        "kernel:stop",
    ]
    assert "must-not-leak" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_worker_stop_aggregates_stable_errors_in_worker_error() -> None:
    events: list[str] = []
    runtime = _runtime(
        "worker",
        _RecordingKernel(events, stop_errors=["module_stop_failed:private"]),
        _RecordingWorkers(events, stop_errors=["worker_stop_timeout:background"]),
    )
    await runtime.start()

    with pytest.raises(WorkerError) as captured:
        await runtime.stop()

    assert captured.value.errors == (
        "worker_stop_timeout:background",
        "module_stop_failed:private",
    )


@pytest.mark.asyncio
async def test_worker_cleanup_exception_still_stops_kernel() -> None:
    events: list[str] = []
    runtime = _runtime(
        "worker",
        _RecordingKernel(events),
        _RecordingWorkers(
            events,
            start_error=RuntimeError("startup-secret"),
            stop_error=RuntimeError("cleanup-secret"),
        ),
    )

    with pytest.raises(WorkerError) as captured:
        await runtime.start()

    assert events == [
        "kernel:start",
        "workers:register:module_worker",
        "workers:start",
        "workers:stop",
        "kernel:stop",
    ]
    assert "startup-secret" not in str(captured.value)
    assert "cleanup-secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_runtime_defers_repeated_cancellation_until_workers_stop() -> None:
    events: list[str] = []
    stop_entered = asyncio.Event()
    release_stop = asyncio.Event()

    class BlockingWorkers(_RecordingWorkers):
        async def stop_all(self) -> list[str]:
            self.events.append("workers:stop")
            stop_entered.set()
            await release_stop.wait()
            self.events.append("workers:stopped")
            return []

    runtime = _runtime(
        "worker",
        _RecordingKernel(events),
        BlockingWorkers(events),
    )
    stopping = asyncio.create_task(runtime.stop())
    await stop_entered.wait()
    concurrent_stop = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0)

    stopping.cancel()
    await asyncio.sleep(0)
    stopping.cancel()
    await asyncio.sleep(0)

    assert events == ["workers:stop"]
    assert not stopping.done()

    release_stop.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    await concurrent_stop

    assert events == ["workers:stop", "workers:stopped", "kernel:stop"]


@pytest.mark.asyncio
async def test_worker_internal_cancellation_preserves_kernel_and_is_retryable() -> None:
    events: list[str] = []

    class CancellingWorkers(_RecordingWorkers):
        async def stop_all(self) -> list[str]:
            self.events.append("workers:stop")
            raise asyncio.CancelledError

    runtime = _runtime(
        "worker",
        _RecordingKernel(events),
        CancellingWorkers(events),
    )

    with pytest.raises(WorkerError) as first:
        await runtime.stop()
    with pytest.raises(WorkerError) as second:
        await runtime.stop()

    assert first.value.errors == ("worker_supervisor_stop_failed",)
    assert second.value.errors == first.value.errors
    assert events == ["workers:stop", "workers:stop"]
    assert runtime._shutdown_task is None


@pytest.mark.asyncio
async def test_runtime_does_not_close_kernel_while_worker_task_is_pending() -> None:
    events: list[str] = []
    release_worker = asyncio.Event()

    async def resistant_worker(stop_event: asyncio.Event) -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await release_worker.wait()

    class WorkerKernel(_RecordingKernel):
        def worker_specs(self) -> list[WorkerSpec]:
            return [
                WorkerSpec(
                    "resistant_worker",
                    resistant_worker,
                    shutdown_timeout_seconds=0.01,
                )
            ]

    kernel = WorkerKernel(events)
    workers = WorkerSupervisor()
    runtime = ApplicationRuntime(
        settings=ApplicationSettings.model_validate({"runtime": {"role": "worker"}}),
        kernel=cast(Kernel, kernel),
        chat_service=DisabledChatService(),
        workers=workers,
        metrics=MetricsRegistry("test_pending_worker"),
    )
    await runtime.start()

    with pytest.raises(WorkerError) as first:
        await runtime.stop()

    assert first.value.errors == ("worker_stop_timeout:resistant_worker",)
    assert events == ["kernel:start"]
    assert workers.shutdown_complete is False
    assert runtime._shutdown_task is None

    release_worker.set()
    await asyncio.sleep(0)
    with pytest.raises(WorkerError) as second:
        await runtime.stop()

    assert second.value.errors == ("worker_stop_timeout:resistant_worker",)
    assert events == ["kernel:start", "kernel:stop"]
    assert workers.shutdown_complete is True


@pytest.mark.asyncio
async def test_runtime_accepts_worker_task_cancellation_during_normal_stop() -> None:
    events: list[str] = []

    async def cancels_after_stop(stop_event: asyncio.Event) -> None:
        await stop_event.wait()
        raise asyncio.CancelledError

    class CancellingKernel(_RecordingKernel):
        def worker_specs(self) -> list[WorkerSpec]:
            return [WorkerSpec("cancels_after_stop", cancels_after_stop)]

    kernel = CancellingKernel(events)
    workers = WorkerSupervisor()
    runtime = ApplicationRuntime(
        settings=ApplicationSettings.model_validate({"runtime": {"role": "worker"}}),
        kernel=cast(Kernel, kernel),
        chat_service=DisabledChatService(),
        workers=workers,
        metrics=MetricsRegistry("test_cancelled_worker"),
    )

    await runtime.start()
    await runtime.stop()
    await runtime.stop()

    assert events == ["kernel:start", "kernel:stop"]
    assert workers.statuses()[0].state is WorkerState.STOPPED
    assert await workers.stop_all() == []


class _RecordingAgentRunsService:
    def __init__(
        self,
        events: list[str],
        *,
        close_failures: int = 0,
    ) -> None:
        self.events = events
        self.close_failures = close_failures
        self.healthy = True
        self.accepting = True
        self.draining = False

    @property
    def runtime_stats(self) -> AgentRunsRuntimeStats:
        return AgentRunsRuntimeStats(
            accepting=self.accepting,
            draining=self.draining,
            healthy=self.healthy and self.accepting and not self.draining,
            queue_depth=0,
            active_phases=0,
            waiting_actions=0,
            cleanup_debt=0,
            runs_by_status={},
        )

    async def start(self) -> None:
        self.events.append("agent_runs:start")

    async def quiesce(self) -> None:
        self.events.append("agent_runs:quiesce")
        self.accepting = False
        self.draining = True

    async def drain(self, *, timeout_seconds: float | None = None) -> bool:
        del timeout_seconds
        return True

    async def close(self) -> None:
        self.events.append("agent_runs:close")
        if self.close_failures:
            self.close_failures -= 1
            raise RuntimeError("private-close-error")


class _OperationalAgentKernel(_RecordingKernel):
    def module_statuses(self) -> list[dict[str, object]]:
        return [
            {
                "name": "agent_runs",
                "required": False,
                "state": "ready",
                "error": None,
            }
        ]

    def route_bindings(self) -> list[object]:
        return []


class _FailedAgentKernel(_OperationalAgentKernel):
    def module_statuses(self) -> list[dict[str, object]]:
        statuses = super().module_statuses()
        statuses[0]["state"] = "failed"
        return statuses


def _agent_runtime(
    events: list[str], service: _RecordingAgentRunsService
) -> ApplicationRuntime:
    return ApplicationRuntime(
        settings=ApplicationSettings.model_validate({"runtime": {"role": "api"}}),
        kernel=cast(Kernel, _OperationalAgentKernel(events)),
        chat_service=DisabledChatService(),
        workers=WorkerSupervisor(),
        metrics=MetricsRegistry("test_agent_runs_lifecycle"),
        agent_runs_service=cast(AppAgentRunsService, service),
        agent_runs_enabled=True,
    )


@pytest.mark.asyncio
async def test_agent_runs_lifecycle_wraps_kernel_provider_lifecycle() -> None:
    events: list[str] = []
    runtime = _agent_runtime(events, _RecordingAgentRunsService(events))

    await runtime.start()
    await runtime.stop()

    assert events == [
        "kernel:start",
        "agent_runs:start",
        "agent_runs:close",
        "kernel:stop",
    ]


def test_agent_runs_health_participates_in_api_readiness() -> None:
    events: list[str] = []
    service = _RecordingAgentRunsService(events)
    runtime = _agent_runtime(events, service)

    assert runtime.health_status()["status"] == "healthy"
    assert runtime.health_status()["agent_runs"] == {
        "status": "healthy",
        "lifecycle": "accepting",
    }

    service.healthy = False

    assert runtime.health_status()["status"] == "degraded"
    assert runtime.health_status()["agent_runs"] == {
        "status": "degraded",
        "lifecycle": "accepting",
    }

    service.accepting = False
    service.draining = True

    assert runtime.health_status()["agent_runs"] == {
        "status": "degraded",
        "lifecycle": "draining",
    }


@pytest.mark.asyncio
async def test_agent_runs_service_does_not_start_for_failed_module() -> None:
    events: list[str] = []
    service = _RecordingAgentRunsService(events)
    runtime = ApplicationRuntime(
        settings=ApplicationSettings.model_validate({"runtime": {"role": "api"}}),
        kernel=cast(Kernel, _FailedAgentKernel(events)),
        chat_service=DisabledChatService(),
        workers=WorkerSupervisor(),
        metrics=MetricsRegistry("test_agent_runs_failed_module"),
        agent_runs_service=cast(AppAgentRunsService, service),
        agent_runs_enabled=True,
    )

    await runtime.start()
    await runtime.stop()

    assert events == ["kernel:start", "kernel:stop"]


@pytest.mark.asyncio
async def test_api_lifespan_uses_complete_application_runtime() -> None:
    events: list[str] = []
    runtime = _agent_runtime(events, _RecordingAgentRunsService(events))
    app = create_runtime_application(runtime)

    async with app.router.lifespan_context(app):
        assert events == ["kernel:start", "agent_runs:start"]

    assert events == [
        "kernel:start",
        "agent_runs:start",
        "agent_runs:close",
        "kernel:stop",
    ]


@pytest.mark.asyncio
async def test_agent_runs_shutdown_must_converge_before_kernel_closes() -> None:
    events: list[str] = []
    service = _RecordingAgentRunsService(events, close_failures=1)
    runtime = _agent_runtime(events, service)
    await runtime.start()

    with pytest.raises(WorkerError) as first:
        await runtime.stop()

    assert first.value.errors == ("agent_runs_service_stop_failed",)
    assert events == [
        "kernel:start",
        "agent_runs:start",
        "agent_runs:close",
    ]
    assert runtime._shutdown_task is None

    await runtime.stop()

    assert events == [
        "kernel:start",
        "agent_runs:start",
        "agent_runs:close",
        "agent_runs:close",
        "kernel:stop",
    ]
