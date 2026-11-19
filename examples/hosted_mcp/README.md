# Calendar connector example

`connectors.py` asks for today's schedule. By default, it uses the Responses API's hosted Google Calendar connector. With `--sdk-managed`, the SDK connects to [Google's official Calendar MCP server](https://developers.google.com/workspace/calendar/api/guides/configure-mcp-server), so the query can run through either the default provider or Codex.

The hosted connector ID is a Responses API feature. Switching to SDK-managed MCP does not configure a Codex App or inherit a ChatGPT calendar connection.

## Setup

The hosted connector requires `GOOGLE_CALENDAR_AUTHORIZATION`: a Google OAuth access token with the `https://www.googleapis.com/auth/calendar.events` scope used in `connectors.py`.

The SDK-managed example requires Google's Workspace Developer Preview access, a Google Cloud project with the Calendar and Calendar MCP APIs enabled, and an OAuth client with **Use this client for an AI-powered agent** selected. The token must permit event reads. Follow Google's setup guide for its consent-screen and OAuth requirements.

The example exposes only `calendar.list_events` and checks that the server provides that tool before calling the model. Write tools are hidden. Required tool selection prevents a successful first turn without an SDK tool request. `mcp_config={"include_server_in_tool_names": True}` gives the dotted MCP name a model-compatible public name; the server still receives `calendar.list_events`.

## Run

Complete the SDK and [Codex setup](../../docs/models/codex.md) first. With `GOOGLE_CALENDAR_AUTHORIZATION` set, run these commands from the repository root:

```sh
# Responses hosted connector
uv run python -m examples.hosted_mcp.connectors

# SDK-managed MCP with the default provider
uv run python -m examples.hosted_mcp.connectors --sdk-managed

# SDK-managed MCP with Codex
uv run python -m examples.hosted_mcp.connectors --sdk-managed --codex

# The same Codex query with streaming
uv run python -m examples.hosted_mcp.connectors --sdk-managed --codex --stream
```

`OPENAI_MODEL` and `CODEX_MODEL` select an optional model for their respective providers. Codex uses its CLI authentication. The SDK sends the Google token as an authorization header to `https://calendarmcp.googleapis.com/mcp/v1`; the model receives the filtered tools and their results.

A successful query should include a tool result from the connected calendar. A local MCP protocol test cannot verify access to Google's service or a user's calendar; that requires the preview access and OAuth token above.
