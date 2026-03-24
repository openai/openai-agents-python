import asyncio
import sys
from contextlib import asynccontextmanager
from typing import cast

import httpx
import pytest
from anyio import ClosedResourceError
from mcp import ClientSession, Tool as MCPTool
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, ErrorData, GetPromptResult, ListPromptsResult, ListToolsResult

from agents.exceptions import UserError
from agents.mcp.server import MCPServerStreamableHttp, _MCPServerWithClientSession

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup  # pyright: ignore[reportMissingImports]


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


class CancelledToolSession:
    async def call_tool(self, tool_name, arguments, meta=None):
        raise asyncio.CancelledError("synthetic call cancellation")


class MixedExceptionGroupSession:
    async def call_tool(self, tool_name, arguments, meta=None):
        req = httpx.Request("POST", "https://example.test/mcp")
        resp = httpx.Response(401, request=req)
        raise BaseExceptionGroup(
            "mixed request failure",
            [
                asyncio.CancelledError("synthetic call cancellation"),
                httpx.HTTPStatusError("HTTP error 401", request=req, response=resp),
            ],
        )


class SharedHttpStatusSession:
    def __init__(self, status_code: int):
        self.status_code = status_code

    async def call_tool(self, tool_name, arguments, meta=None):
        req = httpx.Request("POST", "https://example.test/mcp")
        resp = httpx.Response(self.status_code, request=req)
        raise httpx.HTTPStatusError(
            f"HTTP error {self.status_code}",
            request=req,
            response=resp,
        )


class TimeoutSession:
    def __init__(self, message: str = "timed out"):
        self.call_tool_attempts = 0
        self.message = message

    async def call_tool(self, tool_name, arguments, meta=None):
        self.call_tool_attempts += 1
        raise httpx.TimeoutException(self.message)


class ClosedResourceSession:
    def __init__(self):
        self.call_tool_attempts = 0

    async def call_tool(self, tool_name, arguments, meta=None):
        self.call_tool_attempts += 1
        raise ClosedResourceError()


class McpRequestTimeoutSession:
    def __init__(self, message: str = "timed out"):
        self.call_tool_attempts = 0
        self.message = message

    async def call_tool(self, tool_name, arguments, meta=None):
        self.call_tool_attempts += 1
        raise McpError(
            ErrorData(code=httpx.codes.REQUEST_TIMEOUT, message=self.message),
        )


class IsolatedRetrySession:
    def __init__(self):
        self.call_tool_attempts = 0

    async def call_tool(self, tool_name, arguments, meta=None):
        self.call_tool_attempts += 1
        return CallToolResult(content=[])


class HangingSession:
    async def call_tool(self, tool_name, arguments, meta=None):
        await asyncio.sleep(10)


class DummyStreamableHttpServer(MCPServerStreamableHttp):
    def __init__(self, shared_session: object, isolated_session: object):
        super().__init__(
            params={"url": "https://example.test/mcp"},
            client_session_timeout_seconds=None,
            max_retry_attempts=0,
        )
        self.session = cast(ClientSession, shared_session)
        self._isolated_session = cast(ClientSession, isolated_session)

    @asynccontextmanager
    async def _isolated_client_session(self):
        yield self._isolated_session

    async def list_tools(self, run_context=None, agent=None):
        return [MCPTool(name="tool", inputSchema={})]

    async def list_prompts(self):
        return ListPromptsResult(prompts=[])

    async def get_prompt(self, name, arguments=None):
        raise NotImplementedError


class IsolatedSessionEnterFailure:
    def __init__(self, server: "EnterFailingStreamableHttpServer", message: str):
        self.server = server
        self.message = message

    async def __aenter__(self):
        self.server.isolated_enter_attempts += 1
        raise httpx.TimeoutException(self.message)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class EnterFailingStreamableHttpServer(DummyStreamableHttpServer):
    def __init__(self, shared_session: object, *, isolated_message: str):
        super().__init__(shared_session, IsolatedRetrySession())
        self.isolated_enter_attempts = 0
        self._isolated_message = isolated_message

    def _isolated_client_session(self):
        return IsolatedSessionEnterFailure(self, self._isolated_message)


@pytest.mark.asyncio
async def test_streamable_http_retries_cancelled_request_on_isolated_session():
    shared_session = CancelledToolSession()
    isolated_session = IsolatedRetrySession()
    server = DummyStreamableHttpServer(shared_session, isolated_session)
    server.max_retry_attempts = 1

    result = await server.call_tool("tool", None)

    assert isinstance(result, CallToolResult)
    assert isolated_session.call_tool_attempts == 1


@pytest.mark.asyncio
async def test_streamable_http_retries_5xx_on_isolated_session():
    isolated_session = IsolatedRetrySession()
    server = DummyStreamableHttpServer(SharedHttpStatusSession(504), isolated_session)
    server.max_retry_attempts = 1

    result = await server.call_tool("tool", None)

    assert isinstance(result, CallToolResult)
    assert isolated_session.call_tool_attempts == 1


@pytest.mark.asyncio
async def test_streamable_http_retries_closed_resource_on_isolated_session():
    isolated_session = IsolatedRetrySession()
    server = DummyStreamableHttpServer(ClosedResourceSession(), isolated_session)
    server.max_retry_attempts = 1

    result = await server.call_tool("tool", None)

    assert isinstance(result, CallToolResult)
    assert isolated_session.call_tool_attempts == 1


