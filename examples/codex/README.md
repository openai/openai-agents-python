# Run a Python agent with embedded Codex

Use the [v1.0.0-alpha.2 prerelease](https://github.com/openai/openai-agents-python/releases/tag/v1.0.0-alpha.2) for these examples. `CodexProvider` runs model calls and native workspace tools. The Python `Runner` manages the agent loop, SDK tools, handoffs, guardrails, approvals, and output validation.

## Install

Use CPython 3.10 through 3.14. Create a virtual environment with `python -m venv .venv`, then activate it with `source .venv/bin/activate` on macOS or Linux, or `.\.venv\Scripts\Activate.ps1` in PowerShell.

Run the `pip install` command for your platform under **Install** in the [alpha.2 release notes](https://github.com/openai/openai-agents-python/releases/tag/v1.0.0-alpha.2). Pip downloads the wheel, checks its SHA-256 hash and installs the SDK and embedded Codex runtime.

If you already have a wheel, [verify and install the local file](../../docs/models/codex.md#install-a-github-prerelease-wheel). For an unreleased checkout, [build and install a native wheel](../../native/codex/patches/README.md#test-a-change) from the same revision.

## Authenticate

For a saved login, [install the separate Codex CLI](https://learn.chatgpt.com/docs/codex/cli) and run `codex login` under the same account and `CODEX_HOME` used by Python. The wheel does not install the CLI. Alternatively, pass a key from your secret store to `CodexProvider(api_key=...)`; this does not require the CLI. Codex needs write access to its configuration directory.

Live requests can consume plan usage or incur API charges. The example below disables SDK tracing because trace export needs separate OpenAI API credentials.

## Run your first agent

Save this self-contained script as `agent.py` in your activated environment:

```python
import asyncio

from agents import Agent, ModelSettings, RunConfig, Runner
from agents.decorators import tool
from agents.models.codex import CodexProvider


@tool
def greeting(name: str) -> str:
    """Return a greeting for the requested name."""
    return f"Hello, {name}!"


async def main() -> None:
    provider = CodexProvider(
        config_overrides=('approval_policy="never"', 'sandbox_mode="read-only"'),
    )
    try:
        agent = Agent(
            name="Greeter",
            instructions="Use greeting to greet Ada, then return the greeting.",
            tools=[greeting],
            model_settings=ModelSettings(tool_choice="required"),
        )
        result = await Runner.run(
            agent,
            "Greet Ada.",
            run_config=RunConfig(model_provider=provider, tracing_disabled=True),
        )
        print(result.final_output)
    finally:
        await provider.aclose()


asyncio.run(main())
```

Run `python agent.py`. The agent should return a greeting for Ada; wording can vary. `tool_choice="required"` requires an SDK tool call and disables native shell and image-view tools. The Python callback runs in your application process, outside the Codex workspace sandbox. Register only functions whose effects your application permits. Reuse the provider for runs in the same workspace, then close it when your application finishes. Set `Agent(model=...)` or `RunConfig(model=...)` to choose an available Codex model.

## Choose your next task

Download and extract the [alpha.2 source archive](https://github.com/openai/openai-agents-python/archive/refs/tags/v1.0.0-alpha.2.zip) for the examples. Run the following commands from the extracted source root with the wheel's environment active. Running `uv sync` there replaces the native wheel with the source package.

The live scripts create `CodexProvider()` without a key, so they require the saved CLI login. Disable tracing with `export OPENAI_AGENTS_DISABLE_TRACING=1` on macOS or Linux, or `$env:OPENAI_AGENTS_DISABLE_TRACING = "1"` in PowerShell. Review inherited Codex settings, MCP servers, hooks, workspace permissions, and approval policy before running them.

### Check the installed native runtime offline

```sh
python -I native/codex/verify_installed.py
```

A successful report contains `"passed": true`. The check uses a local server and temporary files. It needs no login or paid model call.

If the check reports `Cannot save this Codex approval`, review the [recovery requirements](../../native/codex/patches/02-approval-recovery.md). System-wide Codex hooks can prevent recovery even with a temporary `CODEX_HOME`. Keep required security hooks enabled.

### Run a released live example

These commands send live requests and may incur charges.

| Task | Command | Prerequisites or effects |
| --- | --- | --- |
| Read a file with a native tool | `python examples/codex/native_workspace.py` | Reads a temporary file using the host's Codex sandbox. |
| Recover a pending native command | `python examples/codex/native_approval.py` | Approve or reject the command after recovery. Approval writes a temporary file. |
| Change SDK tool arguments and results | `python examples/basic/mutable_tool_hooks.py --codex` | Runs tool preparation hooks. |
| Use a local prompt template | `python examples/basic/prompt_template.py --local --codex` | No hosted prompt ID. |
| Continue with SDK history | `python -m examples.basic.previous_response_id --local --codex` | Uses a local SDK session instead of a hosted response ID. |
| Send a local JPEG | `python examples/basic/remote_image.py --codex --file examples/basic/media/image_bison.jpg` | Sends the bundled image to the model. |
| Read PDF text | `python examples/basic/local_file.py --codex` | Install Poppler's `pdftotext`; sends extracted text, without OCR or layout. |
| Query Calendar through SDK MCP | `python examples/hosted_mcp/connectors.py --sdk-managed --codex` | Requires Google preview access and OAuth; see [Calendar setup](../hosted_mcp/README.md#setup). |
| Run local Python | `python examples/tools/local_code_execution.py --codex` | Runs commands as your host user; use a trusted disposable macOS or Linux host. See [execution environment](../tools/local_code_execution.md#execution-environment). |
| Query read-only SQLite sample data | `python -m examples.sandbox.extensions.daytona.usaspending_text2sql.local_agent --demo --codex` | Requires macOS or Linux. Creates a temporary synthetic database; `--demo` avoids official records. |
| Stream a typed wildlife report | `python examples/model_providers/codex_provider.py --codex` | Sends the bundled JPEG, uses a Python catalog tool, and validates structured output. |
| Compare providers on portable tasks | `python examples/model_providers/portable_docs.py --codex --case inventory` | Choose `inventory`, `prompt`, `conversation`, `tools`, `csv`, `stream`, or `all`. Omit `--codex` for the default provider, which needs separate OpenAI credentials. |

For the separate CLI route, run `python examples/tools/codex.py`. It requires both OpenAI credentials for the outer agent and an installed, authenticated Codex CLI. It starts CLI subprocesses and reads repository files.

To try SDK approval recovery offline, run `python examples/memory/resumable_sqlite_session.py`. It uses a scripted model and a local SQLite store.

## Limits and execution boundaries

Native commands can change files and start processes. Cancellation and output guardrails do not undo those effects. An SDK `SandboxAgent`, `ShellTool`, or `ApplyPatchTool` disables competing native workspace tools. Required SDK-tool selection also disables native shell and image-view tools.

Native approval recovery requires a `ResumableSession` and a single pending command or edit. Resume with the same workspace, store, permissions, and hooks. A competing SDK executor or required SDK-tool mode disables native workspace tools. See the [recovery requirements](../../native/codex/patches/02-approval-recovery.md).

Codex starts a fresh ephemeral thread for each model call. Use SDK sessions or replay `result.to_input_list()` for history. Native automatic compaction is unsupported; SDK input compaction through OpenAI's compact endpoint and SDK trace export each need separate OpenAI API credentials. SDK streaming does not expose native command stdout deltas.

Selecting `CodexProvider` does not reroute explicit model objects, voice or Realtime transports, or hosted API clients. See the [canonical provider guide](../../docs/models/codex.md) and [native patch notes](../../native/codex/patches/README.md) for configuration, supported features, and troubleshooting.
