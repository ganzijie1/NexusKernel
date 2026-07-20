"""Strict configuration models used by NexusKernel at runtime."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ServiceSettings(_StrictModel):
    name: str = "NexusKernel Starter"
    version: str = "0.2.0"


class ServerSettings(_StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8030, ge=1, le=65535)
    max_request_body_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_response_buffer_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    request_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)


class RuntimeSettings(_StrictModel):
    role: Literal["api", "worker"] = "api"
    module_cleanup_timeout_seconds: float = Field(
        default=5.0, gt=0, allow_inf_nan=False
    )


class MonitoringSettings(_StrictModel):
    metrics_prefix: str = "nexus_kernel"
    metrics_cache_ttl_seconds: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    health_timeout_seconds: float = Field(default=5.0, gt=0, allow_inf_nan=False)


class LoggingSettings(_StrictModel):
    level: str = "INFO"
    serialize: bool = False
    request_log_enabled: bool = True


class CorsSettings(_StrictModel):
    enabled: bool = True
    allow_origins: list[str] = Field(default_factory=lambda: ["*"])
    allow_methods: list[str] = Field(default_factory=lambda: ["*"])
    allow_headers: list[str] = Field(default_factory=lambda: ["*"])


class ApplicationSettings(_StrictModel):
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)


class ModuleDefinition(_StrictModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)

    enabled: bool = True
    class_path: str = Field(alias="class", min_length=1)
    required: bool = True
    dependencies: list[str] = Field(default_factory=list)
    optional_dependencies: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("dependencies", "optional_dependencies")
    @classmethod
    def validate_dependency_names(cls, names: list[str]) -> list[str]:
        if any(not name.strip() for name in names):
            raise ValueError("module dependency names must not be empty")
        if len(set(names)) != len(names):
            raise ValueError("module dependency names must not be duplicated")
        return names

    @model_validator(mode="after")
    def validate_dependency_categories(self) -> ModuleDefinition:
        if set(self.dependencies).intersection(self.optional_dependencies):
            raise ValueError(
                "required and optional module dependencies must not overlap"
            )
        return self


class ModulesSettings(_StrictModel):
    modules: dict[str, ModuleDefinition]

    @field_validator("modules")
    @classmethod
    def validate_module_names(
        cls, modules: dict[str, ModuleDefinition]
    ) -> dict[str, ModuleDefinition]:
        if any(not name.strip() for name in modules):
            raise ValueError("module names must not be empty")
        for name, definition in modules.items():
            if (
                name in definition.dependencies
                or name in definition.optional_dependencies
            ):
                raise ValueError("modules must not depend on themselves")
        return modules
