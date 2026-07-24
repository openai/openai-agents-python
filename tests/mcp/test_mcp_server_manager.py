import asyncio
import logging
from typing import Any, cast

import pytest
from mcp.types import (
    CallToolResult,
    GetPromptResult,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ReadResourceResult,
    Tool as MCPTool,
)

from agents import _debug
from agents.mcp import MCPServer, MCPServerManager
from agents.mcp._logging import get_mcp_server_log_name
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

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        raise NotImplementedError

    async def list_prompts(self) -> ListPromptsResult:
        raise NotImplementedError

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        raise NotImplementedError

    async def list_resources(self, cursor: str | None = None) -> ListResourcesResult:
        return ListResourcesResult(resources=[])

    async def list_resource_templates(
        self, cursor: str | None = None
    ) -> ListResourceTemplatesResult:
        return ListResourceTemplatesResult(resourceTemplates=[])

    async def read_resource(self, uri: str) -> ReadResourceResult:
        return ReadResourceResult(contents=[])


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

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        raise NotImplementedError

    async def list_prompts(self) -> ListPromptsResult:
        raise NotImplementedError

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        raise NotImplementedError

    async def list_resources(self, cursor: str | None = None) -> ListResourcesResult:
        return ListResourcesResult(resources=[])

    async def list_resource_templates(
        self, cursor: str | None = None
    ) -> ListResourceTemplatesResult:
        return ListResourceTemplatesResult(resourceTemplates=[])

    async def read_resource(self, uri: str) -> ReadResourceResult:
        return ReadResourceResult(contents=[])


class SensitiveNamedServer(FlakyServer):
    def __init__(self, name: str) -> None:
        super().__init__(failures=1)
        self._name = name
        self.name_reads = 0

    @property
    def name(self) -> str:
        self.name_reads += 1
        return self._name

    async def connect(self) -> None:
        raise RuntimeError("SECRET_MCP_CONNECT_ERROR")


class CleanupAwareServer(MCPServer):
    def __init__(self) -> None:
        super().__init__()
        self.connect_calls = 0
        self.cleanup_calls = 0

    @property
    def name(self) -> str:
        return "cleanup-aware"

    async def connect(self) -> None:
        if self.connect_calls > self.cleanup_calls:
            raise RuntimeError("connect called without cleanup")
        self.connect_calls += 1

    async def cleanup(self) -> None:
        self.cleanup_calls += 1

    async def list_tools(
        self, run_context: RunContextWrapper[Any] | None = None, agent: Any | None = None
    ) -> list[MCPTool]:
        raise NotImplementedError

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        raise NotImplementedError

    async def list_prompts(self) -> ListPromptsResult:
        raise NotImplementedError

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        raise NotImplementedError

    async def list_resources(self, cursor: str | None = None) -> ListResourcesResult:
        return ListResourcesResult(resources=[])

    async def list_resource_templates(
        self, cursor: str | None = None
    ) -> ListResourceTemplatesResult:
        return ListResourceTemplatesResult(resourceTemplates=[])

    async def read_resource(self, uri: str) -> ReadResourceResult:
        return ReadResourceResult(contents=[])


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ordinary-server", "ordinary-server"),
        (
            "sse: https://user:password@example.test/events?token=secret#fragment",
            "sse: https://example.test/events",
        ),
        (
            "streamable_http: https://example.test/mcp?token=secret",
            "streamable_http: https://example.test/mcp",
        ),
        (
            "streamable_http: https://user:password@example.test:8443/mcp?token=secret",
            "streamable_http: https://example.test:8443/mcp",
        ),
        ("streamable_http: https://[::1]:8000/mcp", "streamable_http: https://[::1]:8000/mcp"),
        (
            "streamable-http: https://example.test/mcp#secret",
            "streamable-http: https://example.test/mcp",
        ),
        (
            "streamable_http: https://user:password@[invalid/mcp?token=secret",
            "streamable_http: <invalid-url>",
        ),
        (
            "streamable_http: https://user:password/mcp?token=secret",
            "streamable_http: <invalid-url>",
        ),
        ("https://user:password@example.test/mcp?token=secret", "https://example.test/mcp"),
        ("https://user:password@[invalid/mcp?token=secret", "<invalid-url>"),
        ("stdio: python server.py?token=secret", "stdio: python server.py?token=secret"),
    ],
)
def test_get_mcp_server_log_name(name: str, expected: str) -> None:
    assert get_mcp_server_log_name(name) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("redacted", [True, False])
