# Customer Service Example

This example demonstrates a multi-agent airline customer service flow with FAQ
lookup and seat-update tools.

## Optional release-readiness scan

The included `shipgate.yaml` lets you run an advisory static scan of this
example's tool surface before adapting it for production-like use.

```bash
pipx install agents-shipgate
agents-shipgate scan -c examples/customer_service/shipgate.yaml --ci-mode advisory
```

Agents Shipgate reads local source and manifest files only. It does not execute
the agent, call an LLM, call tools, connect to MCP servers, make scanner network
calls, or collect telemetry.

The scan is advisory for this example. It is intended to show which release
metadata would need review before wiring a similar agent to real customer or
airline systems.
