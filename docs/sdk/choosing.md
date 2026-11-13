# Choose how to run your agent

Choose who runs the agent loop and manages its lifetime. The Agents SDK runs Codex in your Python application; your application owns the workers, storage, and credentials. The hosted Agents API lets OpenAI run the loop and manage its sessions.

Decide separately where commands execute. An SDK agent can call Python or MCP tools without a command executor.

## Use your existing Python code

Use the Agents SDK to connect the model and tool loop to your Python code:

- **[Functions as tools](tools.md#python-functions).** `@function_tool` declares a tool from supported type annotations, validates arguments, and calls the Python function.
- **[Application objects](tools.md#application-context).** `ToolContext` gives a tool access to the application's context, such as an account or database client.
- **[Python result types](outputs.md).** Set `output_type` to a supported type, such as a Pydantic model. The SDK validates the result and returns a value of that type in `final_output`.

Configure the agent's tools for your application. If the agent needs file or process access, also choose an executor and its [execution permissions](execution.md#executor-policies).

Follow [installation](installation.md) and the [quickstart](index.md) to build your first agent.

## Let OpenAI run the agent

Choose [Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview) when you want OpenAI to run the agent loop, retain session state and manage recovery.

Choose where commands run separately: an OpenAI-hosted environment, a self-hosted environment, or no command execution. In a self-hosted environment, commands run on your infrastructure and OpenAI runs the agent loop.

## Choose feature combinations

After your first run, use these guides to choose the features your application needs. Each guide explains the supported combinations and their limits.

<a id="choose-output-and-storage"></a>
<a id="combine-approvals-generated-code-and-delegation"></a>
<a id="check-endpoint-support-and-capture-readiness"></a>

| Task | Start here |
| --- | --- |
| Return a typed answer | [Structured output](outputs.md) explains provider-enforced schemas and Python validation. |
| Continue on one machine or store history in a database | [Storage choices](state.md#choose-how-to-store-a-conversation) compares `LocalStorage`, `CustomStorage`, and portable checkpoints. |
| Move work to another worker or wait for a person | Check [capture readiness](state.md#check-support-and-capture-readiness) and the limits on [saving manual requests](state.md#save-a-settled-manual-request). |
| Coordinate tools with generated JavaScript | [Programmatic tool calling](configuration.md#programmatic-tool-calling) explains the execution model and checkpoint limits. |
| Delegate independent work | [Native children](delegation.md#native-children) explains shared tools, permissions, and child lifecycles. |
| Use a custom model endpoint | Run the [endpoint checks](providers.md) for each required feature, including tools, strict output, and WebSockets. |

Configuration support and runtime readiness are different checks. A Session can support checkpoints but still have active work that prevents saving. The [state guide](state.md#check-support-and-capture-readiness) explains how to check both.
