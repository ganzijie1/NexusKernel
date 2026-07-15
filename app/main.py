"""ASGI entry point used by local development and the API container role."""

from nexuskernel.gateway import create_app

app = create_app()
