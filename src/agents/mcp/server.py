from __future__ import annotations

import abc
import asyncio
import inspect
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp import ClientSession, StdioServerParameters, Tool as MCPTool, stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import GetSessionIdCallback, streamablehttp_client
from mcp.shared.message import SessionMessage
from mcp.types import CallToolResult, GetPromptResult, InitializeResult, ListPromptsResult
from typing_extensions import NotRequired, TypedDict

from ..exceptions import UserError
from ..logger import logger
from ..run_context import RunContextWrapper
from .util import ToolFilter, ToolFilterCallable, ToolFilterContext, ToolFilterStatic

if TYPE_CHECKING:
    from ..agent import AgentBase


class MCPServer(abc.ABC):
    """Base class for Model Context Protocol servers."""

    def __init__(self, use_structured_content: bool = False):
        """
        Args:
            use_structured_content: Whether to use `tool_result.structured_content` when calling an
                MCP tool.Defaults to False for backwards compatibility - most MCP servers still
                include the structured content in the `tool_result.content`, and using it by
                default will cause duplicate content. You can set this to True if you know the
                server will not duplicate the structured content in the `tool_result.content`.
        """
        self.use_structured_content = use_structured_content

    @abc.abstractmethod
    async def connect(self):
        """Connect to the server. For example, this might mean spawning a subprocess or
        opening a network connection. The server is expected to remain connected until
        `cleanup()` is called.
        """
        pass

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """A readable name for the server."""
        pass

    @abc.abstractmethod
    async def cleanup(self):
        """Cleanup the server. For example, this might mean closing a subprocess or
        closing a network connection.
        """
        pass

    @abc.abstractmethod
    async def list_tools(
        self,
        run_context: RunContextWrapper[Any] | None = None,
        agent: AgentBase | None = None,
    ) -> list[MCPTool]:
        """List the tools available on the server."""
        pass

    @abc.abstractmethod
    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        """Invoke a tool on the server."""
        pass

    @abc.abstractmethod
    async def list_prompts(
        self,
    ) -> ListPromptsResult:
        """List the prompts available on the server."""
        pass

    @abc.abstractmethod
    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        """Get a specific prompt from the server."""
        pass


