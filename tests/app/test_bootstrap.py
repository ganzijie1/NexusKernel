from __future__ import annotations

from typing import cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.bootstrap import ApplicationRuntime, build_runtime, create_application
from app.modules.chat import ChatModuleConfig
from app.services.chat import DisabledChatService
from nexus_kernel.config import ApplicationSettings
from nexus_kernel.errors import WorkerError
from nexus_kernel.kernel import Kernel
from nexus_kernel.observability import MetricsRegistry
from nexus_kernel.runtime import WorkerSpec, WorkerSupervisor


async def _module_worker(stop_event) -> None:
    await stop_event.wait()


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
        "NEXUS_KERNEL_SERVICE_BEARER_TOKEN",
        "NEXUS_KERNEL_AI_API_KEY",
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


def test_chat_config_defaults_provider_and_redacts_secrets() -> None:
    config = ChatModuleConfig.model_validate(
        {
            "service_bearer_token": "service-secret",  # security-test-fixture
            "ai_api_key": "test-api-key-secret",  # security-test-fixture
        }
    )

    assert config.provider_id == "openai_compatible"
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

    async def start(self) -> None:
        self.events.append("kernel:start")

    async def stop(self) -> list[str]:
        self.events.append("kernel:stop")
        return self.stop_errors

    def worker_specs(self) -> list[WorkerSpec]:
        return [WorkerSpec("module_worker", _module_worker)]


class _RecordingWorkers:
    def __init__(
        self,
        events: list[str],
        *,
        start_error: Exception | None = None,
        stop_errors: list[str] | None = None,
        stop_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.start_error = start_error
        self.stop_errors = stop_errors or []
        self.stop_error = stop_error

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
