"""Public exception hierarchy for NexusKernel framework contracts."""


class FrameworkError(Exception):
    """Base exception for framework-level failures."""


class ConfigError(FrameworkError):
    """Raised when framework configuration cannot be loaded or validated."""


class ModuleDependencyError(FrameworkError):
    """Raised when a module dependency cannot be satisfied."""


class ModuleLookupError(FrameworkError):
    """Raised when a requested module is not registered."""


class ProviderError(FrameworkError):
    """Raised when a provider cannot be initialized or managed."""


class ProviderUnavailableError(ProviderError):
    """Raised when a required provider is unavailable."""
