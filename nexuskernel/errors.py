"""Public exception hierarchy for NexusKernel framework contracts."""


class FrameworkError(Exception):
    """Base exception for framework-level failures."""


class ConfigError(FrameworkError):
    """Raised when framework configuration cannot be loaded or validated."""


class ModuleDependencyError(FrameworkError):
    """Raised when a module dependency cannot be satisfied."""


class ModuleLookupError(FrameworkError):
    """Raised when a requested module is not registered."""


class ModuleAccessError(FrameworkError, AttributeError):
    """Raised when a module proxy is asked for a non-contract attribute."""


class KernelStartupError(FrameworkError):
    """Raised when a required runtime component cannot be started."""


class WorkerError(FrameworkError):
    """Raised when a background worker cannot be registered or supervised."""


class ProviderError(FrameworkError):
    """Raised when a provider cannot be initialized or managed."""


class ProviderUnavailableError(ProviderError):
    """Raised when a required provider is unavailable."""
