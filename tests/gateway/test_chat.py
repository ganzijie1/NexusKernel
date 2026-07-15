from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.services.chat import AppChatService
from nexuskernel.ai import ChatMessage, ChatRequest, ChatResponse, TokenUsage
from nexuskernel.config import ApplicationSettings
from nexuskernel.gateway import create_app
from nexuskernel.kernel import Kernel
from nexuskernel.providers import ProviderRegistry, ProviderSpec


class FakeModelClient:
    provider_id = "openai_compatible"

    def __init__(self) -> None:
        self.closed = False

    async def start(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def chat(
        self, request: ChatRequest, tools: tuple[Any, ...] = ()
    ) -> ChatResponse:
        return ChatResponse(
            message=ChatMessage(role="assistant", content="safe answer"),
            usage=TokenUsage(input_tokens=2, output_tokens=3, total_tokens=5),
            model="fake",
            provider_id=self.provider_id,
            finish_reason="stop",
        )

    async def close(self) -> None:
        self.closed = True


def _settings() -> ApplicationSettings:
    return ApplicationSettings()


def test_chat_auth_and_provider_availability_contracts() -> None:
    no_auth_registry = ProviderRegistry()
    no_auth_service = AppChatService(no_auth_registry, service_token=None)
    no_auth_app = create_app(
        settings=_settings(),
        kernel=Kernel([], providers=no_auth_registry),
        chat_service=no_auth_service,
    )
    with TestClient(no_auth_app, raise_server_exceptions=False) as client:
        no_auth = client.post(
            "/api/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    assert no_auth.status_code == 503
    assert no_auth.json()["error"]["code"] == "auth_not_configured"

    unavailable_registry = ProviderRegistry()
    unavailable_service = AppChatService(
        unavailable_registry, service_token="service-secret"
    )
    unavailable_app = create_app(
        settings=_settings(),
        kernel=Kernel([], providers=unavailable_registry),
        chat_service=unavailable_service,
    )
    with TestClient(unavailable_app, raise_server_exceptions=False) as client:
        missing = client.post(
            "/api/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
        wrong = client.post(
            "/api/v1/chat",
            headers={"authorization": "Bearer wrong"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        unavailable = client.post(
            "/api/v1/chat",
            headers={"authorization": "Bearer service-secret"},
            json={"messages": [{"role": "user", "content": "private prompt"}]},
        )

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    assert wrong.status_code == 401
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "ai_provider_unavailable"
    assert "private prompt" not in unavailable.text


def test_chat_success_and_ready_degradation_without_credentials() -> None:
    model = FakeModelClient()
    registry = ProviderRegistry()
    registry.register(ProviderSpec("ai_runtime", lambda: model, required=False))
    service = AppChatService(registry, service_token="service-secret")
    app = create_app(
        settings=_settings(),
        kernel=Kernel([], providers=registry),
        chat_service=service,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/chat",
            headers={"authorization": "Bearer service-secret"},
            json={"messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 200
    assert response.json()["data"]["message"]["content"] == "safe answer"
    assert response.json()["data"]["usage"]["total_tokens"] == 5
    assert model.closed is True

    degraded_registry = ProviderRegistry()
    degraded_registry.register(
        ProviderSpec(
            "ai_runtime",
            lambda: (_ for _ in ()).throw(RuntimeError("missing credential")),
            required=False,
        )
    )
    degraded_app = create_app(
        settings=_settings(),
        kernel=Kernel([], providers=degraded_registry),
        chat_service=AppChatService(degraded_registry, service_token="service-secret"),
    )
    with TestClient(degraded_app, raise_server_exceptions=False) as client:
        ready = client.get("/health/ready")

    assert ready.status_code == 200
    assert ready.json()["data"]["status"] == "degraded"
