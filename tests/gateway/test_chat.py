from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.routes.chat import create_chat_router
from app.services.chat import AppChatService
from nexus_kernel.ai import (
    AgentIdentity,
    AiProviderError,
    AiTimeoutError,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    TokenUsage,
)
from nexus_kernel.config import ApplicationSettings
from nexus_kernel.gateway import create_app
from nexus_kernel.kernel import Kernel
from nexus_kernel.providers import ProviderRegistry, ProviderSpec


class FakeModelClient:
    provider_id = "openai_compatible"

    def __init__(self) -> None:
        self.closed = False
        self.calls: list[dict[str, Any]] = []

    async def start(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def chat(
        self,
        request: ChatRequest,
        *,
        identity: AgentIdentity,
        tools: tuple[Any, ...] = (),
    ) -> ChatResponse:
        await asyncio.sleep(0)
        self.calls.append({"identity": identity, "tools": tools})
        return ChatResponse(
            message=ChatMessage(role="assistant", content="safe answer"),
            usage=TokenUsage(input_tokens=2, output_tokens=3, total_tokens=5),
            model="fake",
            provider_id=self.provider_id,
            finish_reason="stop",
        )

    async def close(self) -> None:
        self.closed = True


class FakeAgentRuntime:
    provider_id = "google_adk"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def start(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def run(
        self,
        request: ChatRequest,
        *,
        identity: AgentIdentity,
        session_id: str | None = None,
        tools: tuple[Any, ...] = (),
    ) -> ChatResponse:
        await asyncio.sleep(0)
        self.calls.append(
            {"identity": identity, "session_id": session_id, "tools": tools}
        )
        if self.error is not None:
            raise self.error
        return ChatResponse(
            message=ChatMessage(role="assistant", content="agent answer"),
            usage=TokenUsage(),
            model="fake",
            provider_id=self.provider_id,
        )

    async def close(self) -> None:
        return None


class TenantResolver:
    def __init__(self, principal_id: str) -> None:
        self.principal_id = principal_id

    async def resolve(self, request: Request) -> AgentIdentity:
        return AgentIdentity(self.principal_id)


class HeaderTenantResolver:
    async def resolve(self, request: Request) -> AgentIdentity:
        return AgentIdentity(request.headers["x-tenant"])


def _settings() -> ApplicationSettings:
    return ApplicationSettings()


def test_chat_auth_and_provider_availability_contracts() -> None:
    no_auth_registry = ProviderRegistry()
    no_auth_service = AppChatService(no_auth_registry, service_token=None)
    no_auth_app = create_app(
        settings=_settings(),
        kernel=Kernel([], providers=no_auth_registry),
        routers=(create_chat_router(no_auth_service),),
    )
    with TestClient(no_auth_app, raise_server_exceptions=False) as client:
        no_auth = client.post(
            "/api/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    assert no_auth.status_code == 503
    assert no_auth.json()["error"]["code"] == "auth_not_configured"

    unavailable_registry = ProviderRegistry()
    unavailable_service = AppChatService(
        unavailable_registry,
        service_token="service-secret",  # security-test-fixture
    )
    unavailable_app = create_app(
        settings=_settings(),
        kernel=Kernel([], providers=unavailable_registry),
        routers=(create_chat_router(unavailable_service),),
    )
    with TestClient(unavailable_app, raise_server_exceptions=False) as client:
        missing = client.post(
            "/api/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
        wrong = client.post(
            "/api/v1/chat",
            headers={"authorization": "Bearer wrong"},  # security-test-fixture
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        unavailable = client.post(
            "/api/v1/chat",
            headers={  # security-test-fixture
                "authorization": "Bearer service-secret"  # security-test-fixture
            },
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
    service = AppChatService(
        registry,
        service_token="service-secret",  # security-test-fixture
    )
    app = create_app(
        settings=_settings(),
        kernel=Kernel([], providers=registry),
        routers=(create_chat_router(service),),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/chat",
            headers={  # security-test-fixture
                "authorization": "Bearer service-secret"  # security-test-fixture
            },
            json={"messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 200
    assert response.json()["data"]["message"]["content"] == "safe answer"
    assert response.json()["data"]["usage"]["total_tokens"] == 5
    assert model.calls == [{"identity": AgentIdentity("service"), "tools": ()}]
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
        routers=(
            create_chat_router(
                AppChatService(
                    degraded_registry,
                    service_token="service-secret",  # security-test-fixture
                )
            ),
        ),
    )
    with TestClient(degraded_app, raise_server_exceptions=False) as client:
        ready = client.get("/health/ready")

    assert ready.status_code == 200
    assert ready.json()["data"]["status"] == "degraded"


def test_chat_agent_uses_trusted_identity_and_explicit_session_only() -> None:
    agent = FakeAgentRuntime()
    registry = ProviderRegistry()
    registry.register(ProviderSpec("ai_runtime", lambda: agent, required=False))
    service = AppChatService(
        registry, identity_resolver=TenantResolver("trusted-tenant")
    )
    app = create_app(
        settings=_settings(),
        kernel=Kernel([], providers=registry),
        routers=(create_chat_router(service),),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/chat",
            headers={"x-request-id": "untrusted-request-id"},
            json={
                "session_id": "conversation",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    assert agent.calls == [
        {
            "identity": AgentIdentity("trusted-tenant"),
            "session_id": "conversation",
            "tools": (),
        }
    ]


def test_chat_default_bearer_identity_is_fixed_and_user_id_is_rejected() -> None:
    agent = FakeAgentRuntime()
    registry = ProviderRegistry()
    registry.register(ProviderSpec("ai_runtime", lambda: agent, required=False))
    service = AppChatService(
        registry,
        service_token="service-secret",  # security-test-fixture
    )
    app = create_app(
        settings=_settings(),
        kernel=Kernel([], providers=registry),
        routers=(create_chat_router(service),),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        accepted = client.post(
            "/api/v1/chat",
            headers={
                "authorization": "Bearer service-secret",  # security-test-fixture
                "x-request-id": "caller-controlled",
            },
            json={"messages": [{"role": "user", "content": "hello"}]},
        )
        rejected = client.post(
            "/api/v1/chat",
            headers={  # security-test-fixture
                "authorization": "Bearer service-secret"  # security-test-fixture
            },
            json={
                "user_id": "attacker-selected",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert accepted.status_code == 200
    assert agent.calls[0]["identity"] == AgentIdentity("service")
    assert agent.calls[0]["session_id"] is None
    assert rejected.status_code == 422
    assert len(agent.calls) == 1


def test_chat_maps_timeout_to_504_without_cause_details() -> None:
    agent = FakeAgentRuntime(AiTimeoutError("private upstream detail"))
    registry = ProviderRegistry()
    registry.register(ProviderSpec("ai_runtime", lambda: agent, required=False))
    service = AppChatService(
        registry, identity_resolver=TenantResolver("trusted-tenant")
    )
    app = create_app(
        settings=_settings(),
        kernel=Kernel([], providers=registry),
        routers=(create_chat_router(service),),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/chat",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "ai_timeout"
    assert "private upstream detail" not in response.text


def test_chat_maps_provider_tool_or_store_failure_to_safe_503() -> None:
    agent = FakeAgentRuntime(AiProviderError("private provider detail"))
    registry = ProviderRegistry()
    registry.register(ProviderSpec("ai_runtime", lambda: agent, required=False))
    service = AppChatService(
        registry, identity_resolver=TenantResolver("trusted-tenant")
    )
    app = create_app(
        settings=_settings(),
        kernel=Kernel([], providers=registry),
        routers=(create_chat_router(service),),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/chat",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ai_provider_unavailable"
    assert "private provider detail" not in response.text


def _tenant_request(tenant: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat",
            "headers": [(b"x-tenant", tenant.encode("ascii"))],
        }
    )


@pytest.mark.asyncio
async def test_two_tenants_remain_isolated_on_both_ai_dispatch_paths() -> None:
    request = ChatRequest(
        messages=[ChatMessage(role="user", content="hello")],
        session_id="shared-session",
    )

    model = FakeModelClient()
    model_registry = ProviderRegistry()
    model_registry.register(ProviderSpec("ai_runtime", lambda: model))
    await model_registry.initialize_all()
    model_service = AppChatService(
        model_registry, identity_resolver=HeaderTenantResolver()
    )
    await asyncio.gather(
        model_service.chat(request, _tenant_request("tenant-a")),
        model_service.chat(request, _tenant_request("tenant-b")),
    )
    await model_registry.close_all()

    agent = FakeAgentRuntime()
    agent_registry = ProviderRegistry()
    agent_registry.register(ProviderSpec("ai_runtime", lambda: agent))
    await agent_registry.initialize_all()
    agent_service = AppChatService(
        agent_registry, identity_resolver=HeaderTenantResolver()
    )
    await asyncio.gather(
        agent_service.chat(request, _tenant_request("tenant-a")),
        agent_service.chat(request, _tenant_request("tenant-b")),
    )
    await agent_registry.close_all()

    assert {call["identity"] for call in model.calls} == {
        AgentIdentity("tenant-a"),
        AgentIdentity("tenant-b"),
    }
    assert {(call["identity"], call["session_id"]) for call in agent.calls} == {
        (AgentIdentity("tenant-a"), "shared-session"),
        (AgentIdentity("tenant-b"), "shared-session"),
    }


def test_chat_rejects_unsupported_message_shapes_and_coercion() -> None:
    model = FakeModelClient()
    registry = ProviderRegistry()
    registry.register(ProviderSpec("ai_runtime", lambda: model, required=False))
    service = AppChatService(
        registry, identity_resolver=TenantResolver("trusted-tenant")
    )
    app = create_app(
        settings=_settings(),
        kernel=Kernel([], providers=registry),
        routers=(create_chat_router(service),),
    )
    invalid_messages = [
        [{"role": "tool", "content": "external result"}],
        [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "trailing answer"},
        ],
        [
            {"role": "user", "content": "question"},
            {"role": "system", "content": "late instruction"},
            {"role": "user", "content": "again"},
        ],
        [{"role": "user", "content": 123}],
    ]

    with TestClient(app, raise_server_exceptions=False) as client:
        responses = [
            client.post("/api/v1/chat", json={"messages": messages})
            for messages in invalid_messages
        ]
        invalid_sessions = [
            client.post(
                "/api/v1/chat",
                json={
                    "session_id": session_id,
                    "messages": [{"role": "user", "content": "question"}],
                },
            )
            for session_id in ("line\nbreak", "has space", "non-ascii-会话")
        ]

    assert [response.status_code for response in responses] == [422, 422, 422, 422]
    assert [response.status_code for response in invalid_sessions] == [422, 422, 422]
