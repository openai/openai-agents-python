# Tool Identity and Routing

Use this reference for changes involving function-tool names, namespaces, provider wire names, lookup, approvals, tracing, MCP exposure, handoffs, or tool call IDs.

## Identity Layers

One tool can have several related identifiers. They are not interchangeable.

| Layer | Purpose | Canonical source |
|---|---|---|
| Public name | User- and model-facing tool name | `tool.name` |
| Explicit namespace | Distinguishes tools with the same public name | Tool namespace metadata |
| Qualified or dispatch name | Routes a model call to the intended tool | `namespace.name` when a namespace exists |
| Lookup key | Collision-free internal identity | `bare`, `namespaced`, or `deferred_top_level` tuple |
| Approval keys | Matches approval decisions to the intended tool | Canonical qualified and permitted alias keys |
| Trace name | Human-readable tracing label | Explicit trace name or public name |
| Call ID | Identifies one invocation, not the tool definition | Provider-supplied string |

Do not collapse these layers into one string or introduce local rules that only one caller uses.

## Canonical Helpers

Use `src/agents/_tool_identity.py` as the single implementation layer. Important helpers include:

- `get_function_tool_lookup_key_for_tool()` and `get_function_tool_lookup_key_for_call()` for canonical lookup identity.
- `get_function_tool_dispatch_name()` and `get_function_tool_qualified_name()` for routing and display surfaces that require qualification.
- `get_function_tool_approval_keys()` for approval matching.
- `get_function_tool_trace_name()` and `get_tool_call_trace_name()` for trace labels.
- `validate_function_tool_lookup_configuration()` and `build_function_tool_lookup_map()` for collision detection and dispatch maps.
- `normalize_tool_call_for_function_tool()` when provider payloads must be normalized for a selected tool.

If a proposed change bypasses these helpers, first prove that the target surface has intentionally different semantics.

## MCP and Handoff Rules

- `include_server_in_tool_names` is opt-in. Server-prefixed MCP names affect the model-exposed collision-safe name; they do not rename the original tool on the MCP server.
- Reserved names and enabled handoff names participate in collision avoidance only on the paths that expose generated model-facing names.
- `Handoff.default_tool_name()` is the source of default handoff tool names. Keep Realtime and non-Realtime handoff conversion aligned with it.
- Do not forward a naming option through a path where the downstream helper does not consult it and then describe the change as runtime behavior. Trace the complete caller-to-dispatch path first.

## Deferred Tool Search Rules

- A top-level `FunctionTool` with `defer_loading=True` and no explicit namespace uses the synthetic lookup key `("deferred_top_level", tool.name)`.
- The Responses wire shape for a loaded deferred top-level tool can look like `namespace == name`. Treat that namespace as reserved for the synthetic deferred tool-search path, not as a normal explicit namespace.
- `tool_namespace()` must reject an explicit namespace that equals the inner tool name. Otherwise a normal namespaced tool and a deferred top-level tool would have the same wire shape.
- Preserve the synthetic namespace on approval, interruption, tracing, and `ToolContext` surfaces when it identifies the model call, but dispatch the actual local tool through the deferred lookup key and strip the synthetic namespace before invoking the tool.
- Permanent approvals for deferred top-level tools should key by `deferred_top_level:<name>`. A bare-name approval alias is allowed only when no visible bare sibling can make that alias ambiguous.

## Tool Call ID Rules

- Preserve provider-supplied string call IDs across call items, approvals, outputs, retries, and serialized state.
- Do not coerce arbitrary values with `str(...)`. Canonical extractors return a call ID only when the source value is already a string.
- Do not use a call ID as a tool-definition identity or a tool name as an invocation identity.
- When a provider omits a stable identifier, use an existing fingerprint or dedupe policy for that item type instead of inventing a cross-provider ID contract.

## Review Checklist

1. Identify every identifier layer affected by the change.
2. Trace the actual runtime path from model-visible name to lookup, approval, invocation, output, and trace metadata.
3. Compare adjacent canonical helpers before adding conversion or fallback behavior.
4. Test collisions between bare, namespaced, deferred, MCP, local function, and handoff tools when applicable.
5. Require a regression test that fails on the base and proves the model-visible or dispatch behavior, not only an intermediate argument value.

## Sources

- `src/agents/_tool_identity.py`
- `src/agents/agent.py`
- `src/agents/mcp/`
- `src/agents/handoffs/__init__.py`
- `src/agents/run_internal/tool_execution.py`
- `src/agents/run_state.py`
