# Local MCP Server Lifecycle

Use this reference for changes to Python-managed MCP servers, `MCPServerManager`, client-session request ordering, tool caching or filtering, local MCP retries, cancellation, or cleanup. Hosted MCP is a provider tool and follows the OpenAI API contract; use `$openai-knowledge` for that protocol surface. Read [Tool identity and routing](tool-identity.md) for server-prefixed names and [Tool execution lifecycle](tool-execution-lifecycle.md) for approval and invocation behavior after an MCP tool is converted to a `FunctionTool`.

## Connection Ownership and Task Affinity

- A local `MCPServer` owns its transport, `ClientSession`, and `AsyncExitStack` from `connect()` through `cleanup()`. Partial connection failure still requires closing every context already entered.
- Some MCP transports use AnyIO cancel scopes that require connection and cleanup in the same task. Do not wrap either operation in a helper that silently creates another task.
- `MCPServerManager` preserves task affinity in sequential mode and uses one long-lived worker task per server in parallel mode. Timeouts must run inside that owning task; on Python versions without `asyncio.timeout()`, cancel the current worker task and translate only timer-originated cancellation to `TimeoutError`.
- Cleanup runs servers in reverse order and continues across ordinary cleanup failures. Cancellation suppression is an explicit manager policy; do not accidentally convert unrelated `BaseException` failures into recoverable connection errors.
- Server cleanup must clear session and transport-visible state even when exit-stack cleanup raises, so the same server object can reconnect without exposing stale session handles or workers.

## Manager State

- Keep configured servers, connected servers, failed servers, active servers, and per-server errors as distinct views. `active_servers` is the agent-facing list; with `drop_failed_servers=True` it excludes failed connections while preserving configured order.
- Non-strict connection records failures and continues with the connected subset. Strict connection cleans up work started by the failed attempt and restores the previous coherent active state before raising.
- `reconnect(failed_only=True)` retries the deduplicated failed set without disturbing healthy connections. A full reconnect cleans up all servers first and rebuilds manager state.
- Parallel connection still needs deterministic per-server state and complete cleanup after cancellation or one hard failure. Do not let completion order decide `active_servers`, `failed_servers`, or which workers remain registered.

## Shared Session Requests and Retries

- Streamable HTTP can require requests on one shared MCP session to be serialized. The same lock must cover tool calls, tool listing, prompts, and resource operations that share that session; serializing only `call_tool()` still permits sibling cancellation and protocol races.
- Preserve outer cancellation. A cancelled shared request may qualify for an isolated-session retry only when the transport identifies it as an inner or transient session failure and retry budget remains.
- Isolated-session retries are transport-specific recovery. Count isolated session setup and execution against the same retry budget, retry only the supported transient failure shapes, and never replay mixed exception groups or ordinary 4xx failures as if they were safe.
- Generic `list_tools()` and `call_tool()` retries use the configured attempt count and backoff. Validate required arguments locally before starting retries so deterministic input errors never reach the server or consume retry budget.
- MCP tool failure conversion follows the effective server or agent `failure_error_function`. Explicit `None` means propagate; cancellation of the parent run must not become model-visible tool failure output.

## Tool Discovery, Cache, and Filtering

- The unfiltered server tool list is the cacheable value. Apply static or dynamic filters to a copy for each requesting agent and run context; never let one request's filtered or merged metadata mutate the shared cache.
- `cache_tools_list=True` assumes server schemas are stable until `invalidate_tools_cache()` marks them dirty. Connection or filter changes must not accidentally make a stale filtered list authoritative.
- Dynamic filters require both `run_context` and agent. A filter exception excludes that tool and logs the failure rather than exposing it by default.
- Schema conversion to strict form is best effort and must not mutate the MCP server's original input schema. If strict conversion fails, preserve the original schema and keep metadata isolated per converted `FunctionTool`.
- Tool list collision errors, prefixed-name generation, and approval policy validation must be deterministic regardless of server response or connection completion order.

## Review Checklist

1. Identify the task that owns connect, every request, timeout cancellation, and cleanup for each transport.
2. Test partial connect failure, strict and non-strict manager modes, reconnect, repeated cleanup, and manager cancellation.
3. Test overlapping tool, prompt, and resource requests when shared-session serialization is enabled.
4. Prove retries preserve outer cancellation, consume one budget, and do not replay deterministic or unsupported failures.
5. Test cache invalidation, context-dependent filters, schema immutability, duplicate names, and reconnect with the public runner path.

## Sources

- `docs/mcp.md`
- `src/agents/mcp/server.py`
- `src/agents/mcp/manager.py`
- `src/agents/mcp/util.py`
- `tests/mcp/test_mcp_server_manager.py`
- `tests/mcp/test_connect_disconnect.py`
- `tests/mcp/test_client_session_retries.py`
- `tests/mcp/test_caching.py`
- `tests/mcp/test_tool_filtering.py`
- `tests/mcp/test_server_errors.py`
- `tests/mcp/test_runner_calls_mcp.py`
