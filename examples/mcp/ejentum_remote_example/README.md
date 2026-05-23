# MCP Ejentum Remote Example

Connects an Agent to the [Ejentum cognitive harness](https://ejentum.com) over the Streamable HTTP transport (`https://api.ejentum.com/mcp`) with bearer auth, and lets the agent decide when to call one of the four `harness_*` tools (reasoning, code, anti_deception, memory) to retrieve a task-matched cognitive scaffold before generating its answer.

The example demonstrates the same pattern as `streamable_http_remote_example` but adds two things specific to authenticated third-party MCP servers:

- Bearer auth via the `headers` field in the streamable-HTTP transport params
- A short instruction block that tells the agent when to reach for the scaffold tools

Run it with:

```bash
uv run python examples/mcp/ejentum_remote_example/main.py
```

Prerequisites:

- `OPENAI_API_KEY` set for the model calls.
- `EJENTUM_API_KEY` set for the harness server. Get a key at [ejentum.com/dashboard](https://ejentum.com/dashboard); free and paid tiers are available.

To repoint this example at a different authenticated streamable-HTTP MCP server, change `EJENTUM_MCP_URL` and the `headers` dict in `main.py`. The rest of the wiring is reusable.
