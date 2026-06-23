# SDK Maintainer References

This directory captures long-lived implementation contracts of the OpenAI Agents Python SDK that are not replaceable by OpenAI API or platform facts from the Developer Docs MCP. The repo's `docs/` remain an SDK-specific behavioral contract; these references distill the ownership, compatibility, ordering, and failure semantics that maintainers need to preserve that contract.

## Usage

Read the reference map before changing or reviewing an affected runtime boundary, then open only the files relevant to that boundary. During issue and PR review, treat this directory as read-only background: use it to identify expected invariants, adjacent surfaces, and regression risks, but verify the current claim against the remote issue or PR, current code, tests, docs, release boundary, and focused runtime evidence. Do not edit references as a side effect of a review or treat them as proof of current issue status, PR behavior, or repository readiness.

When implementation or dedicated repository-maintenance work establishes a reusable invariant that remains valid beyond one issue or PR, update the narrowest owning reference separately. Preserve the generalized contract, not the case history or decision outcome that revealed it.

## Inclusion Criteria

Add or retain a reference when the knowledge is SDK-specific, stable across multiple releases, easy to violate from one local code path, and expensive to reconstruct from source, tests, and repo docs during every review. Treat `docs/` as the SDK's user-facing behavioral contract; use these references to preserve the implementation constraints behind that contract. Prefer invariants and ownership rules over summaries of individual issues, PRs, or recent fixes.

Do not store current issue or PR status, generic maintainer-review workflow, release notes, OpenAI API or platform behavior available through `$openai-knowledge`, or one-off implementation details in this directory. Put review methodology under `.agents/skills/`, released migration notes in `docs/release.md`, and API or platform facts behind `$openai-knowledge`.

## Reference Map

| Reference | Read before changing or reviewing |
|---|---|
| [Agent definition and run context](agent-definition-and-run-context.md) | Agent fields, cloning, dynamic instructions, enabled tools or handoffs, context wrappers, usage, or public agent identity |
| [Runner lifecycle](runner-lifecycle.md) | Turn accounting, guardrails, handoffs, interruptions, cancellation, or streaming parity |
| [Run item lifecycle](run-item-lifecycle.md) | Model output processing, new item types, stream events, replay conversion, session persistence, or RunState serialization |
| [Function and output schema](function-and-output-schema.md) | Function-tool signatures and metadata, strict JSON schema conversion, or structured output types |
| [Conversation state ownership](conversation-state-ownership.md) | Sessions versus server-managed continuation, input deltas, retries, compaction, or conversation resume |
| [Session persistence](session-persistence.md) | Session input callbacks, per-turn saves, retry rewind, atomicity, or compaction replacement |
| [RunState schema and resume boundary](runstate-schema.md) | Serialized state, schema versions, approvals, agent identity, or durable resume data |
| [Tool identity and routing](tool-identity.md) | Tool names, namespaces, lookup, approvals, MCP naming, handoffs, or call IDs |
| [Tool execution lifecycle](tool-execution-lifecycle.md) | Function-tool planning, approvals, guardrails, concurrency, cancellation, timeouts, or failure conversion |
| [Local MCP server lifecycle](local-mcp-server-lifecycle.md) | Local MCP connection ownership, manager state, request serialization, caching, filtering, retries, or cleanup |
| [Model and provider boundaries](model-provider-boundaries.md) | Model resolution, provider adapters, feature capability, request conversion, terminal events, or retries |
| [Tracing lifecycle](tracing-lifecycle.md) | Trace and span context, processors, export, flush, shutdown, resume, or sensitive data |
| [Realtime session lifecycle](realtime-session-lifecycle.md) | Realtime listeners, connections, background tasks, handoffs, event iteration, or cleanup |
| [Realtime tracing architecture](realtime-tracing.md) | Realtime API server traces versus Agents SDK client traces |
| [Voice pipeline lifecycle](voice-pipeline-lifecycle.md) | VoicePipeline STT/workflow/TTS ownership, event and audio ordering, stream cleanup, PCM framing, or tracing |
| [Sandbox runtime boundary](sandbox-runtime-boundary.md) | Sandbox session ownership, preparation, resume state, manifests, materialization, or cleanup |

## Maintenance Rules

Keep each rule in the narrowest reference that owns it. Cross-link instead of copying detailed rules between files. Describe current architecture and compatibility boundaries, not the chronology of how a bug was found. Use source paths and durable public contracts as anchors, and remove or rewrite guidance when ownership moves.
