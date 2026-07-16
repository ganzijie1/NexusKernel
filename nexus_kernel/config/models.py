"""Strict configuration models used by NexusKernel at runtime."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ServiceSettings(_StrictModel):
    name: str = "NexusKernel Starter"
    version: str = "0.2.0"


class ServerSettings(_StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8030, ge=1, le=65535)
    max_request_body_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    request_timeout_seconds: float = Field(default=30.0, gt=0)


class RuntimeSettings(_StrictModel):
    role: Literal["api", "worker"] = "api"


class MonitoringSettings(_StrictModel):
    metrics_prefix: str = "nexus_kernel"
    metrics_cache_ttl_seconds: float = Field(default=5.0, gt=0)
    health_timeout_seconds: float = Field(default=5.0, gt=0)


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
    config: dict[str, Any] = Field(default_factory=dict)


class ModulesSettings(_StrictModel):
    modules: dict[str, ModuleDefinition]

    @field_validator("modules")
    @classmethod
    def validate_module_names(
        cls, modules: dict[str, ModuleDefinition]
    ) -> dict[str, ModuleDefinition]:
        if any(not name.strip() for name in modules):
            raise ValueError("module names must not be empty")
        return modules
