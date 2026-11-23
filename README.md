# OpenAI Agents SDK [![PyPI](https://img.shields.io/pypi/v/openai-agents?label=pypi%20package)](https://pypi.org/project/openai-agents/)

Build Python agent workflows with instructions, tools, handoffs, and conversation history. Run agents with OpenAI models or another supported model provider.

Choose how to use the SDK:

- **Standard SDK:** Use the default provider for OpenAI Responses or Chat Completions and supported hosted features. Start with the standard installation below.
- **Experimental embedded Codex:** Use `CodexProvider` with `Runner` for Codex model calls and native workspace tools. Follow the experimental Codex section below; the stable PyPI package alone does not include embedded Codex.
- **Separate Codex CLI:** Keep an agent's model and delegate a workspace task to a CLI process with [`codex_tool`](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/docs/tools.md#experimental-codex-tool).

## Get started

For the standard SDK, use Python 3.10 or newer and install `openai-agents` with `pip install openai-agents` or `uv add openai-agents`. Optional extras include `openai-agents[voice]`, `openai-agents[redis]`, and `openai-agents[docker]`.

<a id="venv"></a>
<a id="uv"></a>

For environment setup, see the [SDK quickstart](https://openai.github.io/openai-agents-python/quickstart/).

<a id="example"></a>
<a id="run-your-first-agents"></a>
<a id="run-a-text-agent"></a>

## Run your first agent

This example uses the standard SDK and OpenAI's default model provider. Set `OPENAI_API_KEY` in your environment, save the script as `agent.py`, and run `python agent.py`. It sends live model requests and can incur charges.

```python
from agents import Agent, Runner
from agents.decorators import tool


@tool
def greeting(name: str) -> str:
    return f"Hello, {name}!"


agent = Agent(
    name="Assistant",
    instructions="Use greeting to greet the user, then return the greeting.",
    tools=[greeting],
)
result = Runner.run_sync(agent, "Greet Ada.")
print(result.final_output)
```

The script prints a greeting for Ada; wording can vary. The SDK calls `greeting` when the model requests the tool.

See the [Jupyter example](https://github.com/openai/openai-agents-python/blob/main/examples/basic/hello_world_jupyter.ipynb) for notebook use.

## Experimental embedded Codex

Use `CodexProvider` with the Python `Runner`. Codex Core makes model calls and provides native workspace tools. `Runner` manages the agent loop, Python tools, handoffs, guardrails, approvals, and output validation. Selecting this provider does not reroute explicit model objects, voice or Realtime transports, or hosted API clients. See the [Codex provider guide](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/docs/models/codex.md) for supported features and troubleshooting.

### Install the Codex preview

Use CPython 3.10 through 3.14 and an activated virtual environment. The [v1.0.0-alpha.1 GitHub prerelease](https://github.com/openai/openai-agents-python/releases/tag/v1.0.0-alpha.1), published November 20, 2026, provides `cp310-abi3` wheels for Linux x86-64/ARM64 with glibc 2.28+, macOS 11+ Apple Silicon, and Windows x64. From that tagged release's **Assets**, download `SHA256SUMS` and the `openai_agents-1.0.0a1-` wheel matching your platform. Use only approved, tagged GitHub release wheels. A passing CI run and checksums alone do not establish release approval.

In the download directory, replace `WHEEL_FILE` with the exact local wheel filename. Compute its SHA-256 digest with the command for your platform:

```sh
sha256sum "WHEEL_FILE"       # Linux
shasum -a 256 "WHEEL_FILE"   # macOS
```

```powershell
Get-FileHash -Algorithm SHA256 "WHEEL_FILE"
```

Open `SHA256SUMS` and compare the computed digest with the entry for that exact filename. Uppercase and lowercase digest letters represent the same value. Install only if both the filename and digest match. In the activated virtual environment, run:

```sh
python -m pip install --only-binary=:all: "./WHEEL_FILE"
```

The preview tag points to commit `87e8f533`; this development branch has later changes. Use the release wheel with the [matching release examples](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/examples/codex/README.md#set-up). For current development code, build a wheel from the same source revision. See [installation details](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/docs/models/codex.md#install-a-github-prerelease-wheel) and the [current-source wheel build](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/native/codex/patches/README.md#test-a-change).

### Run a Codex agent

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

Native commands can affect the workspace and start helper processes. Cancellation and output guardrails do not undo those effects. An SDK `SandboxAgent`, `ShellTool`, or `ApplyPatchTool` disables competing native workspace tools. Native approval recovery is development-only and unavailable in alpha.1; it requires a matching [development wheel](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/native/codex/patches/README.md#test-a-change), `ResumableSession`, the same workspace and permissions, no competing SDK executor, and no required SDK-tool mode. See [approval recovery](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/native/codex/patches/02-approval-recovery.md).

Codex starts a fresh ephemeral thread for each model call. Use SDK sessions or replay `result.to_input_list()` for history, and SDK-managed input compaction for long conversations. Native automatic compaction is unsupported; the OpenAI compact endpoint and SDK tracing exporter each need separate OpenAI credentials. See the [provider guide](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/docs/models/codex.md) for configuration and limits, or choose a [Codex example](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/examples/codex/README.md).

## Choose an agent mode

<a id="run-a-sandbox-agent"></a>
<a id="run-a-realtime-agent"></a>
<a id="run-a-voice-agent"></a>

| Task | Guide and setup |
| --- | --- |
| Run a text workflow | [Agents](https://openai.github.io/openai-agents-python/agents/) and [models](https://openai.github.io/openai-agents-python/models/); configure credentials for your provider. |
| Work in a sandbox workspace | [Sandbox agents](https://openai.github.io/openai-agents-python/sandbox_agents/) and [sandbox clients](https://openai.github.io/openai-agents-python/sandbox/clients/); configure a client. The local Unix client supports macOS and Linux; Windows can use Docker or a hosted client. |
| Run a realtime voice or multimodal agent | [Realtime quickstart](https://openai.github.io/openai-agents-python/realtime/quickstart/); configure OpenAI credentials and a WebSocket connection. |
| Combine speech recognition, an agent, and speech generation | [Voice quickstart](https://openai.github.io/openai-agents-python/voice/quickstart/); install the voice extra and configure OpenAI credentials. |

## Guides and examples

<a id="core-concepts"></a>

- [Tools](https://openai.github.io/openai-agents-python/tools/): Python functions, MCP, and hosted tools.
- [Agents as tools](https://openai.github.io/openai-agents-python/tools/#agents-as-tools) and [handoffs](https://openai.github.io/openai-agents-python/handoffs/): delegate tasks to other agents.
- [Guardrails](https://openai.github.io/openai-agents-python/guardrails/) and [human in the loop](https://openai.github.io/openai-agents-python/human_in_the_loop/): validate inputs and outputs, and pause for approval.
- [Sessions](https://openai.github.io/openai-agents-python/sessions/) and [tracing](https://openai.github.io/openai-agents-python/tracing/): manage conversation history and inspect runs.
- [Examples](https://github.com/openai/openai-agents-python/tree/main/examples) and [documentation](https://openai.github.io/openai-agents-python/): find additional workflows and API references.
- [JavaScript/TypeScript SDK](https://github.com/openai/openai-agents-js): use the SDK in JavaScript or TypeScript.

## Get help

Use [GitHub issues](https://github.com/openai/openai-agents-python/issues) for questions, bug reports, and feedback. Report security vulnerabilities through [SECURITY.md](https://github.com/openai/openai-agents-python/blob/main/SECURITY.md).

## Contributing

Follow [CONTRIBUTING.md](https://github.com/openai/openai-agents-python/blob/main/CONTRIBUTING.md) for development setup and contribution policy. Pull requests are limited to repository collaborators.

## Acknowledgements

The SDK uses open-source libraries including [Pydantic](https://docs.pydantic.dev/latest/), [Requests](https://github.com/psf/requests), [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), and [Griffe](https://github.com/mkdocstrings/griffe).
