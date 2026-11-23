# Run a Python agent with embedded Codex

This guide uses the approved [v1.0.0-alpha.1 prerelease](https://github.com/openai/openai-agents-python/releases/tag/v1.0.0-alpha.1). `CodexProvider` supplies model calls and native workspace tools; Python `Runner` manages the agent loop, SDK tools, handoffs, guardrails, approvals, and output validation.

## Install

Use CPython 3.10 through 3.14 on one of the platforms below. Create a virtual environment with `python -m venv .venv`, then activate it with `source .venv/bin/activate` on macOS or Linux, or `.\.venv\Scripts\Activate.ps1` in PowerShell.

Run the one command for your platform. Each URL points to the tagged release wheel and includes its SHA-256 hash from [SHA256SUMS](https://github.com/openai/openai-agents-python/releases/download/v1.0.0-alpha.1/SHA256SUMS); pip verifies the hash before installation.

macOS 11+ on Apple Silicon:

```sh
python -m pip install "https://github.com/openai/openai-agents-python/releases/download/v1.0.0-alpha.1/openai_agents-1.0.0a1-cp310-abi3-macosx_11_0_arm64.whl#sha256=19a49f51ff30a201252ab1059e7b3ca00ab2b53f9d935ae65c489a6ee3284eba"
```

Linux ARM64 with glibc 2.28+:

```sh
python -m pip install "https://github.com/openai/openai-agents-python/releases/download/v1.0.0-alpha.1/openai_agents-1.0.0a1-cp310-abi3-manylinux_2_28_aarch64.whl#sha256=8a776dccb6baa8c197c090305138818bb70411fa34706c128cca8e1acb080e15"
```

Linux x86-64 with glibc 2.28+:

```sh
python -m pip install "https://github.com/openai/openai-agents-python/releases/download/v1.0.0-alpha.1/openai_agents-1.0.0a1-cp310-abi3-manylinux_2_28_x86_64.whl#sha256=bfc5db104b0bb36d7119c8fb3f1d2831b6b8b7d95b63b09b2162bb721433057d"
```

Windows x64:

```sh
python -m pip install "https://github.com/openai/openai-agents-python/releases/download/v1.0.0-alpha.1/openai_agents-1.0.0a1-cp310-abi3-win_amd64.whl#sha256=4bd8f0910bc39ac0074663f1b6a19c6345ab221f1bddb6758e8ad4588a94efa8"
```

The stable PyPI package alone does not include the embedded Codex runtime. For development source newer than alpha.1, build and install a native wheel from the same revision using the [development build instructions](../../native/codex/patches/README.md#test-a-change).

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

The wheel does not contain the example files. Download and extract the [alpha.1 source archive](https://github.com/openai/openai-agents-python/archive/refs/tags/v1.0.0-alpha.1.zip), then change to the extracted source root. Keep using the environment containing the installed wheel; no editable installation or source sync is needed.

The live scripts create `CodexProvider()` without a key, so they require the saved CLI login. Disable tracing with `export OPENAI_AGENTS_DISABLE_TRACING=1` on macOS or Linux, or `$env:OPENAI_AGENTS_DISABLE_TRACING = "1"` in PowerShell. Review inherited Codex settings, MCP servers, hooks, workspace permissions, and approval policy before running them.

### Check the installed native runtime offline

From the alpha.1 source root:

```sh
python native/codex/verify_installed.py
```

A successful report contains `"passed": true`. This check uses local loopback and process fixtures; it needs no login or paid model call.

### Run a released live example

Run these commands from the alpha.1 source root. They send live requests and may incur charges.

| Task | Command | Prerequisites or effects |
| --- | --- | --- |
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

### Development-only examples

These commands use the current development checkout, not alpha.1 release source. Embedded examples require a native wheel built from the same revision:

- `python examples/codex/native_workspace.py`: read a temporary fixture using native workspace execution.
- `python examples/codex/native_approval.py`: recover a pending native command, then explicitly approve or reject it. Approval writes a temporary fixture file.
- `python examples/basic/mutable_tool_hooks.py --codex`: modify SDK tool behavior through hooks.

For an offline SDK approval-recovery example without a native wheel, run `uv sync` followed by `uv run python examples/memory/resumable_sqlite_session.py` in the development checkout. This checks SDK persistence and recovery, not embedded Codex.

## Limits and execution boundaries

Native commands can change files and start processes. Cancellation and output guardrails do not undo those effects. An SDK `SandboxAgent`, `ShellTool`, or `ApplyPatchTool` disables competing native workspace tools. Required SDK-tool selection also disables native shell and image-view tools.

Native approval recovery is development-only and unavailable in alpha.1. It requires a `ResumableSession`, the same workspace, store, permissions, and hooks after recovery, no competing SDK executor, and no required SDK-tool mode. See the [approval recovery patch](../../native/codex/patches/02-approval-recovery.md).

Codex starts a fresh ephemeral thread for each model call. Use SDK sessions or replay `result.to_input_list()` for history. Native automatic compaction is unsupported; SDK input compaction through OpenAI's compact endpoint and SDK trace export each need separate OpenAI API credentials. SDK streaming does not expose native command stdout deltas.

Selecting `CodexProvider` does not reroute explicit model objects, voice or Realtime transports, or hosted API clients. See the [canonical provider guide](../../docs/models/codex.md) and [native patch notes](../../native/codex/patches/README.md) for configuration, supported features, and troubleshooting.
