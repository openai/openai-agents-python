# Build an agent with Codex

Use embedded Codex Core for model calls and native workspace tools. The Python `Runner` manages the agent loop, Python tools, handoffs, guardrails, approvals, and output validation. Codex support is an experimental preview.

## Choose an API

| Your task | Use |
| --- | --- |
| Call OpenAI Responses or Chat Completions, including supported hosted features | The default SDK model provider |
| Run an SDK agent using embedded Codex model calls and native workspace tools | `CodexProvider` with `Runner` |
| Keep an agent's existing model and delegate a workspace task to a separate Codex CLI process | [`codex_tool`](docs/tools.md#experimental-codex-tool) |

Selecting `CodexProvider` does not reroute explicit model objects, voice or Realtime transports, or hosted API clients. Codex does not implement every Responses API feature. See the [Codex provider reference guide](docs/models/codex.md) for supported settings and limits.

## Install the preview

Use CPython 3.10 through 3.14 and an activated virtual environment. The [v1.0.0-alpha.1 GitHub prerelease](https://github.com/openai/openai-agents-python/releases/tag/v1.0.0-alpha.1), published November 20, 2026, provides `cp310-abi3` wheels for Linux x86-64/ARM64 with glibc 2.28+, macOS 11+ Apple Silicon, and Windows x64. From that tagged release's **Assets**, download `SHA256SUMS` and the `openai_agents-1.0.0a1-` wheel matching your platform. Use only approved, tagged GitHub release wheels; a passing CI run and checksums alone do not establish release approval.

In the download directory, replace `WHEEL_FILE` with the exact local wheel filename and compute its SHA-256 digest using the command for your platform:

```sh
sha256sum "WHEEL_FILE"       # Linux
shasum -a 256 "WHEEL_FILE"   # macOS
```

```powershell
Get-FileHash -Algorithm SHA256 "WHEEL_FILE"
```

Open `SHA256SUMS` and compare the computed digest with the entry for that exact filename. Install only if both the filename and digest match. In the activated virtual environment, run:

```sh
python -m pip install --only-binary=:all: "./WHEEL_FILE"
```

The PyPI instructions in the project README cover the standard SDK. The preview tag points to commit `87e8f533`; this development branch has later changes. Use the release wheel with the [matching release examples](examples/codex/README.md#set-up). For current development code, build a wheel from the same source revision. See [installation details](docs/models/codex.md#install-a-github-prerelease-wheel) and the [current-source wheel build](native/codex/patches/README.md#test-a-change).

## Run your first agent

For saved-login authentication, [install the separate Codex CLI](https://learn.chatgpt.com/docs/codex/cli) and run `codex login` under the same account and `CODEX_HOME` that Python uses. The SDK wheel does not install the `codex` command. Alternatively, pass an API key from your secret store to `CodexProvider(api_key=...)`; that method does not require the CLI. Codex needs write access to its configuration directory. This live run sends model requests and can consume plan usage or incur API charges.

Save this as `agent.py`:

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

Run `python agent.py`. Expect a greeting for Ada; the final wording can vary. `tool_choice="required"` requires an SDK tool call and disables native shell and image-view tools. The Python function runs in your application process, outside the Codex workspace sandbox. Only register functions whose effects your application permits. Tracing is disabled so the SDK tracing exporter does not need separate OpenAI credentials.

Reuse the provider across runs in the same workspace, then close it once when your application finishes.

## Choose your next task

| Task | Starting point |
| --- | --- |
| Compare the default provider and Codex | [Provider examples](examples/codex/README.md#choose-a-task) |
| Call Python tools, return typed data, or stream text | [Task chooser](examples/codex/README.md#choose-a-task) |
| Keep conversation history | Use an SDK session or pass `result.to_input_list()` to the next `Runner.run()` call. |
| Inspect a workspace with native commands | Set `CodexProvider(cwd=...)`, choose a sandbox and approval policy, and leave `tool_choice` at its default. See [execution configuration](docs/models/codex.md#configure-execution). |
| Approve and recover a native command or patch | **Development only; unavailable in alpha.1.** Build a [current-source native wheel](native/codex/patches/README.md#test-a-change). Use `ResumableSession` with the Runner, the same workspace and permissions, no SDK workspace executor, and no required SDK-call mode. See [approval recovery](native/codex/patches/02-approval-recovery.md). |
| Stream progress | Call `Runner.run_streamed()`, consume `result.stream_events()`, then inspect `result.final_output`. |
| Compact history | Use SDK-managed input compaction. Native automatic compaction is unsupported; the OpenAI compact endpoint needs separate credentials. |

Native commands can start helper processes and affect the workspace according to the configured policy. Cancellation and output guardrails do not undo effects. Inspect the workspace before retrying. An SDK `SandboxAgent`, `ShellTool`, or `ApplyPatchTool` uses SDK execution instead and disables competing native workspace tools.

## Troubleshoot

| Symptom | Check or action |
| --- | --- |
| Codex asks for a prebuilt wheel | Install the matching native wheel in the Python environment running the app. In a checkout, use `uv run --no-sync` after installing it. |
| Login fails | Check the saved login, writable `CODEX_HOME`, and host login policy, or pass a provider API key. |
| SDK tracing reports an authentication error | Disable SDK tracing or configure its separate OpenAI credentials. Codex login does not authenticate the exporter. |
| Native commands do not run | Check sandbox support, approval policy, and whether required SDK tool choice or an SDK executor has disabled native tools. |
| A setting or hosted feature is rejected | Check the [supported features and limits](docs/models/codex.md#supported-features-and-limits); keep the default provider if your task needs that contract. |
| Conversation history disappears | Pass an SDK session or replay `to_input_list()`; Codex starts a fresh ephemeral thread for each model call. |

Use the [example chooser](examples/codex/README.md) for commands and the [canonical Codex guide](docs/models/codex.md) for detailed behavior.
