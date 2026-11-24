# Hosted tool compatibility

This table covers [file search](file_search.py), [Code Interpreter](code_interpreter.py), [image generation](image_generator.py) and [hosted shell](container_shell_inline_skill.py).

| Python SDK provider | What happens with these four tools |
| --- | --- |
| [Direct Responses](../../src/agents/models/openai_responses.py) | Includes the tools in HTTP requests (buffered or streamed) and WebSocket `response.create` messages. |
| [Chat Completions](../../src/agents/models/chatcmpl_converter.py) | Raises `UserError` before sending the request. |
| [Embedded Codex](../../src/agents/models/codex.py) | [Rejects the tools](../../src/agents/models/_codex_inputs.py) with `UserError` before starting a thread, for both Responses and Responses Lite. |

Check [model support](https://developers.openai.com/api/docs/models) and account access. Including a tool in a request doesn't guarantee execution. Live calls may incur charges.

## Use direct Responses

Responses is the SDK default for unprefixed OpenAI model names. In an async function, use a compatible `model_name`, a configured `hosted_tool` and your `prompt`.

```python
from agents import Agent, Runner

agent = Agent(name="assistant", model=model_name, tools=[hosted_tool])
result = await Runner.run(agent, prompt)
```

If your app defaults to Chat Completions, replace the last line with this per-run override. Use an unprefixed model name. If `agent.model` is an `OpenAIChatCompletionsModel` instance, replace it with that name first.

```python
from contextlib import aclosing

from agents import OpenAIProvider, RunConfig

async with aclosing(OpenAIProvider(use_responses=True)) as provider:
    result = await Runner.run(agent, prompt, run_config=RunConfig(model_provider=provider))
```

For streaming, call `Runner.run_streamed()` and drain `result.stream_events()` inside the block before the provider closes.

## Image and shell settings

- [Image generation](https://developers.openai.com/api/docs/guides/tools-image-generation) uses `Agent.model` to call the tool. The optional `model` in `ImageGenerationTool.tool_config` selects the image generator.
- [Hosted shell](https://developers.openai.com/api/docs/guides/tools-shell) uses `ShellTool(environment={"type": "container_auto"})` or `environment={"type": "container_reference", "container_id": container_id}`. Keep `executor=None`, `needs_approval=False` and `on_approval=None`.
- [Local shell](shell.py) uses `ShellTool(executor=...)`. With `CodexProvider`, this disables native workspace tools. The native shell uses Codex workspace permissions when enabled. Enabling native children with `CodexProvider(children=...)` disables workspace tools and rejects SDK shell tools.

## Where does the agent loop run?

OpenAI can run a hosted tool while your app runs the loop. Choose the [Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview) to have OpenAI run the loop.

| Responsibility | Python SDK | [Agents API quickstart](https://developers.openai.com/api/docs/guides/agents-api/quickstart) |
| --- | --- | --- |
| Loop and history | Your app runs `Runner` and can supply a session. | OpenAI runs your [configured agent](https://developers.openai.com/api/docs/guides/agents-api/configuration) and manages the session. |
| Workspace | Your app runs local tools with the permissions you grant. | Use a hosted sandbox or run the [self-hosted executor](https://developers.openai.com/api/docs/guides/agents-api/environments/self-hosted) in an isolated workspace with limited permissions. |
| Progress | Consume `result.stream_events()`. | Stream [session events](https://developers.openai.com/api/docs/guides/agents-api/sessions/events) or receive lifecycle notifications by webhook. |
