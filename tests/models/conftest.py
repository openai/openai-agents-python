import asyncio
import sys

import pytest


def pytest_asyncio_loop_factories(item: pytest.Item):
    if sys.platform == "win32" and item.path.name == "test_codex_app_server.py":
        # These tests require subprocess pipes, which the Windows selector loop lacks.
        return {"default": asyncio.ProactorEventLoop}
    return {"default": asyncio.new_event_loop}
