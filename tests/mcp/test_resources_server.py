import pytest
from pydantic import AnyUrl

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
    server.add_resource(AnyUrl("docs://{section}/search"), name="Docs Search")

    result = await server.read_resource(AnyUrl("docs://{section}/search"))
    assert result.name == "Docs Search"
