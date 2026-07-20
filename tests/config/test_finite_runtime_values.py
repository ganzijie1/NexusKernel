from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.modules.agent_runs.module import AgentRunsModuleConfig
from app.modules.chat.module import ChatModuleConfig
from app.providers.config import RedisProviderConfig
from nexus_kernel.config import ApplicationSettings


INVALID_FLOAT_VALUES = (True, math.nan, math.inf, -math.inf)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("server", "request_timeout_seconds"),
        ("runtime", "module_cleanup_timeout_seconds"),
        ("monitoring", "metrics_cache_ttl_seconds"),
        ("monitoring", "health_timeout_seconds"),
    ],
)
@pytest.mark.parametrize("value", INVALID_FLOAT_VALUES)
def test_application_settings_reject_non_finite_runtime_limits(
    section: str, field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        ApplicationSettings.model_validate({section: {field: value}})


@pytest.mark.parametrize("value", INVALID_FLOAT_VALUES)
def test_provider_config_rejects_non_finite_timeout(value: object) -> None:
    with pytest.raises(ValidationError):
        RedisProviderConfig.model_validate({"timeout_seconds": value})


@pytest.mark.parametrize(
    "field",
    [
        "timeout_seconds",
        "tool_timeout_seconds",
        "adk_session_ttl_seconds",
        "adk_session_cleanup_timeout_seconds",
    ],
)
@pytest.mark.parametrize("value", INVALID_FLOAT_VALUES)
def test_chat_config_rejects_non_finite_runtime_limits(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        ChatModuleConfig.model_validate({field: value})


@pytest.mark.parametrize(
    "field",
    [
        "phase_timeout_seconds",
        "overall_lifetime_seconds",
        "action_timeout_seconds",
        "terminal_retention_seconds",
        "poll_interval_seconds",
    ],
)
@pytest.mark.parametrize("value", INVALID_FLOAT_VALUES)
def test_agent_runs_config_rejects_non_finite_runtime_limits(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        AgentRunsModuleConfig.model_validate({field: value})
