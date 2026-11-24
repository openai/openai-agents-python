# Hosted tool compatibility

Start with the Python SDK's `Runner` to run the agent loop in your application. Adding an OpenAI-hosted tool leaves the loop there. Choose the [Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview) if you want OpenAI to run the loop.

| Responsibility | Python SDK | Agents API |
| --- | --- | --- |
| Agent and session | Define an `Agent` in Python. Optionally pass a session to `Runner` for history. | Supply an [agent configuration or saved agent ID](https://developers.openai.com/api/docs/guides/agents-api/configuration). OpenAI manages the session. |
| Tool environment | Your app runs local tools. OpenAI runs hosted tools. | For workspace tools, choose an OpenAI-hosted sandbox or connect a [self-hosted sandbox](https://developers.openai.com/api/docs/guides/agents-api/environments/self-hosted). For self-hosted tools, run the executor in an isolated environment you control. |
| Progress | Use `Runner.run_streamed()` and consume `result.stream_events()`. | Stream [session events](https://developers.openai.com/api/docs/guides/agents-api/sessions/events) or use webhooks for lifecycle notifications. |

## Run in your application

With the SDK defaults, the Runner uses the Responses API for unprefixed OpenAI model names. Choose a compatible `model_name` and a configured `hosted_tool` from the examples below. In an async function, run the agent with your `prompt`.

```python
from agents import Agent, Runner

agent = Agent(name="assistant", model=model_name, tools=[hosted_tool])
result = await Runner.run(agent, prompt)
```

If your application defaults to Chat Completions, replace the last line with this per-run override. `aclosing` awaits `provider.aclose()` when the block exits.

```python
from contextlib import aclosing

from agents import OpenAIProvider, RunConfig

async with aclosing(OpenAIProvider(use_responses=True)) as provider:
    result = await Runner.run(agent, prompt, run_config=RunConfig(model_provider=provider))
```

For streaming, start `Runner.run_streamed` and consume `result.stream_events()` before closing the provider.

## Run on the Agents API

Follow the [quickstart](https://developers.openai.com/api/docs/guides/agents-api/quickstart) for client setup and a supported `model_name`. This call starts a task with your `prompt` and returns a session. Use `session.id` to follow progress and retrieve output.

```python
from openai import OpenAI

with OpenAI() as client:
    session = client.beta.agents.sessions.create(
        agent={"model": model_name, "instructions": "Follow the user's request."},
        environment={"type": "openai_hosted"},
        input=prompt,
    )
```

## Hosted tools with the Python Runner

Check the [model reference](https://developers.openai.com/api/docs/models) for tool support before running these examples.

- [File search](file_search.py)
- [Code Interpreter](code_interpreter.py)
- [Image generation](image_generator.py)
- [Hosted shell](container_shell_inline_skill.py)

| SDK adapter | File search, Code Interpreter, image generation, and hosted shell |
| --- | --- |
| [Direct Responses](../../src/agents/models/openai_responses.py) | Serializes requests over HTTP (buffered or streaming) and WebSocket (`response.create`). |
| [Chat Completions](../../src/agents/models/chatcmpl_converter.py) | Raises `UserError` before sending the request. |
| [Embedded Codex](../../src/agents/models/codex.py) (`CodexProvider`) | Raises `UserError` during [tool preparation](../../src/agents/models/_codex_inputs.py), before starting a native thread. Applies to both Responses and Responses Lite in this adapter. |

Local checks use simulated HTTP and WebSocket peers. Serialization alone cannot confirm account access or tool execution. Live examples may incur API charges.

For [image generation](https://developers.openai.com/api/docs/guides/tools-image-generation), `Agent.model` calls the tool. The optional `model` in `ImageGenerationTool.tool_config` selects the image generator.

## Shell execution

| Configuration | Execution |
| --- | --- |
| `ShellTool(environment={"type": "container_auto"})` | OpenAI-managed [hosted shell](https://developers.openai.com/api/docs/guides/tools-shell). For an existing container, `environment={"type": "container_reference", "container_id": container_id}`. Required defaults for both modes — `executor=None`, `needs_approval=False`, and `on_approval=None`. |
| [`ShellTool(executor=...)`](shell.py) | Application-supplied executor in the default `local` environment. With `CodexProvider`, native workspace tools disabled for the run. |
| Native Codex shell, when enabled | Codex workspace execution under Codex permissions, separate from OpenAI-hosted containers. |