@pytest.mark.parametrize(
    ("server_name", "diagnostic_sentinel", "always_hidden"),
    [
        (
            "streamable_http: https://SECRET_CREDENTIAL@example.test/"
            "SECRET_MCP_PATH?token=SECRET_MCP_QUERY#SECRET_MCP_FRAGMENT",
            "SECRET_MCP_PATH",
            ("SECRET_CREDENTIAL", "SECRET_MCP_QUERY", "SECRET_MCP_FRAGMENT"),
        ),
        (
            "SECRET_CUSTOM_MCP_SERVER_NAME",
            "SECRET_CUSTOM_MCP_SERVER_NAME",
            (),
        ),
    ],
)
async def test_manager_sanitizes_url_derived_server_names_in_failure_logs(
    monkeypatch,
    caplog,
    redacted: bool,
    server_name: str,
    diagnostic_sentinel: str,
    always_hidden: tuple[str, ...],
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", redacted)
    server = SensitiveNamedServer(server_name)
    manager = MCPServerManager([server])

    with caplog.at_level(logging.ERROR, logger="openai.agents"):
        await manager.connect_all()

    assert (diagnostic_sentinel not in caplog.text) is redacted
    assert server.name_reads == (0 if redacted else 1)
    for sentinel in always_hidden:
        assert sentinel not in caplog.text
    assert ("SECRET_MCP_CONNECT_ERROR" not in caplog.text) is redacted


class CancelledServer(MCPServer):
    def __init__(self) -> None:
        super().__init__()
        self.resource_open = False
        self.cleanup_calls = 0

    @property
    def name(self) -> str:
        return "cancelled"

    async def connect(self) -> None:
        # Simulate a transport that opened resources before cancellation.
        self.resource_open = True
        raise asyncio.CancelledError()

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        self.resource_open = False

    async def list_tools(
        self, run_context: RunContextWrapper[Any] | None = None, agent: Any | None = None
    ) -> list[MCPTool]:
        raise NotImplementedError

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        meta: dict[str, Any] | None = None,
    ) -> CallToolResult:
        raise NotImplementedError

    async def list_prompts(self) -> ListPromptsResult:
        raise NotImplementedError

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        raise NotImplementedError

    async def list_resources(self, cursor: str | None = None) -> ListResourcesResult:
        return ListResourcesResult(resources=[])

    async def list_resource_templates(
        self, cursor: str | None = None
    ) -> ListResourceTemplatesResult:
        return ListResourceTemplatesResult(resourceTemplates=[])

    async def read_resource(self, uri: str) -> ReadResourceResult:
        return ReadResourceResult(contents=[])


class FailingTaskBoundServer(TaskBoundServer):
    @property
    def name(self) -> str:
        return "failing-task-bound"

    async def connect(self) -> None:
        await super().connect()
        raise RuntimeError("connect failed")


class FatalError(BaseException):
    pass


class FatalTaskBoundServer(TaskBoundServer):
    @property
    def name(self) -> str:
        return "fatal-task-bound"

    async def connect(self) -> None:
        await super().connect()
        raise FatalError("fatal connect failed")


class CleanupFailingServer(TaskBoundServer):
    @property
    def name(self) -> str:
        return "cleanup-failing"

    async def cleanup(self) -> None:
        await super().cleanup()
        raise RuntimeError("cleanup failed")


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


@pytest.mark.asyncio
async def test_manager_reconnect_deduplicates_failures() -> None:
    server = FlakyServer(failures=2)

    async with MCPServerManager([server], connect_in_parallel=True) as manager:
        assert manager.active_servers == []
        assert manager.failed_servers == [server]
        assert server.connect_calls == 1

        await manager.reconnect()
        assert manager.active_servers == []
        assert manager.failed_servers == [server]
        assert server.connect_calls == 2

        await manager.reconnect()
        assert manager.active_servers == [server]
        assert manager.failed_servers == []
        assert server.connect_calls == 3


