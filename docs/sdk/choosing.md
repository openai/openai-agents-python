# Choose how to run your agent

For a new Python agent application, start with Agents SDK v1. It runs the Codex harness in your application and connects it to Python functions, application data and typed results.

Follow the [installation guide](installation.md) to install the SDK preview.

## Use your existing Python code

Agents SDK v1 handles the Python integration while Codex runs the model and tool loop:

- **[Functions as tools](tools.md#python-functions).** `@function_tool` uses supported annotations to declare a tool, validates arguments and calls the Python function.
- **[Application objects](tools.md#application-context).** `ToolContext` gives a tool access to the application's context, such as an account or database client.
- **[Python result types](configuration.md).** Set `output_type` to a supported type, such as a Pydantic model. The SDK validates the result and returns a value of that type in `final_output`.

You operate the application workers, storage and credentials. Configure the agent's tools and [execution permissions](tools.md#executor-policies) for your application.

Continue with the [Agent and Session guide](index.md) to build your first agent.

## Let OpenAI run the agent

Choose [Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview) when you want OpenAI to run the agent loop, retain session state and manage recovery.

Choose its command execution environment separately: OpenAI-hosted, self-hosted or none. A self-hosted environment runs commands on infrastructure you operate; OpenAI still runs the agent loop.

## Choose feature combinations

Start with the smallest configuration that meets the application requirement. These choices have different persistence and authority boundaries:

| Need | Choose | Limit and alternative |
| --- | --- | --- |
| A typed answer from one task | `output_type=Answer` and `agent.run` | Native strict schemas have [restrictions](configuration.md#structured-output). Use `PromptedOutput` when prompt-based JSON validation fits; it does not add provider schema enforcement. |
| Continue on one machine | `LocalStorage` and the same profile | One native writer; no portable `save_state`, `suspend` or `fork`. Prompted corrections require `max_retries=0`. Use a portable checkpoint when moving between workers. |
| Resume a conversation on another worker | An idle `SessionState` checkpoint in trusted application storage | Rebind current tools, credentials and context; copy workspace artifacts separately. Check readiness before capture. |
| Pause for human approval across worker lifetimes | A pending root manual request with plain or matching prompted output, after its policy and preparation work has settled | Native strict output and child requests cannot use this suspension. Executed code VM cells in the root or children also prevent it, even after a cell finishes. Keep a live Session for those cases. |
| Aggregate tool results in generated code | `programmatic_tool_calling=True` | Codex runs JavaScript in V8, not arbitrary Python. Idle checkpoints preserve conversation history, not VM state. Portable manual suspension requires history to prove that no root or child code VM executed, even if a cell finished. See [capture limits](sessions.md#saving-a-settled-manual-request). Use direct function tools for simple calls. |
| Delegate with the same authority | Explicit `NativeAgents` | Children share selected tools, context and policies; their output is text. Model metadata and depth constrain spawning. Use separately configured Agents when distinct tool access is required. |
| Check a custom Responses endpoint | Separate [text, tools, strict-output and WebSocket checks](providers.md) | A text pass and `websockets_allowed` do not establish complete protocol compatibility. |

In an open Session, `session.capabilities.get(feature)` reports configuration support. `await session.checkpoint_status(kind="idle")` or `kind="manual_wait"` checks the current capture boundary. A readiness probe reserves nothing; capture validates again. See [support and readiness](sessions.md#check-support-and-capture-readiness). A root result does not prove that independent children have finished.
