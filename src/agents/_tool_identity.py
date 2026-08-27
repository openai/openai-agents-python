from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from typing_extensions import Required, TypedDict

from . import _debug
from .exceptions import UserError
from .logger import logger

BareFunctionToolLookupKey = tuple[Literal["bare"], str]
NamespacedFunctionToolLookupKey = tuple[Literal["namespaced"], str, str]
DeferredTopLevelFunctionToolLookupKey = tuple[Literal["deferred_top_level"], str]
FunctionToolLookupKey = (
    BareFunctionToolLookupKey
    | NamespacedFunctionToolLookupKey
    | DeferredTopLevelFunctionToolLookupKey
)
HostedMCPApprovalIdentity = tuple[Literal["hosted_mcp"], str, str]
HostedMCPApprovalCallIdentity = tuple[Literal["hosted_mcp_call"], str]
HostedMCPApprovalQueryIdentity = tuple[Literal["hosted_mcp_query"], str, str]
HostedMCPApprovalKey = (
    HostedMCPApprovalIdentity | HostedMCPApprovalCallIdentity | HostedMCPApprovalQueryIdentity
)
NamedToolLookupKey = FunctionToolLookupKey | str


@dataclass(frozen=True)
class HostedMCPApprovalRequestIdentity:
    """Validated identity fields from a hosted MCP approval request."""

    request_id: str | None
    server_label: str | None
    tool_name: str | None

    @property
    def approval_identity(self) -> HostedMCPApprovalIdentity | None:
        """Return the persistent identity when all required fields are available."""
        if self.server_label is None or self.tool_name is None:
            return None
        return ("hosted_mcp", self.server_label, self.tool_name)


def validate_function_tool_fallback_name(name: str) -> str:
    """Return an API-safe generated tool name or require an explicit override."""
    if 1 <= len(name) <= 64 and all(
        char.isascii() and (char.isalnum() or char in {"_", "-"}) for char in name
    ):
        return name
    raise UserError(
        f"Cannot derive a function tool name from callable class {name!r}. Generated names must "
        "contain only ASCII letters, digits, underscores, or hyphens and be at most 64 "
        "characters. Pass name_override to function_tool()."
    )


class SerializedFunctionToolLookupKey(TypedDict, total=False):
    """Serialized representation of a function-tool lookup key."""

    kind: Required[Literal["bare", "namespaced", "deferred_top_level"]]
    name: Required[str]
    namespace: str


def get_mapping_or_attr(value: Any, key: str) -> Any:
    """Read a key from either a mapping or object attribute."""
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _non_empty_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def get_hosted_mcp_approval_request_identity(
    value: Any,
) -> HostedMCPApprovalRequestIdentity | None:
    """Return strictly validated identity fields for a hosted MCP approval request."""
    raw_item = get_mapping_or_attr(value, "raw_item")
    if raw_item is None:
        raw_item = value

    raw_type = get_mapping_or_attr(raw_item, "type")
    if raw_type == "mcp_approval_request":
        request = raw_item
        request_id = _non_empty_string(get_mapping_or_attr(request, "id"))
        tool_name = _non_empty_string(get_mapping_or_attr(request, "name"))
    elif raw_type == "hosted_tool_call":
        request = get_mapping_or_attr(raw_item, "provider_data")
        if get_mapping_or_attr(request, "type") != "mcp_approval_request":
            return None

        provider_request_id = get_mapping_or_attr(request, "id")
        if provider_request_id is None:
            request_id = _non_empty_string(get_mapping_or_attr(raw_item, "call_id"))
            if request_id is None:
                request_id = _non_empty_string(get_mapping_or_attr(raw_item, "id"))
        else:
            request_id = _non_empty_string(provider_request_id)
        tool_name = _non_empty_string(get_mapping_or_attr(request, "name"))
        if tool_name is None:
            tool_name = _non_empty_string(get_mapping_or_attr(raw_item, "name"))
    else:
        return None

    return HostedMCPApprovalRequestIdentity(
        request_id=request_id,
        server_label=_non_empty_string(get_mapping_or_attr(request, "server_label")),
        tool_name=tool_name,
    )


