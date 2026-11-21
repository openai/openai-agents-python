"""Trusted stdio source for one synthetic user."""

URI = "accounts://demo/picker"
META = {"example.com/widget": "account-picker"}
MIME = "text/plain"

# The source owns these grants. Neither the widget nor the agent supplies a user ID.
GRANTS = ("Everyday", "Savings")


def accounts() -> str:
    if not GRANTS:
        raise PermissionError("No accounts available")
    return "\n".join(GRANTS)


def confirm_account(alias: str) -> str:
    if alias not in GRANTS:
        raise PermissionError("Account unavailable")
    return alias


if __name__ == "__main__":
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP as MCPServer

    server = MCPServer("Demo accounts")
    server.resource(URI, mime_type=MIME, meta=META)(accounts)
    server.tool()(confirm_account)
    server.run(transport="stdio")
