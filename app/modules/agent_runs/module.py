"""Agent Runs module configuration and enablement boundary."""

from __future__ import annotations

from pydantic import Field, model_validator

from nexus_kernel.errors import ConfigError
from nexus_kernel.kernel import Module, ModuleConfig


class AgentRunsModuleConfig(ModuleConfig):
    max_entries: int = Field(default=256, ge=1, le=10_000)
    max_concurrent_phases: int = Field(default=4, ge=1, le=64)
    phase_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    overall_lifetime_seconds: float = Field(default=3600.0, gt=0, le=86_400)
    action_timeout_seconds: float = Field(default=900.0, gt=0, le=86_400)
    terminal_retention_seconds: float = Field(default=3600.0, gt=0, le=86_400)
    max_resumes: int = Field(default=16, ge=1, le=128)
    max_payload_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)

    @model_validator(mode="after")
    def validate_deadlines(self) -> AgentRunsModuleConfig:
        if self.phase_timeout_seconds > self.overall_lifetime_seconds:
            raise ValueError("phase timeout must not exceed overall lifetime")
        if self.action_timeout_seconds > self.overall_lifetime_seconds:
            raise ValueError("action timeout must not exceed overall lifetime")
        if self.max_concurrent_phases > self.max_entries:
            raise ValueError("phase concurrency must not exceed run capacity")
        return self


class AgentRunsModule(Module):
    name = "agent_runs"
    required_provider_names = ("ai_runtime",)
    config_schema = AgentRunsModuleConfig

    @property
    def config(self) -> AgentRunsModuleConfig:
        configured = super().config
        if not isinstance(configured, AgentRunsModuleConfig):
            raise ConfigError("Agent Runs module configuration is invalid")
        return configured
