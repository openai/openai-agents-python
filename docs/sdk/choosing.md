# Choose how to run your agent

Use Agents SDK v1 to run Codex in your Python application. Connect Python functions and application data, and return typed results.

Follow the [installation guide](installation.md) to install the SDK preview.

## Use your existing Python code

Codex runs the model and tool loop. Agents SDK v1 connects that loop to your Python code:

- **[Functions as tools](tools.md#python-functions).** `@function_tool` declares a tool from supported type annotations, validates arguments, and calls the Python function.
- **[Application objects](tools.md#application-context).** `ToolContext` gives a tool access to the application's context, such as an account or database client.
- **[Python result types](configuration.md).** Set `output_type` to a supported type, such as a Pydantic model. The SDK validates the result and returns a value of that type in `final_output`.

You manage the application workers, storage, and credentials. Configure the agent's tools and [execution permissions](tools.md#executor-policies) for your application.

Continue with the [Agent and Session guide](index.md) to build your first agent.

## Let OpenAI run the agent

Choose [Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview) when you want OpenAI to run the agent loop, retain session state and manage recovery.

Choose where commands run separately: an OpenAI-hosted environment, a self-hosted environment, or no command execution. In a self-hosted environment, commands run on your infrastructure and OpenAI runs the agent loop.

## Choose feature combinations

Start with the smallest configuration that meets your needs. The table shows what each choice can save and what access it grants:

| Need | Choose | Limit and alternative |
| --- | --- | --- |
| A typed answer from one task | `output_type=Answer` and `agent.run` | Native strict schemas have [restrictions](configuration.md#structured-output). Use `PromptedOutput` to request JSON in the prompt and validate it in Python. The provider does not enforce that schema. |
| Continue on one machine | `LocalStorage` and the same profile | Only one native writer can use the stored conversation at a time. Portable `save_state`, `suspend`, and `fork` are unavailable. Set `max_retries=0` for prompted output. Use a portable checkpoint when moving between workers. |
| Resume a conversation on another worker | An idle `SessionState` checkpoint in trusted application storage | Supply current tools, credentials, and context when restoring. Copy workspace artifacts separately. Check whether the Session is ready before saving. |
| Pause for human approval across worker lifetimes | A pending manual request in the root thread, with plain text or matching prompted output, after policy and preparation work finishes | Native strict output and requests from child threads cannot use this suspension. It is also unavailable if the root or a child has executed a code cell, even if the cell has finished. Keep the Session open for those cases. |
| Aggregate tool results in generated code | `programmatic_tool_calling=True` | Codex runs generated JavaScript in V8. It cannot run arbitrary Python this way. Idle checkpoints save conversation history but cannot save the JavaScript runtime state. To suspend a manual request, the history must show that neither the root nor a child has executed a code cell. See [capture limits](sessions.md#saving-a-settled-manual-request). Use direct function tools for simple calls. |
| Delegate with the same authority | Explicit `NativeAgents` | Children share the selected tools, context, and policies and return text. Model metadata and the nesting depth limit which children can start. Use separately configured Agents when tasks need different tool access. |
| Check a custom Responses endpoint | Separate [text, tools, strict-output and WebSocket checks](providers.md) | A successful text request and `websockets_allowed` do not prove that the endpoint supports the full protocol. |

In an open Session, `session.capabilities.get(feature)` reports whether the configuration supports a feature. Call `await session.checkpoint_status(kind="idle")` or use `kind="manual_wait"` to check whether the Session is ready to save. This check does not reserve the state. The SDK checks again when saving. See [support and readiness](sessions.md#check-support-and-capture-readiness). Independent children can still be running when the root result arrives.
