import asyncio
from contextlib import asynccontextmanager
from typing import cast

import pytest
from mcp import ClientSession, Tool as MCPTool
from mcp.types import CallToolResult, ListToolsResult

from agents.exceptions import UserError
from agents.mcp.server import MCPServerStreamableHttp, _MCPServerWithClientSession


class DummySession:
    def __init__(self, fail_call_tool: int = 0, fail_list_tools: int = 0):
        self.fail_call_tool = fail_call_tool
        self.fail_list_tools = fail_list_tools
        self.call_tool_attempts = 0
        self.list_tools_attempts = 0

    async def call_tool(self, tool_name, arguments, meta=None):
        self.call_tool_attempts += 1
        if self.call_tool_attempts <= self.fail_call_tool:
            raise RuntimeError("call_tool failure")
        return CallToolResult(content=[])

    async def list_tools(self):
        self.list_tools_attempts += 1
        if self.list_tools_attempts <= self.fail_list_tools:
            raise RuntimeError("list_tools failure")
        return ListToolsResult(tools=[MCPTool(name="tool", inputSchema={})])


class DummyServer(_MCPServerWithClientSession):
    def __init__(self, session: DummySession, retries: int):
        super().__init__(
            cache_tools_list=False,
            client_session_timeout_seconds=None,
            max_retry_attempts=retries,
            retry_backoff_seconds_base=0,
        )
        self.session = cast(ClientSession, session)

    def create_streams(self):
        raise NotImplementedError

    @property
    def name(self) -> str:
        return "dummy"


@pytest.mark.asyncio
async def test_call_tool_retries_until_success():
    session = DummySession(fail_call_tool=2)
    server = DummyServer(session=session, retries=2)
    result = await server.call_tool("tool", None)
    assert isinstance(result, CallToolResult)
    assert session.call_tool_attempts == 3


@pytest.mark.asyncio
async def test_list_tools_unlimited_retries():
    session = DummySession(fail_list_tools=3)
    server = DummyServer(session=session, retries=-1)
    tools = await server.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "tool"
    assert session.list_tools_attempts == 4


@pytest.mark.asyncio
async def test_call_tool_validates_required_parameters_before_remote_call():
    session = DummySession()
    server = DummyServer(session=session, retries=0)
    server._tools_list = [  # noqa: SLF001
        MCPTool(
            name="tool",
            inputSchema={
                "type": "object",
                "properties": {"param_a": {"type": "string"}},
                "required": ["param_a"],
            },
        )
    ]

    with pytest.raises(UserError, match="missing required parameters: param_a"):
        await server.call_tool("tool", {})

    assert session.call_tool_attempts == 0


@pytest.mark.asyncio
async def test_call_tool_with_required_parameters_still_calls_remote_tool():
    session = DummySession()
    server = DummyServer(session=session, retries=0)
    server._tools_list = [  # noqa: SLF001
        MCPTool(
            name="tool",
            inputSchema={
                "type": "object",
                "properties": {"param_a": {"type": "string"}},
                "required": ["param_a"],
            },
        )
    ]

    result = await server.call_tool("tool", {"param_a": "value"})
    assert isinstance(result, CallToolResult)
    assert session.call_tool_attempts == 1


@pytest.mark.asyncio
async def test_call_tool_skips_validation_when_tool_is_missing_from_cache():
    session = DummySession()
    server = DummyServer(session=session, retries=0)
    server._tools_list = [MCPTool(name="different_tool", inputSchema={"required": ["param_a"]})]  # noqa: SLF001

    await server.call_tool("tool", {})
    assert session.call_tool_attempts == 1


@pytest.mark.asyncio
async def test_call_tool_skips_validation_when_required_list_is_absent():
    session = DummySession()
    server = DummyServer(session=session, retries=0)
    server._tools_list = [MCPTool(name="tool", inputSchema={"type": "object"})]  # noqa: SLF001

    await server.call_tool("tool", None)
    assert session.call_tool_attempts == 1


@pytest.mark.asyncio
async def test_call_tool_validates_required_parameters_when_arguments_is_none():
    session = DummySession()
    server = DummyServer(session=session, retries=0)
    server._tools_list = [MCPTool(name="tool", inputSchema={"required": ["param_a"]})]  # noqa: SLF001

    with pytest.raises(UserError, match="missing required parameters: param_a"):
        await server.call_tool("tool", None)

    assert session.call_tool_attempts == 0


@pytest.mark.asyncio
async def test_call_tool_rejects_non_object_arguments_before_remote_call():
    session = DummySession()
    server = DummyServer(session=session, retries=0)
    server._tools_list = [MCPTool(name="tool", inputSchema={"required": ["param_a"]})]  # noqa: SLF001

    with pytest.raises(UserError, match="arguments must be an object"):
        await server.call_tool("tool", cast(dict[str, object] | None, ["bad"]))

    assert session.call_tool_attempts == 0


class ConcurrentCancellationSession:
    def __init__(self):
        self._slow_task: asyncio.Task[CallToolResult] | None = None
        self._slow_started = asyncio.Event()

    async def call_tool(self, tool_name, arguments, meta=None):
        if tool_name == "slow":
            self._slow_task = cast(asyncio.Task[CallToolResult], asyncio.current_task())
            self._slow_started.set()
            await asyncio.sleep(0.1)
            return CallToolResult(content=[])

        await self._slow_started.wait()
        assert self._slow_task is not None
        self._slow_task.cancel()
        raise RuntimeError("synthetic request failure")


class IsolatedRetrySession:
    def __init__(self):
        self.call_tool_attempts = 0

    async def call_tool(self, tool_name, arguments, meta=None):
        self.call_tool_attempts += 1
        if tool_name == "slow":
            return CallToolResult(content=[])
        raise RuntimeError("synthetic request failure")


class HangingSession:
    async def call_tool(self, tool_name, arguments, meta=None):
        await asyncio.sleep(10)


class DummyStreamableHttpServer(MCPServerStreamableHttp):
    def __init__(
        self,
        shared_session: ConcurrentCancellationSession,
        isolated_session: IsolatedRetrySession,
    ):
        super().__init__(
            params={"url": "https://example.test/mcp"},
            client_session_timeout_seconds=None,
            max_retry_attempts=0,
        )
        self.session = cast(ClientSession, shared_session)
        self._isolated_session = cast(ClientSession, isolated_session)

    def create_streams(self):
        raise NotImplementedError

    @asynccontextmanager
    async def _isolated_client_session(self):
        yield self._isolated_session


@pytest.mark.asyncio
async def test_streamable_http_retries_cancelled_request_on_isolated_session():
    shared_session = ConcurrentCancellationSession()
    isolated_session = IsolatedRetrySession()
    server = DummyStreamableHttpServer(
        shared_session=shared_session,
        isolated_session=isolated_session,
    )

    results = await asyncio.gather(
        server.call_tool("slow", None),
        server.call_tool("fail", None),
        return_exceptions=True,
    )

    assert isinstance(results[0], CallToolResult)
    assert isinstance(results[1], RuntimeError)
    assert shared_session._slow_task is not None
    assert isolated_session.call_tool_attempts == 1


@pytest.mark.asyncio
async def test_streamable_http_preserves_outer_cancellation():
    isolated_session = IsolatedRetrySession()
    server = DummyStreamableHttpServer(
        shared_session=cast(ConcurrentCancellationSession, HangingSession()),
        isolated_session=isolated_session,
    )

    task = asyncio.create_task(server.call_tool("slow", None))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert isolated_session.call_tool_attempts == 0