@pytest.mark.asyncio
async def test_manager_connect_all_retries_all_servers() -> None:
    server = FlakyServer(failures=1)
    manager = MCPServerManager([server])
    try:
        await manager.connect_all()
        assert manager.active_servers == []
        assert manager.failed_servers == [server]
        assert server.connect_calls == 1

        await manager.connect_all()
        assert manager.active_servers == [server]
        assert manager.failed_servers == []
        assert server.connect_calls == 2
    finally:
        await manager.cleanup_all()


@pytest.mark.asyncio
async def test_manager_connect_all_is_idempotent() -> None:
    server = CleanupAwareServer()

    async with MCPServerManager([server]) as manager:
        assert server.connect_calls == 1
        await manager.connect_all()


@pytest.mark.asyncio
async def test_manager_reconnect_all_avoids_duplicate_connections() -> None:
    server = CleanupAwareServer()

    async with MCPServerManager([server]) as manager:
        assert server.connect_calls == 1
        await manager.reconnect(failed_only=False)


@pytest.mark.asyncio
async def test_manager_strict_reconnect_refreshes_active_servers() -> None:
    server_a = FlakyServer(failures=1)
    server_b = FlakyServer(failures=2)

    async with MCPServerManager([server_a, server_b]) as manager:
        assert manager.active_servers == []

        manager.strict = True
        with pytest.raises(RuntimeError, match="connect failed"):
            await manager.reconnect()

        assert manager.active_servers == [server_a]
        assert manager.failed_servers == [server_b]


@pytest.mark.asyncio
async def test_manager_strict_connect_preserves_existing_active_servers() -> None:
    connected_server = TaskBoundServer()
    failing_server = FlakyServer(failures=2)
    manager = MCPServerManager([connected_server, failing_server])
    try:
        await manager.connect_all()
        assert manager.active_servers == [connected_server]
        assert manager.failed_servers == [failing_server]

        manager.strict = True
        with pytest.raises(RuntimeError, match="connect failed"):
            await manager.connect_all()

        assert manager.active_servers == [connected_server]
        assert manager.failed_servers == [failing_server]
    finally:
        await manager.cleanup_all()


@pytest.mark.asyncio
async def test_manager_strict_connect_cleans_up_connected_servers() -> None:
    connected_server = TaskBoundServer()
    failing_server = FlakyServer(failures=1)
    manager = MCPServerManager([connected_server, failing_server], strict=True)

    with pytest.raises(RuntimeError, match="connect failed"):
        await manager.connect_all()

    assert connected_server.cleaned is True
    assert manager.active_servers == []


@pytest.mark.asyncio
async def test_manager_strict_connect_cleans_up_failed_server() -> None:
    failing_server = FailingTaskBoundServer()
    manager = MCPServerManager([failing_server], strict=True)

    with pytest.raises(RuntimeError, match="connect failed"):
        await manager.connect_all()

    assert failing_server.cleaned is True


@pytest.mark.asyncio
async def test_manager_strict_connect_parallel_cleans_up_failed_server() -> None:
    failing_server = FailingTaskBoundServer()
    manager = MCPServerManager([failing_server], strict=True, connect_in_parallel=True)

    with pytest.raises(RuntimeError, match="connect failed"):
        await manager.connect_all()

    assert failing_server.cleaned is True


@pytest.mark.asyncio
async def test_manager_strict_connect_parallel_cleans_up_workers() -> None:
    connected_server = TaskBoundServer()
    failing_server = FailingTaskBoundServer()
    manager = MCPServerManager(
        [connected_server, failing_server], strict=True, connect_in_parallel=True
    )

    with pytest.raises(RuntimeError, match="connect failed"):
        await manager.connect_all()

    assert connected_server.cleaned is True
    assert failing_server.cleaned is True
    assert manager._workers == {}


