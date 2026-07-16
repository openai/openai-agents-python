# Sandbox Runtime Boundary

Use this reference for changes to sandbox session ownership, `SandboxAgent` preparation, manifests, capabilities, host-path materialization, snapshots, resume state, agent transitions, or cleanup.

## Runtime Ownership

The outer `Runner` owns agent turns, approvals, handoffs, tracing, session history, and `RunState`. A sandbox session owns the execution environment, workspace, processes, mounts, and provider-specific connection state. Do not move one layer's lifecycle into the other without defining resume and cleanup behavior for both.

- A live `SandboxRunConfig.session` is caller-owned. The runner may configure and use it but must not delete or fully tear it down.
- A session created or resumed through `SandboxRunConfig.client` is runner-owned. Cleanup runs pre-stop hooks, persists snapshot-backed workspace state, stops and shuts down the session, deletes provider resources when required, and closes dependencies.
- Session cleanup must be idempotent and release acquired `SandboxAgent` concurrency guards even when persistence or provider cleanup fails.
- A `SandboxAgent` instance cannot be reused concurrently across runs because prepared capability tools and session state are bound to one live run. Clone or construct separate agents for concurrent work.

## Session Source and Saved State

Resolve the session source in this order: injected live session, resumable sandbox state carried by `RunState`, explicit `SandboxRunConfig.session_state`, then a newly created session. Manifest and snapshot inputs seed only a fresh session; they do not overwrite an injected or resumed workspace.

- `RunState` sandbox data and explicit `session_state` represent provider connection or session state used to reconnect to existing work.
- A snapshot represents saved workspace contents used to seed a new session. It is not interchangeable with provider session state.
- Preserve stable per-agent resume identity across handoffs, including graphs with duplicate agent names. Object identity is process-local, so serialized state needs stable keys and explicit current-agent selection.
- Serialize runner-owned sessions after stop-time persistence has completed so a later resume can reattach when the backend survives or reconstruct the workspace from the saved snapshot when it does not.

## Agent Preparation

- Clone capability instances per run before binding them to a live session. Reusing mutable capability objects can leak tools, sampling settings, or session references across runs.
- Validate capability dependencies before exposing tools. Capability tool construction, instruction fragments, input processing, and sampling adjustments must use the same effective capability set.
- Build instructions in the documented order: SDK sandbox base prompt or explicit replacement, agent instructions, capability instructions, remote-mount policy, then the rendered filesystem description.
- Bind capability tools to the live session and preserve a link from the prepared clone to the public `SandboxAgent`. Dynamic instructions and hooks should observe the public agent rather than an internal clone with implementation-only state.
- Handoffs stay in the outer run loop and select another agent-bound sandbox session. A nested `Agent.as_tool()` run owns its own nested runner and sandbox lifecycle.

## Filesystem Trust Boundary

- Manifest entry destinations are workspace-relative and must not escape the workspace. The workspace root itself must be absolute where the backend requires an absolute runtime root.
- `LocalFile` and `LocalDir` sources are host-side inputs. Resolve them against a trusted base directory, require explicit application-controlled `extra_path_grants` outside that base, and reject untrusted manifests that try to authorize their own host access.
- Validate local sources at use time, not only when parsing the manifest. Defend against symlinked sources, parent-directory swaps, platform path aliases, and archive members that change meaning between validation and extraction.
- Archive extraction must reject traversal, unsafe links, and unsupported member types before writing, and enforce entry, byte, and expansion limits without materializing an unbounded member list.
- Extra path grants are runtime access, not durable workspace content. Snapshots and `persist_workspace()` include the workspace root, not arbitrary granted paths.
- Credentials for mounts or providers must remain in the owning adapter and must not appear in generated shell commands, model-visible errors, logs, or serialized sandbox state.

## Provider and Error Boundary

- Normalize backend failures to sandbox errors without discarding provider details needed for diagnosis. Preserve explicit retryability instead of inferring it later from a message string.
- Keep portable sandbox paths separate from host filesystem paths and provider identifiers. Conversion belongs in the backend or materialization boundary, not in agent-facing tools.
- Temporary clones, mounts, sinks, and dependency resources need failure cleanup during partial startup as well as normal shutdown.
- Capability tools should report bounded output and preserve provider exit status or structured error data without exposing private runtime metadata to the model.

## Review Checklist

1. Name the owner of every live session, provider client, mount, process, capability, and temporary resource.
2. Test injected, resumed, explicit-state, snapshot-seeded, and fresh-session paths separately.
3. Verify handoffs, duplicate agent names, interruption resume, and cleanup failure preserve the intended session mapping.
4. Test host-path, symlink, traversal, archive-limit, and credential-redaction boundaries on applicable platforms.
5. Exercise the public `Runner` path so agent preparation, capability binding, persistence, and cleanup run together.

## Sources

- `docs/sandbox/guide.md`
- `docs/sandbox/clients.md`
- `src/agents/sandbox/runtime.py`
- `src/agents/sandbox/runtime_session_manager.py`
- `src/agents/sandbox/runtime_agent_preparation.py`
- `src/agents/sandbox/manifest.py`
- `src/agents/sandbox/materialization.py`
- `src/agents/sandbox/workspace_paths.py`
- `src/agents/sandbox/session/archive_extraction.py`
- `tests/sandbox/test_runtime.py`
- `tests/sandbox/test_runtime_agent_preparation.py`
- `tests/sandbox/test_session_state_roundtrip.py`
- `tests/sandbox/test_materialization.py`
- `tests/sandbox/test_extract.py`
