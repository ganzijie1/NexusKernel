"""Worker-role process lifecycle using the same Kernel as the API role."""

from __future__ import annotations

import asyncio
import signal

from app.bootstrap import ApplicationRuntime, build_runtime


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

    await runtime.kernel.start()
    try:
        await stop_event.wait()
    finally:
        await runtime.kernel.stop()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
