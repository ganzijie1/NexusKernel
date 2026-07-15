from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.bootstrap import create_application
from app.modules.chat import ChatModuleConfig


def test_default_application_starts_degraded_without_credentials(
    monkeypatch,
) -> None:
    for name in (
        "NEXUSKERNEL_SERVICE_BEARER_TOKEN",
        "NEXUSKERNEL_AI_API_KEY",
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


def test_chat_config_defaults_provider_and_redacts_secrets() -> None:
    config = ChatModuleConfig.model_validate(
        {
            "service_bearer_token": "service-secret",
            "ai_api_key": "test-api-key-secret",
        }
    )

    assert config.provider_id == "openai_compatible"
    assert "service-secret" not in repr(config)
    assert "test-api-key-secret" not in repr(config)


def test_chat_config_rejects_credential_bearing_base_url() -> None:
    try:
        ChatModuleConfig.model_validate(
            {"base_url": "https://user:password@models.example/v1"}
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("credential-bearing URL must be rejected")