def get_tool_approval_item_call_id(value: Any) -> str | None:
    """Return the canonical call ID for a tool approval item."""
    hosted_request = get_hosted_mcp_approval_request_identity(value)
    if hosted_request is not None:
        return hosted_request.request_id

    raw_item = get_mapping_or_attr(value, "raw_item")
    if raw_item is None:
        raw_item = value
    call_id = get_mapping_or_attr(raw_item, "call_id") or get_mapping_or_attr(raw_item, "id")
    return _non_empty_string(call_id)


def tool_qualified_name(name: str | None, namespace: str | None = None) -> str | None:
    """Return `namespace.name` when a namespace exists, otherwise `name`."""
    if not isinstance(name, str) or not name:
        return None
    if isinstance(namespace, str) and namespace:
        return f"{namespace}.{name}"
    return name


def tool_trace_name(name: str | None, namespace: str | None = None) -> str | None:
    """Return a display-friendly tool name, collapsing synthetic deferred namespaces."""
    if is_reserved_synthetic_tool_namespace(name, namespace):
        return name
    return tool_qualified_name(name, namespace)


def is_reserved_synthetic_tool_namespace(name: str | None, namespace: str | None) -> bool:
    """Return True when a namespace matches the reserved deferred top-level wire shape."""
    return (
        isinstance(name, str)
        and bool(name)
        and isinstance(namespace, str)
        and bool(namespace)
        and namespace == name
    )


def get_tool_call_namespace(tool_call: Any) -> str | None:
    """Extract an optional namespace from a tool call payload."""
    namespace = get_mapping_or_attr(tool_call, "namespace")
    return namespace if isinstance(namespace, str) and namespace else None


def get_tool_call_name(tool_call: Any) -> str | None:
    """Extract a tool name from a tool call payload."""
    name = get_mapping_or_attr(tool_call, "name")
    return name if isinstance(name, str) and name else None


def get_tool_call_qualified_name(tool_call: Any) -> str | None:
    """Return the qualified name for a tool call payload."""
    return tool_qualified_name(
        get_tool_call_name(tool_call),
        get_tool_call_namespace(tool_call),
    )


def get_function_tool_lookup_key(
    tool_name: str | None,
    tool_namespace: str | None = None,
) -> FunctionToolLookupKey | None:
    """Return the collision-free lookup key for a function tool name/namespace pair."""
    if not isinstance(tool_name, str) or not tool_name:
        return None
    if is_reserved_synthetic_tool_namespace(tool_name, tool_namespace):
        return ("deferred_top_level", tool_name)
    if isinstance(tool_namespace, str) and tool_namespace:
        return ("namespaced", tool_namespace, tool_name)
    return ("bare", tool_name)


def get_function_tool_lookup_key_for_call(tool_call: Any) -> FunctionToolLookupKey | None:
    """Return the collision-free lookup key for a function tool call payload."""
    return get_function_tool_lookup_key(
        get_tool_call_name(tool_call),
        get_tool_call_namespace(tool_call),
    )


def get_function_tool_lookup_key_for_tool(tool: Any) -> FunctionToolLookupKey | None:
    """Return the canonical lookup key for a function tool definition."""
    tool_name = get_function_tool_public_name(tool)
    if tool_name is None:
        return None
    if is_deferred_top_level_function_tool(tool):
        return ("deferred_top_level", tool_name)
    return get_function_tool_lookup_key(tool_name, get_explicit_function_tool_namespace(tool))


def serialize_function_tool_lookup_key(
    lookup_key: FunctionToolLookupKey | None,
) -> SerializedFunctionToolLookupKey | None:
    """Serialize a function-tool lookup key into a JSON-friendly mapping."""
    if lookup_key is None:
        return None

    kind = lookup_key[0]
    if kind == "bare":
        return {"kind": "bare", "name": lookup_key[1]}
    if kind == "namespaced":
        namespaced_lookup_key = cast(NamespacedFunctionToolLookupKey, lookup_key)
        return {
            "kind": "namespaced",
            "namespace": namespaced_lookup_key[1],
            "name": namespaced_lookup_key[2],
        }
    return {"kind": "deferred_top_level", "name": lookup_key[1]}


