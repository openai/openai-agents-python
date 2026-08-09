import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from mcp.types import (
    CallToolResult,
    GetPromptResult,
    ListPromptsResult,
    ListResourcesResult,
    ReadResourceResult,
    Tool as MCPTool,
)

from agents import _debug
from agents.mcp import MCPServer, MCPServerManager, manager as manager_module
from agents.mcp._logging import get_mcp_server_log_name
from agents.run_context import RunContextWrapper

from .model_compat import ListResourceTemplatesResult

TEST_TIMEOUT_SECONDS = 1


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


class BlockingCleanupServer(TaskBoundServer):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_started = asyncio.Event()
        self.allow_cleanup = asyncio.Event()
        self.cleanup_finished = asyncio.Event()
        self.connect_calls = 0
        self.cleanup_calls = 0
        self.active_generation: int | None = None

    async def connect(self) -> None:
        await super().connect()
        self.connect_calls += 1
        self.active_generation = self.connect_calls
        self.cleaned = False

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        self.cleanup_started.set()
        await self.allow_cleanup.wait()
        self.active_generation = None
        try:
            await super().cleanup()
        finally:
            self.cleanup_finished.set()


class BlockingCleanupFailureServer(TaskBoundServer):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_started = asyncio.Event()
        self.allow_cleanup = asyncio.Event()
        self.connect_calls = 0
        self.cleanup_calls = 0

    async def connect(self) -> None:
        await super().connect()
        self.connect_calls += 1
        if self.connect_calls == 1:
            raise RuntimeError("connect failed")

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        self.cleanup_started.set()
        await self.allow_cleanup.wait()
        raise RuntimeError("cleanup failed")


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


class PartialFailureServer(FlakyServer):
    def __init__(self, *, fail_cleanup: bool = False) -> None:
        super().__init__(failures=0)
        self.fail_cleanup = fail_cleanup
        self.cleanup_calls = 0
        self.resource_open = False
        self._connect_task: asyncio.Task[object] | None = None

    @property
    def name(self) -> str:
        return "partial-failure"

    async def connect(self) -> None:
        self.connect_calls += 1
        self._connect_task = asyncio.current_task()
        if self.resource_open:
            raise RuntimeError("connect called without cleanup")
        self.resource_open = True
        if self.connect_calls == 1:
            raise RuntimeError("connect failed after opening resource")

    async def cleanup(self) -> None:
        self.cleanup_calls += 1
        if asyncio.current_task() is not self._connect_task:
            raise RuntimeError("Attempted to exit cancel scope in a different task")
        if self.fail_cleanup:
            raise RuntimeError("cleanup failed")
        self.resource_open = False


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


@pytest.mark.parametrize("field_name", ["connect_timeout_seconds", "cleanup_timeout_seconds"])
@pytest.mark.parametrize(
    ("timeout_seconds", "error_type"),
    [
        (True, TypeError),
        ("1", TypeError),
        (0, ValueError),
        (-1, ValueError),
        (float("nan"), ValueError),
        (float("inf"), ValueError),
        (10**400, ValueError),
    ],
)
def test_manager_rejects_unsupported_lifecycle_timeouts(
    field_name: str,
    timeout_seconds: object,
    error_type: type[Exception],
) -> None:
    kwargs = {field_name: timeout_seconds}

    with pytest.raises(error_type, match=field_name):
        MCPServerManager([], **kwargs)  # type: ignore[arg-type]


def test_manager_validates_lifecycle_timeout_assignment() -> None:
    manager = MCPServerManager(
        [],
        connect_timeout_seconds=1.5,
        cleanup_timeout_seconds=None,
    )

    manager.connect_timeout_seconds = None
    manager.cleanup_timeout_seconds = 2.5

    assert manager.connect_timeout_seconds is None
    assert manager.cleanup_timeout_seconds == 2.5
    with pytest.raises(ValueError, match="connect_timeout_seconds"):
        manager.connect_timeout_seconds = 0
    assert manager.connect_timeout_seconds is None


