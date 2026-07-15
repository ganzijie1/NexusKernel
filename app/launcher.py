"""Container entry point selecting API or Worker from strict runtime config."""

from __future__ import annotations

import asyncio

from app.bootstrap import build_runtime
from app.worker import run_worker
from nexuskernel.gateway import create_app


def main() -> None:
    runtime = build_runtime()
    if runtime.settings.runtime.role == "worker":
        asyncio.run(run_worker(runtime))
        return

    import uvicorn

    application = create_app(
        settings=runtime.settings,
        kernel=runtime.kernel,
        chat_service=runtime.chat_service,
    )
    uvicorn.run(
        application,
        host=runtime.settings.server.host,
        port=runtime.settings.server.port,
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
