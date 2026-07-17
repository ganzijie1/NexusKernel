from __future__ import annotations

import importlib
import sys

from app.agent_applications import AgentRunsRegistration
from app.bootstrap import ApplicationRuntime
from app.services import DisabledChatService
from nexus_kernel.config import ApplicationSettings
from nexus_kernel.kernel import Kernel
from nexus_kernel.observability import MetricsRegistry
from nexus_kernel.runtime import WorkerSupervisor


def test_launcher_builds_api_from_the_complete_application_runtime(monkeypatch) -> None:
    runtime = ApplicationRuntime(
        settings=ApplicationSettings(),
        kernel=Kernel([]),
        chat_service=DisabledChatService(),
        workers=WorkerSupervisor(),
        metrics=MetricsRegistry("test"),
        chat_enabled=False,
    )
    captured: list[ApplicationRuntime] = []
    captured_registration: list[dict[str, object]] = []
    application = object()
    agent_application = object()
    tool = object()
    authorizer = object()
    resolver = object()
    registration = AgentRunsRegistration(
        applications=(agent_application,),  # type: ignore[arg-type]
        tools=(tool,),  # type: ignore[arg-type]
        authorizer=authorizer,  # type: ignore[arg-type]
        identity_resolver=resolver,  # type: ignore[arg-type]
    )

    def fake_create_runtime_application(target: ApplicationRuntime):
        captured.append(target)
        return application

    def fake_build_runtime(**kwargs):
        captured_registration.append(kwargs)
        return runtime

    def registration_factory() -> AgentRunsRegistration:
        return registration

    monkeypatch.setattr("app.launcher.registered_agent_runs", registration_factory)
    monkeypatch.setattr("app.launcher.build_runtime", fake_build_runtime)
    monkeypatch.setattr("app.launcher.configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(
        "app.launcher.create_runtime_application",
        fake_create_runtime_application,
    )
    uvicorn_calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        "uvicorn.run",
        lambda target, **kwargs: uvicorn_calls.append((target, kwargs)),
    )

    from app.launcher import main

    main()

    assert captured == [runtime]
    assert captured_registration == [
        {
            "agent_runs_registration": registration_factory,
        }
    ]
    assert uvicorn_calls[0][0] is application


def test_asgi_entrypoint_uses_the_application_registry(monkeypatch) -> None:
    marker = object()
    registration = AgentRunsRegistration(
        applications=(marker,),  # type: ignore[arg-type]
    )
    captured: list[dict[str, object]] = []

    def registration_factory() -> AgentRunsRegistration:
        return registration

    monkeypatch.setattr(
        "app.agent_applications.registered_agent_runs", registration_factory
    )
    monkeypatch.setattr(
        "app.bootstrap.create_application",
        lambda **kwargs: captured.append(kwargs) or object(),
    )
    sys.modules.pop("app.main", None)
    try:
        importlib.import_module("app.main")
    finally:
        sys.modules.pop("app.main", None)

    assert captured == [{"agent_runs_registration": registration_factory}]