@pytest.mark.asyncio
async def test_manager_parallel_cleanup_clears_worker_on_failure() -> None:
    server = CleanupFailingServer()
    manager = MCPServerManager([server], connect_in_parallel=True)
    await manager.connect_all()
    await manager.cleanup_all()

    assert server not in manager._workers
    assert server not in manager._connected_servers


@pytest.mark.asyncio
async def test_manager_parallel_cleanup_drops_worker_after_error() -> None:
    class HangingCleanupWorker:
        def __init__(self) -> None:
            self.cleanup_calls = 0

        @property
        def is_done(self) -> bool:
            return False

        async def cleanup(self) -> None:
            self.cleanup_calls += 1
            raise RuntimeError("cleanup failed")

    server = FlakyServer(failures=0)
    manager = MCPServerManager([server], connect_in_parallel=True)
    manager._workers[server] = cast(Any, HangingCleanupWorker())

    await manager.cleanup_all()

    assert manager._workers == {}


@pytest.mark.asyncio
async def test_manager_parallel_suppresses_cancelled_error_in_strict_mode() -> None:
    server = CancelledServer()
    manager = MCPServerManager([server], connect_in_parallel=True, strict=True)
    try:
        await manager.connect_all()
        assert manager.active_servers == []
        assert manager.failed_servers == [server]
    finally:
        await manager.cleanup_all()


@pytest.mark.asyncio
async def test_manager_parallel_propagates_cancelled_error_when_unsuppressed() -> None:
    server = CancelledServer()
    manager = MCPServerManager([server], connect_in_parallel=True, suppress_cancelled_error=False)
    try:
        with pytest.raises(asyncio.CancelledError):
            await manager.connect_all()
    finally:
        await manager.cleanup_all()


@pytest.mark.asyncio
async def test_manager_sequential_propagates_base_exception() -> None:
    server = FatalTaskBoundServer()
    manager = MCPServerManager([server])

    with pytest.raises(FatalError, match="fatal connect failed"):
        await manager.connect_all()

    assert server.cleaned is True
    assert manager.failed_servers == [server]


@pytest.mark.asyncio
async def test_manager_parallel_propagates_base_exception() -> None:
    server = FatalTaskBoundServer()
    manager = MCPServerManager([server], connect_in_parallel=True)

    with pytest.raises(FatalError, match="fatal connect failed"):
        await manager.connect_all()

    assert server.cleaned is True
    assert manager._workers == {}


@pytest.mark.asyncio
async def test_manager_parallel_prefers_cancelled_error_when_unsuppressed() -> None:
    cancelled_server = CancelledServer()
    fatal_server = FatalTaskBoundServer()
    manager = MCPServerManager(
        [fatal_server, cancelled_server],
        connect_in_parallel=True,
        suppress_cancelled_error=False,
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await manager.connect_all()
    finally:
        await manager.cleanup_all()


@pytest.mark.asyncio
async def test_manager_cleanup_runs_on_cancelled_error_during_connect() -> None:
    server = CleanupAwareServer()
    cancelled_server = CancelledServer()
    manager = MCPServerManager(
        [server, cancelled_server],
        suppress_cancelled_error=False,
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await manager.connect_all()
        assert server.cleanup_calls == 1
        # The cancelled server must be recorded and cleaned by connect_all()'s
        # failure path — callers cannot rely on a later cleanup_all() because
        # `async with` never reaches __aexit__ when __aenter__ raises.
        assert cancelled_server in manager.failed_servers
        assert cancelled_server.cleanup_calls == 1
        assert cancelled_server.resource_open is False
    finally:
        await manager.cleanup_all()


@pytest.mark.asyncio
async def test_manager_async_with_cleans_cancelled_server_when_unsuppressed() -> None:
    server = CleanupAwareServer()
    cancelled_server = CancelledServer()

    with pytest.raises(asyncio.CancelledError):
        async with MCPServerManager(
            [server, cancelled_server],
            suppress_cancelled_error=False,
        ):
            raise AssertionError("context body should not run when connect raises")

    assert server.cleanup_calls == 1
    assert cancelled_server.cleanup_calls == 1
    assert cancelled_server.resource_open is False
