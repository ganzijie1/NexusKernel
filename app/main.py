"""ASGI entry point used by local development and the API container role."""

from app.agent_applications import registered_agent_runs
from app.bootstrap import create_application

app = create_application(
    agent_runs_registration=registered_agent_runs,
)
