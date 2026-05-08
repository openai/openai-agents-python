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

### 0.17.0

In this version, sandbox local source materialization keeps `LocalFile.src` and `LocalDir.src` within the materialization `base_dir` unless the source path is covered by `Manifest.extra_path_grants`. The `base_dir` is the SDK process current working directory when the manifest is applied; relative local sources are resolved from that directory, while absolute local sources must already be inside it or under an explicit grant. This closes a local artifact boundary issue, but it can affect applications that intentionally copy trusted host files or directories from outside that base directory into a sandbox workspace.

To migrate, grant trusted host roots at the manifest level with `SandboxPathGrant`, preferably as read-only when the sandbox only needs to read those files:

```python
from pathlib import Path

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.entries import Dir, LocalDir

# This is an absolute host path outside the SDK process base_dir.
TRUSTED_DOCS_ROOT = Path("/opt/my-app/docs")

manifest = Manifest(
    extra_path_grants=(
        # This host root is outside the SDK process base_dir, so the manifest must grant it.
        SandboxPathGrant(path=str(TRUSTED_DOCS_ROOT), read_only=True),
    ),
    entries={
        # No grant is needed for local sources that stay under the SDK process base_dir.
        "fixtures": LocalDir(src=Path("fixtures"), description="Local test fixtures."),
        # This entry reads from the granted host root and copies it into the sandbox workspace.
        "docs": LocalDir(src=TRUSTED_DOCS_ROOT, description="Trusted local documents."),
        # Dir creates a sandbox workspace directory; it does not read from the host filesystem.
        "output": Dir(description="Generated artifacts."),
    },
)
```

Treat `extra_path_grants` as trusted application configuration. Do not populate grants from model output or other untrusted manifest input unless your application has already approved those host paths.

### 0.16.0

In this version, the SDK default model is now `gpt-5.4-mini` instead of `gpt-4.1`. This affects agents and runs that do not explicitly set a model. Because the new default is a GPT-5 model, implicit default model settings now include GPT-5 defaults such as `reasoning.effort="none"` and `verbosity="low"`.

If you need to keep the previous default model behavior, set a model explicitly on the agent or run config, or set the `OPENAI_DEFAULT_MODEL` environment variable:

```python
agent = Agent(name="Assistant", model="gpt-4.1")
```

Highlights:

-   `Runner.run`, `Runner.run_sync`, and `Runner.run_streamed` now accept `max_turns=None` to disable the turn limit.
-   Sandbox workspace hydration now rejects tar archives with symlinks that point outside the archive root, including absolute symlink targets, across local, Docker, and provider-backed sandbox implementations.

### 0.15.0

In this version, model refusals are now surfaced explicitly as `ModelRefusalError` instead of being treated as empty text output or, for structured outputs, causing the run loop to retry until `MaxTurnsExceeded`.

This affects code that previously expected a refusal-only model response to complete with `final_output == ""`. To handle refusals without raising, provide a `model_refusal` run error handler:

```python
result = Runner.run_sync(
    agent,
    input,
    error_handlers={"model_refusal": lambda data: data.error.refusal},
)
```

For structured-output agents, the handler can return a value matching the agent's output schema, and the SDK will validate it like other run error handler final outputs.

### 0.14.0

This minor release does **not** introduce a breaking change, but it adds a major new beta feature area: Sandbox Agents, plus the runtime, backend, and documentation support needed to use them across local, containerized, and hosted environments.

Highlights:

-   Added a new beta sandbox runtime surface centered on `SandboxAgent`, `Manifest`, and `SandboxRunConfig`, letting agents work inside persistent isolated workspaces with files, directories, Git repos, mounts, snapshots, and resume support.
-   Added sandbox execution backends for local and containerized development via `UnixLocalSandboxClient` and `DockerSandboxClient`, plus hosted provider integrations for Blaxel, Cloudflare, Daytona, E2B, Modal, Runloop, and Vercel through optional extras.
-   Added sandbox memory support so future runs can reuse lessons from prior runs, with progressive disclosure, multi-turn grouping, configurable isolation boundaries, and persisted-memory examples including S3-backed workflows.
-   Added a broader workspace and resume model, including local and synthetic workspace entries, remote storage mounts for S3/R2/GCS/Azure Blob Storage/S3 Files, portable snapshots, and resume flows via `RunState`, `SandboxSessionState`, or saved snapshots.
-   Added substantial sandbox examples and tutorials under `examples/sandbox/`, covering coding tasks with skills, handoffs, memory, provider-specific setups, and end-to-end workflows such as code review, dataroom QA, and website cloning.
-   Extended the core runtime and tracing stack with sandbox-aware session preparation, capability binding, state serialization, unified tracing, prompt cache key defaults, and safer sensitive MCP output redaction.

### 0.13.0

This minor release does **not** introduce a breaking change, but it includes a notable Realtime default update plus new MCP capabilities and runtime stability fixes.

Highlights:

-   The default websocket Realtime model is now `gpt-realtime-1.5`, so new Realtime agent setups use the newer model without extra configuration.
-   `MCPServer` now exposes `list_resources()`, `list_resource_templates()`, and `read_resource()`, and `MCPServerStreamableHttp` now exposes `session_id` so streamable HTTP sessions can be resumed across reconnects or stateless workers.
-   Chat Completions integrations can now opt into reasoning-content replay via `should_replay_reasoning_content`, improving provider-specific reasoning/tool-call continuity for adapters such as LiteLLM/DeepSeek.
-   Fixed several runtime and session edge cases, including concurrent first writes in `SQLAlchemySession`, compaction requests with orphaned assistant message IDs after reasoning stripping, `remove_all_tools()` leaving MCP/reasoning items behind, and a race in the function-tool batch executor.

### 0.12.0

This minor release does **not** introduce a breaking change. Check [the release notes](https://github.com/openai/openai-agents-python/releases/tag/v0.12.0) for major feature additions.

### 0.11.0

This minor release does **not** introduce a breaking change. Check [the release notes](https://github.com/openai/openai-agents-python/releases/tag/v0.11.0) for major feature additions.

### 0.10.0

This minor release does **not** introduce a breaking change, but it includes a significant new feature area for OpenAI Responses users: websocket transport support for the Responses API.

Highlights:

-   Added websocket transport support for OpenAI Responses models (opt-in; HTTP remains the default transport).
-   Added a `responses_websocket_session()` helper / `ResponsesWebSocketSession` for reusing a shared websocket-capable provider and `RunConfig` across multi-turn runs.
-   Added a new websocket streaming example (`examples/basic/stream_ws.py`) covering streaming, tools, approvals, and follow-up turns.

### 0.9.0

In this version, Python 3.9 is no longer supported, as this major version reached EOL three months ago. Please upgrade to a newer runtime version.

Additionally, the type hint for the value returned from the `Agent#as_tool()` method has been narrowed from `Tool` to `FunctionTool`. This change should not usually cause breaking issues, but if your code relies on the broader union type, you may need to make some adjustments on your side.

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
