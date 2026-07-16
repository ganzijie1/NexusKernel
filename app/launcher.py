"""Container entry point selecting API or Worker from strict runtime config."""

from __future__ import annotations

import asyncio

from app.bootstrap import build_runtime
from app.routes import create_chat_router
from app.worker import run_worker
from nexus_kernel.gateway import create_app
from nexus_kernel.observability import configure_logging


def main() -> None:
    runtime = build_runtime()
    configure_logging(
        level=runtime.settings.logging.level,
        serialize=runtime.settings.logging.serialize,
    )
    if runtime.settings.runtime.role == "worker":
        asyncio.run(run_worker(runtime))
        return

    import uvicorn

    routers = (
        (create_chat_router(runtime.chat_service),) if runtime.chat_enabled else ()
    )
    application = create_app(
        settings=runtime.settings,
        kernel=runtime.kernel,
        routers=routers,
        metrics=runtime.metrics,
    )
    uvicorn.run(
        application,
        host=runtime.settings.server.host,
        port=runtime.settings.server.port,
        proxy_headers=True,
        log_config=None,
    )


if __name__ == "__main__":
    main()
