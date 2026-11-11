# Tools

Declare the functions and services an agent may call in its `tools` list. Codex selects and dispatches calls during the model loop. Python functions run in your application with its permissions. For [MCP tools](mcp.md), Codex manages the connection and the selected server executes the operation.

Examples with `await` or `async with` are fragments for an async function. [Configure the provider and its credentials](configuration.md#provider-connection) before opening a Session.

## Choose a tool

| What you need | Start here |
| --- | --- |
| Call a Python function | [Python functions](#python-functions) |
| Search the web through the native provider | [Native web search](#native-web-search) |
| Search uploaded files, run provider code, or generate images | [Provider-hosted tools](#provider-hosted-tools) |
| Give a function application data | [Application context](#application-context) |
| Connect tools from an MCP server | [MCP servers](mcp.md) |
| Run commands or edit workspace files | [Local and remote execution](execution.md) |
| Delegate work to child agents | [Native children](delegation.md#native-children) |
| Supply a tool result from your application | [Manual tool results](approvals.md#manual-tool-results) |
| Recover an external job after a host restart | [Experimental job recovery](approvals.md#experimental-job-recovery) |

## Python functions

Decorate a function with `@function_tool`. Its annotated parameter types define the tool's input. Codex converts that input into its supported model-visible schema. The docstring describes the function. `name=` and `description=` provide explicit overrides.

```python
from agents import function_tool


@function_tool
async def greeting(name: str) -> str:
    """Return a short greeting for a person."""
    return f"Hello, {name}!"
```

### Declare arguments and return types

Annotate each model argument and declare it as a parameter that accepts a keyword argument. Positional-only parameters, `*args`, and `**kwargs` are unsupported. If an existing function uses them, wrap it in a tool function with named, annotated parameters.

A return annotation is optional. In this preview, the SDK validates the returned value against that annotation when present. If a synchronous function returns an `Awaitable[T]` or `Coroutine[Any, Any, T]`, the SDK awaits it and validates the resolved value as `T`. Supported result formats and safety checks still apply without a return annotation.

Parameter names cannot start with `_`. For a JSON argument such as `_id`, use `record_id: Annotated[str, Field(alias="_id")]` with `Annotated` from `typing` and `Field` from `pydantic`.

The SDK preserves `Annotated` metadata and validates the full declaration before calling the function. Codex includes only the JSON Schema features that the pinned runtime supports in model requests. Python enforces constraints such as `minLength` even when the provider does not receive them. Do not rely on the provider to enforce every Python constraint. On every supported platform, Python rejects invalid arguments before the tool body runs.

### Execution and cancellation

Codex can dispatch a valid tool call while the model response is still streaming. A later stream error or content filter does not undo an effect that has already completed. Codex retains accepted results when it continues the conversation.

The SDK converts Python return values before the native runtime accepts the reply.

The decorator accepts synchronous and asynchronous functions. The SDK runs synchronous functions in a worker with copied context variables. The application context remains the same object. On cancellation, the SDK waits for a running worker before the submission finishes. Use async functions when the body needs cooperative cancellation.

Use `@function_tool(before=..., after=...)` to control a Python callback:

- Before the function runs, `before(context)` receives validated arguments and returns `Allow`, `Deny`, or `ReplaceArguments`.
- After the function runs, `after(context, output)` receives and returns a `ToolOutput`. Changing the reply cannot undo the function's effects.

Use `callback_timeout` to set the [function body's deadline](#callback-deadlines). A manual `FunctionTool` has no callback and cannot use callback or policy settings.

### Inspect Python policy arguments

`ToolContext.original_arguments` contains a snapshot of the received JSON argument mapping. `ToolContext.effective_arguments` contains the validated Python keyword arguments, including applied defaults. The mappings use the incoming JSON field names and Python parameter names respectively. A `before` hook can inspect both without changing them:

```python
from agents import Allow, Deny, ToolContext, function_tool


def check_quantity(ctx: ToolContext[None]) -> Allow | Deny:
    if "quantity" not in ctx.original_arguments:
        return Deny("Supply quantity explicitly.")
    if ctx.effective_arguments["quantity"] > 10:
        return Deny("Request at most 10 items.")
    return Allow()


@function_tool(before=check_quantity)
def quantity_label(quantity: int = 1) -> str:
    return f"Quantity: {quantity}"
```

After an accepted `ReplaceArguments`, the SDK validates the replacement again and updates `effective_arguments`. The `after` hook receives those accepted effective values. `original_arguments` still describes the initial request. `Deny` returns a local tool error without running the function body or its `after` hook.

### Return text, images and tool errors

Use `ToolOutput.text(...)` for explicit text, `ToolOutput.image(...)` for an image reference, and `ToolOutput.error(...)` for a failed tool reply. Combine `ToolOutputText` and `ToolOutputImage` items for mixed content:

```python
from agents import ToolOutput, ToolOutputImage, ToolOutputText, function_tool

image_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
text_reply = ToolOutput.text("Preview ready.")
image_reply = ToolOutput.image(image_url)
error_reply = ToolOutput.error("No preview is available.")


@function_tool
def preview() -> ToolOutput:
    return ToolOutput([ToolOutputText("A one-pixel preview."), ToolOutputImage(image_url)])
```

Annotate functions that return these objects with `ToolOutput`, as above. In `0.24.0-alpha.7`, images require inline base64 image data URLs; remote URLs, file IDs, and local paths are unsupported.

Matching development builds also accept HTTP or HTTPS image URLs and provider-owned image IDs with `ToolOutput.image(...)` or `ToolOutputImage(...)`. Supply exactly one of `image_url` and `file_id`. Optional `detail` accepts `None`, `"auto"`, `"low"`, `"high"`, or `"original"`, subject to the current model's image support. Known combinations that would lose detail fail rather than silently changing the image. These additions require a [matching development build](versions.md).

HTTP(S) URLs and file IDs require API-key authentication and a compatible model that uses standard Responses. Set `Agent.model` explicitly; the current default model uses Responses Lite. `CodexLogin` and Responses Lite do not support these references. Use inline base64 image data URLs on those routes, subject to the model's image and detail support. See [provider configuration](configuration.md#provider-connection).

```python
remote_reply = ToolOutput.image("https://example.com/preview.png", detail="low")
uploaded_reply = ToolOutput.image(file_id="file-your-uploaded-image")
```

These helpers preserve the reference without fetching URLs or uploading files. The native runtime checks the current route, model, and detail before publishing the result. Local filesystem paths are unsupported.

Development builds add `ToolOutput.file(...)` and `ToolOutputFile(...)`. Supply exactly one of `file_id`, canonical base64 `file_data`, or HTTPS `file_url`, with an optional `filename`. Base64 data can include a base64 data-URL prefix. The SDK does not read or upload local files for these declarations. Use a provider and model that support the selected media sources.

This declaration returns text and provider-owned image and document references in order. Replace the fictional IDs with IDs available to the provider and account serving the request:

```python
from agents import ToolOutput, ToolOutputFile, ToolOutputImage, ToolOutputText, function_tool


@function_tool
def report_artifacts() -> ToolOutput:
    return ToolOutput(
        [
            ToolOutputText("Report and chart."),
            ToolOutputImage(file_id="your-image-file-id", detail="high"),
            ToolOutputFile(file_id="your-report-file-id", filename="report.pdf"),
        ]
    )
```

Include `report_artifacts` in the Agent's tools. The references can remain in accepted history and saved state. Keep private file data and IDs out of routine logs.

`ToolOutput.error(...)` returns a failed tool reply that the model can handle. Raising an exception can instead fail the owning turn after cleanup. Neither choice undoes earlier effects. Manual tools can declare `output_type=ToolOutput` and submit these values through a live request.

### Run-wide function error text

Matching [development builds](versions.md) have been accepting `tool_error_formatter` on `Agent.session()`, `Agent.run()`, and `Agent.run_sync()`. A synchronous or asynchronous callback has been formatting invalid original arguments and ordinary function-body exceptions into failed tool replies. Omission has been preserving the existing exception behavior.

```python
import asyncio

from agents import Agent, ToolErrorContext, function_tool


@function_tool
async def lookup(key: str) -> str:
    raise LookupError("Private database detail")


def format_tool_error(error: ToolErrorContext[None]) -> str | None:
    if error.phase == "arguments":
        return "Provide a string key."
    return "Record lookup is unavailable."


async def main() -> None:
    agent = Agent(instructions="Use lookup once, then explain its result.", tools=[lookup])
    result = await agent.run("Look up sample.", tool_error_formatter=format_tool_error)
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

The callback has been receiving the application `context`, observed `call` identity, `phase` (`"arguments"` or `"execution"`), safe `default_message`, and original local `error`. A returned string, including an empty string, has been replacing the tool error text. Returning `None`, returning another type, or raising while the call remains active has been selecting the safe default message. Returned text has been entering model input, history, and traces; application-approved details only.

Per-tool `input_error_function` and `error_function` have been taking precedence for their own phases. Explicit `error_function=None` has been suppressing the run-wide formatter for arguments and body failures. Callback deadlines, cancellation, rewritten arguments, other policy failures, result validation, and explicit approval rejection text have been retaining their existing behavior. Unknown tool names and native MCP or executor failures have been remaining outside this formatter. Cancellation has been waiting for formatter cleanup and discarding late replies; cleanup exceptions have been retaining their original cause.

### Callback deadlines

Set `@function_tool(callback_timeout=2.0)` to limit the callback body, including time spent waiting for synchronous execution. The SDK withholds late results even if the callback suppresses cancellation. It waits for callback cleanup before reporting completion. A running application function can delay cleanup, and the deadline cannot undo earlier effects. The deadline excludes `before`, `after`, argument validation, and result conversion.

## Application context

When a function needs application state, use `ToolContext[Application]` as its first parameter. The SDK supplies that parameter and excludes it from the schema the model receives. Pass the application value when opening or running the agent.

First complete the [installation](installation.md) and [API-key setup](index.md#run-your-first-agent). Save this complete script as `context_agent.py`:

```python
import asyncio
from dataclasses import dataclass

from agents import Agent, ToolContext, function_tool


@dataclass
class Account:
    display_name: str


@function_tool
async def account_name(ctx: ToolContext[Account]) -> str:
    return ctx.context.display_name


async def main() -> None:
    agent = Agent[Account, str](
        instructions="Use account_name to address the user.",
        tools=[account_name],
    )
    result = await agent.run("Greet me.", context=Account("Ada"))
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

In your activated Python environment, run `python context_agent.py`. The script prints a greeting for Ada; the model chooses the wording.

In `Agent[Account, str]`, `Account` is the application context type and `str` is the final output type. The SDK passes the `Account` instance to the Python function through `ctx.context`; the model does not supply that parameter. The context object itself is not model input. The function's returned display name is a tool result, so the model does receive `"Ada"`.

Mypy can lose a tool's application context type when the tool returns `Any`. Use an explicit `Agent[Application, str]` with your context class, as in `Agent[Account, str]` above, to keep `context=` arguments checked when opening Sessions or starting runs. Use the same pattern if inference does not retain the context type.

For context together with structured output, see the [complete typed-context example](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/typed_context.py).

## Native web search

To enable native search, include `WebSearch` when opening the Session. Without it, neither search route is available.

Choose the content and protocol separately:

- `mode="cached"` (default) uses indexed content. `mode="live"` lets Codex request external web access.
- `route="standalone"` (default) uses the native search protocol. `route="hosted"` uses the provider's hosted web-search protocol.

Both routes require a compatible Responses provider. Selecting a route does not add provider support or enable fallback to the other route.

```python
from agents import Agent, WebSearch

search = WebSearch(mode="cached", route="standalone")
agent = Agent(instructions="Research the user's question.", tools=[search])
```

In `0.24.0-alpha.7` or later, explicit turn and step selections can omit `search` to disable search for that operation or preparation. To enable it, select the captured declaration, such as `tools=[search]`. Turns, steps, and idle updates cannot add a search capability absent at opening or change the declaration's settings. Open a new Session to change the declaration.

The hosted route also accepts `allowed_domains`, `blocked_domains`, and approximate `location` settings. These controls require a compatible hosted-search provider and model; they are unsupported on the standalone route. Supply domains without a URL scheme. A location can contain `country`, `region`, `city`, and `timezone`; use a two-letter country code and an IANA timezone such as `America/Chicago`. The Session captures these settings at opening, and the provider applies them. Search history can retain cited URLs and consulted sources, but this interface does not return source passages.

Saved history retains search results but does not enable search. To search after restore, declare `WebSearch` again in the current Agent or opening Session tools. `Environment.network` controls executor commands, not web search.

## Provider-hosted tools

Declare `FileSearch`, `CodeInterpreter`, or `ImageGeneration` in `Agent.tools` to run these tools through the native Responses loop. Use an API-key Responses provider and a model that supports the chosen tools. Hosted tools do not support the Responses Lite protocol or Codex login. The provider validates model-specific settings.

```python
import os

from agents import Agent, CodeInterpreter, FileSearch, ImageGeneration

search = FileSearch(
    vector_store_ids=[os.environ["VECTOR_STORE_ID"]],
    max_num_results=5,
    include_search_results=True,
)
code = CodeInterpreter(container={"type": "auto", "memory_limit": "1g"})
images = ImageGeneration(output_format="png", partial_images=1)
agent = Agent(
    instructions="Use the hosted tools.",
    model=os.environ["HOSTED_TOOLS_MODEL"],
    tools=[search, code, images],
)
```

The Session captures each declaration when it opens. When you supply an explicit tool selection for a turn, include the captured declaration to enable that tool. Leaving it out of that selection disables it for that turn. Open a new Session to change its settings. Declare each hosted tool once. Hosted tools can run alongside Python functions, but their reserved names cannot also name a Python function. The provider executes hosted tools; Python function callbacks do not receive their calls.

`FileSearch` accepts metadata `filters` and `ranking_options` in the provider's format. Set `include_search_results=True` to request raw retrieved chunks. The resulting `file_search_call` item exposes `queries`, `results`, and the full `provider_data`. Without that setting, the provider can omit raw results. Assistant text retains file citations in its `annotations`.

For Code Interpreter, supply an existing container ID or an auto-container configuration with optional `file_ids`, `memory_limit`, and `network_policy`. The provider owns container creation, reuse, and expiration. Closing or cancelling a Session does not delete the container. Each `code_interpreter_call` item exposes its `container_id`, `code`, and `outputs`. Generated-file citations retain the container and file IDs in text `annotations`. Use the provider's Containers API to download files while the container remains active. See the [provider container guide](https://developers.openai.com/api/docs/guides/tools-code-interpreter#containers).

`ImageGeneration` accepts image settings such as quality, size, background, format, compression, and partial-image count. To edit an image, supply the source image as inline base64 `input_image` content and set `action="edit"`. An optional `input_image_mask` can reference a provider file ID or a base64 image URL. A completed `image_generation_call` item exposes the base64 `result` and revised prompt. A failed or unfinished call can have `result=None`; inspect `status` and `provider_data` before decoding it.

`Session.events()` and `Turn.events()` include `HostedToolCallProgress` observations from `agents.observation`. By default, hosted completion observations contain only item identity and status, and progress contains only the provider event type and SDK identities. For a trusted consumer, pass `include_execution_details=True` to either `events()` method. This exposes complete hosted items and provider progress, including code, search results, and image bytes. Keep these payloads private.

These passive streams report gaps if a reader falls behind. They retain at most 16 MiB of base64 per preview and 32 MiB across retained previews. Oversized previews also produce gaps; missed previews cannot be recovered from history. Read `Session.items()` for the final image, including images too large for a completion observation. Final tool items also retain provider fields in `provider_data`, including failure details and image metadata. These items and text annotations survive saved-state restore. Restoring history does not grant tool access: declare the tools again in the current Agent to use them in later turns.

Runnable examples for [file search, Code Interpreter, and images](https://github.com/openai/openai-agents-python/tree/feat/codex-harness/examples/codex/hosted_tools) show each tool separately.

## Connect MCP tools

Use the [MCP guide](mcp.md) to connect servers and control tool calls:

- <a id="native-mcp"></a>[Connect an HTTP server](mcp.md#connect-an-http-server).
- <a id="start-a-stdio-server"></a>[Start a stdio server](mcp.md#start-a-stdio-server).
- <a id="configure-mcp-approval"></a>[Configure MCP approval](mcp.md#configure-mcp-approval).
- <a id="handle-mcp-startup-errors"></a>[Handle MCP startup errors](mcp.md#handle-mcp-startup-errors).
- <a id="mcp-startup-and-restore-limits"></a>[Replace and restore MCP connections](mcp.md#replace-and-restore-mcp-connections).
- <a id="mcp-result-policies"></a>[Apply MCP result policies](mcp.md#mcp-result-policies).

<a id="idle-replacement"></a>
<a id="change-tools-between-turns"></a>

## Replace tools between turns

While idle, call `await session.update(tools=new_tools)` to replace tool definitions. Later turns use the updated definitions, including replacement endpoints and credentials for ordinary MCP declarations. [Service-specific verification and Apps registrations](mcp.md#service-specific-integrations) require a new Session to change their registration or credentials.

Replacements must stay within the tools declared when the Session opened:

- Python and manual tools must use opening tool names.
- MCP declarations must use opening server and raw-tool pairs.
- Each MCP server must retain its opening `defer_loading` value.

To add names or MCP pairs, or change discovery mode, open a new Session. Await a successful update before submitting work. If an update fails without confirming which definitions are active, close the Session and open a new one.

Declare every tool the conversation may need when opening the Session. Then replace the default selection while idle. This example first selects an updated `lookup` implementation and later switches to `summarize`:

```python
from agents import Agent, function_tool


@function_tool
def lookup(key: str):
    return f"Record for {key}"


@function_tool
def summarize(text: str) -> str:
    return text[:80]


@function_tool(name="lookup")
def current_lookup(key: str) -> str:
    return f"Updated record for {key}"


agent = Agent(instructions="Use the tools available for this turn.", tools=[lookup, summarize])
async with agent.session() as session:
    await session.update(tools=[current_lookup])
    first = await session.submit("Look up atlas.", tools=[lookup])
    await first.get_result()
    await session.update(tools=[summarize])
    second = await session.submit("Summarize what you found.")
    result = await second.get_result()
```

Here `lookup` selects the last accepted definition, `current_lookup`. It does not restore the original callback. Temporary `submit(tools=...)` and `before_step` selections cannot replace tools or MCP connections, even with the same names. Use an idle update for those changes. An explicit turn selection can choose a tool omitted from the current default. That selection uses the last accepted definition, including its MCP credentials.

For an idle checkpoint restore, supply matching saved Python and manual definitions. MCP declarations come from the opening `tools=` override, or otherwise `Agent.tools`. Storage does not supply credentials or callbacks. See [save and restore](state.md#save-and-restore-a-portable-checkpoint).

In `0.24.0-alpha.7` or later, an explicit selection can omit the captured `NativeAgents` declaration to disable collaboration calls for that operation or step. Idle tool updates preserve the opening executor and collaboration grants in the defaults. Update tools only after all native children close. See [Session updates](sessions.md#update-tools-instructions-and-model-settings).

For MCP connection and process restrictions, see [replace and restore MCP connections](mcp.md#replace-and-restore-mcp-connections).

<a id="select-one-mcp-tool"></a>

### Select individual tools

Use `function.ref` to select a Python or manual tool. Use `server.tool(raw_name)` to select one MCP tool allowed when the Session opened. Both return a `ToolRef`. The MCP helper supplies the normalized native namespace. These references select the last accepted definitions without adding names or connections.

This fragment uses `lookup` from the preceding staged example and a current MCP declaration named `server` that selects a raw tool named `search`:

```python
from agents import Agent, ToolRef

agent = Agent(instructions="Use the selected lookup and search tools.", tools=[lookup, server])
async with agent.session() as session:
    turn = await session.submit("Find atlas.", tools=[lookup.ref, server.tool("search")])
    result = await turn.get_result()

assert lookup.ref == ToolRef("lookup")
```

To replace an MCP declaration rather than select its tools, use an [idle update](#replace-tools-between-turns). Passing a new declaration to `submit(tools=...)` does not replace the accepted definition.

<a id="schema-discovery"></a>

### Discover tool schemas

Set `defer_loading=True` on `@function_tool(...)`, a manual `FunctionTool`, or an MCP declaration to offer selected schemas through native tool search on models that support discovery. Models without that support receive the selected schemas directly. Each MCP server keeps its own discovery setting for its selected raw tools. Programmatic tool calling retains its native direct/code exposure rules.

```python
from agents import Agent, function_tool


@function_tool(defer_loading=True)
def invoice_status(invoice_id: str) -> str:
    return f"Status for {invoice_id}: paid"


agent = Agent(instructions="Find the requested invoice status.", tools=[invoice_status])
```

Declare discovery settings at opening. Temporary selections cannot change schemas or enable discovery, and MCP idle replacements must retain opening `defer_loading` values.

Discovery does not delay selected MCP connections or startup. The model can discover only selected tools.

## Inspect or change tool values

| Declared tool | Before execution | Before its result is consumed |
| --- | --- | --- |
| Python function | Python validates parameters. `before` can allow, deny or replace arguments. | `after` receives and returns `ToolOutput`. |
| MCP server | `before` sees original input; `approval` sees effective input. `approval_mode` also selects native approval behavior. | `transform_result` accepts or transforms the native MCP result. |
| Environment executor tool | `approval` sees original arguments; only an approved call reaches `transform_arguments`. | `transform_result` changes the result before the model or generated code receives it. Direct and programmatic calls are supported. |
| Native collaboration tool | `NativeAgents.approval` can allow or deny a call. It cannot replace arguments. | No general after-result callback is promised. |

An after-result transform cannot undo an earlier effect. Use [tool notifications](#tool-notifications) to observe activity without changing tool values.

### Validate Python tool values

Put application-specific validation and normalization in the annotated function. For example, this replaces the earlier `greeting` and trims its argument before forming the result:

```python
from agents import function_tool


@function_tool
async def greeting(name: str) -> str:
    """Return a short greeting for a person."""
    return f"Hello, {name.strip()}!"
```

`function_tool` also accepts `before` and `after` for checks shared across Python callbacks. Use the function body for checks specific to that function.

For MCP input, approval and result callbacks, see [MCP result policies](mcp.md#mcp-result-policies).

## Tool notifications

Read `ToolStarted` and `ToolFinished` records from `session.events()` to observe Python and MCP tool activity. Import these types from `agents.observation`. They include call identity and event sequence, without tool arguments or results. `ToolFinished` also reports completion status.

A Python start record means the SDK accepted a function call. An MCP start record can precede approval and server execution. Cancellation may leave an MCP call without a matching finish record. A completion status does not prove that earlier effects were rolled back.

Observations do not delay tool execution or completion. The buffer has a fixed limit. If a reader falls behind and the SDK drops older records, the reader can receive `EventGap`. Use [stream observations](sessions.md#stream-observations) to consume events while waiting for a turn. Use the [supported tool controls](#inspect-or-change-tool-values) to validate or change tool values.

<a id="local-execution"></a>
<a id="replace-write-roots-between-turns"></a>
<a id="remote-execution"></a>
<a id="select-executor-tools"></a>
<a id="argument-validation"></a>
<a id="workspace-and-connection-limits"></a>
<a id="replace-or-restore-an-environment"></a>
<a id="cancel-remote-work"></a>
<a id="manage-the-executor-lifetime"></a>
<a id="network-and-local-socket-access"></a>
<a id="platform-requirements"></a>
<a id="domain-and-local-access-rules"></a>
<a id="unix-sockets-in-an-offline-job"></a>
<a id="docker-creation-and-restore-checks"></a>
<a id="windows-named-pipes-in-an-offline-job"></a>
<a id="executor-policies"></a>
<a id="transform-executor-arguments-and-results"></a>

## Run commands and edit files

Choose a local or remote executor, manage its workspace, and apply network and execution policies. See [run commands and edit files](execution.md).

<a id="native-children"></a>
<a id="configure-roles-and-delegation-limits"></a>
<a id="shared-tools-and-permissions"></a>
<a id="observe-child-results"></a>
<a id="start-steer-and-cancel-child-work"></a>
<a id="close-update-and-restore"></a>
<a id="native-tool-decisions"></a>

## Delegate work to native children

Configure child roles, observe and steer child turns, and control collaboration calls. See [delegate work to native children](delegation.md).

<a id="manual-tool-results"></a>
<a id="reply-to-live-manual-requests"></a>
<a id="expire-pending-requests"></a>
<a id="answer-interactions-during-a-one-shot-run"></a>
<a id="handle-native-approvals-and-forms"></a>
<a id="form-schemas-and-handler-lifetime"></a>
<a id="ask-for-application-approval"></a>
<a id="continue-a-one-shot-manual-request"></a>
<a id="experimental-job-recovery"></a>
<a id="declare-and-register-a-recoverable-tool"></a>
<a id="prepare-restore-and-advance"></a>
<a id="recovery-limits-and-duplicate-prevention"></a>
<a id="optional-mcp-tasks-adapter"></a>
<a id="configure-the-remote-tool"></a>
<a id="admit-pending-requests-and-results"></a>
<a id="reply-to-forms-or-cancel-a-task"></a>
<a id="callback-timeouts-and-context"></a>

## Approvals and manual tool results

Supply typed results, answer native requests, and recover application-owned external jobs. See [approvals and manual tool results](approvals.md).

<a id="automatic-checkpoint-limits"></a>
<a id="resume-a-saved-manual-request"></a>

## Manual-request checkpoints

Check capture eligibility and resume pending manual requests. See [manual-request checkpoints](state.md).
