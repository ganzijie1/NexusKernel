"""Stable AI and Chat error types that never embed provider payloads."""

from nexus_kernel.errors import FrameworkError


class AiProviderError(FrameworkError):
    """Raised when an AI SDK call fails."""


class AiTimeoutError(AiProviderError):
    """Raised when an AI SDK call exceeds its configured timeout."""


class AiToolError(AiProviderError):
    """Raised when a registered server tool cannot be executed safely."""


class AiWorkflowActionUnsupportedError(AiProviderError):
    """Raised when a workflow requests an unregistered interactive action."""