@pytest.mark.asyncio
async def test_streamable_http_retries_mcp_408_on_isolated_session():
    isolated_session = IsolatedRetrySession()
    server = DummyStreamableHttpServer(
        McpRequestTimeoutSession("Timed out while waiting for response to ClientRequest."),
        isolated_session,
    )
    server.max_retry_attempts = 1

    result = await server.call_tool("tool", None)

    assert isinstance(result, CallToolResult)
    assert isolated_session.call_tool_attempts == 1


@pytest.mark.asyncio
async def test_streamable_http_does_not_retry_4xx_on_isolated_session():
    isolated_session = IsolatedRetrySession()
    server = DummyStreamableHttpServer(SharedHttpStatusSession(401), isolated_session)

    with pytest.raises(UserError, match="HTTP error 401"):
        await server.call_tool("tool", None)

    assert isolated_session.call_tool_attempts == 0


@pytest.mark.asyncio
async def test_streamable_http_does_not_isolated_retry_without_retry_budget():
    isolated_session = IsolatedRetrySession()
    server = DummyStreamableHttpServer(CancelledToolSession(), isolated_session)
    server.max_retry_attempts = 0

    with pytest.raises(asyncio.CancelledError):
        await server.call_tool("tool", None)

    assert isolated_session.call_tool_attempts == 0


@pytest.mark.asyncio
async def test_streamable_http_counts_isolated_retry_against_retry_budget():
    shared_session = TimeoutSession("shared timed out")
    isolated_session = TimeoutSession("isolated timed out")
    server = DummyStreamableHttpServer(shared_session, isolated_session)
    server.max_retry_attempts = 2

    with pytest.raises(httpx.TimeoutException, match="shared timed out"):
        await server.call_tool("tool", None)

    assert shared_session.call_tool_attempts == 2
    assert isolated_session.call_tool_attempts == 1


@pytest.mark.asyncio
async def test_streamable_http_counts_isolated_session_setup_failure_against_retry_budget():
    shared_session = TimeoutSession("shared timed out")
    server = EnterFailingStreamableHttpServer(
        shared_session,
        isolated_message="isolated setup timed out",
    )
    server.max_retry_attempts = 2

    with pytest.raises(httpx.TimeoutException, match="shared timed out"):
        await server.call_tool("tool", None)

    assert shared_session.call_tool_attempts == 2
    assert server.isolated_enter_attempts == 1


@pytest.mark.asyncio
async def test_streamable_http_does_not_retry_mixed_exception_groups():
    isolated_session = IsolatedRetrySession()
    server = DummyStreamableHttpServer(MixedExceptionGroupSession(), isolated_session)
    server.max_retry_attempts = 1

    with pytest.raises(UserError, match="HTTP error 401"):
        await server.call_tool("tool", None)

    assert isolated_session.call_tool_attempts == 0


@pytest.mark.asyncio
async def test_streamable_http_preserves_outer_cancellation():
    isolated_session = IsolatedRetrySession()
    server = DummyStreamableHttpServer(HangingSession(), isolated_session)

    task = asyncio.create_task(server.call_tool("slow", None))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert isolated_session.call_tool_attempts == 0


@pytest.mark.asyncio
async def test_streamable_http_preserves_outer_cancellation_during_isolated_retry():
    server = DummyStreamableHttpServer(CancelledToolSession(), HangingSession())
    server.max_retry_attempts = 1

    task = asyncio.create_task(server.call_tool("tool", None))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


class ConcurrentPromptCancellationSession(ConcurrentCancellationSession):
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


class OverlapTrackingSession:
    def __init__(self):
        self.in_flight = 0
        self.max_in_flight = 0

    @asynccontextmanager
    async def _enter_request(self):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.02)
            yield
        finally:
            self.in_flight -= 1

    async def call_tool(self, tool_name, arguments, meta=None):
        async with self._enter_request():
            return CallToolResult(content=[])

    async def list_prompts(self):
        async with self._enter_request():
            return ListPromptsResult(prompts=[])

    async def get_prompt(self, name, arguments=None):
        async with self._enter_request():
            return GetPromptResult(
                description=None,
                messages=[],
            )


class DummyPromptStreamableHttpServer(DummyStreamableHttpServer):
    def __init__(
        self,
        shared_session: OverlapTrackingSession,
        isolated_session: IsolatedRetrySession,
    ):
        super().__init__(shared_session, isolated_session)
        self.session = cast(ClientSession, shared_session)

    async def list_prompts(self):
        session = self.session
        assert session is not None
        return await self._maybe_serialize_request(lambda: session.list_prompts())

    async def get_prompt(self, name, arguments=None):
        session = self.session
        assert session is not None
        return await self._maybe_serialize_request(lambda: session.get_prompt(name, arguments))


@pytest.mark.asyncio
async def test_serialized_session_requests_prevent_sibling_cancellation():
    session = ConcurrentPromptCancellationSession()
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
    session = ConcurrentPromptCancellationSession()
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


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt_method", ["list_prompts", "get_prompt"])
async def test_streamable_http_serializes_call_tool_with_prompt_requests(prompt_method: str):
    shared_session = OverlapTrackingSession()
    isolated_session = IsolatedRetrySession()
    server = DummyPromptStreamableHttpServer(shared_session, isolated_session)

    prompt_request = (
        server.list_prompts() if prompt_method == "list_prompts" else server.get_prompt("prompt")
    )
    results = await asyncio.gather(
        server.call_tool("slow", None),
        prompt_request,
        return_exceptions=True,
    )

    assert isinstance(results[0], CallToolResult)
    if prompt_method == "list_prompts":
        assert isinstance(results[1], ListPromptsResult)
    else:
        assert isinstance(results[1], GetPromptResult)
    assert shared_session.max_in_flight == 1
    assert isolated_session.call_tool_attempts == 0
