"""Worker-role process lifecycle using the same Kernel as the API role."""

from __future__ import annotations

import asyncio
import signal

from app.bootstrap import ApplicationRuntime, build_runtime
from nexus_kernel.observability import configure_logging


async def run_worker(
    runtime: ApplicationRuntime | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    runtime = runtime or build_runtime()
    stop_event = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    await runtime.start()
    stop_task = asyncio.create_task(stop_event.wait())
    failure_task = asyncio.create_task(runtime.workers.wait_required_failure())
    primary_error: BaseException | None = None
    try:
        done, _ = await asyncio.wait(
            {stop_task, failure_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if failure_task in done:
            await failure_task
    except BaseException as exc:
        primary_error = exc

    for task in (stop_task, failure_task):
        if not task.done():
            task.cancel()
    await asyncio.gather(stop_task, failure_task, return_exceptions=True)

    try:
        await runtime.stop()
    except BaseException:
        if primary_error is None:
            raise
    if primary_error is not None:
        raise primary_error


def main() -> None:
    runtime = build_runtime()
    configure_logging(
        level=runtime.settings.logging.level,
        serialize=runtime.settings.logging.serialize,
    )
    asyncio.run(run_worker(runtime))


if __name__ == "__main__":
    main()
