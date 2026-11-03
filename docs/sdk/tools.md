# Tools

Put the tools an agent can use in its `tools` list. Codex admits calls and drives the model loop. A Python tool runs application code with the application's permissions. Codex owns native MCP connections and sends tool calls to the selected MCP server. That server executes the operation in its own process or service.

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

Include `WebSearch` in the opening tools to enable native search. Omitting it disables both the standalone and provider-hosted routes. The default `mode="cached"` uses indexed content; `mode="live"` permits Codex to request external web access. The default `route="standalone"` uses the native standalone search protocol. `route="hosted"` uses the provider's hosted web-search protocol. Both routes require a compatible Responses provider; selecting a route does not add provider support or fall back to the other route.

```python
from agents import Agent, WebSearch

search = WebSearch(mode="cached", route="standalone")
agent = Agent(instructions="Research the user's question.", tools=[search])
```

Keep the captured `search` declaration in every explicit turn or step selection, such as `tools=[search]`. A turn, step or idle update cannot add search or change its mode or route. Open a new Session for a different search declaration. Completed search results remain in saved history. Saved history grants no search capability. If restored work needs search, declare `WebSearch` again in the current Agent or opening Session tools. `Environment.network` controls native executor commands, not this separate search capability.

## Python functions

Decorate a function with `@function_tool`. Its annotated parameter types define the tool's input. Codex converts that input into its supported model-visible schema. The docstring describes the function. `name=` and `description=` provide explicit overrides.

```python
from agents import function_tool


@function_tool
async def greeting(name: str) -> str:
    """Return a short greeting for a person."""
    return f"Hello, {name}!"
```

Each model argument must have a type annotation and accept a keyword argument. Positional-only parameters, `*args` and `**kwargs` are unsupported. If an existing function uses them, wrap it in a tool function with named, annotated parameters.

A return annotation is optional. On this branch, a supplied annotation validates the returned value. A synchronous function may return an `Awaitable[T]` or `Coroutine[Any, Any, T]`; the SDK awaits it and validates the resolved value as `T`. Without a return annotation, supported result formats and safety checks still apply.

Parameter names cannot start with `_`. For a JSON argument such as `_id`, use `record_id: Annotated[str, Field(alias="_id")]` with `Annotated` from `typing` and `Field` from `pydantic`.

Python preserves `Annotated` metadata and validates the full declaration before calling the function. Codex constructs every model request using the subset of JSON Schema supported by the pinned runtime. The provider may not see constraints such as `minLength`, even though Python enforces them. Do not rely on provider-side enforcement of every Python constraint. Invalid arguments fail Python validation before the tool body runs; the same rule applies on every supported platform.

Codex can dispatch a valid tool call while the model response is still streaming. A later stream error or content filter does not undo an effect that has already completed. Codex retains accepted results when it continues the conversation.

Return values are converted at the Python boundary before native reply acceptance.

The decorator accepts synchronous and asynchronous functions. Synchronous functions run in an owned worker with copied context variables. The application context remains the same object. Cancellation joins a running worker before the submission settles; use async functions when the body needs cooperative cancellation.


