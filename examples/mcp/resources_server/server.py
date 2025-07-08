from mcp.server.fastmcp import FastMCP


mcp = FastMCP("Resources Server")


API_REFERENCE_MD = """
# Company API Reference

## Authentication
Use the `Authorization: Bearer <token>` header.

### Endpoints
| Method | Path               | Description        |
|--------|--------------------|--------------------|
| GET    | /users             | List users         |
| POST   | /users             | Create a new user  |
| GET    | /users/{{id}}      | Retrieve a user    |

"""

GETTING_STARTED_MD = """
# Getting Started Guide

Welcome! Follow these steps to get productive quickly.

1. Sign up for an account.
2. Generate an API token.
3. Call `GET /users` to verify your setup.

"""

CHANGELOG_MD = """
# Latest Changelog

## v2.1.0 — 2025-07-01
* Added OAuth 2.1 support
* Reduced request latency by 25 %
* Fixed edge-case bug in /reports endpoint
"""


# ──────────────────────────────────────────────────────────────────────
# 1. Static resources
# ──────────────────────────────────────────────────────────────────────
@mcp.resource(
    "docs://api/reference",
    name="Company API Reference",
    description=(
        "Static Markdown reference covering authentication, every endpoint’s "
        "method and path, request/response schema, and example payloads."
    ),
)
def api_reference() -> str:
    return API_REFERENCE_MD


@mcp.resource(
    "docs://guides/getting-started",
    name="Getting Started Guide",
    description=(
        "Introductory walkthrough for new developers: account creation, token "
        "generation, first API call, and common troubleshooting tips."
    ),
)
def getting_started() -> str:
    return GETTING_STARTED_MD


# ──────────────────────────────────────────────────────────────────────
# 2. Dynamic (async) resource
# ──────────────────────────────────────────────────────────────────────
@mcp.resource(
    "docs://changelog/latest",
    name="Latest Changelog",
    description=(
        "Async resource that delivers the most recent release notes at read-time. "
        "Useful for surfacing new features and bug fixes to the LLM."
    ),
)
async def latest_changelog() -> str:
    return CHANGELOG_MD


# ──────────────────────────────────────────────────────────────────────
# 3. Template resource
# ──────────────────────────────────────────────────────────────────────
@mcp.resource(
    "docs://{section}/search",
    name="Docs Search",
    description=(
        "Template resource enabling full-text search within a chosen docs section "
        "(e.g., api, guides, changelog). The URI parameter {section} must match "
        "the function argument."
    ),
)
def docs_search(section: str) -> str:
    database = {
        "api": API_REFERENCE_MD,
        "guides": GETTING_STARTED_MD,
        "changelog": CHANGELOG_MD,
    }
    return database.get(section, "Section not found.")


if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport="streamable-http")
