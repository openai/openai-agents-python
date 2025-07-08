import pytest
from pydantic import AnyUrl

from .helpers import FakeMCPServer


@pytest.mark.asyncio
async def test_list_resources():
    """Test listing available resources"""
    server = FakeMCPServer()
    server.add_resource(uri=AnyUrl("docs://api/reference"), name="reference")

    result = await server.list_resources()
    assert len(result) == 1
    assert result.resources[0].uri == AnyUrl("docs://api/reference")
    assert result.resources[0].name == "reference"
