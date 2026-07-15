"""ASGI entry point used by local development and the API container role."""

from app.bootstrap import create_application

app = create_application()
