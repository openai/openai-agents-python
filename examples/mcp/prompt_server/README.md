# MCP Prompt Server Example

This example uses a local MCP prompt server in [server.py](server.py).

Run the example via:

```
uv run python examples/mcp/prompt_server/main.py
```

## Details

The example uses the `MCPServerStreamableHttp` class from `agents.mcp`. The server runs in a sub-process at `http://localhost:8000/mcp` and provides user-controlled prompts that generate agent instructions.

The server exposes prompts like `generate_code_review_instructions` that take parameters such as focus area and programming language. The agent calls these prompts to dynamically generate its system instructions based on user-provided parameters.

## Workflow

The example demonstrates two key functions:

1. **`show_available_prompts`** - Lists all available prompts on the MCP server, showing users what prompts they can select from. This demonstrates the discovery aspect of MCP prompts.

2. **`demo_code_review`** - Shows the complete user-controlled prompt workflow:
   - Calls `generate_code_review_instructions` with specific parameters (focus: "security vulnerabilities", language: "python")
   - Uses the generated instructions to create an Agent with specialized code review capabilities
   - Runs the agent against vulnerable sample code (command injection via `os.system`)
   - The agent analyzes the code and provides security-focused feedback using available tools

This pattern allows users to dynamically configure agent behavior through MCP prompts rather than hardcoded instructions. 