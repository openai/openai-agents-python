# Run a Codex agent example

Choose an example for your task. The live examples below are available in the alpha.1 release source. The offline recovery example requires the current development checkout.

## Set up

For the released live examples, open the [alpha.1 source archive](https://github.com/openai/openai-agents-python/archive/refs/tags/v1.0.0-alpha.1.zip) or check out the preview tag. Download and verify the matching [preview wheel](../../README.md#install-the-codex-preview). From the release source root, replace `WHEEL_FILE` with the path to that verified local wheel:

```sh
uv sync
uv pip install --reinstall-package openai-agents "WHEEL_FILE"
```

For the offline SDK check, use the current development checkout and run `uv sync`; no native wheel or credentials are needed. To run development code with embedded Codex, follow the [current-source wheel build](../../native/codex/patches/README.md#test-a-change) and install that wheel instead.

Keep `--no-sync` in the commands below so uv does not replace the wheel with the source checkout's pure Python package. Authenticate with a saved Codex login as described in the [provider guide](../../docs/models/codex.md#authenticate). These scripts create `CodexProvider()` without passing an API key. Disable SDK tracing when relying on saved Codex login:

```sh
export OPENAI_AGENTS_DISABLE_TRACING=1
```

In PowerShell, use `$env:OPENAI_AGENTS_DISABLE_TRACING = "1"`. Live calls consume plan usage or can incur charges. Review the Codex configuration, inherited MCP servers, hooks, workspace, and approval policy before running live scripts. The scripts inherit those settings; their Python callbacks run outside the native sandbox. Use a disposable workspace or a read-only Codex policy when appropriate.

## Choose a task

Run all commands from the repository root. The commands use direct script paths. Offline means no model service or Codex login; the offline check does not exercise embedded Codex.

### Offline SDK check

| Task | Command | Result and effects |
| --- | --- | --- |
| Check approval recovery: [SQLite recovery](../memory/resumable_sqlite_session.py) | `uv run --no-sync python examples/memory/resumable_sqlite_session.py` | Development checkout. Prints the pending `publish_note` call and rejection result. Creates and removes a temporary SQLite database; rejects the simulated publish call. |

### Live model examples

All embedded Codex rows require the matching native wheel, Codex authentication, and an available model. The default-provider row requires OpenAI credentials. All rows send live model requests; review the usage and safety notes above before running them.

| Task | Command | Result and task-specific effects |
| --- | --- | --- |
| Compare providers: [inventory, default provider](../model_providers/portable_docs.py) | `uv run --no-sync python examples/model_providers/portable_docs.py --case inventory` | JSON with `status="passed"`, `sku="desk-lamp"`, and `available_units=42`; reads synthetic inventory in memory. |
| Call a Python tool and validate output: [inventory, Codex](../model_providers/portable_docs.py) | `uv run --no-sync python examples/model_providers/portable_docs.py --codex --case inventory` | The same inventory JSON; reads synthetic inventory in memory. |
| Apply local instructions: [prompt, Codex](../model_providers/portable_docs.py) | `uv run --no-sync python examples/model_providers/portable_docs.py --codex --case prompt` | JSON with `lines=3` and passing status; no intended file writes. |
| Continue a conversation: [conversation, Codex](../model_providers/portable_docs.py) | `uv run --no-sync python examples/model_providers/portable_docs.py --codex --case conversation` | JSON confirming `cedar-482` in session and replayed history; uses in-memory SQLite history across multiple requests. |
| Search local metadata: [tools, Codex](../model_providers/portable_docs.py) | `uv run --no-sync python examples/model_providers/portable_docs.py --codex --case tools` | Catalog ID `bridge-image-001` and `missing_found=false`; searches an in-memory synthetic catalog. |
| Summarize CSV data: [CSV, Codex](../model_providers/portable_docs.py) | `uv run --no-sync python examples/model_providers/portable_docs.py --codex --case csv` | JSON totals `east=15` and `west=7`; parses fixed CSV text in memory. |
| Stream text: [stream, Codex](../model_providers/portable_docs.py) | `uv run --no-sync python examples/model_providers/portable_docs.py --codex --case stream` | JSON confirming streamed and final text `STREAM_OK`; consumes stream events. |
| Combine image input, handoff, tool, and typed output: [wildlife, Codex](../model_providers/codex_provider.py) | `uv run --no-sync python examples/model_providers/codex_provider.py --codex` | Streams a report and exits successfully after checking `synthetic-bison-001`; reads and sends the bundled bison JPEG and searches an in-memory catalog. |

For the six portable cases, omit `--codex` to use the default provider. The task checks remain the same. Run all six cases with `--case all`.

### Separate Codex CLI route

This route requires the default provider's OpenAI credentials, a separately installed and authenticated Codex CLI, and an available configured model. It does not require embedded Codex.

| Task | Command | Result and effects |
| --- | --- | --- |
| Delegate to Codex CLI: [Codex tool](../tools/codex.py) | `uv run --no-sync python examples/tools/codex.py` | Prints summaries of `pyproject.toml` and `AGENTS.md`, plus Codex turn events. Starts CLI subprocesses, reads repository files, and sends outer-agent and Codex model requests. |

The [repository quickstart](../../README.md#run-a-codex-agent) gives a complete first agent. The [Codex provider guide](../../docs/models/codex.md) describes native execution, approvals, history, compaction, and unsupported hosted features.
