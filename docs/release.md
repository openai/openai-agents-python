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

### 0.22.0

Version 0.22.0 tightens failure handling and data isolation for several existing APIs. Applications that construct `OpenAIProvider` with an explicit client and also pass `organization` or `project` to the provider must remove those duplicate arguments.

Highlights:

-   When an agent-level output guardrail blocks final output produced directly by a terminal function tool, the SDK retains a replay-valid call/output pair only when validated fields permit safe reconstruction. The original `function_call_output` payload is replaced with the fixed text `"Output withheld by an output guardrail."` in session history, `RunState`, and streamed result state, and payload-bearing current-response guardrail metadata is cleared or replaced. If the current response contains reasoning or another unsupported shape, the SDK discards the complete current-response suffix instead. Earlier accepted turns and guardrail results remain available. See [Output guardrails](guardrails.md#output-guardrails).
-   Non-streaming OpenAI Responses calls now raise `ModelBehaviorError` when the returned response has terminal status `failed` or `incomplete`, matching the existing streamed terminal-event handling. This applies to `OpenAIResponsesModel` and the Responses path in `AnyLLMModel`. See [Exceptions](running_agents.md#exceptions).
-   [`OpenAIProvider`][agents.models.openai_provider.OpenAIProvider] now also raises `UserError` when `openai_client` is combined with `organization` or `project`. The existing conflicts with `api_key`, `base_url`, and `websocket_base_url` are unchanged. Configure these values on the explicit `AsyncOpenAI` client instead. See [API keys and clients](config.md#api-keys-and-clients).
-   Each `RunResult.to_state()` checkpoint now owns an independent usage snapshot. A resumed result starts with the checkpoint totals and adds its own model calls without mutating the source result or sibling checkpoints. Nested `Agent.as_tool()` resumes continue to aggregate post-resume usage into the active outer run. See [Usage in RunState checkpoints](usage.md#usage-in-runstate-checkpoints).
-   Agent visualization now recursively expands the tools, MCP servers, and downstream handoffs of a target registered with `handoff(agent)`, matching direct `Agent` entries in an agent's `handoffs` list. See [Generating a graph](visualization.md#generating-a-graph).
-   The `Agent.clone()` and `RealtimeAgent.clone()` API guidance now states their existing shallow-copy behavior precisely: list attributes that are not overridden remain the same list objects. Pass a new list when the clone must own the container independently. See [Cloning/copying agents](agents.md#cloningcopying-agents).

### 0.21.0

Version 0.21.0 requires `openai` v3 and moves the Agents SDK's OpenAI HTTP integrations to HTTPX2. Applications that use the default OpenAI client do not need to change their client setup, but applications that customize the OpenAI HTTP layer may need to migrate transport-facing code.

Highlights:

-   The required OpenAI dependency is now `openai>=3.0.0,<4`. A clean core installation uses HTTPX2 and no longer installs legacy `httpx` as a direct dependency.
-   The default OpenAI provider, Voice provider, Responses WebSocket support, tracing exporter, and provider retry normalization now use HTTPX2. Their existing Agents SDK public configuration and runtime behavior remain unchanged.
-   Applications that pass `http_client=` to `AsyncOpenAI` should migrate custom clients, transports, authentication, event hooks, mock transports, timeout values, URLs, requests, responses, and transport exception handling from `httpx` to `httpx2`. Prefer the OpenAI Python SDK's `DefaultAsyncHttpx2Client` when the application needs the OpenAI client's defaults plus custom HTTP options. See [Custom HTTP clients with `openai` v3](config.md#custom-http-clients-with-openai-v3).
-   The Agents SDK does not convert arbitrary legacy HTTPX objects to HTTPX2. The OpenAI Python SDK's temporary legacy-client compatibility path requires an explicit `httpx` installation and should be treated as a migration bridge.
-   Local MCP HTTP customization continues to follow the installed MCP package: MCP Python SDK v1 supplies and uses legacy `httpx`, while MCP Python SDK v2 uses `httpx2`. Ordinary MCP connections do not need application changes. See [MCP Python SDK v1 and v2](mcp.md#mcp-python-sdk-v1-and-v2).
-   Public provider-neutral testing utilities now cover Agent model, Sandbox session, Realtime session, and Voice pipeline workflows without provider or process dependencies. See [Testing](testing.md) for recipes and guidance on when to keep the real provider adapter or integration boundary.

### 0.20.0

Version 0.20.0 includes a potentially breaking MCP dependency migration for applications that customize local MCP HTTP transports. It also updates the SDK default model used when an agent or run does not explicitly select one.

Highlights:

-   The SDK default model is now `gpt-5.6-luna` instead of `gpt-5.4-mini`. The default `reasoning.effort="none"` and `verbosity="low"` settings are unchanged.
-   Explicit agent models, run-level model overrides, and the `OPENAI_DEFAULT_MODEL` environment variable continue to take precedence over the SDK default.
-   Realtime input transcription settings now recognize `gpt-transcribe`, `gpt-live-transcribe`, and `gpt-realtime-whisper`. For low-latency `gpt-live-transcribe` sessions, nested `audio.input.transcription` settings can supply `prompt`, `keywords`, and multiple expected `languages`. The OpenAI client version pinned by this SDK supports the `delay` latency/accuracy level only with `gpt-realtime-whisper`. Use `gpt-transcribe` over WebSocket for transcription after a committed audio turn or for detected-language output. Setting `audio.input.turn_detection=None` explicitly disables automatic turn detection. See [Input transcription settings](realtime/guide.md#input-transcription-settings).
-   Local MCP connections created by the Agents SDK now support MCP Python SDK v2 while retaining v1 compatibility through `mcp>=1.19.0,<3`. The Agents SDK adapts ordinary stdio, SSE, and Streamable HTTP connections automatically. With MCP v2 installed, these connections use `mcp.Client(mode="auto")` to probe the newest supported protocol and fall back to the legacy `initialize` handshake for older servers. If dependency resolution selects MCP v2, applications that supply custom `httpx.Auth` objects or `httpx.AsyncClient` factories must migrate those values to `httpx2`, or pin `mcp<2` to retain the v1 HTTP stack. `MCPServerStreamableHttp`'s `params["ignore_initialized_notification_failure"] = True` option also remains v1-only. See [MCP Python SDK v1 and v2](mcp.md#mcp-python-sdk-v1-and-v2) for migration details.
-   Sandbox mount validation now rejects unsafe credential placement before sandbox or mount-helper side effects. Trusted applications can acknowledge mount-scoped or broad credential exposure for an exact in-container mount path without changing the storage capability tables. These acknowledgements are runtime-only and serialized sandbox state never grants credential authority by itself. At protected mount boundaries, the SDK returns a fresh redacted exception. If the source exception is an exact recognized SDK sandbox error and its approved structured fields validate, the replacement preserves that subtype and the validated safe fields. A recognized `MountConfigError` can also retain an SDK-generated safe validation message. Otherwise, the SDK returns a fresh generic redacted error. Provider-controlled or otherwise unapproved messages, command data, notes, context, causes, and source traceback state are not retained. See [Mounts and remote storage](sandbox/clients.md#mounts-and-remote-storage) and [Resume from session state](sandbox/guide.md#resume-from-session-state).
-   Retry policies can inspect stable replay-safety facts and explicitly set `RetryDecision(approve_unsafe_replay=True)` for a non-streaming request that the provider marked unsafe. This approval does not bypass aborts, emitted streamed output, or separate local-side-effect vetoes such as Programmatic Tool Calling. See [Runner-managed retries](models/index.md#runner-managed-retries).
-   Resumable `RunState` objects can now stage durable user input with `add_input()` before the next model call. Staged input survives serialization, runs through input guardrails, and produces one durable SDK input occurrence across local sessions and server-managed conversations. An explicitly approved unsafe replay can still resend the input to the provider and repeat provider-side work. See [Add input before resuming](results.md#add-input-before-resuming).
-   Runtime reliability fixes align streamed and non-streamed [output-guardrail session persistence](guardrails.md#output-guardrails), preserve `FunctionTool` subclasses during copying and namespacing, and raise an explicit error for [unsupported Chat Completions audio output](models/index.md#chat-completions-compatibility-options) instead of silently completing an empty stream. The `OpenAIResponsesCompactionSession` wrapper attempts and awaits [pre-compaction history recovery](sessions/index.md#auto-compaction-can-block-streaming) before cancellation reaches the caller. A [`VoicePipeline`](voice/pipeline.md#results) consumer now receives transcription-session close failures after a clean run, while an earlier turn failure retains precedence over a later close failure. `RunState` round trips now preserve local shell output, acknowledged computer safety checks, default-valued tool output fields, and Pydantic model or dataclass outputs encountered while traversing dictionaries, lists, or tuples. MCP conversion preserves free-form object schemas and image output, and serializes other raw content blocks such as audio and resource blocks as valid JSON text. `MCPServerManager` serializes overlapping lifecycle operations and applies finite default timeouts to connection and cleanup. Model replay removes server-owned `created_by` metadata from output items before using them as input.

### 0.19.0

This minor release does **not** introduce a breaking change. The minor version bump reflects a significant new OpenAI Responses feature area: Programmatic Tool Calling.

Highlights:

-   Added [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool], which lets supported OpenAI Responses models generate JavaScript to coordinate tools eligible for Programmatic Tool Calling. It supports per-tool `allowed_callers`, structured outputs from `FunctionTool` instances, and integration with Runner streaming, guardrails, approvals, sessions, and `RunState`. See [Programmatic Tool Calling](tools.md#programmatic-tool-calling) for setup and constraints.
-   Added the public `agents.decorators` module and `@tool` as a shorter alias for the existing `@function_tool` decorator, alongside the existing guardrail decorators. `FunctionTool` instances now also support async callable objects.
-   SDK configuration now consistently accepts either typed settings objects or dictionaries across agents, runs, models, sessions, sandboxes, and voice pipelines, with validation for unknown settings.
-   Hardened error and diagnostic logging across models, tools, MCP, Realtime, sessions, sandboxes, and tracing to avoid exposing raw sensitive payloads while preserving useful debugging context.
-   Improved AnyLLM, LiteLLM, and Chat Completions compatibility, preserved session history across model retries, and added provider retry guidance for WebSocket overloads that occur before a response starts, so opt-in Runner retry policies can replay the failed attempt when permitted.
-   Added [S3 mounts that can be configured only when a Vercel sandbox is created](sandbox/clients.md#mounts-and-remote-storage) through `VercelCloudBucketMountStrategy`. Mounted sessions exclude bucket contents from workspace persistence and intentionally do not support dynamic mount changes or session resume.

### 0.18.0

This minor release does **not** introduce a breaking change. The minor version bump is for the Realtime agents default model update only.

Highlights:

-   Realtime agents now use `gpt-realtime-2.1` as the default model, so new Realtime setups use the latest recommended model without extra configuration.

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
-   Added sandbox execution backends for local and containerized development via `UnixLocalSandboxClient` and `DockerSandboxClient`, plus hosted provider integrations for Blaxel, Cloudflare, Daytona, E2B, Modal, Runloop, and Vercel through optional dependency extras in the Python package.
-   Added sandbox memory support so future runs can reuse lessons from prior runs, with progressive disclosure, multi-turn grouping, configurable isolation boundaries, and persisted-memory examples including S3-backed workflows.
-   Added a broader workspace and resume model, including local and synthetic workspace entries, remote storage mounts for S3/R2/GCS/Azure Blob Storage/S3 Files, portable snapshots, and resume flows via `RunState`, `SandboxSessionState`, or saved snapshots.
-   Added substantial sandbox examples and tutorials under `examples/sandbox/`, covering coding tasks with skills, handoffs, memory, provider-specific setups, and end-to-end workflows such as code review, dataroom QA, and website cloning.
-   Extended the core runtime and tracing stack with sandbox-aware session preparation, capability binding, state serialization, unified tracing, prompt cache key defaults, and safer sensitive MCP output redaction.

### 0.13.0

This minor release does **not** introduce a breaking change, but it includes a notable Realtime default update plus new MCP capabilities and runtime stability fixes.

Highlights:

-   The default websocket Realtime model is now `gpt-realtime-1.5`, so new Realtime agent setups use the newer model without extra configuration.
-   `MCPServer` now exposes `list_resources()`, `list_resource_templates()`, and `read_resource()`, and `MCPServerStreamableHttp` now exposes `session_id` so sessions using the MCP Streamable HTTP transport can be resumed across reconnects or stateless workers.
-   Chat Completions integrations can now opt into re-sending existing reasoning content via `should_replay_reasoning_content`, improving provider-specific reasoning/tool-call continuity for adapters such as LiteLLM/DeepSeek.
-   Fixed several runtime and session edge cases, including concurrent first writes in `SQLAlchemySession`, compaction requests with orphaned assistant message IDs after reasoning stripping, `remove_all_tools()` leaving MCP/reasoning items behind, and a race in the batch executor for `FunctionTool` instances.

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

- `FunctionTool` instances wrapping **synchronous** Python callables now execute on worker threads via `asyncio.to_thread(...)` instead of running on the event loop thread. If your tool logic depends on thread-local state or thread-affine resources, migrate to an async tool implementation or make thread affinity explicit in your tool code.
- Local MCP tool failure handling is now configurable, and the default behavior can return model-visible error output instead of failing the whole run. If you rely on fail-fast semantics, set `mcp_config={"failure_error_function": None}`. Server-level `failure_error_function` values override the agent-level setting, so set `failure_error_function=None` on each local MCP server that has an explicit handler.

### 0.7.0

In this version, there were a few behavior changes that can affect existing applications:

- Nested handoff history is now **opt-in** (disabled by default). If you depended on the v0.6.x default nested behavior, explicitly set `RunConfig(nest_handoff_history=True)`.
- The default `reasoning.effort` for `gpt-5.1` / `gpt-5.2` changed to `"none"` (from the previous default `"low"` configured by SDK defaults). If your prompts or quality/cost profile relied on `"low"`, set it explicitly in `model_settings`.

### 0.6.0

In this version, the default handoff history is now packaged into a single assistant message rather than passing the user and assistant turns as separate messages, giving downstream agents a concise, predictable recap
- The existing single-message handoff transcript now starts by default with the exact literal text `For context, here is the conversation so far between the user and the previous agent:` before the `<CONVERSATION HISTORY>` block, so downstream agents get a clearly labeled recap

### 0.5.0

This version doesn’t introduce any visible breaking changes, but it includes new features and a few significant updates under the hood:

- Added support in `RealtimeRunner` for handling [SIP protocol connections](https://platform.openai.com/docs/guides/realtime-sip).
- Significantly revised the internal logic of `Runner#run_sync` for Python 3.14 compatibility

### 0.4.0

In this version, [openai](https://pypi.org/project/openai/) package v1.x versions are no longer supported. Please use openai v2.x along with this SDK.

### 0.3.0

In this version, the Realtime API support migrates to gpt-realtime model and its API interface (GA version).

### 0.2.0

In this version, a few places that used to take `Agent` as an arg, now take `AgentBase` as an arg instead. For example, this applies to the `list_tools()` method signature in MCP servers. This is a purely typing change, you will still receive `Agent` objects. To update, just fix type errors by replacing `Agent` with `AgentBase`.

### 0.1.0

In this version, [`MCPServer.list_tools()`][agents.mcp.server.MCPServer] has two new params: `run_context` and `agent`. You'll need to add these params to every overridden `MCPServer.list_tools()` method in subclasses of `MCPServer`.