class _MCPServerWithClientSession(MCPServer, abc.ABC):
    """Base class for MCP servers that use a `ClientSession` to communicate with the server."""

    def __init__(
        self,
        cache_tools_list: bool,
        client_session_timeout_seconds: float | None,
        tool_filter: ToolFilter = None,
        use_structured_content: bool = False,
    ):
        """
        Args:
            cache_tools_list: Whether to cache the tools list. If `True`, the tools list will be
            cached and only fetched from the server once. If `False`, the tools list will be
            fetched from the server on each call to `list_tools()`. The cache can be invalidated
            by calling `invalidate_tools_cache()`. You should set this to `True` if you know the
            server will not change its tools list, because it can drastically improve latency
            (by avoiding a round-trip to the server every time).

            client_session_timeout_seconds: the read timeout passed to the MCP ClientSession.
            tool_filter: The tool filter to use for filtering tools.
            use_structured_content: Whether to use `tool_result.structured_content` when calling an
                MCP tool. Defaults to False for backwards compatibility - most MCP servers still
                include the structured content in the `tool_result.content`, and using it by
                default will cause duplicate content. You can set this to True if you know the
                server will not duplicate the structured content in the `tool_result.content`.
        """
        super().__init__(use_structured_content=use_structured_content)
        self.session: ClientSession | None = None
        self.exit_stack: AsyncExitStack = AsyncExitStack()
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self.cache_tools_list = cache_tools_list
        self.server_initialize_result: InitializeResult | None = None

        self.client_session_timeout_seconds = client_session_timeout_seconds

        # The cache is always dirty at startup, so that we fetch tools at least once
        self._cache_dirty = True
        self._tools_list: list[MCPTool] | None = None

        self.tool_filter = tool_filter

    async def _apply_tool_filter(
        self,
        tools: list[MCPTool],
        run_context: RunContextWrapper[Any],
        agent: AgentBase,
    ) -> list[MCPTool]:
        """Apply the tool filter to the list of tools."""
        if self.tool_filter is None:
            return tools

        # Handle static tool filter
        if isinstance(self.tool_filter, dict):
            return self._apply_static_tool_filter(tools, self.tool_filter)

        # Handle callable tool filter (dynamic filter)
        else:
            return await self._apply_dynamic_tool_filter(tools, run_context, agent)

    def _apply_static_tool_filter(
        self, tools: list[MCPTool], static_filter: ToolFilterStatic
    ) -> list[MCPTool]:
        """Apply static tool filtering based on allowlist and blocklist."""
        filtered_tools = tools

        # Apply allowed_tool_names filter (whitelist)
        if "allowed_tool_names" in static_filter:
            allowed_names = static_filter["allowed_tool_names"]
            filtered_tools = [t for t in filtered_tools if t.name in allowed_names]

        # Apply blocked_tool_names filter (blacklist)
        if "blocked_tool_names" in static_filter:
            blocked_names = static_filter["blocked_tool_names"]
            filtered_tools = [t for t in filtered_tools if t.name not in blocked_names]

        return filtered_tools

    async def _apply_dynamic_tool_filter(
        self,
        tools: list[MCPTool],
        run_context: RunContextWrapper[Any],
        agent: AgentBase,
    ) -> list[MCPTool]:
        """Apply dynamic tool filtering using a callable filter function."""

        # Ensure we have a callable filter and cast to help mypy
        if not callable(self.tool_filter):
            raise ValueError("Tool filter must be callable for dynamic filtering")
        tool_filter_func = cast(ToolFilterCallable, self.tool_filter)

        # Create filter context
        filter_context = ToolFilterContext(
            run_context=run_context,
            agent=agent,
            server_name=self.name,
        )

        filtered_tools = []
        for tool in tools:
            try:
                # Call the filter function with context
                result = tool_filter_func(filter_context, tool)

                if inspect.isawaitable(result):
                    should_include = await result
                else:
                    should_include = result

                if should_include:
                    filtered_tools.append(tool)
            except Exception as e:
                logger.error(
                    f"Error applying tool filter to tool '{tool.name}' on server '{self.name}': {e}"
                )
                # On error, exclude the tool for safety
                continue

        return filtered_tools

    @abc.abstractmethod
    def create_streams(
        self,
    ) -> AbstractAsyncContextManager[
        tuple[
            MemoryObjectReceiveStream[SessionMessage | Exception],
            MemoryObjectSendStream[SessionMessage],
            GetSessionIdCallback | None,
        ]
    ]:
        """Create the streams for the server."""
        pass

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.cleanup()

    def invalidate_tools_cache(self):
        """Invalidate the tools cache."""
        self._cache_dirty = True

    async def connect(self):
        """Connect to the server."""
        try:
            transport = await self.exit_stack.enter_async_context(self.create_streams())
            # streamablehttp_client returns (read, write, get_session_id)
            # sse_client returns (read, write)

            read, write, *_ = transport

            session = await self.exit_stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    timedelta(seconds=self.client_session_timeout_seconds)
                    if self.client_session_timeout_seconds
                    else None,
                )
            )
            server_result = await session.initialize()
            self.server_initialize_result = server_result
            self.session = session
        except Exception as e:
            logger.error(f"Error initializing MCP server: {e}")
            await self.cleanup()
            raise

    async def list_tools(
        self,
        run_context: RunContextWrapper[Any] | None = None,
        agent: AgentBase | None = None,
    ) -> list[MCPTool]:
        """List the tools available on the server."""
        if not self.session:
            raise UserError("Server not initialized. Make sure you call `connect()` first.")

        # Return from cache if caching is enabled, we have tools, and the cache is not dirty
        if self.cache_tools_list and not self._cache_dirty and self._tools_list:
            tools = self._tools_list
        else:
            # Reset the cache dirty to False
            self._cache_dirty = False
            # Fetch the tools from the server
            self._tools_list = (await self.session.list_tools()).tools
            tools = self._tools_list

        # Filter tools based on tool_filter
        filtered_tools = tools
        if self.tool_filter is not None:
            if run_context is None or agent is None:
                raise UserError("run_context and agent are required for dynamic tool filtering")
            filtered_tools = await self._apply_tool_filter(filtered_tools, run_context, agent)
        return filtered_tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        """Invoke a tool on the server."""
        if not self.session:
            raise UserError("Server not initialized. Make sure you call `connect()` first.")

        return await self.session.call_tool(tool_name, arguments)

    async def list_prompts(
        self,
    ) -> ListPromptsResult:
        """List the prompts available on the server."""
        if not self.session:
            raise UserError("Server not initialized. Make sure you call `connect()` first.")

        return await self.session.list_prompts()

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        """Get a specific prompt from the server."""
        if not self.session:
            raise UserError("Server not initialized. Make sure you call `connect()` first.")

        return await self.session.get_prompt(name, arguments)

    async def cleanup(self):
        """Cleanup the server."""
        async with self._cleanup_lock:
            try:
                await self.exit_stack.aclose()
            except Exception as e:
                logger.error(f"Error cleaning up server: {e}")
            finally:
                self.session = None


