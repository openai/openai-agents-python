# Hosted tool compatibility

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

## Python SDK support

| Adapter in this checkout | File search, Code Interpreter, image generation, and hosted shell |
| --- | --- |
| [Direct Responses](../../src/agents/models/openai_responses.py) | **Supported for request serialization** over HTTP (buffered or streaming) and WebSocket (`response.create`). |
| [Chat Completions](../../src/agents/models/chatcmpl_converter.py) | **SDK-rejected** with `UserError` before sending the request. |
| [Embedded Codex](../../src/agents/models/codex.py) (`CodexProvider`) | **SDK-rejected** with `UserError` during [tool preparation](../../src/agents/models/_codex_inputs.py), before starting a native thread. Same restriction for standard Responses and Responses Lite, regardless of model or login method. |

**Evidence** — local requests to simulated HTTP and WebSocket peers, plus local rejection checks. **Unverified** — live tool execution, account access, Azure deployments, and other providers. **Live examples** — possible API charges.

## Provider-documented support on direct Responses

Three example agent models, with no SDK model allowlist implied. **Listed** — explicit support in the linked OpenAI model reference. **Unverified** — absent from the list, with provider acceptance or rejection unknown.

| Tool and Python example | [`gpt-6.1-sol`](https://developers.openai.com/api/docs/models/gpt-6.1-sol.md) | [`gpt-5.2`](https://developers.openai.com/api/docs/models/gpt-5.2.md) | [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1.md) ([deprecated](https://developers.openai.com/api/docs/models/gpt-4.1)) |
| --- | --- | --- | --- |
| [`FileSearchTool`](file_search.py) | Listed | Listed | Listed |
| [`CodeInterpreterTool`](code_interpreter.py) | Listed | Listed | Listed |
| [`ImageGenerationTool`](image_generator.py) | Listed | Listed | Listed |
| [`ShellTool` with a hosted container](container_shell_inline_skill.py) | Listed | Listed | Unverified |

**[Image generation](https://developers.openai.com/api/docs/guides/tools-image-generation)** — `Agent.model` for the model calling the tool (the table above), and the optional `model` field in `ImageGenerationTool.tool_config` for the image generator.

## Shell execution

| Configuration | Execution |
| --- | --- |
| `ShellTool(environment={"type": "container_auto"})` | OpenAI-managed [hosted shell](https://developers.openai.com/api/docs/guides/tools-shell). For an existing container, `environment={"type": "container_reference", "container_id": container_id}`. Required defaults for both modes — `executor=None`, `needs_approval=False`, and `on_approval=None`. |
| [`ShellTool(executor=...)`](shell.py) | Application-supplied executor in the default `local` environment. With `CodexProvider`, native workspace tools disabled for the run. |
| Native Codex shell, when enabled | Codex workspace execution under Codex permissions, separate from OpenAI-hosted containers. |
