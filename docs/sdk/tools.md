# Tools

Add the tools an agent can use to its `tools` list. Codex checks tool calls before dispatch and runs the model loop. Python tools run application code with the application's permissions. For native MCP tools, Codex manages the connection and sends calls to the selected server. The server runs each operation in its own process or service.

Examples with `await` or `async with` are fragments for an async function. Configure the native provider and its credentials before opening a session; see [Configure an agent](configuration.md).

## Choose a tool

| What you need | Start here |
| --- | --- |
| Call a Python function | [Python functions](#python-functions) |
| Search the web through the native provider | [Native web search](#native-web-search) |
| Give a function application data | [Application context](#application-context) |
| Connect tools from an MCP server | [Native MCP](#native-mcp) |
| Run remote processes or file operations | [Remote execution](#remote-execution) |
| Delegate work to child agents | [Native children](#native-children) |
| Supply a tool result from your application | [Manual tool results](#manual-tool-results) |
| Recover an external job after a host restart | [Experimental job recovery](#experimental-job-recovery) |

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

Keep the original `search` declaration in every explicit turn or step selection, such as `tools=[search]`. Turns, steps, and idle updates cannot add search or change its mode or route. Open a new Session to change the declaration.

Saved history retains search results but does not enable search. To search after restore, declare `WebSearch` again in the current Agent or opening Session tools. `Environment.network` controls executor commands, not web search.

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

Each model argument must have a type annotation and accept a keyword argument. Positional-only parameters, `*args` and `**kwargs` are unsupported. If an existing function uses them, wrap it in a tool function with named, annotated parameters.

A return annotation is optional. On this branch, the SDK validates the returned value against that annotation when present. If a synchronous function returns an `Awaitable[T]` or `Coroutine[Any, Any, T]`, the SDK awaits it and validates the resolved value as `T`. Supported result formats and safety checks still apply without a return annotation.

Parameter names cannot start with `_`. For a JSON argument such as `_id`, use `record_id: Annotated[str, Field(alias="_id")]` with `Annotated` from `typing` and `Field` from `pydantic`.

Python preserves `Annotated` metadata and validates the full declaration before calling the function. Codex includes only the JSON Schema features that the pinned runtime supports in model requests. Python enforces constraints such as `minLength` even when the provider does not receive them. Do not rely on the provider to enforce every Python constraint. On every supported platform, Python rejects invalid arguments before the tool body runs.

### Execution and cancellation

Codex can dispatch a valid tool call while the model response is still streaming. A later stream error or content filter does not undo an effect that has already completed. Codex retains accepted results when it continues the conversation.

The SDK converts Python return values before the native runtime accepts the reply.

The decorator accepts synchronous and asynchronous functions. The SDK runs synchronous functions in a worker with copied context variables. The application context remains the same object. On cancellation, the SDK waits for a running worker before the submission finishes. Use async functions when the body needs cooperative cancellation.

Set `before` and `after` directly on `@function_tool(...)` to control Python callback tools. `before(context)` receives the validated arguments and returns `Allow`, `Deny`, or `ReplaceArguments`. After execution, `after(context, output)` receives and returns a `ToolOutput`. It cannot undo the tool's effects. Set the function body's deadline with `callback_timeout`. See [callback deadlines](#callback-deadlines). A `FunctionTool` without a callback cannot use callback or policy settings.

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

Use `ToolOutput.text(...)` for explicit text, `ToolOutput.image(...)` for an inline image, and `ToolOutput.error(...)` for a failed tool reply. Combine `ToolOutputText` and `ToolOutputImage` items for mixed content:

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

Annotate functions that return these objects with `ToolOutput`, as above. Images require inline base64 image data URLs. Remote URLs, file IDs, and local paths are unsupported.

`ToolOutput.error(...)` returns a failed tool reply that the model can handle. Raising an exception can instead fail the owning turn after cleanup. Neither choice undoes earlier effects. Manual tools can declare `output_type=ToolOutput` and submit these values through a live request.

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

For context together with structured output, see the [complete typed-context example](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/typed_context.py).

## Native MCP

Use `HttpMCP` for Streamable HTTP and `StdioMCP` for a server process. Both declarations select the server's raw tool names. For this HTTP example, set `DOCS_MCP_URL` and `DOCS_MCP_TOKEN` for your server, which must provide `search`:

```python
import os

from agents import Agent, HttpMCP

agent = Agent(
    instructions="Answer using the documentation search tool.",
    tools=[
        HttpMCP(
            name="docs",
            url=os.environ["DOCS_MCP_URL"],
            tools=("search",),
            bearer_token=os.environ["DOCS_MCP_TOKEN"],
        )
    ],
)
```

Read the token in application code and pass its value as `bearer_token`. The declaration keeps that value. Restore uses the application's current declarations and credentials.

### Handle MCP startup errors

A fresh or idle-restored Session captures MCP declarations when it opens. The SDK checks that the server provides selected tools at these points:

- For a turn selection, handle startup or missing-tool errors from `turn.get_result()`.
- For an idle selection, handle those errors from `session.update()`.
- When restoring a pending manual request, the SDK prepares selected MCP resources before exposing fresh requests. Errors can prevent the Session from opening.

Codex normalizes the tool namespaces the model sees. Conflicting names fail before the model can use the tools.

For lists that mix context-aware Python tools and MCP declarations, MyPy may need an explicit `list[Tool[Application]]` or `Agent[Application, Answer]` annotation.

### Configure MCP approval

MCP selection and approval are separate. The default `approval_mode="auto"` keeps Codex's native approval rules, which can require interaction when a tool lacks annotations or can make changes. Answer supported live requests through `session.interactions()` or a one-shot handler. A run without a handler can raise `InteractionRequired` when a native decision is needed. A successful read-only tool does not prove that a write tool can run without interaction.

To explicitly approve calls to selected tools, use `approval_mode="approve"`:

```python
writer = HttpMCP(
    name="records",
    url="https://mcp.example.com/mcp",
    tools=("write_record",),
    approval_mode="approve",
)
```

Codex applies that setting only to the selected raw tool names. Native constraints and strict review still apply. The setting does not answer a server's own requests for interactive input, sandbox server startup, or limit effects the server does not disclose. Result policies still run after execution and cannot undo those effects. Do not change a tool's read-only annotation to obtain approval.

Approval settings follow the current descriptor during idle replacement and restore. Restoring with the default `auto` does not inherit an explicit approval from the saved Session.

### Start a stdio server

For a trusted stdio server, use `StdioMCP` and supply its command:

```python
from agents import StdioMCP

server = StdioMCP(
    name="docs",
    command="/opt/docs-server/bin/server",
    args=("--read-only",),
    env_vars=("DOCS_API_KEY",),
    cwd="/srv/docs",
    tools=("search",),
)
```

By default, `location="host"` starts the server on the application host. Set `location="environment"` to start it on the remote executor passed to `agent.session(environment=...)`. A connected Environment can host stdio MCP servers even when `Environment.tools` is empty.

Codex manages the process and its standard input and output connection until the Session closes. Supply a trusted command because the process uses the selected machine's permissions. Tool selection and approval do not sandbox server startup. Codex passes arguments as separate values without shell expansion.

Configure the server's process environment as follows:

- Use an absolute `cwd`. If omitted, host servers use Codex's runtime directory and remote servers use the Environment's working directory.
- Use `env_vars` to select variables from the machine that runs the server. Codex reads their values at startup.
- Pass arguments as separate values. The declaration captures the arguments and variable names.

Restore uses the application's current server declarations and Environment.

## Idle replacement

While idle, call `await session.update(tools=new_tools)` to replace tool definitions. Later turns use the updated definitions, including replacement MCP endpoints and credentials.

Replacements must stay within the tools declared when the Session opened:

- Python and manual tools must use opening tool names.
- MCP declarations must use opening server and raw-tool pairs.
- Each MCP server must retain its opening `defer_loading` value.

To add names or MCP pairs, or change discovery mode, open a new Session. Await a successful update before submitting work. If an update fails without confirming which definitions are active, close the Session and open a new one.

### Change tools between turns

For a conversation that uses different tools at each stage, declare all the tools when opening the Session. Select a smaller set before the first model turn:

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

### MCP startup and restore limits

Selecting fewer tools before the first turn can leave omitted MCP servers unstarted. A later explicit selection can still use them. To exclude a tool for the whole Session, leave it out when opening.

For an idle checkpoint restore, supply matching saved Python and manual definitions. MCP declarations come from the opening `tools=` override, or otherwise `Agent.tools`. Storage does not supply credentials or callbacks. See [save and restore](sessions.md#compact-save-and-restore).

If the Session opened with `NativeAgents`, retain that declaration in every explicit selection. Update tools only after all native children close. See [Session updates](sessions.md#update-tools-instructions-and-model-settings).

To use host stdio after replacement, open with at least one `StdioMCP(location="host")` declaration. A Session opened with only HTTP or environment stdio cannot later start a host process. Environment stdio requires the Session's current Environment. Both locations remain limited to opening server and tool-name pairs.

With the pinned native revision, host stdio initialization rejects incompatible `CODEX_EXEC_SERVER_NOISE_*` settings from the environment. It does not silently select the remote executor those settings describe. HTTP-only Sessions do not require a local executor.

### Select one MCP tool

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

To replace an MCP declaration rather than select its tools, use an [idle update](#idle-replacement). Passing a new declaration to `submit(tools=...)` does not replace the accepted definition.

### Schema discovery

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

## Callback deadlines

Set `@function_tool(callback_timeout=2.0)` to limit the callback body, including time spent waiting for synchronous execution. The SDK withholds late results even if the callback suppresses cancellation. It waits for callback cleanup before reporting completion. A running application function can delay cleanup, and the deadline cannot undo earlier effects. The deadline excludes `before`, `after`, argument validation, and result conversion.

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

### MCP result policies

Set `before`, `approval` and `transform_result` directly on `HttpMCP` or `StdioMCP`. `before(context, call)` returns `Allow`, `ReplaceArguments` or `StopTurn`. MCP `approval` sees the effective arguments after `before`. This differs from executor approval, which sees the original arguments first. `before` has its own 30-second deadline; `transform_result_timeout` bounds the result callback, not approval.

The `transform_result` callback receives `MCPResultContext` and a native MCP `CallToolResult` dictionary. It returns a dictionary directly or through an awaitable. The dictionary uses `content`, `structuredContent`, `isError`, and `_meta`.

`transform_result_timeout=None`, including omission, uses a 30-second result-policy deadline. An explicit timeout must be greater than zero and at most 3,600 seconds, and requires `transform_result`. This deadline is separate from server execution and approval. Unlike Python `callback_timeout=None`, `None` does not disable the MCP result-policy deadline.

The following declaration logs call identity and returns the whole result unchanged, preserving its content, structured data, error flag and metadata. Set `DOCS_MCP_URL` and `DOCS_MCP_TOKEN` for your Streamable HTTP server, which must provide a `search` tool. Include `server` in the Agent's `tools` list.

```python
import os
from typing import Any

from agents import HttpMCP, MCPResultContext


def log_mcp_result(ctx: MCPResultContext[None], result: dict[str, Any]) -> dict[str, Any]:
    print(ctx.server_name, ctx.tool_name, ctx.call_id)
    return result


server = HttpMCP(
    name="docs",
    url=os.environ["DOCS_MCP_URL"],
    tools=("search",),
    bearer_token=os.environ["DOCS_MCP_TOKEN"],
    transform_result=log_mcp_result,
    transform_result_timeout=30,
)
```

MCP result transforms run after server execution and before native events, history, the model, or code cells receive the result. They cover direct calls, programmatic calls, and declared native children. Child callbacks receive the child's call identity.

Invalid, late, or failed decisions withhold the original result and fail the submission. A transform cannot turn a server error into success, undo server effects, or reveal unreported effects.

Checkpoints store no transform callbacks. Restore uses the current Agent's declarations and credentials.

## Tool notifications

Read `ToolStarted` and `ToolFinished` records from `session.events()` to observe Python and MCP tool activity. Import these types from `agents.observation`. They include call identity and event sequence, without tool arguments or results. `ToolFinished` also reports completion status.

A Python start record means the SDK accepted a function call. An MCP start record can precede approval and server execution. Cancellation may leave an MCP call without a matching finish record. A completion status does not prove that earlier effects were rolled back.

Observations do not delay tool execution or completion. The buffer has a fixed limit. If a reader falls behind and the SDK drops older records, the reader can receive `EventGap`. Use [stream observations](sessions.md#stream-observations) to consume events while waiting for a turn. Use the [supported tool controls](#inspect-or-change-tool-values) to validate or change tool values.

## Local execution

Use `Environment.local()` to run selected tools with the SDK's native sandbox on a supported Linux or macOS host. The wheel supplies the local executor helper. Give the executor an existing absolute working directory and select the tools the model may call. Use a workspace your application owns.

On macOS, local execution needs a usable per-user `launchd` domain and OS support for signaling a specific process instance. If either capability is unavailable, the SDK fails before running local commands. You can import the wheel and use model-only or WSS sessions on supported hosts that lack either capability.

```python
from pathlib import Path

from agents import Agent, Environment

workspace = Path("/jobs/worker/work")
environment = Environment.local(
    cwd=workspace,
    tools=("exec_command", "apply_patch"),
)
agent = Agent(instructions="Work in the assigned workspace.")
async with agent.session(environment=environment) as session:
    turn = await session.submit("List the files in the workspace.")
    print((await turn.get_result()).final_output)
```

Create the workspace before opening the Session. The SDK checks that the executor supports the required sandbox. It refuses startup when the helper or required sandbox support is unavailable. The sandbox governs executor tools. Your application owns the permissions of Python callbacks, the model connection, and any MCP services it supplies.

Local commands can read the workspace, any additional `writable_roots`, and the platform files they need to run. The SDK starts them with a minimal environment and a system `PATH`. Command output can reach the model provider. For untrusted prompts or files, run each job in a job-private OS sandbox or VM. Keep host credentials and other jobs' data outside that boundary.

Install the wheel and native helpers outside the workspace and any additional write roots. Local startup and write-root changes reject paths that overlap the helper installation. If you keep `.venv` in a project checkout, give the job a separate workspace.

### Replace write roots between turns

Writes to host files are limited to `cwd` and the additional `writable_roots`. On Linux, a command can also write to a private temporary filesystem. Those writes disappear when the sandbox exits. The runtime resolves the paths on the executor. It keeps its protections for native metadata. For a linked Git worktree that needs to update Git state, explicitly grant the required Git directories after checking which paths they expose.

Pass `writable_roots` when constructing the local Environment or a compatible Linux or macOS WSS Environment. Passing it to the WSS constructor opts into the same strict write policy. The policy disables native sandbox escalation and permission requests that could widen the grants.

Use a supported local executor or Linux WSS executor to change write grants between turns. With macOS WSS, choose the write roots in the Environment when opening the Session. The SDK cannot confirm that processes from earlier remote commands have exited. You can resubmit an equivalent grant if the paths pass validation.

In a Session without `NativeAgents`, wait for the active turn and any descendant or background executor work to settle. Collect the results of any yielded tool cells before changing the roots. Then supply the complete replacement list on the next root turn. If an active root uses a symlink, the path must still resolve to the same directory when you submit it again. When adding a directory to the current root list, pass the canonical executor path from your application's configuration. Use the same rule when restoring a grant you removed. If the Session uses `NativeAgents`, set its write roots when opening it.

```python
# After earlier work settles.
turn = await session.submit(
    "Write the report in the assigned output directory.",
    writable_roots=[Path("/jobs/worker/output")],
)
await turn.get_result()
```

The output directory must already exist on the executor. The working directory stays writable. On executors that support replacement, an empty list clears the additional grants. Omit `writable_roots` to keep the last applied policy. Start a new conversation to change the [network policy](#network-and-local-socket-access). See [restore executor policy](sessions.md#restore-executor-policy) for when the current Environment must match the last applied policy.

## Remote execution

Remote execution requires an application-owned service that implements the Codex exec-server protocol. Use a version compatible with the SDK's pinned Codex runtime. This guide assumes that service is already deployed and has an authenticated WSS endpoint.

Pass `Environment` to a Session or one-shot run to connect to that service. The Environment configures the Session and does not belong in the Agent's `tools` list. Set `EXECUTOR_URL` to the service's WSS endpoint and `EXECUTOR_TOKEN` to its current credential:

```python
import os

from agents import Agent, Environment


environment = Environment(
    url=os.environ["EXECUTOR_URL"],
    cwd="/workspace",
    headers={"Authorization": f"Bearer {os.environ['EXECUTOR_TOKEN']}"},
    tools=("exec_command", "write_stdin", "apply_patch"),
)
agent = Agent(instructions="Inspect the assigned workspace.")
async with agent.session(environment=environment) as session:
    turn = await session.submit("List the workspace files.")
    result = await turn.get_result()
```

`agent.run(..., environment=environment)` and `agent.run_sync(..., environment=environment)` accept the same connection data. `Environment.tools` defaults to an empty tuple, so process and file tools must be selected explicitly. If the WSS connection fails, the SDK reports a startup cause, such as a certificate error, HTTP status, or timeout. To run tools on the application host, select [local execution](#local-execution) explicitly.

### Select executor tools

Set `cwd` to an absolute executor path or `file:` URI. The selected native model and runtime must support the tools:

- `exec_command` starts commands.
- `write_stdin` polls commands or writes to a TTY. It requires `exec_command`; ordinary pipe-mode stdin is unsupported.
- `apply_patch` edits files.

There is no separate Python process or file client.

### Argument validation

Before dispatch, the SDK checks `exec_command` and `write_stdin` arguments against their pinned object schemas. It rejects malformed JSON, missing fields, unknown keys, wrong types, and invalid enum values. Omitting an optional field differs from passing `null`.

A rejection fails the turn without automatic retry. Earlier calls may have effects or be cancelled; later calls are not admitted. The rejected call does not enter accepted history. Its error names the tool and includes a SHA-256 digest of the call ID to match an application-owned provider record.

These checks cover the two pinned schemas, not arbitrary JSON Schema or the freeform `apply_patch` grammar. They do not guarantee rollback.

The schemas currently advertise `number` for numeric fields. Codex applies narrower integer and range requirements, with unsigned values for time and token budgets. A schema-valid number can therefore pass admission and still fail native parsing. The SDK passes accepted arguments unchanged; it does not round values or insert defaults.

### Workspace and connection limits

Selected tools can modify the assigned remote workspace. By default the SDK requests writes in `cwd`, protects native metadata, and requests restricted networking. Supply [additional write roots](#replace-write-roots-between-turns) through application code. A model's `workdir` argument only sets a command's working directory.

The application must isolate workspaces and tenants and choose an executor that enforces the required OS limits. Selecting tools does not approve permission escalation. `Environment` has no `sandbox_mode` option.

Configure the connection with current application-owned credentials:

- Supply credentials in `headers`. The declaration's representation omits header values and the URL.
- Use WSS with the native system trust store. TLS verification cannot be bypassed.
- Set `connect_timeout` to positive finite seconds, or `None` for the native default.
- Omit userinfo, query parameters, and fragments from URLs.

### Replace or restore an Environment

A Session captures one Environment. Tool replacement cannot change the Environment, and it does not belong in `update(tools=...)`. To change the connection or selected executor tools, open a new Session. The same Agent can serve Sessions with different Environments. Restore uses the current Environment you supply. Check the [executor policy matching rules](sessions.md#restore-executor-policy) for your type of restore. Supply current credentials and start new processes through the current Session.

Python, manual, HTTP MCP and stdio MCP tools can coexist with an Environment. A `StdioMCP` server runs on the host by default, or on the executor with `location="environment"`.

### Cancel remote work

On cancellation, the SDK waits for native work to settle and its callbacks to finish. Closing also asks the native owner to terminate known background processes.

An acknowledgment confirms only that the executor accepted the termination attempt. It does not prove process exit, complete output collection, or rollback. Rejected or timed-out termination attempts raise an error while the Session closes.

A lost process-start acknowledgment can leave earlier effects and pending cleanup. Do not retry a consequential command merely because its result is missing.

### Manage the executor lifetime

Closing the Session closes the SDK's connection. Your application still owns the executor service and workspace.

To manage provisioning and release, pass an async context manager that yields an `Environment`. The SDK enters it when opening the Session and exits after releasing native resources, including after opening fails or is cancelled. The manager can then release the executor.

If opening fails and automatic cleanup cannot finish, follow the [provider recovery procedure](configuration.md#recover-a-failed-managed-environment-opening).

This fragment uses the preceding `agent` and requires application functions `acquire_executor()` and `release_executor(job)`. The acquired job supplies its authenticated WSS `url`, assigned absolute `cwd` and current `headers`. The application implements provisioning, release and rollback if acquisition fails:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agents import Environment


@asynccontextmanager
async def executor_lease() -> AsyncIterator[Environment]:
    job = await acquire_executor()
    try:
        yield Environment(
            url=job.url,
            cwd=job.cwd,
            headers=job.headers,
            tools=("exec_command", "write_stdin", "apply_patch"),
        )
    finally:
        await release_executor(job)


async with agent.session(environment=executor_lease()) as session:
    turn = await session.submit("Inspect the assigned workspace.")
    result = await turn.get_result()
```

For each opening, including restore:

1. Create a fresh manager. To restore, pass `environment=executor_lease()` with `state=saved_state`.
2. Supply current credentials and a policy matching any saved network-policy digest.
3. Retain the workspace separately if later work needs its files. Checkpoints do not recreate files.

The SDK enters and exits the manager in one lease task. `ContextVar` changes stay in that task, so pass application data through `context=`. The manager cannot suppress exceptions from the separate Session body.

Close the Session on its opening event loop and handle release failures from Session exit. The SDK keeps the lease until native resource release finishes. Session exit does not guarantee successful backend shutdown.

### Network and local socket access

Pass a `NetworkPolicy` as `network` when constructing a local or WSS Environment. It controls network destinations and local socket access for native commands. This policy applies to the selected executor. The model connection, Python functions, and MCP clients use their own connections. Omitting `network` preserves the default executor behavior.

#### Platform requirements

Explicit policies have platform limits:

- On macOS, `NetworkPolicy()` grants no network destinations or Unix sockets.
- Linux cannot enforce Unix-socket denial. The SDK rejects an explicit policy before commands start unless `allow_all_unix_sockets=True`, even with domain rules.
- Windows rejects every explicit network policy in this revision. Its default named-pipe path remains available.

#### Domain and local access rules

The policy accepts `allowed_domains` and `denied_domains`. A denial takes precedence over an allowance. On a macOS executor, `NetworkPolicy(allowed_domains=("api.example.com",))` permits that host through the managed proxy without granting Unix socket access. Rules cover hosts across all ports. Supply hostnames or supported host patterns without a scheme or port. The proxy checks hostnames and cannot inspect the contents of encrypted traffic. The policy never permits an upstream proxy or an unsandboxed fallback.

On Linux and macOS executors, `allow_all_unix_sockets=True` grants access to every reachable Unix socket. Use this grant only on an executor private to the job. Keep every socket the job does not need out of reach. The SDK rejects grants for individual socket paths.

`allow_local_binding=True` allows local listeners where supported and adds loopback and private IPv4 ranges to proxy bypass rules. Domain rules apply only to traffic through the managed proxy.

On macOS, local binding can also allow direct traffic to any reachable IP address on port 53, including non-DNS traffic. To enforce stricter limits, the application needs network isolation outside the executor.

Leave `allow_local_binding=False` when the job only needs Unix sockets. An empty domain list does not cancel explicit local-access or Unix-socket grants.

The SDK captures the policy when creating the Environment and checks the reported executor OS and managed-proxy support before commands run. Unsupported settings fail rather than grant broader access.

Every explicit network policy disables sandbox escalation while retaining native workspace limits and command approval callbacks. Calls with `sandbox_permissions="require_escalated"` fail before starting, even if the Session opened successfully.

#### Unix sockets in an offline job

This example grants Unix socket access on a private Linux or macOS executor. Configure the executor so the job can reach only its startup socket. The policy leaves the network destination list empty.

```python
import os

from agents import Environment, NetworkPolicy

environment = Environment(
    url=os.environ["EXECUTOR_URL"],
    cwd="/workspace",
    headers={"Authorization": "Bearer " + os.environ["EXECUTOR_TOKEN"]},
    tools=["exec_command"],
    network=NetworkPolicy(allow_all_unix_sockets=True),
)
```

Pass this environment to `agent.session(environment=environment)` or `agent.run(..., environment=environment)`. The command can bind, listen and connect to **every Unix socket the job can reach**. On Linux this includes abstract sockets, which have no filesystem path. The grant excludes VSOCK sockets. Native workspace write limits still apply. A reachable service can have more access than the command. Even a read-only socket mount allows requests.

The backend must enforce the offline boundary:

- Isolate commands, exec-server, managed proxy, and DNS resolver together.
- Exclude host and other jobs' service sockets from mounts and the socket namespace.
- Close inherited connections before starting the executor.

An empty destination list does not prevent proxy hostname lookups before denial. Linux processes can bind local IP sockets inside their isolated namespace. On macOS, the all-sockets grant lets the proxy forward HTTP to any reachable Unix socket. Use an independently isolated macOS VM or equivalent backend so Codex can apply its command sandbox once. A second outer Seatbelt profile is unsupported.

The SDK cannot infer an offline boundary from a URL. The backend must verify it.

Supply the same normalized network policy when restoring a checkpoint or reopening a local conversation with a recorded policy. Your application grants access through its current Environment. The SDK compares the current policy with the saved policy. Start a new conversation to change the network policy.

Python callbacks can run fixed operations directly in the application's worker. See [Restore an Environment](sessions.md#compact-save-and-restore).

#### Docker creation and restore checks

The reusable `examples/offline_docker.py` checks Linux Docker networking at creation and attachment:

1. `create_offline_job` starts a private container with `--network none`.
2. On every authenticated gateway attachment, use `open_exec_stdio`, including for restore. It inspects the same full container ID passed to `docker exec -i`.
3. Before starting exec-server, the check rejects a wrong network mode, external attached network, missing inspection data, or a stopped container.

The application owns the Docker daemon, image, container ID and authenticated gateway route. Keep that job record outside conversation checkpoints. The gateway stays outside the offline container and sends the exec-server protocol through standard input and output; the example publishes no container port. Closing a gateway attachment does not end the job. Keep the container for restore and remove it when the job finishes.

The image and host must support the native executor and its Linux sandbox. Docker's network setting alone does not establish that support. The example's network checks do not attest an arbitrary WSS URL or prevent a Docker administrator from changing the job later. Native macOS executors and Windows named pipes use their separate platform paths; a Docker Linux guest uses the Linux path regardless of the controller's OS.

### Windows named pipes in an offline job

For a job-local named pipe on Windows x64:

1. Select `tools=["exec_command"]` and omit `network`. Unix socket grants do not apply.
2. Have the provisioner resolve the workspace on the Windows executor and pass its canonical path as `Environment.cwd`.
3. Give the executor account read and write access to its private workspace. The native command sandbox still applies.

The Python controller cannot resolve remote paths locally. The executor handshake reports its own working directory, which can differ from the job's workspace.

The pipe must allow the restricted job process to connect, read and write. A server that creates additional pipe instances needs permission to create them (`FILE_CREATE_PIPE_INSTANCE`).

For example, a Win32 listener can create a single pipe instance with `PIPE_REJECT_REMOTE_CLIENTS` and grant read and write access only to the current logon SID.

Your backend must keep the command, executor and any proxy and DNS resolver inside the same offline, job-private VM or equivalent environment. Keep host and other jobs' service pipes out of reach. Close inherited handles and connections that could reach outside the job. The SDK does not create or verify this isolation.

### Executor policies

Use `Environment.approval(context, arguments)` to let the application decide whether an executor call can run. Return a bool or `StopTurn`, directly or through an awaitable. The callback receives the original arguments before transformation. It has no transform deadline and may wait for a person.

If approval denies the call, returns `StopTurn`, raises an error, or is cancelled, the SDK does not invoke `transform_arguments`. Approval cannot grant extra host or native permissions. It controls selected executor calls, not MCP server startup.

The effect of a denial depends on the tool and how the model calls it:

| Decision | Effect |
| --- | --- |
| `Environment.approval` returns `False` for a direct call | Denies the call. The owning turn fails with `NativeError`; the model does not continue that turn. |
| `Environment.approval` returns `False` for a call from generated code | Stops and joins the owning turn before subsequent generated-code operations can run. Generated code cannot catch the denial to continue execution. |
| MCP `approval` returns `False` for a direct call | Denies the call. The owning turn fails with `NativeError`. |
| MCP `approval` returns `False` for a call from generated code | Rejects that nested MCP call. Generated code can catch the rejection and continue; this denial does not itself stop the owning turn. |
| `StopTurn(...)` from executor approval or an argument transform, or from MCP `before` or `approval` | Stops and joins the exact owning root or child turn. |

After a direct root-call denial, submit a new turn to continue.

Import `StopTurn` and `TurnStoppedError` from `agents`. Once a stopped root's work and cleanup finish, `await turn.get_result()` raises `TurnStoppedError("Turn stopped by policy")`. A captured child Turn that was stopped can report `NativeError` instead. Stopping one child does not stop the root, other children, or later turns. The stopped call does not run, but earlier effects remain.

#### Transform executor arguments and results

`transform_arguments(context, value)` accepts or transforms arguments before direct or programmatic calls. It can return raw arguments, `Allow`, `ReplaceArguments` or `StopTurn`. A raw dictionary with an `action` key stays argument data; only SDK decision objects control the call. `transform_result(context, value)` transforms the executor result before the model or generated code receives it. Use it for direct executor calls in the default mode and for calls from generated code with `Agent(programmatic_tool_calling=True)`.

Both transforms can be synchronous or asynchronous. `ExecutorContext` supplies application context, the owning Python Session and Agent, and root or child call identity:

- `agent_role` and `model` identify the native child role and executing model when Codex supplies them. Root calls have no child role, and child calls do not create Python Agents.
- Direct calls set `call_id`.
- Programmatic calls set `outer_call_id`, `cell_id`, and `runtime_tool_call_id`.

Callbacks cannot replace these identities.

An executor approval or transform callback cannot observe its own Session, submit work to it, update it, interrupt it, checkpoint it, or close it. Start any event observer in an application task outside the callback. Use application context for data needed to make a decision.

Arguments for `exec_command` and `write_stdin` are JSON objects; `apply_patch` receives a patch string. Native validation still applies after transformation. A result transform runs after the remote effect and cannot undo it. `policy_timeout` bounds transform decisions in seconds and defaults to 30. The SDK rejects invalid, late, or failed decisions. The callbacks cover root and native child calls.

See [programmatic tool calling](configuration.md#programmatic-tool-calling). Saved configuration contains no policy callbacks, credentials, endpoints or network-policy names. The [network-policy support](#network-and-local-socket-access) also compares the current Environment policy with the recorded digest on restore. This does not redact command history or native debug logs.

## Native children

Add `NativeAgents()` to `Agent.tools` or opening `agent.session(tools=[...])`. Supply an Environment with `exec_command`, `write_stdin`, and `apply_patch`. Children share its connection and workspace policy, plus the parent's provider and credentials.

```python
from agents import Agent, NativeAgents

agent = Agent(
    model="gpt-5.5",
    instructions="Delegate independent workspace tasks to native children.",
    tools=[NativeAgents()],
)
async with agent.session(environment=environment) as session:
    turn = await session.submit("Review the workspace using independent child tasks.")
    result = await turn.get_result()
```

### Configure roles and delegation limits

Built-in `default`, `explorer`, and `worker` roles inherit `Agent.instructions`. For separate developer instructions, import `NativeRole` from `agents` and pass `additional_roles=[NativeRole(name=..., description=..., instructions=...)]`. Additional roles cannot replace built-in roles. Full-history forks inherit the parent's role. To select another role, use `fork_turns="none"`.

Children inherit the parent's model and reasoning effort unless a role supplies compatible settings. Model selection has these limits:

- Native children require an explicit model that supports all three native executor tools. The SDK validates each declared model and reasoning effort before connecting to the executor.
- A role can set `model` and `model_settings={"reasoning_effort": ...}`. Other role model settings are unsupported.
- If the Agent enables programmatic tool calling, each selected role model must support it. Some models can run as children with programmatic tool calling disabled but cannot delegate further.
- Unknown or incompatible model metadata fails without fallback.

Set delegation limits on `NativeAgents`:

- `max_depth=1` (default) permits direct children only. Larger positive values allow further delegation but do not add model capabilities.
- `max_children=None` uses Codex's native limit. A positive value limits concurrent children across the tree, not the total created.

### Shared tools and permissions

The root receives `spawn_agent`, `wait_agent`, `send_message`, `followup_task`, `interrupt_agent`, and `list_agents` in the native `collaboration` namespace. There is no programmatic child-creation method.

Children share selected executor, Python, manual, HTTP MCP, and local stdio MCP tools, application context, and policies. Before dispatch, the runtime rejects unknown roles, model-selected override arguments, and unselected calls. Executor permissions apply at every depth.

By default, `NativeAgents` sets native approval policy to `never` for root and child turns. Codex can return permission denials but cannot ask the application to approve escalation. `Environment.approval` still controls selected executor calls but cannot grant extra native permissions. Without `NativeAgents`, existing native approval behavior remains when `Environment.network` is omitted. An explicit [network policy](#network-and-local-socket-access) disables escalation.

Set `NativeAgents(interactions=True)` to receive supported child executor approvals, MCP forms and MCP URL requests through `session.interactions()` or `on_interaction`. Requests retain the originating child's identity. HTTP forms must come from a request associated with that child. Background forms without that association remain unsupported. This option does not enable the child's `request_user_input` or `request_permissions` prompts, or root escalation.

With this option, each child turn starts its own stdio MCP connections and closes them when that turn ends. Server process state and subscriptions do not survive between child turns. Unowned server-startup prompts fail child creation.

The root can use structured `output_type`; children return text. Answer manual calls through `on_interaction` or `session.interactions()`. Pending child manual requests cannot be checkpointed.

### Observe child results

Read descendants with `async for child in session.subagents()`. Each immutable `Subagent` snapshot contains identity, `status`, and the current or last observed `turn`. The turn is `None` until observed.

```python
async for child in session.subagents():
    if child.turn is not None:
        result = await child.turn.get_result()
        print(child.id, result.final_output)
```

An `active` child can be idle or unloaded. `is_live` reports whether the snapshot captured a loaded active instance. Close or reload can make a snapshot stale; `start()` checks that the captured instance remains current.

`Turn.get_result()` returns the child's `Completed[str]`, retaining its text and latest reported thread usage after later turns. Missing usage is `None`; root results exclude child usage. Native failures and interruptions raise `NativeError`. Python tool failures can raise the function's exception after cleanup.

A child's callback `submission` is a `ChildInvocation` in `ToolContext`, `MCPResultContext`, and `ExecutorContext`. This identity cannot wait for or cancel work. Wait through `Subagent.turn` in application code. `session.events()` covers root and child work.

### Start, steer, and cancel child work

Use the captured child and turn handles:

- `await child.start(message)` starts work on an idle, live child and returns its `Turn[str]`.
- `await child.turn.steer(message)` steers the observed active turn.
- `await child.try_cancel(child.turn)` cancels the captured child turn. `await session.cancel_all()` cancels the tree. See [interruption](sessions.md#interrupt-work) for return values and cleanup limits.

These methods cannot create children or resume closed or reloaded instances. Reading descendants starts no work. The methods have no pagination arguments. Cancelling a result reader leaves native work running.

During a stop of all work, new child input is rejected but pending manual replies remain allowed. Afterward, validated explicit input to a live child can lift the stop; queued automatic wake-ups cannot.

Late steering may start a follow-up turn after the observed turn finishes. The captured Turn still returns its original result. Follow successors through `session.events()` or a fresh descendant snapshot.

### Close, update, and restore

Root completion does not imply child completion. Closing the Session shuts down the native tree.

Model settings, instructions, and `NativeAgents` remain fixed for the Session. Use [idle tool replacement](#idle-replacement) only after all children close.

Once work and callbacks finish, idle checkpoints can retain child conversations, task history, and older goals and checklists. Restore requires current `NativeAgents` and matching Python and manual definitions, uses the current Agent's MCP declarations, and creates fresh runtime resources. Saved goals and checklists grant no tools. Live child Turns do not transfer, and interrupted child work does not resume automatically. See [save and restore](sessions.md#compact-save-and-restore).

## Manual tool results

Use `FunctionTool` when the application must supply a tool result later. The declaration has no function body. Its `input_type` is a Pydantic model. The SDK validates the result against `output_type` before sending it to the native runtime.

```python
from agents import Agent, FunctionTool
from pydantic import BaseModel


class LookupInput(BaseModel):
    key: str


class LookupOutput(BaseModel):
    label: str


lookup = FunctionTool(
    name="lookup",
    description="Look up a label by key.",
    input_type=LookupInput,
    output_type=LookupOutput,
)
agent = Agent(instructions="Use lookup to find labels.", tools=[lookup])


async def answer(event):
    request = lookup.match(event)
    if request is None:
        raise ValueError("Unexpected manual tool")
    value = LookupOutput(label=f"Label for {request.arguments.key}")
    return request.reply(value)


result = await agent.run("Find the label for record-a.", on_interaction=answer)
```

### Reply to live manual requests

`match()` accepts only requests issued for that exact declaration object. It also preserves the request's input and output types for static checking. Manual calls can wait concurrently. Reply to each request separately. Answering one request does not consume another.

A live manual request is limited to 65,536 bytes of UTF-8 encoded request data, including its arguments and request metadata. An oversized request fails its owning submission with `NativeError` before argument validation or delivery to a handler. Keep manual-tool payloads within this limit.

`request.reply(value)` packages a value for a one-shot handler to return. In an explicit Session, read `session.interactions()` and call `await request.respond(value)`. If validation fails, you can retry the live request. Duplicate replies, replies to expired requests, and replies that do not belong to the waiting call fail. Native acceptance delivers the reply to the original waiting call. It does not confirm a durable history write or start a new user turn.

`session.interactions()` also delivers child requests after the root finishes. `request.subagent_id` identifies the child, or is `None` for root work. Requests from different children have separate authority even if call IDs coincide. Manual tools work in direct mode and programmatic tool calling. Pending child requests and pending code cells require a live Session.

`ActionRequested` contains sensitive typed arguments and lets the application reply to a live request. Avoid logging or storing it. Passive events do not let the application reply. A new interaction cursor receives still-pending requests without replay sequence numbers.

### Expire pending requests

To expire pending root requests, use `await turn.cancel(mode="force")`. To include children and the queue, use `await session.cancel_all(mode="force")`. Session exit also expires pending requests. Cancelling a `turn.get_result()` task or closing an observation cursor leaves them live. See [interrupt work](sessions.md#interrupt-work).

### Answer interactions during a one-shot run

Pass `on_interaction` to `agent.run()` or `agent.run_sync()`. The handler returns the reply created by the exact request it receives, directly or through an awaitable. For a manual function, use the preceding `answer` example. The SDK validates and submits the reply. Do not call `respond()` in a handler and return `None`. That fails, as does a reply for a different, expired or already answered request.

#### Handle native approvals and forms

The handler can receive supported native root requests too. Approval requests use `request.reply("allow_once")` or `request.reply("deny")`. Form requests use `request.reply(values)`; `request.reply(None)` declines a form or URL request. Native acknowledgement means the reply was enqueued, not that an external approval or URL verification succeeded. Native child requests require `NativeAgents(interactions=True)`; manual child requests do not require that option.

Import `ApprovalRequest` and `ElicitationRequest` from `agents` to distinguish native approval requests from form or URL requests:

| Request | Inspect | One-shot reply | Explicit Session reply |
| --- | --- | --- | --- |
| `ApprovalRequest` | `description` | `reply("allow_once")` or `reply("deny")` | `await respond("allow_once")` or `await respond("deny")` |
| `ElicitationRequest` form | `description` and `schema` | `reply(values)` or `reply(None)` to decline | `await respond(values)` or `await decline()` |
| `ElicitationRequest` URL | `description`, `url` and `elicitation_id`; `schema` is empty | `reply_url_accept()` to consent or `reply(None)` to decline | `await accept_url()` to consent or `await decline()` |

This handler requires an asynchronous application function `collect_form_values(description, schema)`. That function must render the supplied fields and return the user's answer mapping, or `None` to decline. Pass `answer_native` as `on_interaction` when the run expects native requests:

```python
from agents import ApprovalRequest, ElicitationRequest


async def answer_native(request):
    if isinstance(request, ApprovalRequest):
        return request.reply("deny")
    if isinstance(request, ElicitationRequest):
        if not request.schema:
            return request.reply(None)
        values = await collect_form_values(request.description, request.schema)
        return request.reply(values)
    raise TypeError("Unexpected interaction")
```

#### Form schemas and handler lifetime

`ElicitationRequest.schema` is a detached schema mapping. The SDK validates form replies against the supported supplied field constraints. The SDK never visits a URL. A Session-scoped URL request has `turn_id=None` and `submission_id=None`, even while a turn is active. Do not infer an originating turn from the Session's current work.

URL consent does not establish completion or verification. In an explicit Session, after consent, `await request.wait_url_completion(timeout=30)` waits for the issuing server to report completion for that URL interaction. The application decides whether to trust that server's report. A timeout expires the interaction and raises `TimeoutError`. Cancelling the waiter stops only that wait. Keep the Session open while waiting. The preceding handler declines URLs by default.

Handlers may run concurrently. Synchronous handlers run in worker threads and must support concurrent calls. When root work finishes, fails, or is cancelled, the run closes its Session and waits for handlers to finish. Python cannot forcibly stop synchronous worker threads. Use an explicit Session if children must continue after the root finishes or if you need to retry an invalid manual result.

Standard MCP forms support primitive fields and enum-backed multi-select arrays, including titled choices; nested objects and general arrays remain unsupported on that route. OpenAI extension forms support Draft 2020-12 object schemas with nested objects, arrays and single-hop references to local `$defs`. Extension `format` values are annotations; regex keywords, resource identifiers and dynamic references are unsupported. The SDK does not fetch schema resources.

Native child interactions require the [explicit opt-in](#shared-tools-and-permissions). Use manual tools for application-owned questions, including supported child manual requests. Live replies do not make code cells, processes or child turns portable.

### Ask for application approval

Use `Environment.approval` for an application decision before selected executor calls. MCP declarations expose `before`, `approval` and `transform_result` alongside native `approval_mode`; they do not accept a per-call `interceptor`. `NativeAgents.approval` controls native collaboration calls. See [executor policies](#executor-policies) and [native tool decisions](#native-tool-decisions).

For questions your application asks, declare a `FunctionTool` with typed question arguments and a typed answer. Its `on_interaction` handler can wait for the user and return a reply created by that exact request. This also works in native children and programmatic tool calling while the run remains active.

### Continue a one-shot manual request

For an ordinary Python/manual run with plain-text Agent output, `agent.run()` and `agent.run_sync()` can start without `on_interaction`. If the model never calls a manual tool, the run completes normally. If it calls one, the SDK waits for a supported settled manual boundary, then raises `InteractionRequired` with a portable checkpoint. Completed tool effects are not replayed on restore.

```python
from agents import InteractionRequired

try:
    completed = await agent.run("Find the label for record-a.")
except InteractionRequired as exc:
    if exc.state is None:
        raise
    completed = await agent.run(None, state=exc.state, on_interaction=answer)
```

This fragment uses `agent` and `answer` from [Manual tool results](#manual-tool-results). Always check `exc.state`.

#### Automatic checkpoint limits

Without `before_step`, selected manual tools without a handler cannot save automatically with any of these features:

- Programmatic tool calling or non-plain Agent output, including `PromptedOutput`.
- Selected MCP tools, local storage, an Environment, or `NativeAgents`.
- A `WebSearch` declaration on the opening Agent.

The SDK rejects these combinations without a checkpoint. It checks visible declarations before opening and may check saved selections or changed Session definitions afterward, before submitting work.

With `before_step`, the SDK opens the Session and checks each step's selected tools. Steps without selected manual tools can complete normally. If the model calls a selected manual tool without a handler, the run raises `InteractionRequired` with `state=None` after opening and inference. Earlier tool effects may already have occurred. Other native questions and late undeclared manual calls do not produce portable checkpoints.

For supported live interactions, use a handler. You can also open an explicit Session and use `suspend()` when the pending manual request supports suspension.

#### Resume a saved manual request

`input=None` resumes the existing turn. Ordinary input cannot replace a pending turn. A pending restore without a handler raises `InteractionRequired` with the supplied checkpoint still in `state`. Tools and `before_step` cannot be changed on an already admitted turn. Restore uses fresh requests for the current declarations; old handles do not transfer. Completed results are not called again.

Idle checkpoints accept ordinary input for a new turn. `return_state=True` saves successful completion in `Completed.state`; it defaults to `False`, leaving `state=None`. Both Agent run methods require a bool and fail if saving fails.

An explicit Session supports more manual suspension cases, including matching `PromptedOutput`. A bare structured Agent output or `NativeOutput` cannot suspend a pending manual turn; finish it in the live Session and save after completion. See [saving a settled manual request](sessions.md#saving-a-settled-manual-request) for output fingerprints, `before_step`, active repair, code cells and child limits.

## Experimental job recovery

`DeferredTools` connects a manual `FunctionTool` to an external job managed by your application. The host can then resume work after its process restarts. The host manages the durable journal, current permissions, provider configuration, and wake-up schedule. This helper does not let arbitrary tool calls survive a restart. Import it from `agents.experimental.deferred`.

### Declare and register a recoverable tool

Declare the tool, then register the exact object used by the Session. Registration returns `None` and rejects decorated callbacks.

Recovery declarations must meet these requirements:

- Input and result types are strict Pydantic models with `extra="forbid"`.
- Supported values are JSON strings, booleans, integers, finite floats, nulls, ordered lists, and nested strict models.
- Inputs can use JSON scalar defaults, `Literal`, and supported unions. Aliases must identify provider fields unambiguously.
- Results require every public field and preserve field and list order. Nullable fields are supported, but defaults, `Literal`, and general unions are not.
- Generated request IDs belong in the original input, never in default factories. Restore must not create new IDs.

Registration rejects custom validators, serializers, default factories, computed fields, custom initialization, and rich types such as `datetime` or `set`. Check custom inputs in host or provider code before external effects, and convert richer results to supported public models. Use live manual replies for custom serialization or scalar results.



```python
from pydantic import BaseModel, ConfigDict

from agents import FunctionTool
from agents.experimental.deferred import DeferredTools


class ReportInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    report_key: str


class ReportResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    row_count: int


report = FunctionTool(
    name="report",
    description="Count the rows in a report.",
    input_type=ReportInput,
    output_type=ReportResult,
)
```

Configure an Agent with `tools=[report]`. The following fragment requires an application implementation of `JobJournal` and a `JobProvider[ReportInput]` that returns a row count:

```python
jobs = DeferredTools(journal=journal, tenant=tenant, run_id=run_id)
jobs.register(
    report,
    provider_id="report.v1",
    provider=provider,
    project=lambda value: ReportResult.model_validate({"row_count": value}),
)
```

The host's `project` function selects fields the model may receive. `max_output_bytes` limits public result JSON to 16,384 bytes by default. Keep provider receipts and private values in the host journal.

### Prepare, restore, and advance

Run recovery from host code outside `on_interaction`:

1. Match a live root request with `report.match(request)`.
2. Call `await jobs.prepare(session, request)`. Preparation suspends the Session and expires live requests without calling the provider. The journal must atomically save the checkpoint and all registered pending bindings before preparation returns. If writing fails or is interrupted, inspect the journal before continuing.
3. Restore the prepared checkpoint with a matching Agent and obtain a fresh request from `restored.interactions()`.
4. Call `await jobs.advance(restored, request)`. If it returns a `Turn`, await `get_result()` for resumed model work. Strings such as `"pending"`, `"input_required"`, and `"unknown"` are host-only statuses.

Keep the original prepared checkpoint while sibling calls remain outstanding. Later restores can deliver public results retained in the journal without restarting jobs.



`advance()` has no background poller:

- `wait_seconds=0.0` (default) starts once without polling. The host schedules later observations.
- A positive wait permits one additional bounded observation after creation.
- `timeout_seconds=10` (default) covers authorization, provider I/O, persistence, and delivery. Cleanup is cooperative. A timeout does not authorize another creation.

For a single call, `async with jobs.start(session, request) as (restored, outcome)` prepares, restores, and advances while managing the restored Session. It uses declarations and resolved model credentials captured at opening and rejects unsupported changes before suspension.

If credentials, definitions, or instructions changed, prepare and restore explicitly with current configuration.

### Recovery limits and duplicate prevention

Prepare a root request before its first restore. Recovery supports plain-text and supported `PromptedOutput` roots. It cannot prepare pending native-schema output, requests using an Environment, native agents, programmatic tool calling, or child requests. Use live replies for those cases. See [saving a settled manual request](sessions.md#saving-a-settled-manual-request).

The journal must let only one worker claim creation and must check current permissions, exact binding, and generation on every operation.

By default, registration requires `provider.reconcile_creation()` and target-side duplicate prevention for an immutable job key. If a creation acknowledgment is lost, reconciliation must establish whether the job started before the host proceeds. The acknowledgment identifies the job, not its final result.

Set `require_reconciliation=False` only for approved, low-impact work that can remain `"unknown"`. An unknown outcome gives no permission to repeat an external effect.

Automatic delivery rejects public results that contain the current model API key, job key, or provider receipt, even as an incidental substring. Do not start the job again after this rejection. The host can review the result and use `await request.respond(safe_value)` on the fresh live request. The host must record that completion itself.

### Optional MCP Tasks adapter

`MCPTasksProvider` is an optional experimental adapter for one host-selected remote MCP tool. It does not change [native MCP](#native-mcp) execution. Install exactly `mcp==2.0.0` and `mcp-types==2.0.0` in the host environment, then import `MCPTasksProvider` and `MCPInputReply` from `agents.experimental.mcp_tasks`. Normal SDK imports do not require these packages.

#### Configure the remote tool

Construct `MCPTasksProvider(endpoint=..., tool=..., authorize=..., admit_pending=..., admit_result=...)` with trusted host configuration. Tool arguments cannot select the endpoint or tool. `authorize(claim, operation)` checks current permissions and returns a bearer token without its prefix for each HTTP request. The adapter refuses redirects. HTTPS uses the host's proxy and CA settings. Construct a fresh provider after changing the host's outbound network settings.

This adapter cannot reconcile lost creation receipts. Register it with `require_reconciliation=False` only for approved, low-impact work that can remain `"unknown"`. Before creation, the adapter reads at most 32 pages of tool declarations. If page 32 still has a continuation cursor, the adapter refuses the listing before sending `tools/call`.

#### Admit pending requests and results

Supply a fast synchronous `admit_pending(claim, task_id, requests_json)` callback. At creation, `requests_json` is `None`. For a form, it holds the complete request mapping as compact JSON with sorted keys.

Return exactly `True` only when trusted data for `claim.binding` confirms the task ID and every form key, prompt, and schema. Treat these values as private even on refusal; a peer can copy earlier credentials into them.

Missing or refused admission leaves new creation `"unknown"` without a receipt. A refused form remains `"unknown"` with its accepted receipt. Neither permits another creation.

Completed updates omit peer text, structured fields, and metadata by default. To admit data, supply a fast synchronous `admit_result(claim, result)` callback returning JSON with finite numbers. The adapter stores that data under `structuredContent` for the projection function and retains the peer's `isError` flag.

Check fields against trusted per-job data when independent verification matters. Restore the admission policy with the job. Verify or quarantine older journal rows because cached results can return without another admission callback.

#### Reply to forms or cancel a task

Form replies require `allow_input_replies=True` and a vetted target that binds each request key to one immutable request for the task's lifetime. The target must reject retired or satisfied keys. Changed requests need new keys and fresh approval. Standard `tasks/update` cannot require a matching revision before applying an update. Supported forms contain flat strings, booleans, numbers and string lists, including choices. Other validation constraints are refused.

Use `advance()` for approved mutations:

- For a form reply, pass `operation="respond"` and `response=MCPInputReply(challenge=..., responses=...)` with host-approved `ElicitResult` values from `mcp_types`.
- For cancellation, pass `operation="cancel"`.

Both operations re-read the task before dispatch, even before its next poll time. Supply `reserve_mutation(claim, reply)` for either operation; cancellation passes `None`.

Immediately before sending, the host must atomically check current permissions, binding, generation, receipt, and approval, then reserve all selected form keys or the task's single cancellation. Return exactly `True` only after a new reservation commits. Keep reservations spent after retries or lost acknowledgments. After a crash, observe the task before deciding what remains unknown.

#### Callback timeouts and context

Initial authorization and each network exchange have separate timeout budgets. Cooperative host callback cleanup can exceed them. Authorization and admission callbacks run on the caller's loop, so keep callbacks short.

Each HTTP request uses a separate copy of the operation's context. Rebinding a `ContextVar` does not affect later requests, but mutable context values remain shared. Reset context tokens in the callback that created them.

## Native tool decisions

Set `NativeAgents.approval` to approve collaboration calls before native dispatch. The callback receives `ToolContext` and `NativeToolCall` and can be synchronous or asynchronous. It covers root and descendant collaboration calls. It does not control Python functions, MCP tools or Environment executor tools.

This declaration denies new child creation while allowing other selected collaboration calls:

```python
from agents import NativeAgents, NativeToolCall, ToolContext


def approve_call(context: ToolContext[None], call: NativeToolCall) -> bool:
    return call.name != "spawn_agent"


children = NativeAgents(approval=approve_call)
```

Return `True` to allow the pending call or `False` to deny it. An exception, a non-boolean result or cancellation denies the call. Approval callbacks cannot replace the call's arguments or result. The call record includes thread, turn and call IDs; its `input` and `result` can contain sensitive data and are omitted from its representation.

During cleanup, the Session waits for approval callbacks that have started. Callbacks must not wait on, interrupt, close, or change their own Session. Use [tool notifications](#tool-notifications) when a passive notification is sufficient.
