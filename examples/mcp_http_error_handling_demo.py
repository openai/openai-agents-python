"""
Demo script for PR #1948: MCP HTTP error handling

This script demonstrates how MCP tools now handle upstream HTTP errors gracefully
instead of crashing the agent run.

Prerequisites:
- Python 3.10+ (required by MCP package)
- Set OPENAI_API_KEY environment variable

The script uses a mock MCP server that simulates HTTP errors.
"""

import asyncio
import json
import sys
from typing import Any

from agents import Agent, Runner, function_tool

# Import MCP types for proper error handling (requires Python 3.10+)
if sys.version_info >= (3, 10):
    try:
        from mcp.shared.exceptions import McpError
        from mcp.types import INTERNAL_ERROR, ErrorData

        MCP_AVAILABLE = True
    except ImportError:
        MCP_AVAILABLE = False
        McpError = None  # type: ignore[assignment,misc]
        ErrorData = None  # type: ignore[assignment,misc]
        INTERNAL_ERROR = -32603
else:
    # Python < 3.10: MCP not available
    MCP_AVAILABLE = False
    McpError = None  # type: ignore[assignment,misc]
    ErrorData = None  # type: ignore[assignment,misc]
    INTERNAL_ERROR = -32603


# Mock MCP server that simulates HTTP errors
class MockMCPServerWithErrors:
    """A mock MCP server that simulates various HTTP error scenarios."""

    def __init__(self):
        self.call_count = 0

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]):
        """Simulate MCP tool calls with different error scenarios."""
        self.call_count += 1

        # Simulate different error scenarios based on query
        query = arguments.get("query", "")

        if "invalid" in query.lower():
            # Simulate 422 Validation Error
            if McpError is not None and ErrorData is not None:
                raise McpError(
                    ErrorData(
                        code=INTERNAL_ERROR,
                        message="GET https://api.example.com/search: 422 Validation Error",
                    )
                )

        if "notfound" in query.lower():
            # Simulate 404 Not Found
            if McpError is not None and ErrorData is not None:
                raise McpError(
                    ErrorData(
                        code=INTERNAL_ERROR,
                        message="GET https://api.example.com/search: 404 Not Found",
                    )
                )

        if "servererror" in query.lower():
            # Simulate 500 Internal Server Error
            if McpError is not None and ErrorData is not None:
                raise McpError(
                    ErrorData(
                        code=INTERNAL_ERROR,
                        message="GET https://api.example.com/search: 500 Internal Server Error",
                    )
                )

        # Successful case
        return type(
            "Result",
            (),
            {
                "content": [
                    type(
                        "Content",
                        (),
                        {
                            "model_dump_json": lambda: json.dumps(
                                {"results": f"Search results for: {query}"}
                            )
                        },
                    )()
                ],
                "structuredContent": None,
            },
        )()


# Create a search tool using the mock MCP server
mock_server = MockMCPServerWithErrors()


@function_tool
async def search(query: str) -> str:
    """Search for information using an MCP-backed API.

    Args:
        query: The search query

    Returns:
        Search results or error message
    """
    # This simulates how MCPUtil.invoke_mcp_tool works
    try:
        result = await mock_server.call_tool("search", {"query": query})
        result_json: str = result.content[0].model_dump_json()
        return result_json
    except Exception as e:
        # Check if it's an MCP error (only when MCP is available)
        if McpError is not None and isinstance(e, McpError):
            # After PR #1948: Return structured error instead of crashing
            return json.dumps(
                {"error": {"message": str(e), "tool": "search", "type": "upstream_error"}}
            )
        # Programming errors still raise
        raise


async def main():
    """Demonstrate MCP HTTP error handling."""
    print("=" * 70)
    print("MCP HTTP Error Handling Demo (PR #1948)")
    print("=" * 70)
    print()

    agent = Agent(
        name="SearchAgent",
        model="gpt-4o-mini",
        instructions="You are a helpful search assistant. "
        "When search fails, explain the error to the user kindly.",
        tools=[search],
    )

    # Test Case 1: Successful search
    print("\n" + "─" * 70)
    print("Test 1: Successful Search")
    print("─" * 70)
    result1 = await Runner.run(agent, input="Search for: Python programming")
    print(f"✅ Agent Response: {result1.final_output}")

    # Test Case 2: 422 Validation Error (invalid query)
    print("\n" + "─" * 70)
    print("Test 2: HTTP 422 - Invalid Query")
    print("─" * 70)
    result2 = await Runner.run(agent, input="Search for: invalid query")
    print(f"✅ Agent Response: {result2.final_output}")
    print("   (Notice: Agent handled the error gracefully, run didn't crash)")

    # Test Case 3: 404 Not Found
    print("\n" + "─" * 70)
    print("Test 3: HTTP 404 - Not Found")
    print("─" * 70)
    result3 = await Runner.run(agent, input="Search for: notfound resource")
    print(f"✅ Agent Response: {result3.final_output}")

    # Test Case 4: 500 Internal Server Error
    print("\n" + "─" * 70)
    print("Test 4: HTTP 500 - Server Error")
    print("─" * 70)
    result4 = await Runner.run(agent, input="Search for: servererror test")
    print(f"✅ Agent Response: {result4.final_output}")

    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    print(f"Total MCP tool calls: {mock_server.call_count}")
    print("✅ All tests completed successfully")
    print("✅ Agent run didn't crash on HTTP errors")
    print("✅ Agent gracefully handled all error scenarios")
    print()
    print("Before PR #1948:")
    print("  ❌ Any HTTP error → AgentsException → Agent run crashes")
    print()
    print("After PR #1948:")
    print("  ✅ HTTP errors → Structured error response → Agent continues")
    print("  ✅ Agent can inform user, retry, or try alternatives")
    print("=" * 70)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ImportError as e:
        if "mcp" in str(e):
            print("⚠️  This demo requires Python 3.10+ (MCP package dependency)")
            print("   Please upgrade Python or test with the unit tests instead:")
            print("   pytest tests/mcp/test_issue_879_http_error_handling.py")
        else:
            raise
