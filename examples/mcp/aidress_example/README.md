# MCP Agent Discovery Example

Connects to [Aidress](https://api.aidress.ai), a public registry of third-party agents, over the Streamable HTTP transport (`https://api.aidress.ai/mcp-http/mcp`), and uses it to discover agents that can do a task the user needs done.

Discovery is the point of the example: the agent does not start from a list of known counterparts, it searches the registry by capability and finds out at runtime who exists and what they offer. Because the candidates it turns up are agents the user has never worked with, the second step is due diligence — each candidate's trust score, verification status, completed transaction count, and flags — before one is recommended. The registry holds real third-party agents, so the results and the numbers change over time and the example does not hard-code any agent id.

The registry also exposes tools that mutate its state, including registering an agent and proxying a call to one. Discovery needs none of them, so the example passes `create_static_tool_filter` and the agent can reach only the read-only lookups.

Run it with:

```bash
uv run python examples/mcp/aidress_example/main.py
```

Prerequisites:

- `OPENAI_API_KEY` set for the model calls.
- No registry credentials. The read-only tools used here are public; the tools this example filters out are the ones that require a key.
