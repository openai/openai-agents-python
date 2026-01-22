import asyncio
from typing import Any

import pytest
from mcp.types import CallToolResult, GetPromptResult, ListPromptsResult, Tool as MCPTool

from agents.mcp import MCPServer, MCPServerManager
from agents.run_context import RunContextWrapper


class TaskBoundServer(MCPServer):
    def __init__(self) -> None:
        super().__init__()
        self._connect_task: asyncio.Task[object] | None = None
        self.cleaned = False

    @property
    def name(self) -> str:
        return "task-bound"

    async def connect(self) -> None:
        self._connect_task = asyncio.current_task()

    async def cleanup(self) -> None:
        if self._connect_task is None:
            raise RuntimeError("Server was not connected")
        if asyncio.current_task() is not self._connect_task:
            raise RuntimeError("Attempted to exit cancel scope in a different task")
        self.cleaned = True

    async def list_tools(
        self, run_context: RunContextWrapper[Any] | None = None, agent: Any | None = None
    ) -> list[MCPTool]:
        raise NotImplementedError

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        raise NotImplementedError

    async def list_prompts(self) -> ListPromptsResult:
        raise NotImplementedError

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        raise NotImplementedError


class FlakyServer(MCPServer):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures_remaining = failures
        self.connect_calls = 0

    @property
    def name(self) -> str:
        return "flaky"

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise RuntimeError("connect failed")

    async def cleanup(self) -> None:
        return None

    async def list_tools(
        self, run_context: RunContextWrapper[Any] | None = None, agent: Any | None = None
    ) -> list[MCPTool]:
        raise NotImplementedError

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        raise NotImplementedError

    async def list_prompts(self) -> ListPromptsResult:
        raise NotImplementedError

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_manager_keeps_connect_and_cleanup_in_same_task() -> None:
    server = TaskBoundServer()

    async with MCPServerManager([server]) as manager:
        assert manager.active_servers == [server]

    assert server.cleaned is True


@pytest.mark.asyncio
async def test_manager_connects_in_worker_tasks_when_parallel() -> None:
    server = TaskBoundServer()

    async with MCPServerManager([server], connect_in_parallel=True) as manager:
        assert manager.active_servers == [server]
        assert server._connect_task is not None
        assert server._connect_task is not asyncio.current_task()

    assert server.cleaned is True


@pytest.mark.asyncio
async def test_cross_task_cleanup_raises_without_manager() -> None:
    server = TaskBoundServer()

    connect_task = asyncio.create_task(server.connect())
    await connect_task

    with pytest.raises(RuntimeError, match="cancel scope"):
        await server.cleanup()


@pytest.mark.asyncio
async def test_manager_reconnect_failed_only() -> None:
    server = FlakyServer(failures=1)

    async with MCPServerManager([server]) as manager:
        assert manager.active_servers == []
        assert manager.failed_servers == [server]

        await manager.reconnect()
        assert manager.active_servers == [server]
        assert manager.failed_servers == []
