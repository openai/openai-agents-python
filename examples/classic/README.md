# Classic examples

These examples use the established Python runtime through `agents.classic`. Install the matching SDK with its `classic` extra; see [Classic setup](../../CLASSIC.md). Install any additional `classic-*` extras or dependencies listed by each example.

Run examples from the repository root. For example, with model credentials configured:

```sh
python -m examples.classic.basic.hello_world
```

- [Agent patterns](agent_patterns/README.md): Routing, handoffs, tools, and guardrails.
- [MCP](mcp/): Connect agents to MCP servers.
- [Sandbox](sandbox/README.md): Run tools in local and remote sandboxes.
- [Voice](voice/static/README.md): Build a voice workflow.

Most examples call live services and need credentials. Provider, audio, and server examples have additional setup requirements in their directories.
