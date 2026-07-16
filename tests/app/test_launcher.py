from __future__ import annotations

from app.bootstrap import ApplicationRuntime
from app.services import DisabledChatService
from nexus_kernel.config import ApplicationSettings
from nexus_kernel.kernel import Kernel
from nexus_kernel.observability import MetricsRegistry
from nexus_kernel.runtime import WorkerSupervisor


def test_launcher_omits_chat_router_when_module_is_disabled(monkeypatch) -> None:
    runtime = ApplicationRuntime(
        settings=ApplicationSettings(),
        kernel=Kernel([]),
        chat_service=DisabledChatService(),
        workers=WorkerSupervisor(),
        metrics=MetricsRegistry("test"),
        chat_enabled=False,
    )
    captured: dict[str, object] = {}

    def fake_create_app(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("app.launcher.build_runtime", lambda: runtime)
    monkeypatch.setattr("app.launcher.configure_logging", lambda **kwargs: None)
    monkeypatch.setattr("app.launcher.create_app", fake_create_app)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)

    from app.launcher import main

    main()

    assert captured["routers"] == ()
    assert captured["metrics"] is runtime.metrics
