from typing import Any

import pytest

from agents import Agent, Runner
from agents.mcp import MCPServer

from ..fake_model import FakeModel
from ..test_responses import get_text_message


class FakeMCPResourceServer(MCPServer):
    """Fake MCP server for testing resource functionality"""

    def __init__(self, server_name: str = "fake_resource_server"):
        self.resources: list[Any] = []
        self.resource_contents: dict[str, str] = {}
        self._server_name = server_name

    def add_resource(
        self, uri: str, name: str, description: str | None = None, mime_type: str = "text/plain"
    ):
        """Add a resource to the fake server"""
        from mcp.types import Resource
        from pydantic import AnyUrl

        uri_obj = AnyUrl(uri)
        resource = Resource(uri=uri_obj, name=name, description=description, mimeType=mime_type)
        self.resources.append(resource)

    def set_resource_content(self, uri: str, content: str):
        """Set the content that should be returned for a resource"""
        self.resource_contents[uri] = content

    async def connect(self):
        pass

    async def cleanup(self):
        pass

    async def list_resources(self, run_context=None, agent=None):
        """List available resources"""
        from mcp.types import ListResourcesResult

        return ListResourcesResult(resources=self.resources)

    async def read_resource(self, uri: str):
        """Read a resource"""
        from mcp.types import ReadResourceResult, TextResourceContents
        from pydantic import AnyUrl

        if uri not in self.resource_contents:
            raise ValueError(f"Resource '{uri}' not found")

        content = self.resource_contents[uri]
        uri_obj = AnyUrl(uri)
        contents = TextResourceContents(uri=uri_obj, mimeType="text/plain", text=content)

        return ReadResourceResult(contents=[contents])

    async def list_tools(self, run_context=None, agent=None):
        return []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None):
        raise NotImplementedError("This fake server doesn't support tools")

    async def list_prompts(self, run_context=None, agent=None):
        from mcp.types import ListPromptsResult

        return ListPromptsResult(prompts=[])

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None):
        raise NotImplementedError("This fake server doesn't support prompts")

    @property
    def name(self) -> str:
        return self._server_name


@pytest.mark.asyncio
async def test_list_resources():
    """Test listing available resources"""
    server = FakeMCPResourceServer()
    server.add_resource(
        uri="file:///sample.txt",
        name="sample.txt",
        description="A sample text file",
    )

    result = await server.list_resources()

    assert len(result.resources) == 1
    assert str(result.resources[0].uri) == "file:///sample.txt"
    assert result.resources[0].name == "sample.txt"
    assert result.resources[0].description == "A sample text file"


@pytest.mark.asyncio
async def test_read_resource():
    """Test reading a resource"""
    server = FakeMCPResourceServer()
    server.add_resource(
        uri="file:///sample.txt",
        name="sample.txt",
        description="A sample text file",
    )
    server.set_resource_content("file:///sample.txt", "This is the content of sample.txt")

    result = await server.read_resource("file:///sample.txt")

    assert len(result.contents) == 1
    assert str(result.contents[0].uri) == "file:///sample.txt"
    assert result.contents[0].text == "This is the content of sample.txt"
    assert result.contents[0].mimeType == "text/plain"


@pytest.mark.asyncio
async def test_read_resource_not_found():
    """Test reading a resource that doesn't exist"""
    server = FakeMCPResourceServer()

    with pytest.raises(ValueError, match="Resource 'file:///nonexistent.txt' not found"):
        await server.read_resource("file:///nonexistent.txt")


@pytest.mark.asyncio
async def test_multiple_resources():
    """Test server with multiple resources"""
    server = FakeMCPResourceServer()

    # Add multiple resources
    server.add_resource(
        uri="file:///doc1.txt",
        name="doc1.txt",
        description="First document",
    )
    server.add_resource(
        uri="file:///doc2.txt",
        name="doc2.txt",
        description="Second document",
    )

    server.set_resource_content("file:///doc1.txt", "Content of document 1")
    server.set_resource_content("file:///doc2.txt", "Content of document 2")

    # Test listing resources
    resources_result = await server.list_resources()
    assert len(resources_result.resources) == 2

    resource_uris = [str(r.uri) for r in resources_result.resources]
    assert "file:///doc1.txt" in resource_uris
    assert "file:///doc2.txt" in resource_uris

    # Test reading each resource
    doc1_result = await server.read_resource("file:///doc1.txt")
    assert doc1_result.contents[0].text == "Content of document 1"

    doc2_result = await server.read_resource("file:///doc2.txt")
    assert doc2_result.contents[0].text == "Content of document 2"


