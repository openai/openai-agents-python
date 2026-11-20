import asyncio
import sys
from collections.abc import Callable

if sys.platform == "win32":
    # Importing Modal during collection changes the Windows loop policy.
    # The platform's default Proactor loop supports subprocess pipes.
    def pytest_asyncio_loop_factories() -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
        return {"proactor": asyncio.ProactorEventLoop}