def test_manager_defaults_to_finite_lifecycle_timeouts() -> None:
    manager = MCPServerManager([])

    assert manager.connect_timeout_seconds == 10.0
    assert manager.cleanup_timeout_seconds == 10.0


@pytest.mark.asyncio
@pytest.mark.parametrize("connect_in_parallel", [False, True])
async def test_manager_uses_current_lifecycle_timeouts(
    connect_in_parallel: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = TaskBoundServer()
    observed_timeouts: list[float | None] = []

    async def run_with_timeout(
        func: Callable[[], Awaitable[Any]], timeout_seconds: float | None
    ) -> None:
        observed_timeouts.append(timeout_seconds)
        await func()

    monkeypatch.setattr(manager_module, "_run_with_timeout_in_task", run_with_timeout)
    manager = MCPServerManager(
        [server],
        connect_timeout_seconds=None,
        cleanup_timeout_seconds=None,
        connect_in_parallel=connect_in_parallel,
    )
    manager.connect_timeout_seconds = 1.5
    await manager.connect_all()

    manager.cleanup_timeout_seconds = 2.5
    await manager.cleanup_all()

    assert server.cleaned is True
    assert manager._workers == {}
    assert observed_timeouts == [1.5, 2.5]


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
async def test_manager_serializes_overlapping_parallel_cleanup_calls() -> None:
    server = BlockingCleanupServer()
    manager = MCPServerManager([server], connect_in_parallel=True)
    await manager.connect_all()

    first_cleanup = asyncio.create_task(manager.cleanup_all())
    second_cleanup: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(server.cleanup_started.wait(), timeout=TEST_TIMEOUT_SECONDS)
        second_cleanup = asyncio.create_task(manager.cleanup_all())

        server.allow_cleanup.set()
        await asyncio.wait_for(
            asyncio.gather(first_cleanup, second_cleanup), timeout=TEST_TIMEOUT_SECONDS
        )

        assert server.cleanup_calls == 1
        assert manager._workers == {}
        assert manager._connected_servers == set()
    finally:
        server.allow_cleanup.set()
        tasks = [first_cleanup]
        if second_cleanup is not None:
            tasks.append(second_cleanup)
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=TEST_TIMEOUT_SECONDS
        )
        await asyncio.wait_for(manager.cleanup_all(), timeout=TEST_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_manager_serializes_parallel_cleanup_and_full_reconnect() -> None:
    server = BlockingCleanupServer()
    manager = MCPServerManager([server], connect_in_parallel=True)
    await manager.connect_all()

    cleanup_task = asyncio.create_task(manager.cleanup_all())
    reconnect_task: asyncio.Task[list[MCPServer]] | None = None
    try:
        await asyncio.wait_for(server.cleanup_started.wait(), timeout=TEST_TIMEOUT_SECONDS)
        reconnect_task = asyncio.create_task(manager.reconnect(failed_only=False))
        await asyncio.sleep(0)

        assert not reconnect_task.done()
        assert server.connect_calls == 1

        server.allow_cleanup.set()
        await asyncio.wait_for(
            asyncio.gather(cleanup_task, reconnect_task), timeout=TEST_TIMEOUT_SECONDS
        )

        assert server.connect_calls == 2
        assert server.cleanup_calls == 1
        assert server.active_generation == 2
        assert manager.active_servers == [server]
        assert manager._connected_servers == {server}
    finally:
        server.allow_cleanup.set()
        tasks: list[asyncio.Task[Any]] = [cleanup_task]
        if reconnect_task is not None:
            tasks.append(reconnect_task)
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=TEST_TIMEOUT_SECONDS
        )
        await asyncio.wait_for(manager.cleanup_all(), timeout=TEST_TIMEOUT_SECONDS)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["connect_all", "reconnect", "cleanup_all"])
