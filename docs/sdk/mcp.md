# Connect MCP tools

Use Model Context Protocol (MCP) servers to expose external tools to an agent. Codex manages the connection and dispatches calls; the selected server executes the operation with its own permissions.

[Install the SDK](installation.md) and [configure the provider](configuration.md#provider-connection) first. Examples with `await` belong inside an async application.

## Connect an HTTP server

Use `HttpMCP` for Streamable HTTP. The declaration selects the server's raw tool names. For this HTTP example, set `DOCS_MCP_URL` and `DOCS_MCP_TOKEN` for your server, which must provide `search`:

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

## Start a stdio server

For a trusted stdio server, use `StdioMCP` and supply its command. Replace the example paths with your server executable and working directory; this server must provide a raw tool named `search`:

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

Include `server` in the Agent's `tools` list. By default, `location="host"` starts the server on the application host. Set `location="environment"` to start it on the remote executor passed to `agent.session(environment=...)`. A connected Environment can host stdio MCP servers even when `Environment.tools` is empty.

Codex manages the process and its standard input and output connection until the Session closes. Supply a trusted command because the process uses the selected machine's permissions. Tool selection and approval do not sandbox server startup.

Configure the server's process environment as follows:

- Use an absolute `cwd`. If omitted, host servers use Codex's runtime directory and remote servers use the Environment's working directory.
- Use `env_vars` to select variables from the machine that runs the server. Codex reads their values at startup.
- Pass arguments as separate values. Codex does not expand them in a shell. The declaration captures the arguments and variable names.

Restore uses the application's current server declarations and Environment.

## Configure MCP approval

MCP selection and approval are separate. The default `approval_mode="auto"` keeps Codex's native approval rules, which can require interaction when a tool lacks annotations or can make changes. Answer supported live requests through `session.interactions()` or a [one-shot handler](approvals.md#handle-native-approvals-and-forms). A run without a handler can raise `InteractionRequired` when a native decision is needed. A successful read-only tool does not prove that a write tool can run without interaction.

To explicitly approve calls to selected tools, use `approval_mode="approve"`. This fragment reuses the `HttpMCP` import above:

```python
writer = HttpMCP(
    name="records",
    url="https://mcp.example.com/mcp",
    tools=("write_record",),
    approval_mode="approve",
)
```

Codex applies that setting only to the selected raw tool names. Native constraints and strict review still apply. The setting does not answer a server's own requests for interactive input, sandbox server startup, or limit effects the server does not disclose. Result policies still run after execution and cannot undo those effects. Do not change a tool's read-only annotation to obtain approval.

Approval settings follow the current declaration during idle replacement and restore. Restoring with the default `auto` does not inherit an explicit approval from the saved Session.

## Handle MCP startup errors

A fresh or idle-restored Session captures MCP declarations when it opens. The SDK checks that the server provides selected tools at these points:

- For a turn selection, handle startup or missing-tool errors from `turn.get_result()`.
- For an idle selection, handle those errors from `session.update()`.
- When restoring a pending manual request, the SDK prepares selected MCP resources before exposing fresh requests. Errors can prevent the Session from opening.

Codex normalizes the tool namespaces the model sees. Conflicting names fail before the model can use the tools.

For lists that mix context-aware Python tools and MCP declarations, MyPy may need an explicit `list[Tool[Application]]` or `Agent[Application, Answer]` annotation.

## Select and discover tools

Include each MCP declaration in the opening Agent or Session `tools` list. Use `server.tool(raw_name)` to select one of its raw tool names for a turn or step. See [select individual tools](tools.md#select-individual-tools) for a Python and MCP example.

Set `defer_loading=True` on the declaration to offer its selected schemas through native tool search on compatible models. Other models receive the schemas directly. Discovery does not delay the selected server's connection or startup. See [tool schema discovery](tools.md#discover-tool-schemas) for the common selection rules.

## Replace and restore MCP connections

Selecting fewer tools before the first turn can leave omitted MCP servers unstarted. A later explicit selection can still use them. To exclude a tool for the whole Session, leave it out when opening.

Restore uses the opening `tools=` override, or otherwise the current `Agent.tools`. Storage does not supply credentials or callbacks. For the shared Python, manual and native-child requirements, see [tool replacement](tools.md#replace-tools-between-turns) and [save and restore](state.md#save-and-restore-a-portable-checkpoint).

To use host stdio after replacement, open with at least one `StdioMCP(location="host")` declaration. A Session opened with only HTTP or environment stdio cannot later start a host process. Environment stdio requires the Session's current Environment. Both locations remain limited to opening server and tool-name pairs.

With the pinned native revision, host stdio initialization rejects incompatible `CODEX_EXEC_SERVER_NOISE_*` settings from the environment. It does not silently select the remote executor those settings describe. HTTP-only Sessions do not require a local executor.

## MCP result policies

Configure MCP policies on `HttpMCP` or `StdioMCP` in execution order:

1. `before(context, call)` returns `Allow`, `ReplaceArguments`, or `StopTurn` within its own 30-second deadline.
2. `approval` receives the effective arguments accepted after `before`.
3. If execution proceeds, the server runs the operation.
4. `transform_result` accepts or changes the reply before consumers receive it.

Executor policies use a different order: [executor approval](execution.md#executor-policies) receives the original arguments before transformation. `transform_result_timeout` limits the MCP result callback, not approval.

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

## Observe MCP calls

Read `ToolStarted` and `ToolFinished` from `session.events()` to observe call identity and completion status. These passive records omit arguments and results. An MCP start can precede approval and execution, and cancellation can leave a call without a finish record. See [tool notifications](tools.md#tool-notifications) for buffer limits and event consumption.
