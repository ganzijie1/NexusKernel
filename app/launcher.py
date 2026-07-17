"""Container entry point selecting API or Worker from strict runtime config."""

from __future__ import annotations

import asyncio

from app.agent_applications import registered_agent_runs
from app.bootstrap import build_runtime, create_runtime_application
from app.worker import run_worker
from nexus_kernel.observability import configure_logging


def main() -> None:
    runtime = build_runtime(
        agent_runs_registration=registered_agent_runs,
    )
    configure_logging(
        level=runtime.settings.logging.level,
        serialize=runtime.settings.logging.serialize,
    )
    if runtime.settings.runtime.role == "worker":
        asyncio.run(run_worker(runtime))
        return

    import uvicorn

    application = create_runtime_application(runtime)
    uvicorn.run(
        application,
        host=runtime.settings.server.host,
        port=runtime.settings.server.port,
        proxy_headers=True,
        log_config=None,
    )


if __name__ == "__main__":
    main()