@pytest.mark.parametrize("suppress_cancelled_error", [True, False])
async def test_manager_applies_cancellation_policy_while_waiting_for_lifecycle_lock(
    operation: str,
    suppress_cancelled_error: bool,
) -> None:
    server = BlockingCleanupServer()
    manager = MCPServerManager(
        [server],
        connect_in_parallel=True,
        suppress_cancelled_error=suppress_cancelled_error,
    )
    await manager.connect_all()

    lock_owner = asyncio.create_task(manager.cleanup_all())
    waiter: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(server.cleanup_started.wait(), timeout=TEST_TIMEOUT_SECONDS)
        if operation == "connect_all":
            waiter = asyncio.create_task(manager.connect_all())
        elif operation == "reconnect":
            waiter = asyncio.create_task(manager.reconnect(failed_only=False))
        else:
            waiter = asyncio.create_task(manager.cleanup_all())
        await asyncio.sleep(0)

        assert not waiter.done()
        waiter.cancel()
        result = await asyncio.wait_for(
            asyncio.gather(waiter, return_exceptions=True), timeout=TEST_TIMEOUT_SECONDS
        )

        if suppress_cancelled_error:
            if operation == "cleanup_all":
                assert result[0] is None
            else:
                assert result[0] == [server]
        else:
            assert isinstance(result[0], asyncio.CancelledError)
    finally:
        server.allow_cleanup.set()
        tasks: list[asyncio.Task[Any]] = [lock_owner]
        if waiter is not None:
            tasks.append(waiter)
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=TEST_TIMEOUT_SECONDS
        )
        await asyncio.wait_for(manager.cleanup_all(), timeout=TEST_TIMEOUT_SECONDS)


