# Choose how to run your agent

Choose who runs the agent loop and manages its lifetime. The Agents SDK runs Codex in your Python application; your application owns the workers, storage, and credentials. The hosted Agents API lets OpenAI run the loop and manage its sessions.

Decide separately where commands execute. An SDK agent can call Python or MCP tools without a command executor.

## Use your existing Python code

Use the Agents SDK to connect the model and tool loop to your Python code:

- **[Functions as tools](tools.md#python-functions).** `@function_tool` declares a tool from supported type annotations, validates arguments, and calls the Python function.
- **[Application objects](tools.md#application-context).** `ToolContext` gives a tool access to the application's context, such as an account or database client.
- **[Python result types](configuration.md).** Set `output_type` to a supported type, such as a Pydantic model. The SDK validates the result and returns a value of that type in `final_output`.

Configure the agent's tools for your application. If the agent needs file or process access, also choose an executor and its [execution permissions](execution.md#executor-policies).

Follow [installation](installation.md) and the [quickstart](index.md) to build your first agent.

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
| Keep history in an application database | `CustomStorage` backed by a `SessionStore` | Start with the [storage guide](state.md) and check its preview limits. Reconcile uncertain tool effects before resuming. |
| Move to another worker | An idle `SessionState` checkpoint in trusted application storage | Supply current tools, credentials, and context when restoring. Copy workspace artifacts separately. Check readiness before saving. |

### Combine approvals, generated code, and delegation

To wait for a human reply after the current worker closes, [suspend a settled pending manual request on the root Session](state.md#save-a-settled-manual-request). Finish policy and preparation work before saving. On restore, supply the matching output declaration for plain text, native strict output, or supported prompted output.

Keep the Session open instead when any of these limits applies:

- The pending request belongs to a child thread.
- The root or a child still has active code or background processes.
- A native approval or MCP question still needs a reply.
- The root operation uses `before_step`.

For individual Python calls, use function tools directly. Set `programmatic_tool_calling=True` when the model needs generated JavaScript to coordinate tool calls and aggregate their results in V8. This JavaScript can call the declared tools; it cannot execute arbitrary Python.

A checkpoint retains settled native stored values and terminal code-cell observations without replaying completed cells. It does not preserve active continuations or the arbitrary JavaScript heap. See [capture limits](state.md#save-a-settled-manual-request) before combining generated code with suspension.

Declare `NativeAgents` to delegate with the same permissions. Children share selected tools, context, and policies and return text. Model metadata and nesting depth limit which children can start. Use separately configured Agents when tasks need different tool access.

### Check endpoint support and capture readiness

For a custom Responses endpoint, run separate [text, tools, strict-output, and WebSocket checks](providers.md). A text response and `websockets_allowed` do not prove full protocol support.

In an open Session, `session.capabilities.get(feature)` reports configuration support. To check whether the Session is ready to save, call `await session.checkpoint_status(kind="idle")` or use `kind="manual_wait"`.

A readiness check reserves no state. The SDK checks again when saving, and independent children can still be running when the root result arrives. See [support and readiness](state.md#check-support-and-capture-readiness).
