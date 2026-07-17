from __future__ import annotations

from types import MappingProxyType

import pytest

from nexus_kernel.ai import (
    AdkApplicationRegistry,
    AdkApplicationSpec,
    WorkflowNodeResult,
    WorkflowActionSpec,
    WorkflowInvocationResult,
    WorkflowRequestInputNodeSpec,
    WorkflowTransformNodeSpec,
)


def _factory(context: object) -> object:
    return object()


def _identity_transform(value: object) -> WorkflowNodeResult:
    return WorkflowNodeResult(output=value)  # type: ignore[arg-type]


def _constant_transform(value: object) -> WorkflowNodeResult:
    del value
    return WorkflowNodeResult(output="constant")


def _alternate_factory(context: object) -> object:
    del context
    return {"application": "alternate"}


def test_application_registry_is_frozen_and_fingerprints_public_contract() -> None:
    action = WorkflowActionSpec(
        code="provide_details",
        kind="input",
        response_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    application = AdkApplicationSpec(
        agent_id="research_team",
        revision="v1",
        kind="team",
        factory=_factory,
        action_specs=(action,),
        resumable=True,
        output_format="json",
    )
    registry = AdkApplicationRegistry((application,))

    assert registry.resolve("research_team") is application
    assert registry.resolve("missing") is None
    assert len(application.fingerprint) == 64
    assert isinstance(action.response_schema, MappingProxyType)
    with pytest.raises(TypeError):
        action.response_schema["type"] = "string"  # type: ignore[index]


def test_application_contract_rejects_unsafe_action_and_result_shapes() -> None:
    with pytest.raises(ValueError, match="valid JSON Schema"):
        WorkflowActionSpec(
            code="details",
            kind="input",
            response_schema={"type": "not-a-json-type"},
        )
    with pytest.raises(ValueError, match="action_specs"):
        AdkApplicationSpec(
            agent_id="team",
            revision="v1",
            kind="team",
            factory=_factory,
            resumable=True,
        )
    with pytest.raises(ValueError, match="strict JSON"):
        WorkflowInvocationResult(
            state="succeeded", output=object(), output_format="json"
        )

    assert (
        WorkflowInvocationResult(
            state="succeeded", output=None, output_format="json"
        ).output
        is None
    )

    boolean_schema = WorkflowActionSpec(
        code="deny_input",
        kind="input",
        response_schema=False,
    )
    assert boolean_schema.response_schema is False


def test_function_node_contract_is_frozen_allowlisted_and_fingerprinted() -> None:
    transform = WorkflowTransformNodeSpec(
        name="classify",
        transform=_identity_transform,
        input_schema={"type": "string"},
        output_schema={"type": "string"},
        routes=("accepted", "rejected"),
        timeout_seconds=2,
        max_attempts=2,
    )
    request = WorkflowRequestInputNodeSpec(
        name="approval",
        action_code="confirm",
    )
    action = WorkflowActionSpec(
        code="confirm",
        kind="confirmation",
        response_schema={"type": "boolean"},
    )
    first = AdkApplicationSpec(
        agent_id="review_flow",
        revision="v1",
        kind="graph",
        factory=_factory,
        action_specs=(action,),
        function_nodes=(transform, request),
        resumable=True,
    )
    changed = AdkApplicationSpec(
        agent_id="review_flow",
        revision="v1",
        kind="graph",
        factory=_factory,
        action_specs=(action,),
        function_nodes=(
            WorkflowTransformNodeSpec(
                name="classify",
                transform=_identity_transform,
                input_schema={"type": "string"},
                output_schema={"type": "string"},
                routes=("accepted",),
                timeout_seconds=2,
                max_attempts=2,
            ),
            request,
        ),
        resumable=True,
    )

    assert first.fingerprint != changed.fingerprint
    assert isinstance(transform.input_schema, MappingProxyType)
    with pytest.raises(ValueError, match="reference an action_spec"):
        AdkApplicationSpec(
            agent_id="invalid_flow",
            revision="v1",
            kind="graph",
            factory=_factory,
            function_nodes=(request,),
        )
    with pytest.raises(ValueError, match="only for graph"):
        AdkApplicationSpec(
            agent_id="invalid_team",
            revision="v1",
            kind="team",
            factory=_factory,
            function_nodes=(transform,),
        )


def test_application_fingerprint_tracks_factory_and_transform_semantics() -> None:
    original = AdkApplicationSpec(
        agent_id="semantic_flow",
        revision="v1",
        kind="graph",
        factory=_factory,
        function_nodes=(
            WorkflowTransformNodeSpec(
                name="transform",
                transform=_identity_transform,
            ),
        ),
    )
    equivalent = AdkApplicationSpec(
        agent_id="semantic_flow",
        revision="v1",
        kind="graph",
        factory=_factory,
        function_nodes=(
            WorkflowTransformNodeSpec(
                name="transform",
                transform=_identity_transform,
            ),
        ),
    )
    changed_factory = AdkApplicationSpec(
        agent_id="semantic_flow",
        revision="v1",
        kind="graph",
        factory=_alternate_factory,
        function_nodes=original.function_nodes,
    )
    changed_transform = AdkApplicationSpec(
        agent_id="semantic_flow",
        revision="v1",
        kind="graph",
        factory=_factory,
        function_nodes=(
            WorkflowTransformNodeSpec(
                name="transform",
                transform=_constant_transform,
            ),
        ),
    )

    assert original.fingerprint == equivalent.fingerprint
    assert original.fingerprint != changed_factory.fingerprint
    assert original.fingerprint != changed_transform.fingerprint


def test_transform_result_rejects_null_nonfinite_and_invalid_routes() -> None:
    with pytest.raises(ValueError, match="must not be null"):
        WorkflowNodeResult(output=None)
    with pytest.raises(ValueError, match="finite"):
        WorkflowNodeResult(output={"value": float("inf")})
    with pytest.raises(ValueError, match="route"):
        WorkflowNodeResult(output=True, route="Not Allowed")

    frozen = WorkflowNodeResult(output=[1, {"nested": [2]}])
    assert WorkflowNodeResult(output=frozen.output).output == frozen.output
