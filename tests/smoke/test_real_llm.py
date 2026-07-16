from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import create_application


@pytest.mark.real_llm
def test_real_llm_chat_when_explicitly_enabled() -> None:
    if os.getenv("RUN_REAL_LLM_SMOKE") != "true":
        pytest.skip("real LLM smoke requires RUN_REAL_LLM_SMOKE=true")
    if not os.getenv("NEXUS_KERNEL_AI_API_KEY"):
        pytest.skip("real LLM smoke requires NEXUS_KERNEL_AI_API_KEY")
    service_token = os.getenv("NEXUS_KERNEL_SERVICE_BEARER_TOKEN")
    if not service_token:
        pytest.fail("real LLM smoke requires NEXUS_KERNEL_SERVICE_BEARER_TOKEN")

    with TestClient(create_application(), raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/chat",
            headers={"authorization": f"Bearer {service_token}"},
            json={"messages": [{"role": "user", "content": "Reply with OK."}]},
        )

    assert response.status_code == 200
    assert response.json()["data"]["message"]["content"]
