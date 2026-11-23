# OpenAI Agents SDK [![PyPI](https://img.shields.io/pypi/v/openai-agents?label=pypi%20package)](https://pypi.org/project/openai-agents/)

Build Python agent workflows with instructions, tools, handoffs, and conversation history. Run agents with OpenAI models or another supported model provider.

**Experimental Codex preview:** Start with the [Codex quickstart](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/CODEX.md) to run an SDK agent with embedded Codex Core. The preview uses platform-specific native wheels from a GitHub prerelease; installing the stable PyPI package alone does not provide embedded Codex. See the [Codex provider guide](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/docs/models/codex.md) for capabilities, authentication, execution controls, and limits, or choose a [Codex example](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/examples/codex/README.md).

## Get started

Use Python 3.10 or newer. Choose an installation:

- Standard SDK: `pip install openai-agents` or `uv add openai-agents`.
- Embedded Codex preview: follow the [preview installation instructions](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/CODEX.md#install-the-preview) for your platform.
- Optional features: install `openai-agents[voice]` for voice pipelines, `openai-agents[redis]` for Redis sessions, or `openai-agents[docker]` for the Docker sandbox client.

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

The script prints a greeting for Ada; wording can vary. The SDK calls `greeting` when the model requests the tool. For a complete embedded Codex example and its separate credential and tracing requirements, use the [Codex quickstart](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/CODEX.md#run-your-first-agent).

See the [Jupyter example](https://github.com/openai/openai-agents-python/blob/main/examples/basic/hello_world_jupyter.ipynb) for notebook use.

## Choose an agent mode

<a id="run-a-sandbox-agent"></a>
<a id="run-a-realtime-agent"></a>
<a id="run-a-voice-agent"></a>

| Task | Guide and setup |
| --- | --- |
| Run a text workflow | [Agents](https://openai.github.io/openai-agents-python/agents/) and [models](https://openai.github.io/openai-agents-python/models/); configure credentials for your provider. |
| Run with embedded Codex | [Codex preview](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/CODEX.md); install a matching native wheel and configure Codex authentication. |
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
