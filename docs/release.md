# Release process/changelog

The project follows a slightly modified version of semantic versioning using the form `0.Y.Z`. The leading `0` indicates the SDK is still evolving rapidly. Increment the components as follows:

## Minor (`Y`) versions

We will increase minor versions `Y` for **breaking changes** to any public interfaces that are not marked as beta. For example, going from `0.0.x` to `0.1.x` might include breaking changes.

If you don't want breaking changes, we recommend pinning to `0.0.x` versions in your project.

## Patch (`Z`) versions

We will increment `Z` for non-breaking changes:

-   Bug fixes
-   New features
-   Changes to private interfaces
-   Updates to beta features

## Breaking change changelog

### 0.8.0

In this version, two runtime behavior changes may require migration work:

- Function tools wrapping **synchronous** Python callables now execute on worker threads via `asyncio.to_thread(...)` instead of running on the event loop thread. If your tool logic depends on thread-local state or thread-affine resources, migrate to an async tool implementation or make thread affinity explicit in your tool code.
- Local MCP tool failure handling is now configurable, and the default behavior can return model-visible error output instead of failing the whole run. If you rely on fail-fast semantics, set `mcp_config={"failure_error_function": None}`. Server-level `failure_error_function` values override the agent-level setting, so set `failure_error_function=None` on each local MCP server that has an explicit handler.

### 0.7.0

In this version, there were a few behavior changes that can affect existing applications:

- Nested handoff history is now **opt-in** (disabled by default). If you depended on the v0.6.x default nested behavior, explicitly set `RunConfig(nest_handoff_history=True)`.
- The default `reasoning.effort` for `gpt-5.1` / `gpt-5.2` changed to `"none"` (from the previous default `"low"` configured by SDK defaults). If your prompts or quality/cost profile relied on `"low"`, set it explicitly in `model_settings`.

### 0.6.0

In this version, the default handoff history is now packaged into a single assistant message instead of exposing the raw user/assistant turns, giving downstream agents a concise, predictable recap
- The existing single-message handoff transcript now by default starts with "For context, here is the conversation so far between the user and the previous agent:" before the `<CONVERSATION HISTORY>` block, so downstream agents get a clearly labeled recap

### 0.5.0

This version doesn’t introduce any visible breaking changes, but it includes new features and a few significant updates under the hood:

- Added support for `RealtimeRunner` to handle [SIP protocol connections](https://platform.openai.com/docs/guides/realtime-sip)
- Significantly revised the internal logic of `Runner#run_sync` for Python 3.14 compatibility

### 0.4.0

In this version, [openai](https://pypi.org/project/openai/) package v1.x versions are no longer supported. Please use openai v2.x along with this SDK.

### 0.3.0

In this version, the Realtime API support migrates to gpt-realtime model and its API interface (GA version).

### 0.2.0

In this version, a few places that used to take `Agent` as an arg, now take `AgentBase` as an arg instead. For example, the `list_tools()` call in MCP servers. This is a purely typing change, you will still receive `Agent` objects. To update, just fix type errors by replacing `Agent` with `AgentBase`.

### 0.1.0

In this version, [`MCPServer.list_tools()`][agents.mcp.server.MCPServer] has two new params: `run_context` and `agent`. You'll need to add these params to any classes that subclass `MCPServer`.
