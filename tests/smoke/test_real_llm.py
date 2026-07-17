from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import create_application
from app.modules.agent_runs.definitions import default_adk_applications
from nexus_kernel.ai import (
    AdkApplicationSpec,
    AgentIdentity,
    GoogleAdkRuntime,
    WorkflowActionResponse,
    WorkflowActionSpec,
    WorkflowInvocation,
    WorkflowNodeResult,
    WorkflowRequestInputNodeSpec,
    WorkflowTransformNodeSpec,
)
from tests.project_contract import load_project_contract


PROJECT = load_project_contract()


def _require_real_adk_environment() -> GoogleAdkRuntime:
    if os.getenv("RUN_REAL_LLM_SMOKE") != "true":
        pytest.skip("real ADK smoke requires RUN_REAL_LLM_SMOKE=true")
    provider_name = PROJECT.environment_name("AI_PROVIDER")
    if os.getenv(provider_name) != "google_adk":
        pytest.fail(f"real ADK smoke requires {provider_name}=google_adk")
    api_key_name = PROJECT.environment_name("AI_API_KEY")
    api_key = os.getenv(api_key_name)
    if not api_key:
        pytest.fail(f"real ADK smoke requires {api_key_name}")
    model_name = PROJECT.environment_name("AI_MODEL")
    model = os.getenv(model_name)
    if not model:
        pytest.fail(f"real ADK smoke requires {model_name}")
    base_url = os.getenv(PROJECT.environment_name("AI_BASE_URL")) or None
    return GoogleAdkRuntime(
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout_seconds=120,
        tool_timeout_seconds=30,
    )


def _managed_route_application() -> AdkApplicationSpec:
    def classify(value: Any) -> WorkflowNodeResult:
        del value
        return WorkflowNodeResult(
            output={"decision": "accepted"},
            route="accepted",
        )

    def finish(value: Any) -> WorkflowNodeResult:
        assert isinstance(value, Mapping)
        return WorkflowNodeResult(output={"complete": value["decision"] == "accepted"})

    def factory(context: Any) -> object:
        from google.adk.apps import App
        from google.adk.workflow import Edge, START, Workflow

        classify_node = context.function_nodes["classify"]
        finish_node = context.function_nodes["finish"]
        return App(
            name="real_smoke_route",
            root_agent=Workflow(
                name="real_smoke_route",
                edges=[
                    Edge(from_node=START, to_node=classify_node),
                    Edge(
                        from_node=classify_node,
                        to_node=finish_node,
                        route="accepted",
                    ),
                ],
                max_concurrency=1,
            ),
        )

    return AdkApplicationSpec(
        agent_id="real_smoke_route",
        revision="v1",
        kind="graph",
        factory=factory,
        function_nodes=(
            WorkflowTransformNodeSpec(
                name="classify",
                transform=classify,
                input_schema={"type": "string"},
                output_schema={"type": "object"},
                routes=("accepted",),
            ),
            WorkflowTransformNodeSpec(
                name="finish",
                transform=finish,
                input_schema={"type": "object"},
                output_schema={"type": "object"},
            ),
        ),
        output_format="json",
    )


def _managed_hitl_application() -> AdkApplicationSpec:
    def finish(value: Any) -> WorkflowNodeResult:
        return WorkflowNodeResult(output={"approved": value})

    def factory(context: Any) -> object:
        from google.adk.apps import App
        from google.adk.workflow import START, Workflow

        return App(
            name="real_smoke_hitl",
            root_agent=Workflow(
                name="real_smoke_hitl",
                edges=[
                    (
                        START,
                        context.function_nodes["approval"],
                        context.function_nodes["finish"],
                    )
                ],
                max_concurrency=1,
            ),
        )

    return AdkApplicationSpec(
        agent_id="real_smoke_hitl",
        revision="v1",
        kind="graph",
        factory=factory,
        action_specs=(
            WorkflowActionSpec(
                code="approve",
                kind="confirmation",
                response_schema={"type": "boolean"},
            ),
        ),
        function_nodes=(
            WorkflowRequestInputNodeSpec(name="approval", action_code="approve"),
            WorkflowTransformNodeSpec(
                name="finish",
                transform=finish,
                input_schema={"type": "boolean"},
                output_schema={"type": "object"},
            ),
        ),
        output_format="json",
        resumable=True,
    )


@pytest.mark.real_llm
def test_real_llm_chat_when_explicitly_enabled() -> None:
    if os.getenv("RUN_REAL_LLM_SMOKE") != "true":
        pytest.skip("real LLM smoke requires RUN_REAL_LLM_SMOKE=true")
    api_key_name = PROJECT.environment_name("AI_API_KEY")
    service_token_name = PROJECT.environment_name("SERVICE_BEARER_TOKEN")
    if not os.getenv(api_key_name):
        pytest.skip(f"real LLM smoke requires {api_key_name}")
    service_token = os.getenv(service_token_name)
    if not service_token:
        pytest.fail(f"real LLM smoke requires {service_token_name}")

    with TestClient(create_application(), raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/chat",
            headers={"authorization": f"Bearer {service_token}"},
            json={"messages": [{"role": "user", "content": "Reply with OK."}]},
        )

    assert response.status_code == 200
    assert response.json()["data"]["message"]["content"]


@pytest.mark.real_llm
@pytest.mark.asyncio
async def test_real_adk_graph_team_and_continuation_when_explicitly_enabled() -> None:
    runtime = _require_real_adk_environment()
    identity = AgentIdentity("real-smoke")
    graph, team = default_adk_applications()
    try:
        graph_result = await runtime.invoke(
            graph,
            WorkflowInvocation(
                session_id="real-smoke-graph",
                text='Return only the JSON object {"ok": true}.',
            ),
            identity=identity,
        )
        first_team_result = await runtime.invoke(
            team,
            WorkflowInvocation(
                session_id="real-smoke-team",
                text="Ask one concise clarification question before answering.",
            ),
            identity=identity,
        )
        continued_team_result = await runtime.invoke(
            team,
            WorkflowInvocation(
                session_id="real-smoke-team",
                text="Continue using this clarification: choose the shortest answer.",
            ),
            identity=identity,
        )
        assert graph_result.state == "succeeded"
        assert first_team_result.state == "succeeded"
        assert continued_team_result.state == "succeeded"
    finally:
        await runtime.close()


@pytest.mark.real_llm
@pytest.mark.asyncio
async def test_real_adk_route_and_hitl_resume_when_explicitly_enabled() -> None:
    runtime = _require_real_adk_environment()
    identity = AgentIdentity("real-smoke")
    route = _managed_route_application()
    hitl = _managed_hitl_application()
    try:
        routed = await runtime.invoke(
            route,
            WorkflowInvocation(session_id="real-smoke-route", text="start"),
            identity=identity,
        )
        pending = await runtime.invoke(
            hitl,
            WorkflowInvocation(session_id="real-smoke-hitl", text="start"),
            identity=identity,
        )
        assert pending.state == "requires_action"
        completed = await runtime.invoke(
            hitl,
            WorkflowInvocation(
                session_id="real-smoke-hitl",
                resume_token=pending.resume_token,
                responses=(
                    WorkflowActionResponse(
                        runtime_action_id=pending.actions[0].runtime_action_id,
                        kind="confirmation",
                        value=True,
                    ),
                ),
            ),
            identity=identity,
        )
        assert routed.output == {"complete": True}
        assert completed.output == {"approved": True}
    finally:
        await runtime.close()
