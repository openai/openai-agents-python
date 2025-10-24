import pytest
from mcp.server.fastmcp import FastMCP


def test_fastmcp_invalid_name_type():
    with pytest.raises(TypeError, match="name must be a string or None"):
        FastMCP(name=123)
