"""Application-owned production registry for asynchronous ADK agents."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from fastapi import Request

from nexus_kernel.ai import AdkApplicationSpec, AgentIdentity, AgentToolSpec


AgentAuthorizer = Callable[[AgentIdentity, AdkApplicationSpec], bool | Awaitable[bool]]


class TrustedIdentityResolver(Protocol):
    async def resolve(self, request: Request) -> AgentIdentity: ...


@dataclass(frozen=True, slots=True)
class AgentRunsRegistration:
    applications: tuple[AdkApplicationSpec, ...] = ()
    tools: tuple[AgentToolSpec, ...] = ()
    authorizer: AgentAuthorizer | None = None
    identity_resolver: TrustedIdentityResolver | None = None


def registered_adk_applications() -> tuple[AdkApplicationSpec, ...]:
    """Return production definitions registered by this application."""

    return ()


def registered_adk_tools() -> tuple[AgentToolSpec, ...]:
    """Return app-owned tool adapters referenced by registered definitions."""

    return ()


def authorize_registered_adk_application(
    identity: AgentIdentity, application: AdkApplicationSpec
) -> bool:
    """Fail closed until the application supplies its authorization policy."""

    del identity, application
    return False


def registered_agent_runs() -> AgentRunsRegistration:
    """Build the immutable production registration passed to every entry point."""

    return AgentRunsRegistration(
        applications=registered_adk_applications(),
        tools=registered_adk_tools(),
        authorizer=authorize_registered_adk_application,
    )


__all__ = [
    "AgentAuthorizer",
    "AgentRunsRegistration",
    "TrustedIdentityResolver",
    "authorize_registered_adk_application",
    "registered_adk_applications",
    "registered_adk_tools",
    "registered_agent_runs",
]
