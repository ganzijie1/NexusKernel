"""Stable AI and Chat error types that never embed provider payloads."""

from nexuskernel.errors import FrameworkError


class AiProviderError(FrameworkError):
    """Raised when an AI SDK call fails."""


class AiTimeoutError(AiProviderError):
    """Raised when an AI SDK call exceeds its configured timeout."""


class AuthNotConfiguredError(FrameworkError):
    """Raised when the service Bearer token has not been configured."""


class UnauthorizedError(FrameworkError):
    """Raised when a request does not present the configured Bearer token."""


class AiProviderUnavailableError(FrameworkError):
    """Raised when Chat has no usable AI runtime."""