class MCPServerStdioParams(TypedDict):
    """Mirrors `mcp.client.stdio.StdioServerParameters`, but lets you pass params without another
    import.
    """

    command: str
    """The executable to run to start the server. For example, `python` or `node`."""

    args: NotRequired[list[str]]
    """Command line args to pass to the `command` executable. For example, `['foo.py']` or
    `['server.js', '--port', '8080']`."""

    env: NotRequired[dict[str, str]]
    """The environment variables to set for the server. ."""

    cwd: NotRequired[str | Path]
    """The working directory to use when spawning the process."""

    encoding: NotRequired[str]
    """The text encoding used when sending/receiving messages to the server. Defaults to `utf-8`."""

    encoding_error_handler: NotRequired[Literal["strict", "ignore", "replace"]]
    """The text encoding error handler. Defaults to `strict`.

    See https://docs.python.org/3/library/codecs.html#codec-base-classes for
    explanations of possible values.
    """


class MCPServerStdio(_MCPServerWithClientSession):
    """MCP server implementation that uses the stdio transport. See the [spec]
    (https://spec.modelcontextprotocol.io/specification/2024-11-05/basic/transports/#stdio) for
    details.
    """

    def __init__(
        self,
        params: MCPServerStdioParams,
        cache_tools_list: bool = False,
        name: str | None = None,
        client_session_timeout_seconds: float | None = 5,
        tool_filter: ToolFilter = None,
        use_structured_content: bool = False,
    ):
        """Create a new MCP server based on the stdio transport.

        Args:
            params: The params that configure the server. This includes the command to run to
                start the server, the args to pass to the command, the environment variables to
                set for the server, the working directory to use when spawning the process, and
                the text encoding used when sending/receiving messages to the server.
            cache_tools_list: Whether to cache the tools list. If `True`, the tools list will be
                cached and only fetched from the server once. If `False`, the tools list will be
                fetched from the server on each call to `list_tools()`. The cache can be
                invalidated by calling `invalidate_tools_cache()`. You should set this to `True`
                if you know the server will not change its tools list, because it can drastically
                improve latency (by avoiding a round-trip to the server every time).
            name: A readable name for the server. If not provided, we'll create one from the
                command.
            client_session_timeout_seconds: the read timeout passed to the MCP ClientSession.
            tool_filter: The tool filter to use for filtering tools.
            use_structured_content: Whether to use `tool_result.structured_content` when calling an
                MCP tool. Defaults to False for backwards compatibility - most MCP servers still
                include the structured content in the `tool_result.content`, and using it by
                default will cause duplicate content. You can set this to True if you know the
                server will not duplicate the structured content in the `tool_result.content`.
        """
        super().__init__(
            cache_tools_list,
            client_session_timeout_seconds,
            tool_filter,
            use_structured_content,
        )

        self.params = StdioServerParameters(
            command=params["command"],
            args=params.get("args", []),
            env=params.get("env"),
            cwd=params.get("cwd"),
            encoding=params.get("encoding", "utf-8"),
            encoding_error_handler=params.get("encoding_error_handler", "strict"),
        )

        self._name = name or f"stdio: {self.params.command}"

    def create_streams(
        self,
    ) -> AbstractAsyncContextManager[
        tuple[
            MemoryObjectReceiveStream[SessionMessage | Exception],
            MemoryObjectSendStream[SessionMessage],
            GetSessionIdCallback | None,
        ]
    ]:
        """Create the streams for the server."""
        return stdio_client(self.params)

    @property
    def name(self) -> str:
        """A readable name for the server."""
        return self._name


class MCPServerSseParams(TypedDict):
    """Mirrors the params in`mcp.client.sse.sse_client`."""

    url: str
    """The URL of the server."""

    headers: NotRequired[dict[str, str]]
    """The headers to send to the server."""

    timeout: NotRequired[float]
    """The timeout for the HTTP request. Defaults to 5 seconds."""

    sse_read_timeout: NotRequired[float]
    """The timeout for the SSE connection, in seconds. Defaults to 5 minutes."""


