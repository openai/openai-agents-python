# OpenAI Agents SDK [![PyPI](https://img.shields.io/pypi/v/openai-agents?label=pypi%20package)](https://pypi.org/project/openai-agents/)

Build Python agent workflows with instructions, tools, handoffs, and conversation history. Run agents with OpenAI models or another supported model provider.

**Experimental Codex preview:** Start with the [Codex quickstart](CODEX.md) to run an SDK agent with embedded Codex Core. The preview uses platform-specific native wheels from a GitHub prerelease; installing the stable PyPI package alone does not provide embedded Codex. See the [Codex provider guide](docs/models/codex.md) for capabilities, authentication, execution controls, and limits, or choose a [Codex example](examples/codex/README.md).

## Get started

Use Python 3.10 or newer. Choose an installation:

- Standard SDK: `pip install openai-agents` or `uv add openai-agents`.
- Embedded Codex preview: follow the [preview installation instructions](CODEX.md#install-the-preview) for your platform.
- Optional features: install `openai-agents[voice]` for voice pipelines, `openai-agents[redis]` for Redis sessions, or `openai-agents[docker]` for the Docker sandbox client.

<a id="venv"></a>
<a id="uv"></a>

For environment setup, see the [SDK quickstart](docs/quickstart.md).

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

The script prints a greeting for Ada; wording can vary. The SDK calls `greeting` when the model requests the tool. For a complete embedded Codex example and its separate credential and tracing requirements, use the [Codex quickstart](CODEX.md#run-your-first-agent).

See the [Jupyter example](examples/basic/hello_world_jupyter.ipynb) for notebook use.

## Choose an agent mode

<a id="run-a-sandbox-agent"></a>
<a id="run-a-realtime-agent"></a>
<a id="run-a-voice-agent"></a>

| Task | Guide and setup |
| --- | --- |
| Run a text workflow | [Agents](docs/agents.md) and [models](docs/models/index.md); configure credentials for your provider. |
| Run with embedded Codex | [Codex preview](CODEX.md); install a matching native wheel and configure Codex authentication. |
| Work in a sandbox workspace | [Sandbox agents](docs/sandbox_agents.md) and [sandbox clients](docs/sandbox/clients.md); configure a client. The local Unix client supports macOS and Linux; Windows can use Docker or a hosted client. |
| Run a realtime voice or multimodal agent | [Realtime quickstart](docs/realtime/quickstart.md); configure OpenAI credentials and a WebSocket connection. |
| Combine speech recognition, an agent, and speech generation | [Voice quickstart](docs/voice/quickstart.md); install the voice extra and configure OpenAI credentials. |

## Guides and examples

<a id="core-concepts"></a>

- [Tools](docs/tools.md): Python functions, MCP, and hosted tools.
- [Agents as tools](docs/tools.md#agents-as-tools) and [handoffs](docs/handoffs.md): delegate tasks to other agents.
- [Guardrails](docs/guardrails.md) and [human in the loop](docs/human_in_the_loop.md): validate inputs and outputs, and pause for approval.
- [Sessions](docs/sessions/index.md) and [tracing](docs/tracing.md): manage conversation history and inspect runs.
- [Examples](examples/) and [documentation](docs/index.md): find additional workflows and API references.
- [JavaScript/TypeScript SDK](https://github.com/openai/openai-agents-js): use the SDK in JavaScript or TypeScript.

## Get help

Use [GitHub issues](https://github.com/openai/openai-agents-python/issues) for questions, bug reports, and feedback. Report security vulnerabilities through [SECURITY.md](SECURITY.md).

## Contributing

Follow [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and contribution policy. Pull requests are limited to repository collaborators.

## Acknowledgements

The SDK uses open-source libraries including [Pydantic](https://docs.pydantic.dev/latest/), [Requests](https://github.com/psf/requests), [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), and [Griffe](https://github.com/mkdocstrings/griffe).