@pytest.mark.asyncio
async def test_resource_with_different_mime_types():
    """Test resources with different MIME types"""
    server = FakeMCPResourceServer()

    server.add_resource(
        uri="file:///data.json",
        name="data.json",
        description="JSON data file",
        mime_type="application/json",
    )
    server.add_resource(
        uri="file:///readme.md",
        name="readme.md",
        description="Markdown file",
        mime_type="text/markdown",
    )

    result = await server.list_resources()

    assert len(result.resources) == 2
    json_resource = next(r for r in result.resources if str(r.uri) == "file:///data.json")
    assert json_resource.mimeType == "application/json"

    md_resource = next(r for r in result.resources if str(r.uri) == "file:///readme.md")
    assert md_resource.mimeType == "text/markdown"


@pytest.mark.asyncio
async def test_agent_with_resource_server():
    """Test using an agent with a resource server"""
    server = FakeMCPResourceServer()
    server.add_resource(
        uri="file:///context.txt",
        name="context.txt",
        description="Context information",
    )
    server.set_resource_content(
        "file:///context.txt", "Important context: Project is written in Python 3.11"
    )

    # Get context from resource
    resource_result = await server.read_resource("file:///context.txt")
    context = resource_result.contents[0].text

    # Create agent with resource context
    model = FakeModel()
    agent = Agent(
        name="resource_agent",
        instructions=f"You are a helpful assistant. {context}",
        model=model,
        mcp_servers=[server],
    )

    # Mock model response
    model.add_multiple_turn_outputs(
        [[get_text_message("Based on the context, I know the project uses Python 3.11.")]]
    )

    # Run the agent
    result = await Runner.run(agent, input="What version of Python does the project use?")

    assert "Python 3.11" in result.final_output or "python 3.11" in result.final_output.lower()
    # Check instructions (it could be a string or callable)
    instructions = agent.instructions if isinstance(agent.instructions, str) else ""
    assert "Important context: Project is written in Python 3.11" in instructions


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_agent_with_resources_streaming(streaming: bool):
    """Test using resources with streaming and non-streaming"""
    server = FakeMCPResourceServer()
    server.add_resource(
        uri="file:///config.txt",
        name="config.txt",
        description="Configuration",
    )
    server.set_resource_content("file:///config.txt", "Server port: 8080")

    # Get configuration from resource
    resource_result = await server.read_resource("file:///config.txt")
    config = resource_result.contents[0].text

    # Create agent
    model = FakeModel()
    agent = Agent(
        name="streaming_resource_agent",
        instructions=f"Configuration: {config}",
        model=model,
        mcp_servers=[server],
    )

    model.add_multiple_turn_outputs([[get_text_message("The server runs on port 8080.")]])

    if streaming:
        streaming_result = Runner.run_streamed(agent, input="What port does the server run on?")
        async for _ in streaming_result.stream_events():
            pass
        final_result = streaming_result.final_output
    else:
        result = await Runner.run(agent, input="What port does the server run on?")
        final_result = result.final_output

    assert "8080" in final_result


@pytest.mark.asyncio
async def test_resource_cleanup():
    """Test that resource server cleanup works correctly"""
    server = FakeMCPResourceServer()
    server.add_resource("file:///test.txt", "test.txt", "Test file")
    server.set_resource_content("file:///test.txt", "Test content")

    # Test that server works before cleanup
    result = await server.read_resource("file:///test.txt")
    assert result.contents[0].text == "Test content"

    # Cleanup should not raise any errors
    await server.cleanup()

    # Server should still work after cleanup (in this fake implementation)
    result = await server.read_resource("file:///test.txt")
    assert result.contents[0].text == "Test content"


@pytest.mark.asyncio
async def test_empty_resource_list():
    """Test listing resources when none are available"""
    server = FakeMCPResourceServer()

    result = await server.list_resources()

    assert len(result.resources) == 0


@pytest.mark.asyncio
async def test_resource_without_description():
    """Test resource without optional description"""
    server = FakeMCPResourceServer()
    server.add_resource(
        uri="file:///minimal.txt",
        name="minimal.txt",
    )

    result = await server.list_resources()

    assert len(result.resources) == 1
    assert str(result.resources[0].uri) == "file:///minimal.txt"
    assert result.resources[0].name == "minimal.txt"


@pytest.mark.asyncio
async def test_resource_uri_formats():
    """Test various URI formats for resources"""
    server = FakeMCPResourceServer()

    # Test different URI schemes
    server.add_resource("file:///path/to/file.txt", "file.txt", "File URI")
    server.add_resource("http://example.com/resource", "web-resource", "HTTP URI")
    server.add_resource("custom://namespace/resource", "custom-resource", "Custom URI")

    result = await server.list_resources()

    assert len(result.resources) == 3
    uris = [str(r.uri) for r in result.resources]
    assert "file:///path/to/file.txt" in uris
    assert "http://example.com/resource" in uris
    assert "custom://namespace/resource" in uris