class MCPServerSse(_MCPServerWithClientSession):
    """MCP server implementation that uses the HTTP with SSE transport. See the [spec]
    (https://spec.modelcontextprotocol.io/specification/2024-11-05/basic/transports/#http-with-sse)
    for details.
    """

    def __init__(
        self,
        params: MCPServerSseParams,
        cache_tools_list: bool = False,
        name: str | None = None,
        client_session_timeout_seconds: float | None = 5,
        tool_filter: ToolFilter = None,
        use_structured_content: bool = False,
    ):
        """Create a new MCP server based on the HTTP with SSE transport.

        Args:
            params: The params that configure the server. This includes the URL of the server,
                the headers to send to the server, the timeout for the HTTP request, and the
                timeout for the SSE connection.

            cache_tools_list: Whether to cache the tools list. If `True`, the tools list will be
                cached and only fetched from the server once. If `False`, the tools list will be
                fetched from the server on each call to `list_tools()`. The cache can be
                invalidated by calling `invalidate_tools_cache()`. You should set this to `True`
                if you know the server will not change its tools list, because it can drastically
                improve latency (by avoiding a round-trip to the server every time).

            name: A readable name for the server. If not provided, we'll create one from the
                URL.

            client_session_timeout_seconds: the read timeout passed to the MCP ClientSession.
            tool_filter: The tool filter to use for filtering tools.
            use_structured_content: Whether to use `tool_result.structured_content` when calling an
                MCP tool. Defaults to False for backwards compatibility - most MCP servers still
                include the structured content in the `tool_result.content`, and using it by
                default will cause duplicate content. You can set this to True if you know the
                server will not duplicate the structured content in the `tool_result.content`.
        """
        super().__init__(
            cache_tools_list,
            client_session_timeout_seconds,
            tool_filter,
            use_structured_content,
        )

        self.params = params
        self._name = name or f"sse: {self.params['url']}"

    def create_streams(
        self,
    ) -> AbstractAsyncContextManager[
        tuple[
            MemoryObjectReceiveStream[SessionMessage | Exception],
            MemoryObjectSendStream[SessionMessage],
            GetSessionIdCallback | None,
        ]
    ]:
        """Create the streams for the server."""
        return sse_client(
            url=self.params["url"],
            headers=self.params.get("headers", None),
            timeout=self.params.get("timeout", 5),
            sse_read_timeout=self.params.get("sse_read_timeout", 60 * 5),
        )

    @property
    def name(self) -> str:
        """A readable name for the server."""
        return self._name


class MCPServerStreamableHttpParams(TypedDict):
    """Mirrors the params in`mcp.client.streamable_http.streamablehttp_client`."""

    url: str
    """The URL of the server."""

    headers: NotRequired[dict[str, str]]
    """The headers to send to the server."""

    timeout: NotRequired[timedelta | float]
    """The timeout for the HTTP request. Defaults to 5 seconds."""

    sse_read_timeout: NotRequired[timedelta | float]
    """The timeout for the SSE connection, in seconds. Defaults to 5 minutes."""

    terminate_on_close: NotRequired[bool]
    """Terminate on close"""


class MCPServerStreamableHttp(_MCPServerWithClientSession):
    """MCP server implementation that uses the Streamable HTTP transport. See the [spec]
    (https://modelcontextprotocol.io/specification/2025-03-26/basic/transports#streamable-http)
    for details.
    """

    def __init__(
        self,
        params: MCPServerStreamableHttpParams,
        cache_tools_list: bool = False,
        name: str | None = None,
        client_session_timeout_seconds: float | None = 5,
        tool_filter: ToolFilter = None,
        use_structured_content: bool = False,
    ):
        """Create a new MCP server based on the Streamable HTTP transport.

        Args:
            params: The params that configure the server. This includes the URL of the server,
                the headers to send to the server, the timeout for the HTTP request, and the
                timeout for the Streamable HTTP connection and whether we need to
                terminate on close.

            cache_tools_list: Whether to cache the tools list. If `True`, the tools list will be
                cached and only fetched from the server once. If `False`, the tools list will be
                fetched from the server on each call to `list_tools()`. The cache can be
                invalidated by calling `invalidate_tools_cache()`. You should set this to `True`
                if you know the server will not change its tools list, because it can drastically
                improve latency (by avoiding a round-trip to the server every time).

            name: A readable name for the server. If not provided, we'll create one from the
                URL.

            client_session_timeout_seconds: the read timeout passed to the MCP ClientSession.
            tool_filter: The tool filter to use for filtering tools.
            use_structured_content: Whether to use `tool_result.structured_content` when calling an
                MCP tool. Defaults to False for backwards compatibility - most MCP servers still
                include the structured content in the `tool_result.content`, and using it by
                default will cause duplicate content. You can set this to True if you know the
                server will not duplicate the structured content in the `tool_result.content`.
        """
        super().__init__(
            cache_tools_list,
            client_session_timeout_seconds,
            tool_filter,
            use_structured_content,
        )

        self.params = params
        self._name = name or f"streamable_http: {self.params['url']}"

    def create_streams(
        self,
    ) -> AbstractAsyncContextManager[
        tuple[
            MemoryObjectReceiveStream[SessionMessage | Exception],
            MemoryObjectSendStream[SessionMessage],
            GetSessionIdCallback | None,
        ]
    ]:
        """Create the streams for the server."""
        return streamablehttp_client(
            url=self.params["url"],
            headers=self.params.get("headers", None),
            timeout=self.params.get("timeout", 5),
            sse_read_timeout=self.params.get("sse_read_timeout", 60 * 5),
            terminate_on_close=self.params.get("terminate_on_close", True),
        )

    @property
    def name(self) -> str:
        """A readable name for the server."""
        return self._name
