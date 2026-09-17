# Examples

The [examples directory on GitHub](https://github.com/openai/openai-agents-python/tree/main/examples) contains runnable scripts that mirror the guides in this documentation. Use them when you want a full file to copy, run locally, or compare against your own project.

## Browse by topic

| Topic | Directory | Good starting files |
| --- | --- | --- |
| First run and tools | [`examples/basic/`](https://github.com/openai/openai-agents-python/tree/main/examples/basic) | `hello_world.py`, `tools.py`, `hello_world_jupyter.ipynb` |
| Routing and handoffs | [`examples/agent_patterns/`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns), [`examples/handoffs/`](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs) | `routing.py` |
| MCP servers | [`examples/mcp/`](https://github.com/openai/openai-agents-python/tree/main/examples/mcp), [`examples/hosted_mcp/`](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp) | See directory READMEs |
| Sessions and memory | [`examples/memory/`](https://github.com/openai/openai-agents-python/tree/main/examples/memory) | Compaction and session examples |
| Sandbox agents | [`examples/sandbox/`](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox) | Workspace and manifest flows |
| Voice and realtime | [`examples/voice/`](https://github.com/openai/openai-agents-python/tree/main/examples/voice), [`examples/realtime/`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) | Quickstart-style pipelines |
| Tools and providers | [`examples/tools/`](https://github.com/openai/openai-agents-python/tree/main/examples/tools), [`examples/model_providers/`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers) | Provider-specific setup |

The [Quickstart](quickstart.md#reference-examples) links to a few of these paths explicitly; this page is the broader map.

## Run examples from a clone

If you cloned the repository, see [`examples/README.md`](https://github.com/openai/openai-agents-python/blob/main/examples/README.md) for the supported runner workflow. From the repository root:

```bash
make sync
make examples-run
```

Run a subset with:

```bash
make examples-run EXAMPLES_ARGS="--filter basic"
```

Logs are written under `.tmp/examples-start-logs/`. Set `OPENAI_API_KEY` (and any example-specific variables documented in the script) before running examples that call live models.

## Related guides

- [Quickstart](quickstart.md) — minimal first agent and handoffs
- [Tools](tools.md) — function tools, MCP, and agents-as-tools
- [MCP](mcp.md) — connecting MCP servers to agents
- [Sessions](sessions/index.md) — memory across turns
- [Sandbox agents quickstart](sandbox_agents.md) — isolated workspace runs