Python callback tools accept flat `before` and `after` fields on `@function_tool(...)`. `before(context)` sees validated effective arguments and returns `Allow`, `Deny` or `ReplaceArguments`. `after(context, output)` receives and returns a `ToolOutput` after execution; it cannot undo the effect. Set the body deadline with `callback_timeout`; see [callback deadlines](#callback-deadlines). A callback-less `FunctionTool` cannot use callback or policy settings.

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

The return value must match a supplied return annotation. Annotate a function that returns these objects with `ToolOutput`, as above. Images require an inline base64 image data URL; remote URLs, file IDs and local paths are unsupported. An explicit `ToolOutput.error(...)` delivers a failed tool reply that the model can handle. Raising an exception can instead fail the owning turn after cleanup. Neither choice undoes earlier effects. Manual tools can also declare `output_type=ToolOutput` and submit these values through a live request.

## Application context

Use `ToolContext[Application]` as the first parameter when a function needs application state. That parameter is injected and excluded from the model-visible schema. Pass the application value when opening or running the agent.

First complete the [current-source setup](index.md#before-you-begin), including model credentials. Save this complete script as `context_agent.py`:

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

Run `.venv/bin/python context_agent.py` on Linux/macOS or `.\.venv\Scripts\python.exe context_agent.py` on Windows. The script prints a greeting for Ada; the model chooses the wording.

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

Read the token in application code and pass its value as `bearer_token`. The descriptor keeps the token value supplied at construction; restore uses the application's current descriptors. A fresh or idle-restored Session captures MCP declarations at opening. The SDK checks an MCP server's native tool roster when a turn or idle update selects its tools. Handle startup or missing-tool errors from `turn.get_result()` if a turn selects them, or from `session.update()` if an idle update selects them. A pending manual restore readies its selected MCP resources before exposing fresh manual requests to the application; those errors can prevent the Session from opening. MCP credentials come from the current configuration. Model-visible namespace normalization follows Codex, so conflicting names fail before those tools become available to the model. Use an explicit `list[Tool[Application]]` or `Agent[Application, Answer]` annotation when MyPy needs it for lists that mix context-aware Python tools with MCP declarations.

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

Codex owns the process and its stdin/stdout connection until the Session closes. Supply a trusted command: the process uses the selected machine's permissions. Tool selection and approval do not sandbox server startup. Arguments are separate values without shell expansion.

Use an absolute `cwd` for predictable resolution. If omitted, host servers use Codex's runtime directory; remote servers use the Environment's working directory. `env_vars` selects variable names from that machine, not from the other machine. The declaration captures argument and variable-name sequences; current values are read when the server starts. Restore uses the application's current server declarations and Environment.

## Idle replacement

Call `await session.update(tools=new_tools)` while idle to replace Python or manual tools with names selected when the Session opened, or MCP declarations for the same opening server/raw-tool pairs. Later turns use the updated definitions. A replacement MCP declaration supplies the current endpoint and credentials. Its `defer_loading` value must match that server's opening value; discovery mode cannot change during the Session. No update can add names or MCP pairs absent at opening.

To use additional tool names or MCP server/tool pairs, open a new Session. Await a successful update before submitting work. If an update fails without confirming which definitions are active, close the Session and open a new one.

For a staged conversation, declare the full set you intend to admit and narrow it before the first model turn:

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

Here `lookup` selects the last accepted binding, `current_lookup`. It does not restore the original callback. Temporary `submit(tools=...)` and `before_step` selections cannot install fresh same-name tools or MCP connections. Use an idle update for those changes. An explicit turn selection may still choose a tool omitted from the current default; it uses the last accepted binding, including its MCP credentials.

In a fresh or idle-restored Session, narrowing before the first turn can leave omitted MCP servers unstarted. A later explicit selection can still use an omitted server. To exclude a capability for the whole Session, leave it out at opening. For new names, open a new Session. An idle checkpoint restore still requires matching saved Python/manual definitions and uses the opening Session's MCP declarations, including any `tools=` override (otherwise `Agent.tools`). It does not recover credentials or callbacks from storage. See [save and restore](sessions.md#compact-save-and-restore).

If the Session opened with `NativeAgents`, retain that fixed grant in every explicit selection, even when removing Python, manual or MCP tools. Update tools only after all native children have closed. See [Session updates](sessions.md#update-tools-instructions-and-model-settings).

To use host stdio after replacement, include at least one `StdioMCP(location="host")` declaration when opening the Session. Opening only HTTP or environment stdio cannot later acquire a host process capability. Environment stdio requires the Session's current Environment. Both locations remain subject to the opening server/tool-name ceiling.

At the fixed native revision, host stdio initialization rejects incompatible ambient `CODEX_EXEC_SERVER_NOISE_*` executor settings; it does not silently select that remote executor. HTTP-only Sessions do not require a local executor.

### Select one MCP tool

Use `function.ref` to select a Python or manual tool and `server.tool(raw_name)` to select one admitted MCP leaf. Both return a `ToolRef`; the MCP helper supplies the normalized native namespace. These references select the last accepted bindings and grant no new names or connections.

This fragment uses `lookup` from the preceding staged example and a current MCP declaration named `server` that selects a raw tool named `search`:

```python
from agents import Agent, ToolRef

agent = Agent(instructions="Use the selected lookup and search tools.", tools=[lookup, server])
async with agent.session() as session:
    turn = await session.submit("Find atlas.", tools=[lookup.ref, server.tool("search")])
    result = await turn.get_result()

assert lookup.ref == ToolRef("lookup")
```

Constructing a new narrower MCP descriptor for `submit(tools=...)` does not replace the admitted descriptor. Use `session.update(tools=...)` while idle to change bindings, within the opening server/tool-name ceiling.

### Schema discovery

Set `defer_loading=True` on `@function_tool(...)`, a manual `FunctionTool`, or an MCP declaration to offer selected schemas through native tool search on models that support discovery. Models without that support receive the selected schemas directly. Each MCP server keeps its own discovery setting for its selected raw tools. Programmatic tool calling retains its native direct/code exposure rules.

```python
from agents import Agent, function_tool


@function_tool(defer_loading=True)
def invoice_status(invoice_id: str) -> str:
    return f"Status for {invoice_id}: paid"


agent = Agent(instructions="Find the requested invoice status.", tools=[invoice_status])
```

Declare the intended tools before opening the Session. Temporary turn or step selections can only select accepted bindings; passing a fresh declaration cannot enable discovery or replace a schema. MCP idle replacements must retain that server's opening `defer_loading` value. Discovery neither delays acquisition of selected MCP resources nor exposes an unselected tool. Narrow selections still apply to the schemas a model can discover.

## Callback deadlines

Set `@function_tool(callback_timeout=2.0)` to limit the callback body, including time queued for sync execution. A late result is withheld even when the callback suppresses cancellation. The SDK joins callback cleanup before reporting settlement. A running application function can delay that join; the deadline cannot undo its earlier effects. `before`, `after`, argument validation and result conversion are outside the callback deadline.

## Inspect or change tool values

| Declared tool | Before execution | Before its result is consumed |
| --- | --- | --- |
| Python function | Python validates parameters. `before` can allow, deny or replace arguments. | `after` receives and returns `ToolOutput`. |
| MCP server | `before` sees original input; `approval` sees effective input. `approval_mode` also selects native approval behavior. | `transform_result` accepts or transforms the native MCP result. |
| Environment executor tool | `approval` sees original arguments; only allowance permits `transform_arguments`. | `transform_result` changes the result before a code cell receives it. Direct result rewriting is unsupported. |
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

Set `transform_result` on `HttpMCP` or `StdioMCP`. Its callback receives `MCPResultContext` and a native MCP `CallToolResult` dictionary, and returns an accepted dictionary directly or through an awaitable. The dictionary uses `content`, `structuredContent`, `isError` and `_meta`.

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

MCP result transforms run after the selected server tool executes, before native event, history, model, or code-cell consumers receive its result. They support direct and programmatic tool calls. They also apply to declared native children with each child's call identity. Set `transform_result_timeout` to bound the callback independently of server execution. Invalid, late or failed decisions withhold the original result and fail the submission. A server error cannot be changed into success. Editing a result cannot undo server effects or reveal effects the server did not report. Checkpoints store no transform callback; restore uses the current Agent's descriptors and credentials.

## Tool notifications

Read `ToolStarted` and `ToolFinished` records from `session.events()` to observe Python and MCP tool activity. Import these types from `agents.observation`. They include call identity and event sequence, without tool arguments or results. `ToolFinished` also reports completion status.

A Python start record means the SDK accepted a function call. An MCP start record can precede approval and server execution. Cancellation may leave an MCP call without a matching finish record. A completion status does not prove that earlier effects were rolled back.

Observations are passive and do not delay tool execution or settlement. The buffer is bounded, so a slow reader can receive `EventGap` after older records are dropped. Use [stream observations](sessions.md#stream-observations) to consume events while waiting for a turn. Use the [supported tool controls](#inspect-or-change-tool-values) to validate or change tool values.

## Remote execution

Remote execution requires an application-owned service that implements the Codex exec-server protocol. Use a version compatible with the SDK's pinned Codex runtime. This guide assumes that service is already deployed and has an authenticated WSS endpoint.

Pass `Environment` to a Session or one-shot run to connect to that service. It is session configuration, outside the Agent's `tools` list. Set `EXECUTOR_URL` to the service's WSS endpoint and `EXECUTOR_TOKEN` to its current credential:

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

`agent.run(..., environment=environment)` and `agent.run_sync(..., environment=environment)` accept the same connection data. `Environment.tools` defaults to an empty tuple, so process and file tools must be selected explicitly. The SDK does not fall back to running these tools on the application host.

`cwd` is an absolute executor path or `file:` URI. The selected native model and runtime must support the tools. `exec_command` starts a command, `write_stdin` polls it or writes to a TTY, and `apply_patch` edits files. `write_stdin` requires `exec_command`. Ordinary pipe-mode stdin is not supported. There is no separate Python process or file client.

For the pinned native runtime, `exec_command` and `write_stdin` arguments must match their advertised object schemas before dispatch. The SDK rejects malformed JSON, missing required fields, unknown keys, wrong types, and invalid enum values. Omitting an optional field is different from passing `null`. A rejection fails the current turn without an automatic retry. Earlier calls may already have effects or may be cancelled; later calls are not admitted. The rejected call does not enter accepted conversation history. Its error names the tool and includes a SHA-256 digest of the call ID for correlation with an application-owned provider record. This check covers these two pinned schemas, not arbitrary JSON Schema or the freeform `apply_patch` grammar. It does not promise rollback.

The schemas currently advertise `number` for numeric fields. Codex applies narrower integer and range requirements, with unsigned values for time and token budgets. A schema-valid number can therefore pass admission and still fail native parsing. The SDK passes accepted arguments unchanged; it does not round values or insert defaults.

Selected process and file tools can modify the assigned remote workspace. The SDK requests native write access only to `cwd`, retains native protection for metadata directories, and requests restricted networking. Changing a command's `workdir` does not grant another writable workspace. These requests are not a universal OS-enforcement guarantee: the application owns remote workspace and tenant isolation and must choose an executor that enforces its required limits. Selecting tools does not approve permission escalation. An Environment has no `sandbox_mode` option.

Supply current application-owned credentials in `headers`. Header values and the URL are omitted from the declaration's representation. WSS uses the native system trust store; there is no TLS-verification bypass. `connect_timeout` is a positive finite number of seconds, or `None` for the native default. URLs cannot contain userinfo, query parameters or fragments.

A Session captures one Environment. Tool replacement does not replace it, and it does not belong in `update(tools=...)`. Open a new Session to change the connection or selected executor tools. The same Agent can serve Sessions with different Environments. Restore uses the supplied current Environment; saved history carries no credentials or launch authority. Live processes are not portable.

Python, manual, HTTP MCP and stdio MCP tools can coexist with an Environment. A `StdioMCP` server runs on the host by default, or on the executor with `location="environment"`.

Cancellation settles native work and joins SDK-owned callbacks. Closing also asks the native owner to terminate its known background processes. A successful acknowledgment means the executor accepted a termination attempt; it does not prove physical process exit, completed output drain, or rollback. A rejected or timed-out termination attempt raises an error while the Session closes. If a process-start acknowledgment is lost, an effect may already have occurred and native cleanup may still be pending. Do not retry a consequential command merely because its result is missing.

### Manage the executor lifetime

A supplied `Environment` is a borrowed connection descriptor. Closing the Session closes the SDK's connection. The application still owns the executor service and workspace. You can also pass an async context manager that yields an `Environment`. The SDK enters that manager when opening the Session. It keeps the manager entered until native resources retire, including after an opening failure or cancellation. The manager can then release the application-owned executor.

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

Create a fresh manager for each opening, including restore: use `environment=executor_lease()` alongside `state=saved_state` when restoring a portable checkpoint. The yielded descriptor supplies current credentials and must match any saved network-policy digest. Retain the workspace separately if later work needs its files; a checkpoint does not recreate them.

The SDK enters and exits the manager in one lease task. `ContextVar` changes inside the manager stay in that task; pass application data through `context=`. The manager cannot suppress exceptions from the separate Session body. Close the Session on the event loop where it opened, and handle release failures from Session exit. The SDK retains the lease while native resource retirement is incomplete. Session exit does not guarantee successful backend teardown.

### Network and local socket access

**Unreleased.** Set `Environment.network` to a `NetworkPolicy` to configure network destinations and local socket access for native commands. These settings do not control the model connection, Python functions, or MCP clients. Omitting `network` preserves the default executor behavior.

On a macOS executor, `NetworkPolicy()` grants no network destinations or Unix sockets. A Linux executor cannot enforce Unix-socket denial with an explicit policy. If `allow_all_unix_sockets` is `False` (the default), the SDK rejects the policy before a Linux command starts, even when the policy specifies domain rules.

The policy accepts `allowed_domains` and `denied_domains`. A denial takes precedence over an allowance. On a macOS executor, `NetworkPolicy(allowed_domains=("api.example.com",))` permits that host through the managed proxy without granting Unix socket access. Rules cover hosts across all ports. Supply hostnames or supported host patterns without a scheme or port. The proxy checks hostnames and cannot inspect the contents of encrypted traffic. The policy never permits an upstream proxy or an unsandboxed fallback.

On Linux and macOS executors, `allow_all_unix_sockets=True` grants access to every reachable Unix socket. Use this grant only on an executor private to the job. Keep every socket the job does not need out of reach. The SDK rejects grants for individual socket paths.

`allow_local_binding=True` allows local listeners where the executor supports them. It also adds loopback and private IPv4 ranges to the proxy bypass rules. The managed proxy checks domain rules only for traffic that passes through it. On a macOS executor, local binding can also allow direct traffic to any reachable IP address on destination port 53, including non-DNS traffic. This direct traffic bypasses the domain rules. If the job needs stricter limits, the application must enforce them with network isolation outside the executor. Leave `allow_local_binding` at its default of `False` when the job only needs Unix sockets. An empty domain list does not cancel an explicit local-access or Unix-socket grant.

The SDK captures the policy when the Environment is created. It checks the executor's reported OS and managed-proxy capability before commands run. Windows executors reject every explicit network policy in this revision; their default named-pipe path remains available. An unsupported setting or incompatible configuration fails rather than silently granting broader access. Every explicit network policy disables sandbox escalation while retaining native workspace limits and the configured command approval callbacks. A command with `sandbox_permissions="require_escalated"` is denied before it starts, although the Session can open successfully.

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

For a hard offline boundary, your backend must isolate the commands, exec-server, managed proxy and DNS resolver together. Keep host and other jobs' service sockets out of the job's mounts and socket namespace. Close inherited connections before starting the executor. The managed proxy can look up a hostname before denying a request, even with an empty destination list. On Linux, a process can bind a local IP socket inside its isolated namespace. On macOS, the all-sockets grant lets the proxy forward HTTP to any Unix socket it can reach. Use an independently isolated macOS VM or equivalent backend so Codex can apply its command sandbox once. A second outer Seatbelt profile is unsupported.

The backend supplies and verifies the offline boundary; the SDK cannot infer it from a URL. Restore requires the same normalized network policy from current application code. The checkpoint's policy field stores only a digest. The SDK uses the digest to compare policies. The digest grants no access and provides no encryption. Native debug logs and command history can still contain policy details. To change the policy, start a new Session. Python callbacks can run fixed operations directly in the application's worker. [Restore an Environment](sessions.md#compact-save-and-restore)

#### Docker creation and restore checks

For a Linux Docker executor, the reusable `examples/offline_docker.py` example checks the container's network configuration at creation and attachment. `create_offline_job` starts a private container with `--network none`. `open_exec_stdio` inspects the same full container ID that it passes to `docker exec -i`. Use that function on every authenticated gateway attachment, including an attachment that restores a saved Session. The check rejects a container with the wrong network mode, an external attached network, missing inspection data, or a stopped container before starting the exec-server.

The application owns the Docker daemon, image, container ID and authenticated gateway route. Keep that job record outside conversation checkpoints. The gateway stays outside the offline container and sends the exec-server protocol through standard input and output; the example publishes no container port. Closing a gateway attachment does not end the job. Keep the container for restore and remove it when the job finishes.

The image and host must support the native executor and its Linux sandbox. Docker's network setting alone does not establish that support. The example's network checks do not attest an arbitrary WSS URL or prevent a Docker administrator from changing the job later. Native macOS executors and Windows named pipes use their separate platform paths; a Docker Linux guest uses the Linux path regardless of the controller's OS.

### Windows named pipes in an offline job

**Unreleased.** For a job-local named pipe on a Windows x64 executor, select `tools=["exec_command"]` and omit `network`. Unix socket grants do not apply to Windows named pipes. Have the provisioner resolve the assigned workspace on the Windows executor and pass that canonical path as `Environment.cwd`. The executor account needs read and write access to its private workspace. The native command sandbox still applies. The Python controller cannot resolve a remote path locally. The executor's handshake reports its own working directory, which can differ from the job's workspace.

The pipe must allow the restricted job process to connect, read and write. A server that creates additional pipe instances needs permission to create them (`FILE_CREATE_PIPE_INSTANCE`).

For example, a Win32 listener can create a single pipe instance with `PIPE_REJECT_REMOTE_CLIENTS` and grant read and write access only to the current logon SID.

Your backend must keep the command, executor and any proxy and DNS resolver inside the same offline, job-private VM or equivalent environment. Keep host and other jobs' service pipes out of reach. Close inherited handles and connections that could reach outside the job. The SDK does not create or verify this isolation.

### Executor policies

Use `Environment.approval(context, arguments)` for an application decision before a selected executor call. Return a bool or `StopTurn`, directly or through an awaitable. Approval sees the original arguments first and has no transform deadline; it may wait for a person. Denial, `StopTurn`, errors or cancellation never invoke `transform_arguments`. Approval cannot grant extra host or native permissions. It applies to selected executor tool calls, not to MCP server startup.

Denial has different scopes on these routes:

| Decision | Effect |
| --- | --- |
| `Environment.approval` returns `False` for a direct call | Denies the call. The owning turn fails with `NativeError`; the model does not continue that turn. |
| `Environment.approval` returns `False` for a call from generated code | Stops and joins the owning turn before subsequent generated-code operations can run. Generated code cannot catch the denial to continue execution. |
| MCP `approval` returns `False` for a direct call | Denies the call. The owning turn fails with `NativeError`. |
| MCP `approval` returns `False` for a call from generated code | Rejects that nested MCP call. Generated code can catch the rejection and continue; this denial does not itself stop the owning turn. |
| `StopTurn(...)` from executor approval or an argument transform, or from MCP `before` or `approval` | Stops and joins the exact owning root or child turn. |

After a direct root-call denial, submit a new turn to continue.

Import `StopTurn` and `TurnStoppedError` from `agents`. After successful cleanup of a stopped root, `await turn.get_result()` raises `TurnStoppedError("Turn stopped by policy")` after owned work settles. A captured stopped child Turn can report `NativeError` instead. A child stop does not stop the root, another child or later turns. The stopped call does not run, but earlier effects remain.

`transform_arguments(context, value)` accepts or transforms arguments before direct or programmatic calls. It can return raw arguments, `Allow`, `ReplaceArguments` or `StopTurn`. A raw dictionary with an `action` key stays argument data; only SDK decision objects control the call. `transform_result(context, value)` transforms the executor result before the model or generated code receives it. Use it for direct executor calls in the default mode and for calls from generated code with `Agent(programmatic_tool_calling=True)`.

Both transforms can be synchronous or asynchronous. `ExecutorContext` supplies the application context, owning Python Session and Agent, and actual root or child call identity. Use `agent_role` for the native child role and `model` for the executing model when Codex supplies them. A root call has no native child role; a child call does not create another Python Agent. Direct calls set `call_id`; programmatic calls set `outer_call_id`, `cell_id` and `runtime_tool_call_id`. These identities cannot be replaced.

An executor approval or transform callback cannot observe, submit to, update, interrupt, checkpoint or close its own Session. Start any event observer in an application task outside the callback. Use application context for data needed to make a decision.

Arguments for `exec_command` and `write_stdin` are JSON objects; `apply_patch` receives a patch string. Native validation still applies after transformation. A result transform runs after the remote effect and cannot undo it. `policy_timeout` bounds transform decisions in seconds and defaults to 30. Invalid, late or failed decisions fail closed. The callbacks cover root and native child calls.

See [programmatic tool calling](configuration.md#programmatic-tool-calling). Saved configuration contains no policy callbacks, credentials, endpoints or network-policy names. The [unreleased network-policy support](#network-and-local-socket-access) also compares the current Environment policy with the recorded digest on restore. This does not redact command history or native debug logs.

## Native children

Add `NativeAgents()` to the Agent's `tools`, or pass it in `agent.session(tools=[...])` to choose tools for that Session. Pass an Environment selecting all three executor tools to the Session so the model can delegate work to subagents. Children share the executor connection, workspace policy, provider and credentials. They inherit the parent's model and reasoning effort unless an application-declared role supplies its own compatible settings.

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

Codex's `default`, `explorer` and `worker` roles are available without custom roles. Built-in roles inherit `Agent.instructions`. If a distinct task needs its own developer instructions, pass `additional_roles=[NativeRole(name=..., description=..., instructions=...)]` to `NativeAgents`. Import `NativeRole` from `agents`. An additional role cannot replace a built-in role. Native full-history forks inherit the parent's role; selecting a different role requires `fork_turns="none"`.

The root model gets the native `collaboration` tools: `spawn_agent`, `wait_agent`, `send_message`, `followup_task`, `interrupt_agent` and `list_agents`. Children share the selected executor tools, Python and manual tools, application context, HTTP or local stdio MCP tools, and policies. `max_depth=1` permits direct children only. Set a larger positive depth to permit bounded delegation by descendants. Native admission rejects unknown roles, model-selected override arguments and unselected calls before dispatch. The executor's permission policy applies at every depth.

`NativeAgents` sets native approval policy to `never` for the root and its children. Codex can return permission denials to the model, but cannot ask the application to approve native permission escalation. The application's `Environment.approval` callback still applies to selected executor calls on root and child turns. That callback cannot grant extra native permissions. Sessions without `NativeAgents` keep their existing native approval behavior when `Environment.network` is omitted. An explicit [network policy](#network-and-local-socket-access) disables sandbox escalation.

This feature requires an explicit model compatible with the three native tools. A `NativeRole` can specify `model` and `model_settings={"reasoning_effort": ...}`; other role model settings are unsupported. The SDK validates every declared model and inherited reasoning combination before opening an executor connection. When a role selects a different model, that model must support programmatic tool calling if the Agent enables it. Some compatible models can serve as children with programmatic tool calling disabled, but cannot delegate further. `max_depth` sets a permission ceiling and does not add model capabilities. Unknown or incompatible metadata fails without a fallback. `max_children=None` uses Codex's native limit; a positive value limits concurrent children across the tree, not the total ever created.

The root can use a structured `output_type`. Child results remain text. Manual tools work through `on_interaction` during a one-shot run or through `session.interactions()`; pending child manual requests cannot be checkpointed. Model settings, instructions and the `NativeAgents` declaration remain fixed for the Session. Use [idle tool replacement](#idle-replacement) after all native children have closed. There is no programmatic child-creation method.

After all work and callbacks settle, idle checkpoints can retain child conversation, task history and older goal/checklist data. Restore requires current `NativeAgents` and matching Python and manual tool definitions, and creates fresh runtime resources. MCP declarations come from the current Agent. Saved goal/checklist data grants no tools. Live child Turns do not transfer, and restore does not resume interrupted child work automatically. See [save and restore](sessions.md#compact-save-and-restore).

Read descendants with `async for child in session.subagents()`. Each immutable `Subagent` captures its identity, `status` and current or last observed `turn`. The turn is `None` until observed. `Subagent.status` describes the child's lifecycle; an `active` child can be idle or unloaded. `is_live` says whether the snapshot captured a loaded active instance. It can become stale after a close or reload, so `start()` still validates the captured incarnation.

```python
async for child in session.subagents():
    if child.turn is not None:
        result = await child.turn.get_result()
        print(child.id, result.final_output)
```

`Turn.get_result()` returns that child's `Completed[str]`, preserving its text and latest reported thread usage even after later turns. Missing usage is `None`; root results do not aggregate it. Native child failures and interruptions raise `NativeError`. If a Python tool function fails, the result can instead raise that function's exception after cleanup. Cancelling a reader leaves native work running. Use `await session.cancel_all()` for the tree or `await child.try_cancel(child.turn)` for the captured child turn. See [interruption](sessions.md#interrupt-work) for the boolean result and cleanup limits of child cancellation.

`await child.start(message)` starts the captured live child when it is idle and returns its exact `Turn[str]`. To steer an observed active child turn, use `await child.turn.steer(message)`. These calls cannot create a child or resume a closed or reloaded incarnation. Reading descendants starts no work, and these methods have no pagination arguments.

During an all-scope stop, new child input rejects until the stop settles. An already pending manual reply remains allowed. Afterward, validated explicit input to a still-live child can lift the stop; autonomous queued wakes cannot. Late child steering may start an automatic successor after the observed native turn finishes. The captured child Turn still returns the result for its original native turn. Follow the successor through `session.events()` or a fresh `session.subagents()` snapshot.

In a child's `ToolContext`, `MCPResultContext` or `ExecutorContext`, `submission` is a `ChildInvocation`. This callback identity grants no wait or cancellation authority. Read a `Subagent.turn` in application code to wait for observed work. Closing the Session shuts down its native tree; root completion alone does not imply child completion. `session.events()` spans both root and child work.

## Manual tool results

Use `FunctionTool` when the application must supply a tool result later. The declaration
has no function body. Its `input_type` is a Pydantic model; its `output_type` validates
the result before native delivery.

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

`match()` accepts only requests issued for that exact declaration object. It also
preserves the request's input and output types for static checking. Manual calls
can wait concurrently. Reply to each request separately; replying to one does not
consume another request.

A live manual request is limited to 65,536 bytes of UTF-8 encoded request data, including its arguments and request metadata. An oversized request fails its owning submission with `NativeError` before argument validation or delivery to a handler. Keep manual-tool payloads within this limit.

`request.reply(value)` packages a value for a one-shot handler to return. In an explicit Session, read `session.interactions()` and call `await request.respond(value)`. Validation failure leaves that live request available for retry. Duplicate, expired and foreign requests fail. Native acceptance delivers to the original waiting call; it does not acknowledge a durable history write or start a new user turn.

`session.interactions()` also delivers child requests after the root finishes. `request.subagent_id` identifies the child, or is `None` for root work. Requests from different children have separate authority even if call IDs coincide. Manual tools work in direct mode and programmatic tool calling. Pending child requests and pending code cells require a live Session.

`ActionRequested` carries sensitive typed arguments and live reply authority. Avoid logging or persisting it. Passive events never grant reply authority. A new interaction cursor sees still-pending requests; it has no replay sequence.

To expire pending root requests, use `await turn.cancel(mode="force")`. To include children and the queue, use `await session.cancel_all(mode="force")`. Session exit also expires pending requests. Cancelling a `turn.get_result()` task or closing an observation cursor leaves them live. See [interrupt work](sessions.md#interrupt-work).

### Answer interactions during a one-shot run

Pass `on_interaction` to `agent.run()` or `agent.run_sync()`. The handler returns the reply created by the exact request it receives, directly or through an awaitable. For a manual function, use the preceding `answer` example. The SDK validates and submits the reply. Do not call `respond()` in a handler and return `None`. That fails, as does a reply for a different, expired or already answered request.

The handler can receive supported native root requests too. Approval requests use `request.reply("allow_once")` or `request.reply("deny")`. Form requests use `request.reply(values)`; `request.reply(None)` declines a form or URL request. Native acknowledgement means the reply was enqueued, not that an external approval or URL verification succeeded. Supported manual child requests remain distinct from native child prompts.

Import `ApprovalRequest` and `ElicitationRequest` from `agents` to distinguish native approvals from questions whose fields arrive at runtime:

| Request | Inspect | One-shot reply | Explicit Session reply |
| --- | --- | --- | --- |
| `ApprovalRequest` | `description` | `reply("allow_once")` or `reply("deny")` | `await respond("allow_once")` or `await respond("deny")` |
| `ElicitationRequest` form | `description` and `schema` | `reply(values)` or `reply(None)` to decline | `await respond(values)` or `await decline()` |
| `ElicitationRequest` URL | `description`; `schema` is empty | `reply(None)` to decline | `await decline()` |

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

`ElicitationRequest.schema` is a detached schema mapping. The SDK validates form replies against the supported supplied field constraints. URL requests support decline only; the SDK does not visit or verify the URL in `description`. A Session-scoped URL request has `turn_id=None` and `submission_id=None`, even while a turn is active. Do not infer an originating turn from the Session's current work.

Handlers may run concurrently. Synchronous handlers run in worker threads and must be safe to call concurrently. The run closes its Session and joins handlers when root work finishes, fails or is cancelled. Python cannot forcibly stop synchronous worker threads. Use an explicit Session if children must continue after root completion, or to retry an invalid manual result.

Standard MCP forms support primitive fields and enum-backed multi-select arrays, including titled choices; nested objects and general arrays remain unsupported on that route. OpenAI extension forms support Draft 2020-12 object schemas with nested objects, arrays and single-hop references to local `$defs`. Extension `format` values are annotations; regex keywords, resource identifiers and dynamic references are unsupported. The SDK does not fetch schema resources.

Native prompts from subagents and end-to-end URL verification remain unsupported. Use manual tools for application-owned questions, including supported child manual requests. Live replies do not make code cells, processes or child turns portable.

### Ask for application approval

Use `Environment.approval` for an application decision before selected executor calls. MCP declarations expose `before`, `approval` and `transform_result` alongside native `approval_mode`; they do not accept a per-call `interceptor`. `NativeAgents.approval` controls native collaboration calls. See [executor policies](#executor-policies) and [native tool decisions](#native-tool-decisions).

For questions your application owns, declare a `FunctionTool` with typed question
arguments and a typed answer. Its `on_interaction` handler can await the user
and return the exact request's reply. This also works in native children and
programmatic tool calling while the run remains active.

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

This fragment uses `agent` and `answer` from [Manual tool results](#manual-tool-results). Always check `exc.state`. Without `before_step`, selected manual tools without a handler cannot use this automatic save with programmatic tool calling, non-plain Agent output (including `PromptedOutput`), selected MCP tools, local storage, an Environment or `NativeAgents`. A `WebSearch` declaration on the opening Agent also prevents automatic saving. The SDK rejects these combinations without a checkpoint. It checks visible declarations before opening; it may check saved selections or changed Session bindings after opening, before submitting work. With `before_step`, the SDK opens the Session and evaluates each step's selection. Steps that select no manual tools can complete normally. If a selected manual tool is called without a handler, the run raises `InteractionRequired` with `state=None` after opening and inference; earlier tool effects may already have occurred. Other native questions and late undeclared manual calls do not become portable checkpoints. For supported live interactions use a handler, or open an explicit Session and use `suspend()` when its manual boundary supports it.

`input=None` resumes the existing turn. Ordinary input cannot replace a pending turn. A pending restore without a handler raises `InteractionRequired` with the supplied checkpoint still in `state`. Tools and `before_step` cannot be changed on an already admitted turn. Restore uses fresh requests for the current declarations; old handles do not transfer. Completed results are not called again.

Idle checkpoints accept ordinary input for a new turn. `return_state=True` saves successful completion in `Completed.state`; it defaults to `False`, leaving `state=None`. Both Agent run methods require a bool and fail if saving fails.

An explicit Session supports more manual suspension cases, including matching `PromptedOutput`. A bare structured Agent output or `NativeOutput` cannot suspend a pending manual turn; finish it in the live Session and save after completion. See [saving a settled manual request](sessions.md#saving-a-settled-manual-request) for output fingerprints, `before_step`, active repair, code cells and child limits.

## Experimental job recovery

`DeferredTools` connects a manual `FunctionTool` to an application-owned external job so the host can resume work after the host application process restarts. The host owns the durable journal, current permissions, provider configuration and wake-up schedule. This helper does not make arbitrary tool calls durable. Import it from `agents.experimental.deferred`.

Declare the tool separately, then register the exact object used by the Session. Registration returns `None` and rejects decorated callbacks. Both input and result types must be strict Pydantic models with `extra="forbid"`. Make every public result field required. Supply generated request IDs in the original input so restore does not create new IDs.

The recovery declaration profile is narrower than general Pydantic support. Use JSON strings, booleans, integers, finite floats, nulls, ordered lists and nested strict models with `extra="forbid"`. Registration rejects custom validators, serializers, default factories, computed fields, custom initialization and rich field types such as `datetime` or `set`. Perform custom input checks in host or provider code before an external effect, and project rich results into the supported public model. Use live manual replies when custom serialization is required.

Inputs can also use JSON scalar defaults, `Literal` values and supported unions. Input aliases must identify provider fields unambiguously. Results require every public field and preserve declared field/list order; they do not accept the input profile's defaults, `Literal` or general union fields. Nullable result fields remain supported. Required request IDs belong in the supplied input, never in a default factory.

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

The host's `project` function selects the result fields the model may receive. `max_output_bytes` defaults to 16,384 and bounds the public result's JSON size. Keep provider receipts and private values in the host journal. Use a live manual reply for custom serialization or a scalar result.

From host code outside `on_interaction`, call `await jobs.prepare(session, request)` for a live root request matched by `report.match(request)`. Preparation suspends the Session and expires its live requests. The journal must atomically save the checkpoint and all registered pending bindings before preparation returns. Preparation never calls the job provider. After an interrupted or failed journal write, inspect the journal before deciding what work remains.

Restore the prepared checkpoint with a matching Agent, obtain a fresh request from `restored.interactions()`, then call `await jobs.advance(restored, request)`. A returned `Turn` represents resumed model work; await its `get_result()`. A string such as `"pending"`, `"input_required"` or `"unknown"` is a host-only status. Keep the original prepared checkpoint while sibling calls remain outstanding. The journal can retain completed public results for delivery on later restores without restarting the external job.

`advance()` defaults to `wait_seconds=0.0`: it starts once without polling. The host schedules later observations. A positive wait allows one additional bounded observation after creation. `timeout_seconds` defaults to 10 seconds and covers authorization, provider I/O, persistence and delivery. A timeout does not authorize another creation. The helper joins cooperative cleanup and leaves no poller running.

Alternatively, `async with jobs.start(session, request) as (restored, outcome)` prepares, restores and advances one call. The context owns the restored Session. This shortcut uses the opening Session's captured declarations and resolved model credentials. For rotated credentials or changed tool bindings or instructions, use explicit preparation and restore with current configuration. The shortcut rejects unsupported changes before suspending the Session.

Prepare a root request before its first restore. Plain-text and supported `PromptedOutput` roots can use recovery; pending native-schema output cannot. An Environment, native agents, programmatic tool calling and child requests are outside this helper's preparation contract. Use live replies for those cases. See [saving a settled manual request](sessions.md#saving-a-settled-manual-request) for the underlying checkpoint limits.

The journal must allow only one worker to claim creation and must check current permissions and the exact binding and generation on every operation. By default, registration requires `provider.reconcile_creation()` and target-side duplicate prevention for an immutable job key. If the host loses the creation acknowledgment, reconciliation must determine whether the external job started before the host can proceed. This acknowledgment identifies the job; it is not the final job result. Set `require_reconciliation=False` only for approved, low-impact work that may remain `"unknown"`. An unknown outcome gives no permission to repeat an external effect.

Automatic delivery rejects public results containing the current model API key, job key or provider receipt, including an incidental substring match. Such a rejection does not permit another start. The host can review the result and use `await request.respond(safe_value)` on the fresh live request. The host must record that completion itself.

### Optional MCP Tasks adapter

`MCPTasksProvider` is an optional experimental adapter for one host-selected remote MCP tool. It does not change [native MCP](#native-mcp) execution. Install exactly `mcp==2.0.0` and `mcp-types==2.0.0` in the host environment, then import `MCPTasksProvider` and `MCPInputReply` from `agents.experimental.mcp_tasks`. Normal SDK imports do not require these packages.

Construct `MCPTasksProvider(endpoint=..., tool=..., authorize=..., admit_pending=..., admit_result=...)` with trusted host configuration. Tool arguments cannot select the endpoint or tool. `authorize(claim, operation)` checks current permissions and returns a bearer token without its prefix for each HTTP request. The adapter refuses redirects. HTTPS uses the host's proxy and CA settings. Construct a fresh provider after changing host egress settings.

This adapter cannot reconcile lost creation receipts. Register it with `require_reconciliation=False` only for approved, low-impact work that can remain `"unknown"`. Before creation, the adapter reads at most 32 pages of tool declarations. If page 32 still has a continuation cursor, the adapter refuses the listing before sending `tools/call`.

Supply a fast synchronous `admit_pending(claim, task_id, requests_json)` callback. At creation, `requests_json` is `None`. For a form, it contains the complete request mapping as compact JSON with sorted keys. Return exactly `True` only when trusted data for `claim.binding` confirms the task ID and every form key, prompt and schema. Treat these values as private even on refusal: a peer can copy an earlier credential into them. Missing or refused admission leaves a new creation `"unknown"` without a receipt. A refused form remains `"unknown"` with its accepted receipt. Neither result permits another creation.

Completed updates omit peer text, structured fields and metadata by default. To admit selected data, supply a fast synchronous `admit_result(claim, result)` callback. Its return must be finite JSON data; the adapter stores that data under `structuredContent` for the registered projector. The update also retains the peer's `isError` flag. Check fields against trusted per-job data when independent verification matters. Restore the admission policy with the job. Verify or quarantine older journal rows before use because cached updates and public results can return without another admission callback.

Form replies require `allow_input_replies=True` and a vetted target that binds each request key to one immutable request for the task's lifetime. The target must reject retired or satisfied keys. Changed requests need new keys and fresh approval. Standard `tasks/update` has no revision precondition. Supported forms contain flat strings, booleans, numbers and string lists, including choices. Other validation constraints are refused.

For a form reply, call `advance(..., operation="respond", response=MCPInputReply(challenge=..., responses=...))` with host-approved `ElicitResult` values from `mcp_types`. For cancellation, use `operation="cancel"`. Both operations re-read the task before dispatch, even before its next poll time. Supply `reserve_mutation(claim, reply)` for either operation; cancellation passes `None`. Immediately before sending, the host must atomically check current permissions, binding, generation, receipt and approval, then reserve all selected form keys or the task's single cancellation. Return exactly `True` only after a new reservation commits. Reservations stay spent across retries and lost acknowledgments. After a crash, observe the task before deciding what remains unknown.

Initial authorization and each network exchange have separate timeout budgets. The adapter waits for cooperative host callback cleanup, which can exceed a timeout. Authorization and admission callbacks run on the caller's loop. Each HTTP request uses a separate copy of the operation's context; rebinding a `ContextVar` there does not change later requests. Mutable context values remain shared. Keep callbacks short and reset a context token in the callback that created it.

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

Session cleanup joins approval callbacks that have started. Callbacks must not wait on, interrupt, close or mutate their own Session. Use [tool notifications](#tool-notifications) when a passive notification is sufficient.
