import asyncio
import json
import shutil
from typing import Any

from mcp import Tool as MCPTool
from mcp.types import CallToolResult, GetPromptResult, ListPromptsResult, PromptMessage, TextContent

from agents.mcp import MCPServer
from agents.mcp.server import _MCPServerWithClientSession
from agents.mcp.util import ToolFilter

tee = shutil.which("tee") or ""
assert tee, "tee not found"


# Added dummy stream classes for patching stdio_client to avoid real I/O during tests
class DummyStream:
    async def send(self, msg):
        pass

    async def receive(self):
        raise Exception("Dummy receive not implemented")


class DummyStreamsContextManager:
    async def __aenter__(self):
        return (DummyStream(), DummyStream())

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class _TestFilterServer(_MCPServerWithClientSession):
    """Minimal implementation of _MCPServerWithClientSession for testing tool filtering"""

    def __init__(self, tool_filter: ToolFilter, server_name: str):
        # Initialize parent class properly to avoid type errors
        super().__init__(
            cache_tools_list=False,
            client_session_timeout_seconds=None,
            tool_filter=tool_filter,
        )
        self._server_name: str = server_name
        # Override some attributes for test isolation
        self.session = None
        self._cleanup_lock = asyncio.Lock()

    def create_streams(self):
        raise NotImplementedError("Not needed for filtering tests")

    @property
    def name(self) -> str:
        return self._server_name


class FakeMCPServer(MCPServer):
    def __init__(
        self,
        tools: list[MCPTool] | None = None,
        tool_filter: ToolFilter = None,
        server_name: str = "fake_mcp_server",
    ):
        super().__init__(use_structured_content=False)
        self.tools: list[MCPTool] = tools or []
        self.tool_calls: list[str] = []
        self.tool_results: list[str] = []
        self.tool_filter = tool_filter
        self._server_name = server_name

    def add_tool(self, name: str, input_schema: dict[str, Any]):
        self.tools.append(MCPTool(name=name, inputSchema=input_schema))

    async def connect(self):
        pass

    async def cleanup(self):
        pass

    async def list_tools(self, run_context=None, agent=None):
        tools = self.tools

        # Apply tool filtering using the REAL implementation
        if self.tool_filter is not None:
            # Use the real _MCPServerWithClientSession filtering logic
            filter_server = _TestFilterServer(self.tool_filter, self.name)
            tools = await filter_server._apply_tool_filter(tools, run_context, agent)

        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        self.tool_calls.append(tool_name)
        self.tool_results.append(f"result_{tool_name}_{json.dumps(arguments)}")
        return CallToolResult(
            content=[TextContent(text=self.tool_results[-1], type="text")],
        )

    async def list_prompts(self, run_context=None, agent=None) -> ListPromptsResult:
        """Return empty list of prompts for fake server"""
        return ListPromptsResult(prompts=[])

    async def get_prompt(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> GetPromptResult:
        """Return a simple prompt result for fake server"""
        content = f"Fake prompt content for {name}"
        message = PromptMessage(role="user", content=TextContent(type="text", text=content))
        return GetPromptResult(description=f"Fake prompt: {name}", messages=[message])

    @property
    def name(self) -> str:
        return self._server_name
