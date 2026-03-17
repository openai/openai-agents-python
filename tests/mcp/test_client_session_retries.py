import asyncio
from typing import cast

import pytest
from mcp import ClientSession, Tool as MCPTool
from mcp.types import CallToolResult, ListToolsResult

from agents.exceptions import UserError
from agents.mcp.server import _MCPServerWithClientSession


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
    def __init__(self, session: DummySession, retries: int, *, serialize_requests: bool = False):
        super().__init__(
            cache_tools_list=False,
            client_session_timeout_seconds=None,
            max_retry_attempts=retries,
            retry_backoff_seconds_base=0,
        )
        self.session = cast(ClientSession, session)
        self._serialize_session_requests = serialize_requests

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

    async def list_tools(self):
        return ListToolsResult(tools=[MCPTool(name="tool", inputSchema={})])

    async def list_prompts(self):
        await self._slow_started.wait()
        assert self._slow_task is not None
        self._slow_task.cancel()
        raise RuntimeError("synthetic request failure")

    async def get_prompt(self, name, arguments=None):
        await self._slow_started.wait()
        assert self._slow_task is not None
        self._slow_task.cancel()
        raise RuntimeError("synthetic request failure")


@pytest.mark.asyncio
async def test_serialized_session_requests_prevent_sibling_cancellation():
    session = ConcurrentCancellationSession()
    server = DummyServer(session=cast(DummySession, session), retries=0, serialize_requests=True)

    results = await asyncio.gather(
        server.call_tool("slow", None),
        server.call_tool("fail", None),
        return_exceptions=True,
    )

    assert isinstance(results[0], CallToolResult)
    assert isinstance(results[1], RuntimeError)


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt_method", ["list_prompts", "get_prompt"])
async def test_serialized_prompt_requests_prevent_tool_cancellation(prompt_method: str):
    session = ConcurrentCancellationSession()
    server = DummyServer(session=cast(DummySession, session), retries=0, serialize_requests=True)

    prompt_request = (
        server.list_prompts() if prompt_method == "list_prompts" else server.get_prompt("prompt")
    )
    results = await asyncio.gather(
        server.call_tool("slow", None),
        prompt_request,
        return_exceptions=True,
    )

    assert isinstance(results[0], CallToolResult)
    assert isinstance(results[1], RuntimeError)
