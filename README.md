# OpenAI Agents SDK [![PyPI](https://img.shields.io/pypi/v/openai-agents?label=pypi%20package)](https://pypi.org/project/openai-agents/)

The OpenAI Agents SDK is a lightweight yet powerful framework for building multi-agent workflows. It is provider-agnostic, supporting the OpenAI Responses and Chat Completions APIs, as well as 100+ other LLMs.

<img src="https://cdn.openai.com/API/docs/images/orchestration.png" alt="Image of the Agents Tracing UI" style="max-height: 803px;">

> [!NOTE]
> Looking for the JavaScript/TypeScript version? Check out [Agents SDK JS/TS](https://github.com/openai/openai-agents-js).

### Core concepts:

1. [**Agents**](https://openai.github.io/openai-agents-python/agents): LLMs configured with instructions, tools, guardrails, and handoffs
1. **[Agents as tools](https://openai.github.io/openai-agents-python/tools/#agents-as-tools) / [Handoffs](https://openai.github.io/openai-agents-python/handoffs/)**: Delegating to other agents for specific tasks
1. [**Tools**](https://openai.github.io/openai-agents-python/tools/): Various Tools let agents take actions (functions, MCP, hosted tools)
1. [**Guardrails**](https://openai.github.io/openai-agents-python/guardrails/): Configurable safety checks for input and output validation
1. [**Human in the loop**](https://openai.github.io/openai-agents-python/human_in_the_loop/): Built-in mechanisms for involving humans across agent runs
1. [**Sessions**](https://openai.github.io/openai-agents-python/sessions/): Automatic conversation history management across agent runs
1. [**Tracing**](https://openai.github.io/openai-agents-python/tracing/): Built-in tracking of agent runs, allowing you to view, debug and optimize your workflows
1. [**Realtime Agents**](https://openai.github.io/openai-agents-python/realtime/quickstart/): Build powerful voice agents with full features

Explore the [examples](https://github.com/openai/openai-agents-python/tree/main/examples) directory to see the SDK in action, and read our [documentation](https://openai.github.io/openai-agents-python/) for more details.

## Get started

To get started, set up your Python environment (Python 3.10 or newer required), and then install OpenAI Agents SDK package.

### venv

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install openai-agents
```

For voice support, install with the optional `voice` group: `pip install 'openai-agents[voice]'`. For Redis session support, install with the optional `redis` group: `pip install 'openai-agents[redis]'`.

### uv

If you're familiar with [uv](https://docs.astral.sh/uv/), installing the package would be even easier:

```bash
uv init
uv add openai-agents
```

For voice support, install with the optional `voice` group: `uv add 'openai-agents[voice]'`. For Redis session support, install with the optional `redis` group: `uv add 'openai-agents[redis]'`.

## Run your first agent

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

(_If running this, ensure you set the `OPENAI_API_KEY` environment variable_)

(_For Jupyter notebook users, see [hello_world_jupyter.ipynb](https://github.com/openai/openai-agents-python/blob/main/examples/basic/hello_world_jupyter.ipynb)_)

Explore the [examples](https://github.com/openai/openai-agents-python/tree/main/examples) directory to see the SDK in action, and read our [documentation](https://openai.github.io/openai-agents-python/) for more details.

## Acknowledgements

We'd like to acknowledge the excellent work of the open-source community, especially:

-   [Pydantic](https://docs.pydantic.dev/latest/) (data validation) and [PydanticAI](https://ai.pydantic.dev/) (advanced agent framework)
-   [LiteLLM](https://github.com/BerriAI/litellm) (unified interface for 100+ LLMs)
-   [MkDocs](https://github.com/squidfunk/mkdocs-material)
-   [Griffe](https://github.com/mkdocstrings/griffe)
-   [uv](https://github.com/astral-sh/uv) and [ruff](https://github.com/astral-sh/ruff)

We're committed to continuing to build the Agents SDK as an open source framework so others in the community can expand on our approach.