def deserialize_function_tool_lookup_key(data: Any) -> FunctionToolLookupKey | None:
    """Deserialize a persisted function-tool lookup key mapping."""
    if not isinstance(data, dict):
        return None

    kind = data.get("kind")
    name = data.get("name")
    if not isinstance(kind, str) or not isinstance(name, str) or not name:
        return None

    if kind == "bare":
        return ("bare", name)
    if kind == "deferred_top_level":
        return ("deferred_top_level", name)
    if kind == "namespaced":
        namespace = data.get("namespace")
        if isinstance(namespace, str) and namespace:
            return ("namespaced", namespace, name)
    return None


def get_tool_call_trace_name(tool_call: Any) -> str | None:
    """Return the trace display name for a tool call payload."""
    return tool_trace_name(
        get_tool_call_name(tool_call),
        get_tool_call_namespace(tool_call),
    )


def get_tool_trace_name_for_tool(tool: Any) -> str | None:
    """Return the trace display name for a tool definition."""
    trace_name = getattr(tool, "trace_name", None)
    if isinstance(trace_name, str) and trace_name:
        return trace_name

    tool_name = getattr(tool, "name", None)
    return tool_name if isinstance(tool_name, str) and tool_name else None


def _remove_tool_call_namespace(tool_call: Any) -> Any:
    """Return a shallow copy of the tool call without its namespace field."""
    if isinstance(tool_call, dict):
        normalized_tool_call = dict(tool_call)
        normalized_tool_call.pop("namespace", None)
        return normalized_tool_call

    model_dump = getattr(tool_call, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(exclude_unset=True)
        if isinstance(payload, dict):
            payload.pop("namespace", None)
            try:
                return type(tool_call)(**payload)
            except Exception:
                return payload

    return tool_call


def restore_tool_call_routing_identity(
    tool_call: Any,
    lookup_key: FunctionToolLookupKey | None,
) -> Any:
    """Fill an absent call namespace from a persisted lookup key."""
    if lookup_key is None or get_tool_call_name(tool_call) != lookup_key[-1]:
        return tool_call
    if get_tool_call_namespace(tool_call) is not None or lookup_key[0] == "bare":
        return tool_call

    namespace = lookup_key[1]
    if isinstance(tool_call, dict):
        restored = dict(tool_call)
        restored["namespace"] = namespace
        return restored

    model_dump = getattr(tool_call, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(exclude_unset=True)
        if isinstance(payload, dict):
            payload["namespace"] = namespace
            try:
                return type(tool_call)(**payload)
            except Exception:
                return payload
    return tool_call


def has_function_tool_shape(tool: Any) -> bool:
    """Return True when the object looks like a FunctionTool instance."""
    return callable(getattr(tool, "on_invoke_tool", None)) and isinstance(
        getattr(tool, "params_json_schema", None), dict
    )


def get_function_tool_public_name(tool: Any) -> str | None:
    """Return the public name exposed for a function tool."""
    if not has_function_tool_shape(tool):
        return None
    tool_name = getattr(tool, "name", None)
    return tool_name if isinstance(tool_name, str) and tool_name else None


def get_function_tool_namespace(tool: Any) -> str | None:
    """Return the explicit namespace for a function tool, if any."""
    return get_explicit_function_tool_namespace(tool)


def get_explicit_function_tool_namespace(tool: Any) -> str | None:
    """Return only explicitly attached namespace metadata for a function tool."""
    explicit_namespace = getattr(tool, "_tool_namespace", None)
    if isinstance(explicit_namespace, str) and explicit_namespace:
        return explicit_namespace
    return None


def get_function_tool_namespace_description(tool: Any) -> str | None:
    """Return the namespace description attached to a function tool, if any."""
    description = getattr(tool, "_tool_namespace_description", None)
    return description if isinstance(description, str) and description else None


def is_deferred_top_level_function_tool(tool: Any) -> bool:
    """Return True when the tool is deferred-loading without an explicit namespace."""
    return (
        bool(getattr(tool, "defer_loading", False))
        and get_explicit_function_tool_namespace(tool) is None
        and get_function_tool_public_name(tool) is not None
    )


def get_function_tool_dispatch_name(tool: Any) -> str | None:
    """Return the canonical dispatch key for a function tool."""
    tool_name = get_function_tool_public_name(tool)
    if tool_name is None:
        return None
    return tool_qualified_name(tool_name, get_explicit_function_tool_namespace(tool))


def get_function_tool_lookup_keys(tool: Any) -> tuple[FunctionToolLookupKey, ...]:
    """Return all lookup keys that should resolve this function tool."""
    tool_name = get_function_tool_public_name(tool)
    if tool_name is None:
        return ()

    lookup_keys: list[FunctionToolLookupKey] = []
    dispatch_key = get_function_tool_lookup_key(
        tool_name,
        get_explicit_function_tool_namespace(tool),
    )
    if dispatch_key is not None and not is_deferred_top_level_function_tool(tool):
        lookup_keys.append(dispatch_key)

    synthetic_lookup_key = get_deferred_top_level_function_tool_lookup_key(tool)
    if synthetic_lookup_key is not None and synthetic_lookup_key not in lookup_keys:
        lookup_keys.append(synthetic_lookup_key)

    return tuple(lookup_keys)


def should_allow_bare_name_approval_alias(tool: Any, all_tools: Sequence[Any]) -> bool:
    """Allow bare-name approval aliases only for deferred top-level tools without visible peers."""
    tool_name = get_function_tool_public_name(tool)
    if tool_name is None or not is_deferred_top_level_function_tool(tool):
        return False

    for candidate in all_tools:
        if candidate is tool or get_function_tool_public_name(candidate) != tool_name:
            continue
        if get_explicit_function_tool_namespace(candidate) is not None:
            continue
        if bool(getattr(candidate, "defer_loading", False)):
            continue
        return False

    return True


def get_deferred_top_level_function_tool_lookup_key(
    tool: Any,
) -> DeferredTopLevelFunctionToolLookupKey | None:
    """Return the synthetic lookup key used for deferred top-level tool calls."""
    tool_name = get_function_tool_public_name(tool)
    if tool_name is None or not is_deferred_top_level_function_tool(tool):
        return None
    return ("deferred_top_level", tool_name)


def validate_function_tool_namespace_shape(
    tool_name: str | None,
    tool_namespace: str | None,
) -> None:
    """Reject reserved namespace shapes that collide with deferred top-level tool calls."""
    if not is_reserved_synthetic_tool_namespace(tool_name, tool_namespace):
        return

    reserved_key = tool_qualified_name(tool_name, tool_namespace) or tool_name or "unknown_tool"
    raise UserError(
        "Responses tool-search reserves the synthetic namespace "
        f"`{reserved_key}` for deferred top-level function tools. "
        "Rename the namespace or tool name to avoid ambiguous dispatch."
    )


def _format_tool_name_collision_message(
    lookup_key: BareFunctionToolLookupKey,
    entries: Sequence[tuple[str, int, Any]],
) -> str:
    """Build a detailed diagnostic for errors or unredacted warnings."""
    derived_owners: dict[str, str] = {}
    for entry_type, _, entry in entries:
        identity_attribute = (
            "_agent_tool_default_identity" if entry_type == "tool" else "_default_tool_identity"
        )
        default_identity = getattr(entry, identity_attribute, None)
        if not (
            isinstance(default_identity, tuple)
            and len(default_identity) == 2
            and all(isinstance(value, str) for value in default_identity)
        ):
            continue
        agent_name, derived_tool_name = default_identity
        current_tool_name = (
            get_function_tool_public_name(entry)
            if entry_type == "tool"
            else getattr(entry, "tool_name", None)
        )
        if current_tool_name == derived_tool_name:
            override_parameter = "tool_name" if entry_type == "tool" else "tool_name_override"
            derived_owners.setdefault(agent_name, override_parameter)

    if len(entries) == 2 and len(derived_owners) == 2:
        (
            (prior_agent_name, prior_override_parameter),
            (agent_name, override_parameter),
        ) = list(derived_owners.items())[:2]
        if override_parameter == prior_override_parameter == "tool_name":
            configuration_type = "agent tool"
            name_label = "tool name"
            override_instruction = "`tool_name=`"
        elif override_parameter == prior_override_parameter == "tool_name_override":
            configuration_type = "handoff"
            name_label = "handoff tool name"
            override_instruction = "`tool_name_override=`"
        else:
            configuration_type = "agent routing"
            name_label = "tool name"
            override_instruction = "`tool_name=` or `tool_name_override=`"
        return (
            f"Ambiguous {configuration_type} configuration: agents "
            f"{prior_agent_name!r} and {agent_name!r} both derive the {name_label} "
            f"`{lookup_key[1]}`. Pass an explicit {override_instruction} to one of them."
        )

    entry_types = {entry_type for entry_type, _, _ in entries}
    if entry_types == {"tool"}:
        return (
            "Ambiguous function tool configuration: the tool name "
            f"`{lookup_key[1]}` is used by multiple tools. Assign a unique routed name "
            "to every colliding function tool with `name_override=`, `tool_name=`, or "
            "a namespace."
        )
    if entry_types == {"handoff"}:
        return (
            "Ambiguous handoff configuration: the handoff tool name "
            f"`{lookup_key[1]}` is used by multiple handoffs. Pass a unique "
            "`tool_name_override=` to each handoff."
        )
    return (
        "Ambiguous tool routing configuration: the tool name "
        f"`{lookup_key[1]}` is used by both a function tool and a handoff. "
        "Assign a unique routed name to every colliding function tool and handoff "
        "with `name_override=`, `tool_name=`, `tool_name_override=`, or a namespace."
    )


def resolve_tool_name_collisions(
    tools: Sequence[Any],
    handoffs: Sequence[Any] = (),
    *,
    collision_policy: Literal["warn", "error"],
) -> tuple[list[Any], list[Any]]:
    """Resolve bare function-tool and handoff name collisions before model exposure."""
    validate_function_tool_lookup_configuration(tools)

    owners: dict[BareFunctionToolLookupKey, list[tuple[str, int, Any]]] = {}
    for index, tool in enumerate(tools):
        lookup_key = get_function_tool_lookup_key_for_tool(tool)
        if lookup_key is not None and lookup_key[0] == "bare":
            owners.setdefault(lookup_key, []).append(("tool", index, tool))

    for index, handoff in enumerate(handoffs):
        tool_name = getattr(handoff, "tool_name", None)
        if isinstance(tool_name, str) and tool_name:
            owners.setdefault(("bare", tool_name), []).append(("handoff", index, handoff))

    retained_tool_indices = set(range(len(tools)))
    retained_handoff_indices = set(range(len(handoffs)))
    for lookup_key, entries in owners.items():
        if len(entries) < 2:
            continue

        if collision_policy == "error":
            raise UserError(_format_tool_name_collision_message(lookup_key, entries))
        if _debug.DONT_LOG_TOOL_DATA:
            logger.warning(
                "Tool name collision detected. Assign unique routed tool names or enable tool "
                "data logging for details."
            )
        else:
            logger.warning("%s", _format_tool_name_collision_message(lookup_key, entries))

        handoff_entries = [entry for entry in entries if entry[0] == "handoff"]
        winner = handoff_entries[-1] if handoff_entries else entries[-1]
        for entry_type, index, _ in entries:
            if (entry_type, index) == (winner[0], winner[1]):
                continue
            if entry_type == "tool":
                retained_tool_indices.discard(index)
            else:
                retained_handoff_indices.discard(index)

    return (
        [tool for index, tool in enumerate(tools) if index in retained_tool_indices],
        [handoff for index, handoff in enumerate(handoffs) if index in retained_handoff_indices],
    )


def validate_function_tool_lookup_configuration(tools: Sequence[Any]) -> None:
    """Reject function-tool combinations that are ambiguous on the Responses wire."""
    qualified_name_owners: dict[str, Any] = {}
    deferred_top_level_name_owners: dict[str, Any] = {}
    for tool in tools:
        tool_name = get_function_tool_public_name(tool)
        explicit_namespace = get_explicit_function_tool_namespace(tool)
        validate_function_tool_namespace_shape(tool_name, explicit_namespace)

        deferred_lookup_key = get_deferred_top_level_function_tool_lookup_key(tool)
        if deferred_lookup_key is not None:
            deferred_name = deferred_lookup_key[1]
            prior_deferred_owner = deferred_top_level_name_owners.get(deferred_name)
            if prior_deferred_owner is not None:
                raise UserError(
                    "Ambiguous function tool configuration: the deferred top-level tool name "
                    f"`{deferred_name}` is used by multiple tools. Rename one of the "
                    "deferred-loading top-level function tools to avoid ambiguous dispatch."
                )
            deferred_top_level_name_owners[deferred_name] = tool

        qualified_name = get_function_tool_qualified_name(tool)
        if qualified_name is None:
            continue

        prior_owner = qualified_name_owners.get(qualified_name)
        if prior_owner is None:
            qualified_name_owners[qualified_name] = tool
            continue

        prior_namespace = get_explicit_function_tool_namespace(prior_owner)
        if explicit_namespace is None and prior_namespace is None:
            continue

        raise UserError(
            "Ambiguous function tool configuration: the qualified name "
            f"`{qualified_name}` is used by multiple tools. "
            "Rename the namespace-wrapped function or dotted top-level tool to avoid "
            "ambiguous dispatch."
        )


def build_function_tool_lookup_map(tools: Sequence[Any]) -> dict[FunctionToolLookupKey, Any]:
    """Build a function-tool lookup map using last-wins precedence."""
    validate_function_tool_lookup_configuration(tools)
    tool_map: dict[FunctionToolLookupKey, Any] = {}
    for tool in tools:
        for lookup_key in get_function_tool_lookup_keys(tool):
            tool_map[lookup_key] = tool
    return tool_map


def get_function_tool_approval_keys(
    *,
    tool_name: str | None,
    tool_namespace: str | None = None,
    allow_bare_name_alias: bool = False,
    tool_lookup_key: FunctionToolLookupKey | None = None,
    prefer_legacy_same_name_namespace: bool = False,
    include_legacy_deferred_key: bool = False,
) -> tuple[str, ...]:
    """Return approval keys for a tool name/namespace pair."""
    if not isinstance(tool_name, str) or not tool_name:
        return ()

    approval_keys: list[str] = []
    lookup_key = tool_lookup_key
    if lookup_key is None and not (
        prefer_legacy_same_name_namespace
        and is_reserved_synthetic_tool_namespace(tool_name, tool_namespace)
    ):
        lookup_key = get_function_tool_lookup_key(tool_name, tool_namespace)

    qualified_name = tool_qualified_name(tool_name, tool_namespace)

    if allow_bare_name_alias and tool_name not in approval_keys:
        approval_keys.append(tool_name)

    if lookup_key is not None:
        if lookup_key[0] == "namespaced":
            key = tool_qualified_name(lookup_key[2], lookup_key[1])
        elif lookup_key[0] == "deferred_top_level":
            key = f"deferred_top_level:{lookup_key[1]}"
        else:
            key = lookup_key[1]
        if key is not None and key not in approval_keys:
            approval_keys.append(key)
        if (
            include_legacy_deferred_key
            and lookup_key[0] == "deferred_top_level"
            and qualified_name is not None
            and qualified_name not in approval_keys
        ):
            approval_keys.append(qualified_name)
    elif qualified_name is not None and qualified_name not in approval_keys:
        approval_keys.append(qualified_name)

    if not approval_keys:
        approval_keys.append(tool_name)

    return tuple(approval_keys)


def normalize_tool_call_for_function_tool(tool_call: Any, tool: Any) -> Any:
    """Strip synthetic namespaces from deferred top-level tool calls."""
    tool_name = get_function_tool_public_name(tool)
    if tool_name is None or not is_deferred_top_level_function_tool(tool):
        return tool_call

    if get_tool_call_name(tool_call) != tool_name:
        return tool_call

    if get_tool_call_namespace(tool_call) != tool_name:
        return tool_call

    return _remove_tool_call_namespace(tool_call)


def get_function_tool_qualified_name(tool: Any) -> str | None:
    """Return the qualified lookup key for a function tool."""
    return get_function_tool_dispatch_name(tool)


def get_function_tool_trace_name(tool: Any) -> str | None:
    """Return the trace display name for a function tool."""
    tool_name = get_function_tool_public_name(tool)
    if tool_name is None:
        return None
    return tool_trace_name(tool_name, get_function_tool_namespace(tool))
