"""FastAPI gateway factory and response contracts."""

from nexus_kernel.gateway.app import ApiError, create_app
from nexus_kernel.gateway.responses import (
    ErrorDetail,
    ErrorEnvelope,
    ResponseMeta,
    SuccessEnvelope,
    error_response,
    success_response,
)

__all__ = [
    "ApiError",
    "ErrorDetail",
    "ErrorEnvelope",
    "ResponseMeta",
    "SuccessEnvelope",
    "create_app",
    "error_response",
    "success_response",
]
