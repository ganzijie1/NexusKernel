"""Lazy, side-effect-free Agent Runs definitions for local validation."""

from __future__ import annotations

from typing import Any, cast

from nexus_kernel.ai import (
    AdkApplicationBuildContext,
    AdkApplicationSpec,
)


def _sample_graph_factory(context: AdkApplicationBuildContext) -> object:
    from google.adk.agents import LlmAgent
    from google.adk.apps import App
    from google.adk.workflow import JoinNode, START, Workflow

    model = cast(Any, context.model)
    left = LlmAgent(name="left", model=model, mode="single_turn")
    right = LlmAgent(name="right", model=model, mode="single_turn")
    joined = JoinNode(name="joined")
    workflow = Workflow(
        name="sample_graph",
        edges=[(START, (left, right), joined)],
        max_concurrency=2,
    )
    return App(name="sample_graph", root_agent=workflow)


def _sample_team_factory(context: AdkApplicationBuildContext) -> object:
    from google.adk.agents import LlmAgent
    from google.adk.apps import App

    model = cast(Any, context.model)
    task = LlmAgent(name="task", model=model, mode="task")
    single_turn = LlmAgent(name="single_turn", model=model, mode="single_turn")
    coordinator = LlmAgent(
        name="coordinator",
        model=model,
        mode="chat",
        sub_agents=[task, single_turn],
    )
    return App(name="sample_team", root_agent=coordinator)


def default_adk_applications() -> tuple[AdkApplicationSpec, ...]:
    """Return local examples without importing ADK or constructing SDK objects."""

    return (
        AdkApplicationSpec(
            agent_id="sample_graph",
            revision="v1",
            kind="graph",
            factory=_sample_graph_factory,
            output_format="json",
        ),
        AdkApplicationSpec(
            agent_id="sample_team",
            revision="v1",
            kind="team",
            factory=_sample_team_factory,
        ),
    )


__all__ = ["default_adk_applications"]
