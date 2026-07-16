"""Application-owned trusted identity resolution for Chat requests."""

from __future__ import annotations

import hmac
from typing import Protocol

from fastapi import Request
from pydantic import SecretStr

from nexus_kernel.ai import AgentIdentity
from nexus_kernel.errors import FrameworkError


class AuthNotConfiguredError(FrameworkError):
    """Raised when the application Bearer token is not configured."""


class UnauthorizedError(FrameworkError):
    """Raised when a request does not present the configured Bearer token."""


class TrustedIdentityResolver(Protocol):
    async def resolve(self, request: Request) -> AgentIdentity: ...


class BearerIdentityResolver:
    """Resolve one service principal after constant-time Bearer verification."""

    def __init__(
        self, service_token: str | None, *, principal_id: str = "service"
    ) -> None:
        self._service_token = SecretStr(service_token) if service_token else None
        self._identity = AgentIdentity(principal_id)

    async def resolve(self, request: Request) -> AgentIdentity:
        if self._service_token is None:
            raise AuthNotConfiguredError("Chat authentication is not configured")
        authorization = request.headers.get("authorization")
        if authorization is None or not authorization.startswith("Bearer "):
            raise UnauthorizedError("Chat request is unauthorized")
        presented = authorization.removeprefix("Bearer ")
        expected = self._service_token.get_secret_value()
        if not presented or not hmac.compare_digest(presented, expected):
            raise UnauthorizedError("Chat request is unauthorized")
        return self._identity