@pytest.mark.asyncio
@pytest.mark.parametrize("suppress_cancelled_error", [True, False])
async def test_manager_retains_parallel_cleanup_worker_after_caller_cancellation(
    suppress_cancelled_error: bool,
) -> None:
    server = BlockingCleanupServer()
    manager = MCPServerManager(
        [server],
        connect_in_parallel=True,
        suppress_cancelled_error=suppress_cancelled_error,
    )
    await manager.connect_all()

    original_worker = manager._workers[server]
    cleanup_task = asyncio.create_task(manager.cleanup_all())
    connect_task: asyncio.Task[list[MCPServer]] | None = None
    try:
        await asyncio.wait_for(server.cleanup_started.wait(), timeout=TEST_TIMEOUT_SECONDS)
        cleanup_task.cancel()
        cleanup_result = await asyncio.wait_for(
            asyncio.gather(cleanup_task, return_exceptions=True),
            timeout=TEST_TIMEOUT_SECONDS,
        )

        if suppress_cancelled_error:
            assert cleanup_result[0] is None
        else:
            assert isinstance(cleanup_result[0], asyncio.CancelledError)
        assert manager._workers[server] is original_worker
        assert not original_worker.is_done

        connect_task = asyncio.create_task(manager.connect_all())
        await asyncio.sleep(0)

        assert not connect_task.done()
        assert manager._workers[server] is original_worker
        assert server.connect_calls == 1

        server.allow_cleanup.set()
        await asyncio.wait_for(connect_task, timeout=TEST_TIMEOUT_SECONDS)

        assert original_worker.is_done
        assert manager._workers[server] is not original_worker
        assert manager._connected_servers == {server}
        assert manager.active_servers == [server]
        assert manager.failed_servers == []
        assert manager.errors == {}
        assert server.connect_calls == 2
        assert server.cleanup_calls == 1
        assert server.active_generation == 2
    finally:
        server.allow_cleanup.set()
        tasks: list[asyncio.Task[Any]] = [cleanup_task]
        if connect_task is not None:
            tasks.append(connect_task)
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=TEST_TIMEOUT_SECONDS
        )
        await asyncio.wait_for(manager.cleanup_all(), timeout=TEST_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_manager_discards_parallel_cleanup_worker_after_cancelled_caller() -> None:
    server = BlockingCleanupServer()
    manager = MCPServerManager([server], connect_in_parallel=True)
    await manager.connect_all()

    original_worker = manager._workers[server]
    cleanup_task = asyncio.create_task(manager.cleanup_all())
    try:
        await asyncio.wait_for(server.cleanup_started.wait(), timeout=TEST_TIMEOUT_SECONDS)
        cleanup_task.cancel()
        cleanup_result = await asyncio.wait_for(
            asyncio.gather(cleanup_task, return_exceptions=True), timeout=TEST_TIMEOUT_SECONDS
        )
        assert cleanup_result[0] is None

        assert manager._workers[server] is original_worker
        assert not original_worker.is_done

        server.allow_cleanup.set()
        await asyncio.wait_for(asyncio.shield(original_worker._task), timeout=TEST_TIMEOUT_SECONDS)
        await asyncio.sleep(0)

        assert manager._workers == {}
        assert manager._connected_servers == set()
    finally:
        server.allow_cleanup.set()
        await asyncio.wait_for(
            asyncio.gather(cleanup_task, return_exceptions=True), timeout=TEST_TIMEOUT_SECONDS
        )
        await asyncio.wait_for(manager.cleanup_all(), timeout=TEST_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_manager_preserves_cleanup_failure_after_cancelled_retry() -> None:
    server = BlockingCleanupFailureServer()
    manager = MCPServerManager([server], connect_in_parallel=True)
    await manager.connect_all()

    first_retry = asyncio.create_task(manager.reconnect())
    second_retry: asyncio.Task[list[MCPServer]] | None = None
    try:
        await asyncio.wait_for(server.cleanup_started.wait(), timeout=TEST_TIMEOUT_SECONDS)
        first_retry.cancel()
        assert await asyncio.wait_for(first_retry, timeout=TEST_TIMEOUT_SECONDS) == []

        second_retry = asyncio.create_task(manager.reconnect())
        await asyncio.sleep(0)
        assert not second_retry.done()

        server.allow_cleanup.set()
        assert await asyncio.wait_for(second_retry, timeout=TEST_TIMEOUT_SECONDS) == []

        assert server.connect_calls == 1
        assert server.cleanup_calls == 1
        assert manager.active_servers == []
        assert manager.failed_servers == [server]
        assert str(manager.errors[server]) == "cleanup failed"
        worker = manager._workers[server]
        assert worker.is_done
        assert str(worker.cleanup_error) == "cleanup failed"

        assert await asyncio.wait_for(manager.connect_all(), timeout=TEST_TIMEOUT_SECONDS) == []
        assert server.connect_calls == 1
    finally:
        server.allow_cleanup.set()
        tasks: list[asyncio.Task[Any]] = [first_retry]
        if second_retry is not None:
            tasks.append(second_retry)
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=TEST_TIMEOUT_SECONDS
        )
        await asyncio.wait_for(manager.cleanup_all(), timeout=TEST_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_manager_bounds_wait_for_stopping_parallel_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run_without_internal_timeout(
        func: Callable[[], Awaitable[Any]], timeout_seconds: float | None
    ) -> None:
        del timeout_seconds
        await func()

    monkeypatch.setattr(manager_module, "_run_with_timeout_in_task", run_without_internal_timeout)
    server = BlockingCleanupServer()
    manager = MCPServerManager(
        [server],
        connect_in_parallel=True,
        cleanup_timeout_seconds=0.05,
    )
    await manager.connect_all()

    original_worker = manager._workers[server]
    try:
        await asyncio.wait_for(manager.cleanup_all(), timeout=TEST_TIMEOUT_SECONDS)

        assert isinstance(manager.errors[server], asyncio.TimeoutError)
        assert manager._workers[server] is original_worker
        assert not original_worker.is_done

        assert await asyncio.wait_for(manager.connect_all(), timeout=TEST_TIMEOUT_SECONDS) == []

        assert isinstance(manager.errors[server], asyncio.TimeoutError)
        assert manager._workers[server] is original_worker
        assert server.connect_calls == 1
    finally:
        server.allow_cleanup.set()
        await asyncio.wait_for(asyncio.shield(original_worker._task), timeout=TEST_TIMEOUT_SECONDS)
        await asyncio.sleep(0)
        await asyncio.wait_for(manager.cleanup_all(), timeout=TEST_TIMEOUT_SECONDS)

    assert manager._workers == {}
    assert manager._connected_servers == set()
    assert server.cleanup_calls == 1


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
@pytest.mark.parametrize("connect_in_parallel", [False, True])
async def test_manager_reconnect_cleans_partial_failure_before_retry(
    connect_in_parallel: bool,
) -> None:
    healthy_server = CleanupAwareServer()
    failed_server = PartialFailureServer()
    manager = MCPServerManager(
        [healthy_server, failed_server], connect_in_parallel=connect_in_parallel
    )
    try:
        await manager.connect_all()

        assert manager.active_servers == [healthy_server]
        assert manager.failed_servers == [failed_server]

        await manager.reconnect()

        assert manager.active_servers == [healthy_server, failed_server]
        assert manager.failed_servers == []
        assert failed_server not in manager.errors
        assert failed_server.connect_calls == 2
        assert failed_server.cleanup_calls == 1
        assert failed_server.resource_open is True
        assert healthy_server.connect_calls == 1
        assert healthy_server.cleanup_calls == 0
    finally:
        await manager.cleanup_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("connect_in_parallel", [False, True])
async def test_manager_reconnect_does_not_retry_after_cleanup_failure(
    connect_in_parallel: bool,
) -> None:
    server = PartialFailureServer(fail_cleanup=True)
    manager = MCPServerManager([server], connect_in_parallel=connect_in_parallel)

    await manager.connect_all()
    await manager.reconnect()

    assert manager.active_servers == []
    assert manager.failed_servers == [server]
    assert server.connect_calls == 1
    assert server.cleanup_calls == 1
    assert server.resource_open is True
    assert str(manager.errors[server]) == "cleanup failed"
    if connect_in_parallel:
        worker = manager._workers[server]
        assert worker.is_done
        assert str(worker.cleanup_error) == "cleanup failed"
    else:
        assert manager._workers == {}


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
async def test_manager_parallel_cleanup_retains_worker_outcome_on_failure() -> None:
    server = CleanupFailingServer()
    manager = MCPServerManager([server], connect_in_parallel=True)
    await manager.connect_all()
    await manager.cleanup_all()

    worker = manager._workers[server]
    assert worker.is_done
    assert str(worker.cleanup_error) == "cleanup failed"
    assert server not in manager._connected_servers


@pytest.mark.asyncio
async def test_manager_parallel_cleanup_retains_worker_after_error() -> None:
    class HangingCleanupWorker:
        def __init__(self) -> None:
            self.cleanup_calls = 0
            self.error = RuntimeError("cleanup failed")

        @property
        def is_done(self) -> bool:
            return self.cleanup_calls > 0

        @property
        def cleanup_error(self) -> BaseException | None:
            return self.error if self.is_done else None

        async def cleanup(self, timeout_seconds: float | None) -> None:
            self.cleanup_calls += 1
            raise self.error

    server = FlakyServer(failures=0)
    manager = MCPServerManager([server], connect_in_parallel=True)
    manager._workers[server] = cast(Any, HangingCleanupWorker())

    await manager.cleanup_all()

    assert manager._workers[server].cleanup_error is not None


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


def test_manager_accepts_one_shot_iterables() -> None:
    server_a = FlakyServer(failures=0)
    server_b = FlakyServer(failures=0)

    manager = MCPServerManager(iter([server_a, server_b]))

    assert manager.all_servers == [server_a, server_b]
    assert manager.active_servers == [server_a, server_b]


@pytest.mark.asyncio
async def test_manager_connects_servers_from_a_one_shot_iterable() -> None:
    server_a = CleanupAwareServer()
    server_b = CleanupAwareServer()

    async with MCPServerManager(server for server in (server_a, server_b)) as manager:
        assert manager.active_servers == [server_a, server_b]
        assert server_a.connect_calls == 1
        assert server_b.connect_calls == 1


@pytest.mark.asyncio
async def test_manager_restores_one_shot_iterable_servers_after_a_failed_connect() -> None:
    server = FlakyServer(failures=1)

    manager = MCPServerManager(iter([server]), strict=True, drop_failed_servers=False)

    with pytest.raises(RuntimeError):
        await manager.connect_all()

    # drop_failed_servers=False keeps failed servers active, so the restored list must match
    # what an equivalent list argument produces.
    assert manager.active_servers == [server]
