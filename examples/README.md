# Python Agents SDK examples

Start by choosing one example for your task. From the repository root, set up the development environment with `make sync`, configure the credentials required by the example, and run a single script:

```sh
uv run python examples/basic/hello_world.py
```

This example uses `OPENAI_API_KEY` and sends a live model request. Other examples may need optional extras, external services, or permission to run commands or change data. Check the linked instructions and the script before running it. For embedded Codex, use the matching wheel and source instructions in the [Codex guide](codex/README.md) instead of syncing the source environment.

## Choose a task

| Task | Examples |
| --- | --- |
| Start an agent, use prompts, or stream output | [Basic examples](basic/README.md) |
| Call Python, hosted, or local execution tools | [Tools](tools/README.md) |
| Package and install an agent plugin (branch preview) | [Authoring guide](plugins/AUTHORING.md) and [plugin examples](plugins/README.md) |
| Keep conversation history or recover approvals | [Memory and sessions](memory/README.md) |
| Connect to MCP servers | [MCP](mcp/README.md) and [hosted MCP](hosted_mcp/README.md) |
| Transfer work between agents | [Handoffs](handoffs/) |
| Compose agent workflows | [Agent patterns](agent_patterns/README.md) |
| Use another model provider | [Model providers](model_providers/README.md) |
| Build speech pipelines | [Static voice](voice/static/README.md) and [streamed voice](voice/streamed/README.md) |
| Build a realtime app | [Realtime app](realtime/app/README.md) |
| Run workspace tasks in a sandbox | [Sandbox examples](sandbox/README.md) |
| Try embedded Codex | [Codex guide and task chooser](codex/README.md) |

## Run the example suite

The suite supplies automatic inputs and approvals by default. It can call live models, execute host commands, modify data, and incur costs. Preview the selected commands first:

```sh
make examples-run EXAMPLES_ARGS="--dry-run"
```

To explicitly run the full default suite in the foreground:

```sh
make examples-run
```

Pass runner arguments through `EXAMPLES_ARGS`, for example:

```sh
make examples-run EXAMPLES_ARGS="--filter basic"
make examples-run EXAMPLES_ARGS="--include-server --include-audio"
```

Use `make examples-run-background` for a background run. The remaining lifecycle targets are `make examples-status`, `make examples-stop`, `make examples-logs`, and `make examples-tail`. Set `EXAMPLES_LOG` to select a specific file for `examples-tail`.

Every normal run writes a main log and per-example logs under `.tmp/examples-start-logs/`. Use `EXAMPLES_ARGS="--filter <substring>"` to run a focused subset again when needed.

The defaults include interactive examples and exclude server, audio, and external examples unless selected. `EXAMPLES_UV_EXTRAS` controls the optional dependency extras installed by `uv`; set it to an empty value to disable extras. `EXAMPLES_INCLUDE_INTERACTIVE`, `EXAMPLES_INCLUDE_SERVER`, `EXAMPLES_INCLUDE_AUDIO`, and `EXAMPLES_INCLUDE_EXTERNAL` provide environment-based inclusion overrides.

The repository skill `examples-run-analysis` is analysis-only. After a manual run completes, use it to inspect the main log, every relevant per-example log, and example source. The skill never starts, retries, stops, or controls the example process.
