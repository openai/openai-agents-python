from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Reference Policy Server")


@mcp.tool()
def get_policy_reference(topic: str) -> str:
    """Return short internal policy guidance for a supported topic."""
    normalized = topic.strip().lower()
    if "discount" in normalized:
        return (
            "Discount policy: discounts from 11 to 15 percent require regional sales director "
            "approval. Discounts above 15 percent require both finance and the regional sales "
            "director."
        )
    if "security" in normalized or "review" in normalized:
        return (
            "Security review policy: any new data export workflow must finish security review "
            "before kickoff or production access."
        )
    return "No policy reference is available for that topic in this demo."


if __name__ == "__main__":
    mcp.run()
