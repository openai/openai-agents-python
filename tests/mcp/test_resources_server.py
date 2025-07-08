import pytest
from pydantic import AnyUrl

from agents import Agent, Runner

from .helpers import FakeMCPServer


@pytest.mark.asyncio
async def test_list_resources():
    """Test listing available resources"""
    server = FakeMCPServer()
    server.add_resource(uri=AnyUrl("docs://api/reference"), name="reference")

    result = await server.list_resources()
    assert len(result.resources) == 1
    assert result.resources[0].uri == AnyUrl("docs://api/reference")
    assert result.resources[0].name == "reference"

@pytest.mark.asyncio
async def test_list_resource_templates():
    """Test listing available resource templates"""
    server = FakeMCPServer()
    server.add_resource_template(uri="docs://{section}/search", name="Docs Search")
    server.add_resource_template(uri="api://{router}/get", name="APIs Search")

    result = await server.list_resource_templates()
    assert len(result.resourceTemplates) == 2
    assert result.resourceTemplates[0].uriTemplate == "docs://{section}/search"
    assert result.resourceTemplates[0].name == "Docs Search"

@pytest.mark.asyncio
async def test_read_resource():
    """Test getting a resource"""
    server = FakeMCPServer()
    server.add_resource(AnyUrl("docs://api/reference"), name="Docs Search")

    await server.read_resource(AnyUrl("docs://api/reference"))

@pytest.mark.asyncio
async def test_read_resource_not_found():
    """Test getting a resource that doesn't exist"""
    server = FakeMCPServer()

    uri = "docs://api/reference"
    with pytest.raises(KeyError, match=f"Resource {uri} not found"):
        await server.read_resource(AnyUrl(uri))

@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_agent_with_resources(streaming: bool):
    """Test agent with resources"""
    server = FakeMCPServer()

    agent = Agent(
        name="Assistant",
        instructions="Answer users queries using the available resources",
        mcp_servers=[server],
    )

    message = "What's the process to access the APIs? What are the available endpoints?"
    if streaming:
        streaming_result = Runner.run_streamed(agent, input=message)
        async for _ in streaming_result.stream_events():
            pass
        final_result = streaming_result.final_output
    else:
        result = await Runner.run(agent, input=message)
        final_result = result.final_output

    assert final_result is not None
