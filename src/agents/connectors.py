from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from openai.types.responses.tool_param import Mcp

from .exceptions import UserError
from .mcp import MCPServer, MCPServerSse, MCPServerStdio, MCPServerStreamableHttp
from .mcp.server import RequireApprovalSetting
from .tool import HostedMCPTool, MCPToolApprovalFunction, Tool

ConnectorPolicyLabel = Literal[
    "read_only",
    "write",
    "destructive",
    "external_send",
    "network",
    "secret_access",
    "local_execution",
    "sandbox_required",
]
"""Coarse policy labels callers can use to route connector approval and sandbox decisions."""


HostedConnectorAuthorization = (
    str | Mapping[str, str] | Callable[[str, str, Mapping[str, Any]], str | None]
)
"""Authorization source for hosted connectors loaded from a package app manifest."""


@dataclass(frozen=True)
class ConnectorComponents:
    """Resolved runtime surfaces exposed by a connector package."""

    tools: tuple[Tool, ...] = ()
    mcp_servers: tuple[MCPServer, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    policy_labels: tuple[ConnectorPolicyLabel, ...] = ()


@dataclass
class Connector:
    """A package-level connector surface for Agents SDK.

    Connectors intentionally compose existing SDK primitives instead of introducing a new runtime:
    local and hosted tools continue to flow through `Tool`, while local MCP servers continue to flow
    through `MCPServer`.
    """

    name: str
    description: str | None = None
    tools: list[Tool] = field(default_factory=list)
    mcp_servers: list[MCPServer] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    policy_labels: set[ConnectorPolicyLabel] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise TypeError(f"Connector name must be a string, got {type(self.name).__name__}")
        if self.description is not None and not isinstance(self.description, str):
            raise TypeError(
                "Connector description must be a string or None, "
                f"got {type(self.description).__name__}"
            )
        if not isinstance(self.tools, list):
            raise TypeError(f"Connector tools must be a list, got {type(self.tools).__name__}")
        if not isinstance(self.mcp_servers, list):
            raise TypeError(
                f"Connector mcp_servers must be a list, got {type(self.mcp_servers).__name__}"
            )
        if not isinstance(self.metadata, dict):
            raise TypeError(
                f"Connector metadata must be a dict, got {type(self.metadata).__name__}"
            )
        if not isinstance(self.policy_labels, set):
            raise TypeError(
                f"Connector policy_labels must be a set, got {type(self.policy_labels).__name__}"
            )

    def components(self) -> ConnectorComponents:
        """Return immutable runtime surfaces for callers that want explicit composition."""
        return ConnectorComponents(
            tools=tuple(self.tools),
            mcp_servers=tuple(self.mcp_servers),
            metadata=self.metadata,
            policy_labels=tuple(sorted(self.policy_labels)),
        )

    @classmethod
    def from_tools(
        cls,
        name: str,
        tools: Iterable[Tool],
        *,
        description: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        policy_labels: Iterable[ConnectorPolicyLabel] | None = None,
    ) -> Connector:
        """Create a connector from SDK tools."""
        return cls(
            name=name,
            description=description,
            tools=list(tools),
            metadata=dict(metadata or {}),
            policy_labels=set(policy_labels or ()),
        )

    @classmethod
    def from_mcp_server(
        cls,
        name: str,
        server: MCPServer,
        *,
        description: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        policy_labels: Iterable[ConnectorPolicyLabel] | None = None,
    ) -> Connector:
        """Create a connector from a local MCP server instance."""
        return cls.from_mcp_servers(
            name,
            [server],
            description=description,
            metadata=metadata,
            policy_labels=policy_labels,
        )

    @classmethod
    def from_mcp_servers(
        cls,
        name: str,
        servers: Iterable[MCPServer],
        *,
        description: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        policy_labels: Iterable[ConnectorPolicyLabel] | None = None,
    ) -> Connector:
        """Create a connector from local MCP server instances."""
        return cls(
            name=name,
            description=description,
            mcp_servers=list(servers),
            metadata=dict(metadata or {}),
            policy_labels=set(policy_labels or ()),
        )

    @classmethod
    def from_hosted_connector(
        cls,
        name: str,
        *,
        connector_id: str,
        authorization: str,
        server_label: str | None = None,
        allowed_tools: list[str] | None = None,
        require_approval: RequireApprovalSetting = None,
        defer_loading: bool = False,
        on_approval_request: MCPToolApprovalFunction | None = None,
        tool_config: Mapping[str, Any] | None = None,
        description: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        policy_labels: Iterable[ConnectorPolicyLabel] | None = None,
    ) -> Connector:
        """Create a connector for an OpenAI-hosted connector exposed through hosted MCP."""
        config = _build_hosted_mcp_tool_config(
            server_label=server_label or name,
            connector_id=connector_id,
            authorization=authorization,
            allowed_tools=allowed_tools,
            require_approval=require_approval,
            defer_loading=defer_loading,
            extra_config=tool_config,
        )
        return cls.from_tools(
            name,
            [HostedMCPTool(tool_config=config, on_approval_request=on_approval_request)],
            description=description,
            metadata={
                **dict(metadata or {}),
                "hosted_connector": {
                    "connector_id": connector_id,
                    "server_label": server_label or name,
                },
            },
            policy_labels=set(policy_labels or ()) | {"network"},
        )

    @classmethod
    def from_hosted_mcp(
        cls,
        name: str,
        *,
        server_url: str,
        server_label: str | None = None,
        allowed_tools: list[str] | None = None,
        require_approval: RequireApprovalSetting = None,
        defer_loading: bool = False,
        on_approval_request: MCPToolApprovalFunction | None = None,
        tool_config: Mapping[str, Any] | None = None,
        description: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        policy_labels: Iterable[ConnectorPolicyLabel] | None = None,
    ) -> Connector:
        """Create a connector for a remote MCP server executed by the hosted Responses tool."""
        config = _build_hosted_mcp_tool_config(
            server_label=server_label or name,
            server_url=server_url,
            allowed_tools=allowed_tools,
            require_approval=require_approval,
            defer_loading=defer_loading,
            extra_config=tool_config,
        )
        return cls.from_tools(
            name,
            [HostedMCPTool(tool_config=config, on_approval_request=on_approval_request)],
            description=description,
            metadata={
                **dict(metadata or {}),
                "hosted_mcp": {
                    "server_url": server_url,
                    "server_label": server_label or name,
                },
            },
            policy_labels=set(policy_labels or ()) | {"network"},
        )

    @classmethod
    def from_package(
        cls,
        path: str | Path,
        *,
        authorization: HostedConnectorAuthorization | None = None,
        hosted_mcp_require_approval: RequireApprovalSetting = None,
    ) -> Connector:
        """Load a connector from a shared Codex plugin package layout.

        The initial package bridge supports `.codex-plugin/plugin.json`, `.mcp.json`, and optional
        `.app.json` hosted connector IDs. App entries become hosted MCP tools only when an
        authorization source is supplied.
        """
        package_root = Path(path).expanduser().resolve()
        manifest_path = package_root / ".codex-plugin" / "plugin.json"
        if not manifest_path.exists():
            raise UserError(f"Connector package manifest not found: {manifest_path}")

        manifest = _read_json_object(manifest_path)
        name = _expect_str(manifest.get("name"), "Connector package name")
        description = _optional_str(manifest.get("description"), "Connector package description")
        metadata: dict[str, Any] = {
            key: value
            for key, value in manifest.items()
            if key
            not in {
                "description",
                "mcpServers",
                "mcp_servers",
                "apps",
            }
        }

        mcp_servers, policy_labels = _load_manifest_mcp_servers(package_root, manifest)
        tools = _load_manifest_app_tools(
            package_root,
            manifest,
            authorization=authorization,
            require_approval=hosted_mcp_require_approval,
        )
        if tools:
            policy_labels.add("network")

        return cls(
            name=name,
            description=description,
            tools=tools,
            mcp_servers=mcp_servers,
            metadata=metadata,
            policy_labels=policy_labels,
        )


def _build_hosted_mcp_tool_config(
    *,
    server_label: str,
    server_url: str | None = None,
    connector_id: str | None = None,
    authorization: str | None = None,
    allowed_tools: list[str] | None = None,
    require_approval: RequireApprovalSetting = None,
    defer_loading: bool = False,
    extra_config: Mapping[str, Any] | None = None,
) -> Mcp:
    config: dict[str, Any] = {"type": "mcp", "server_label": server_label}
    if server_url is not None:
        config["server_url"] = server_url
    if connector_id is not None:
        config["connector_id"] = connector_id
    if authorization is not None:
        config["authorization"] = authorization
    if allowed_tools is not None:
        config["allowed_tools"] = allowed_tools
    if require_approval is not None:
        config["require_approval"] = require_approval
    if defer_loading:
        config["defer_loading"] = True
    if extra_config:
        config.update(extra_config)
    return cast(Mcp, config)


def _load_manifest_mcp_servers(
    package_root: Path, manifest: Mapping[str, Any]
) -> tuple[list[MCPServer], set[ConnectorPolicyLabel]]:
    mcp_manifest_value = manifest.get("mcpServers") or manifest.get("mcp_servers")
    if mcp_manifest_value is None:
        return [], set()

    mcp_manifest_path = _resolve_package_path(package_root, mcp_manifest_value, "mcpServers")
    mcp_manifest = _read_json_object(mcp_manifest_path)
    server_configs = mcp_manifest.get("mcpServers") or mcp_manifest.get("mcp_servers")
    if not isinstance(server_configs, Mapping):
        raise UserError(f"MCP manifest must contain an object of servers: {mcp_manifest_path}")

    servers: list[MCPServer] = []
    policy_labels: set[ConnectorPolicyLabel] = set()
    for server_name, raw_config in server_configs.items():
        if not isinstance(server_name, str):
            raise UserError("MCP server names must be strings")
        if not isinstance(raw_config, Mapping):
            raise UserError(f"MCP server config for {server_name!r} must be an object")
        if raw_config.get("enabled") is False:
            continue
        server, server_policy_labels = _build_mcp_server(package_root, server_name, raw_config)
        servers.append(server)
        policy_labels.update(server_policy_labels)

    return servers, policy_labels


def _build_mcp_server(
    package_root: Path, server_name: str, config: Mapping[str, Any]
) -> tuple[MCPServer, set[ConnectorPolicyLabel]]:
    cache_tools_list = bool(config.get("cache_tools_list", False))
    client_session_timeout_seconds = _optional_float(
        config.get("client_session_timeout_seconds"), "client_session_timeout_seconds"
    )
    use_structured_content = bool(config.get("use_structured_content", False))
    max_retry_attempts = int(config.get("max_retry_attempts", 0))
    retry_backoff_seconds_base = float(config.get("retry_backoff_seconds_base", 1.0))
    require_approval = cast(RequireApprovalSetting, config.get("require_approval"))

    if "command" in config:
        params: dict[str, Any] = {"command": _expect_str(config["command"], "MCP command")}
        if "args" in config:
            params["args"] = _expect_str_list(config["args"], "MCP args")
        if "env" in config:
            params["env"] = _expect_str_map(config["env"], "MCP env")
        if "cwd" in config:
            params["cwd"] = _resolve_package_path(package_root, config["cwd"], "MCP cwd")
        for key in ("encoding", "encoding_error_handler"):
            if key in config:
                params[key] = _expect_str(config[key], f"MCP {key}")
        return (
            MCPServerStdio(
                cast(Any, params),
                cache_tools_list=cache_tools_list,
                name=server_name,
                client_session_timeout_seconds=client_session_timeout_seconds,
                use_structured_content=use_structured_content,
                max_retry_attempts=max_retry_attempts,
                retry_backoff_seconds_base=retry_backoff_seconds_base,
                require_approval=require_approval,
            ),
            {"local_execution"},
        )

    if "url" in config:
        params = {"url": _expect_str(config["url"], "MCP url")}
        for key in ("headers", "timeout", "sse_read_timeout"):
            if key in config:
                params[key] = config[key]
        transport = str(config.get("transport") or config.get("type") or "streamable_http")
        if transport == "sse":
            return (
                MCPServerSse(
                    cast(Any, params),
                    cache_tools_list=cache_tools_list,
                    name=server_name,
                    client_session_timeout_seconds=client_session_timeout_seconds,
                    use_structured_content=use_structured_content,
                    max_retry_attempts=max_retry_attempts,
                    retry_backoff_seconds_base=retry_backoff_seconds_base,
                    require_approval=require_approval,
                ),
                {"network"},
            )
        return (
            MCPServerStreamableHttp(
                cast(Any, params),
                cache_tools_list=cache_tools_list,
                name=server_name,
                client_session_timeout_seconds=client_session_timeout_seconds,
                use_structured_content=use_structured_content,
                max_retry_attempts=max_retry_attempts,
                retry_backoff_seconds_base=retry_backoff_seconds_base,
                require_approval=require_approval,
            ),
            {"network"},
        )

    raise UserError(f"MCP server config for {server_name!r} must include either 'command' or 'url'")


def _load_manifest_app_tools(
    package_root: Path,
    manifest: Mapping[str, Any],
    *,
    authorization: HostedConnectorAuthorization | None,
    require_approval: RequireApprovalSetting,
) -> list[Tool]:
    apps_manifest_value = manifest.get("apps")
    if apps_manifest_value is None:
        return []

    app_manifest_path = _resolve_package_path(package_root, apps_manifest_value, "apps")
    app_manifest = _read_json_object(app_manifest_path)
    apps = app_manifest.get("apps")
    if not isinstance(apps, Mapping):
        raise UserError(f"App manifest must contain an 'apps' object: {app_manifest_path}")

    tools: list[Tool] = []
    for app_name, raw_config in apps.items():
        if not isinstance(app_name, str):
            raise UserError("App names must be strings")
        if not isinstance(raw_config, Mapping):
            raise UserError(f"App config for {app_name!r} must be an object")
        connector_id = _expect_str(raw_config.get("id"), f"App id for {app_name!r}")
        resolved_authorization = _resolve_authorization(
            authorization, app_name, connector_id, raw_config
        )
        if resolved_authorization is None:
            continue
        connector = Connector.from_hosted_connector(
            app_name,
            connector_id=connector_id,
            authorization=resolved_authorization,
            server_label=app_name,
            require_approval=require_approval,
        )
        tools.extend(connector.tools)

    return tools


def _resolve_authorization(
    authorization: HostedConnectorAuthorization | None,
    app_name: str,
    connector_id: str,
    app_config: Mapping[str, Any],
) -> str | None:
    if authorization is None:
        return None
    if isinstance(authorization, str):
        return authorization
    if isinstance(authorization, Mapping):
        return authorization.get(app_name) or authorization.get(connector_id)
    return authorization(app_name, connector_id, app_config)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except OSError as exc:
        raise UserError(f"Unable to read connector package file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise UserError(f"Invalid connector package JSON: {path}") from exc
    if not isinstance(value, dict):
        raise UserError(f"Connector package JSON must be an object: {path}")
    return value


def _resolve_package_path(package_root: Path, value: Any, field_name: str) -> Path:
    path_value = _expect_str(value, f"{field_name} path")
    path = Path(path_value)
    if path.is_absolute():
        candidate = path.resolve()
    else:
        candidate = (package_root / path).resolve()
    if not _is_relative_to(candidate, package_root):
        raise UserError(f"{field_name} path must stay inside the connector package: {value}")
    return candidate


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _expect_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise UserError(f"{field_name} must be a non-empty string")
    return value


def _optional_str(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _expect_str(value, field_name)


def _expect_str_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise UserError(f"{field_name} must be a list of strings")
    return value


def _expect_str_map(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(map_value, str) for key, map_value in value.items()
    ):
        raise UserError(f"{field_name} must be an object of string values")
    return dict(value)


def _optional_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float):
        raise UserError(f"{field_name} must be a number")
    return float(value)
