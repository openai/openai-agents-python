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

Start with the smallest configuration that meets your needs. Check the limits below before combining features.

### Choose output and storage

| Need | Choose | Limits |
| --- | --- | --- |
| A typed answer | `output_type=Answer` and `agent.run` | Native strict schemas have [restrictions](configuration.md#structured-output). `PromptedOutput` requests JSON and validates it in Python without provider schema enforcement. |
| Continue on one machine | `LocalStorage` and the same profile | One native writer at a time. No portable `save_state`, `suspend`, or `fork`. Prompted output requires `max_retries=0`. |
| Move to another worker | An idle `SessionState` checkpoint in trusted application storage | Supply current tools, credentials, and context when restoring. Copy workspace artifacts separately. Check readiness before saving. |

### Combine approvals, generated code, and delegation

To pause for human approval across worker lifetimes, the root must have a pending manual request with plain text or matching prompted output. Policy and preparation work must finish before capture.

Keep the Session open instead when any of these limits applies:

- Native strict output is active.
- The pending request belongs to a child thread.
- The root or a child has executed a code cell, even if the cell has finished.

Set `programmatic_tool_calling=True` to aggregate tool results with generated JavaScript in V8. It cannot run arbitrary Python this way. Idle checkpoints preserve conversation history but not the JavaScript runtime state. Manual suspension requires history proving that neither the root nor a child has executed a code cell. See [capture limits](sessions.md#saving-a-settled-manual-request). Use direct function tools for simple calls.

Declare `NativeAgents` to delegate with the same permissions. Children share selected tools, context, and policies and return text. Model metadata and nesting depth limit which children can start. Use separately configured Agents when tasks need different tool access.

### Check endpoint support and capture readiness

For a custom Responses endpoint, run separate [text, tools, strict-output, and WebSocket checks](providers.md). A text response and `websockets_allowed` do not prove full protocol support.

In an open Session, `session.capabilities.get(feature)` reports configuration support. To check whether the Session is ready to save, call `await session.checkpoint_status(kind="idle")` or use `kind="manual_wait"`.

A readiness check reserves no state. The SDK checks again when saving, and independent children can still be running when the root result arrives. See [support and readiness](sessions.md#check-support-and-capture-readiness).
