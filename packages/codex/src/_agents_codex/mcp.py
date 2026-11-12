"""Selected MCP tools whose connection and execution belong to Core."""

from __future__ import annotations

import inspect as inspect
import json as json
import re as re
import sys as sys
from collections.abc import (
    Awaitable as Awaitable,
    Callable as Callable,
    Mapping as Mapping,
    Sequence as Sequence,
)
from dataclasses import dataclass as dataclass, field as field
from functools import wraps as _wraps
from traceback import clear_frames as _clear_frames
from typing import (
    TYPE_CHECKING as TYPE_CHECKING,
    Any as Any,
    ClassVar as ClassVar,
    Generic as Generic,
    Literal as Literal,
    NoReturn as _NoReturn,
    get_origin as get_origin,
)
from urllib.parse import unquote as unquote, urlsplit as urlsplit

from typing_extensions import NotRequired as NotRequired, TypedDict as TypedDict, TypeVar as TypeVar

from ._mcp_errors import _protect_mcp_methods
from ._mcp_names import (
    _project_name as _project_name,
    _project_tool_names as _project_tool_names,
)
from ._public import preserve_module_identity as _preserve_module_identity
from .tools import ToolCallInfo as ToolCallInfo, ToolRef as ToolRef

if TYPE_CHECKING:
    from .executor import Environment
    from .native_agents import ChildInvocation
    from .native_interception import Allow, NativeToolCall, ReplaceArguments, StopTurn
    from .turn import Turn

ContextT_co = TypeVar("ContextT_co", covariant=True, default=None)
ContextT_contra = TypeVar("ContextT_contra", contravariant=True, default=object)


class _McpValues(dict[str, str]):
    """An immutable capture that retains ordinary dataclass copy support."""

    __slots__ = ("_initialized",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        try:
            if getattr(self, "_initialized", False):
                self._immutable()
            super().__init__(*args, **kwargs)
            self._initialized = True
        finally:
            del self, args, kwargs

    def _immutable(self: object, *args: Any, **kwargs: Any) -> _NoReturn:
        del self, args, kwargs
        raise TypeError("MCP configuration values are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __copy__(self) -> _McpValues:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _McpValues:
        return self

    def __reduce__(self) -> tuple[Any, tuple[dict[str, str]]]:
        return type(self), (dict(self),)


def _protect_mcp_initializer(declaration: type[Any]) -> None:
    """Keep rejected credentials out of generated initializer traceback frames."""
    initialize = declaration.__init__

    @_wraps(initialize)
    def protected(self: Any, *args: Any, **kwargs: Any) -> None:
        try:
            initialize(self, *args, **kwargs)
        except BaseException as error:
            _clear_frames(error.__traceback__)
            object.__setattr__(error, "__context__", None)
            object.__setattr__(error, "__cause__", None)
            for name in ("env", "headers", "bearer_token", "access_token"):
                if name in self.__dict__:
                    object.__setattr__(self, name, None)
            raise
        finally:
            del self, args, kwargs

    declaration.__init__ = protected


class MCPResource(TypedDict):
    """Resource metadata returned by the native MCP transport."""

    uri: str
    name: str
    title: NotRequired[str]
    description: NotRequired[str]
    mimeType: NotRequired[str]
    size: NotRequired[int]
    annotations: NotRequired[dict[str, Any]]
    icons: NotRequired[list[dict[str, Any]]]
    _meta: NotRequired[dict[str, Any]]


class MCPResourceTemplate(TypedDict):
    """A URI template advertised by an MCP server."""

    uriTemplate: str
    name: str
    title: NotRequired[str]
    description: NotRequired[str]
    mimeType: NotRequired[str]
    annotations: NotRequired[dict[str, Any]]
    icons: NotRequired[list[dict[str, Any]]]
    _meta: NotRequired[dict[str, Any]]


class MCPTextResourceContents(TypedDict):
    """Text returned as data, without adding instructions or model context."""

    uri: str
    text: str
    mimeType: NotRequired[str]
    _meta: NotRequired[dict[str, Any]]


class MCPBlobResourceContents(TypedDict):
    """Binary resource data encoded as a base64 string by the transport."""

    uri: str
    blob: str
    mimeType: NotRequired[str]
    _meta: NotRequired[dict[str, Any]]


class MCPListResourcesResult(TypedDict):
    """One resource page. Pass nextCursor to request the next page."""

    resources: list[MCPResource]
    resultType: NotRequired[str]
    ttlMs: NotRequired[int]
    cacheScope: NotRequired[Literal["public", "private"]]
    nextCursor: NotRequired[str]
    _meta: NotRequired[dict[str, Any]]


class MCPListResourceTemplatesResult(TypedDict):
    """One resource-template page with a caller-owned continuation cursor."""

    resourceTemplates: list[MCPResourceTemplate]
    resultType: NotRequired[str]
    ttlMs: NotRequired[int]
    cacheScope: NotRequired[Literal["public", "private"]]
    nextCursor: NotRequired[str]
    _meta: NotRequired[dict[str, Any]]


class MCPReadResourceResult(TypedDict):
    """The native transport's resource result, preserving text and blob contents."""

    contents: list[MCPTextResourceContents | MCPBlobResourceContents]
    resultType: NotRequired[str]
    ttlMs: NotRequired[int]
    cacheScope: NotRequired[Literal["public", "private"]]
    _meta: NotRequired[dict[str, Any]]


class MCPPromptArgument(TypedDict):
    """An argument advertised by an MCP prompt."""

    name: str
    title: NotRequired[str]
    description: NotRequired[str]
    required: NotRequired[bool]


class MCPPrompt(TypedDict):
    """Prompt metadata returned in a complete native catalog."""

    name: str
    title: NotRequired[str]
    description: NotRequired[str]
    arguments: NotRequired[list[MCPPromptArgument]]
    icons: NotRequired[list[dict[str, Any]]]
    _meta: NotRequired[dict[str, Any]]


class _MCPPromptContentMetadata(TypedDict):
    annotations: NotRequired[dict[str, Any]]
    _meta: NotRequired[dict[str, Any]]


class MCPPromptTextContent(_MCPPromptContentMetadata):
    """Text returned as caller data."""

    type: Literal["text"]
    text: str


class MCPPromptImageContent(_MCPPromptContentMetadata):
    """An image whose data remains base64 encoded."""

    type: Literal["image"]
    data: str
    mimeType: str


class MCPPromptAudioContent(_MCPPromptContentMetadata):
    """Audio whose data remains base64 encoded."""

    type: Literal["audio"]
    data: str
    mimeType: str


class MCPPromptResourceContent(_MCPPromptContentMetadata):
    """Embedded text or blob resource contents, including nested metadata."""

    type: Literal["resource"]
    resource: MCPTextResourceContents | MCPBlobResourceContents


class MCPPromptResourceLink(MCPResource):
    """Resource metadata tagged as a prompt content link."""

    type: Literal["resource_link"]


MCPPromptContent = (
    MCPPromptTextContent
    | MCPPromptImageContent
    | MCPPromptAudioContent
    | MCPPromptResourceContent
    | MCPPromptResourceLink
)


class MCPPromptMessage(TypedDict):
    """A prompt message that the application chooses how to use."""

    role: Literal["user", "assistant"]
    content: MCPPromptContent


class MCPGetPromptResult(TypedDict):
    """Rendered prompt messages without automatic model-context insertion."""

    messages: list[MCPPromptMessage]
    description: NotRequired[str]
    _meta: NotRequired[dict[str, Any]]
    resultType: NotRequired[str]


@dataclass(frozen=True)
class MCPResultContext(Generic[ContextT_co]):
    """Application context and the actual native MCP call identity.

    Session supplies this value to a selected result transform. It does not grant
    permission to execute tools or change a native call's authority.
    """

    context: ContextT_co = field(repr=False)
    submission: Turn[Any] | ChildInvocation
    call_id: str
    server_name: str
    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict, repr=False, kw_only=True)


@dataclass(frozen=True)
class MCPCallContext(Generic[ContextT_co]):
    """One admitted native MCP before/approval call, with raw descriptor names.

    call.call_id is None if native supplied no provider call ID. It is never
    replaced with the outer or nested runtime ID. Neither this value nor the
    paired NativeToolCall grants permission or changes which tool runs.
    original_arguments and effective_arguments are detached normalized JSON
    mappings. Before receives the original values; approval receives any
    replacement values. Mutating a nested view never changes the admitted input.
    """

    context: ContextT_co = field(repr=False)
    submission: Turn[Any] | ChildInvocation
    call: ToolCallInfo
    server_name: str
    tool_name: str
    original_arguments: Mapping[str, Any] = field(default_factory=dict, repr=False, kw_only=True)
    effective_arguments: Mapping[str, Any] = field(default_factory=dict, repr=False, kw_only=True)


@dataclass(frozen=True)
class MCPMetadataContext(Generic[ContextT_co]):
    """One approved call's effective arguments, before native file substitution.

    Arguments are detached. The context grants no permission and contains no
    fabricated original arguments or provider IDs for generated-code calls.
    """

    context: ContextT_co = field(repr=False)
    submission: Turn[Any] | ChildInvocation
    call: ToolCallInfo
    server_name: str
    tool_name: str
    arguments: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class _MCP(Generic[ContextT_contra]):
    """Shared options; use StdioMCP or HttpMCP to declare a server.

    defer_loading=True admits native schema discovery for this server's
    selected leaves, when a direct-mode model supports it. Other servers keep
    their own presentation. CodeMode keeps its native direct/code exposure.
    This never changes acquisition timing or admits a deselected leaf.

    before receives MCPCallContext and the native input. Return Allow,
    ReplaceArguments or StopTurn. approval sees the effective NativeToolCall;
    False denies locally. before has a 30-second deadline, independent of
    transform_result_timeout. The latter defaults to 30 seconds when omitted.
    Approval is not a transform deadline. transform_result receives the
    separate MCPResultContext and an MCP result dictionary after the effect.
    extract_application_data receives a copy of the result and returns a JSON
    mapping retained by the SDK when native accepts the result.
    HTTP and WebSocket model providers support direct and generated-code
    replacement. Native continuation uses the corrected input and invalidates
    the changed provider item without changing the configured transport.
    StopTurn stops and joins only the owning root or child turn.
    """

    name: str
    tools: Sequence[str]
    approval_mode: Literal["native", "preapproved"] = "native"
    startup_timeout: float = 10
    execution_timeout: float = 60
    transform_result: (
        Callable[
            [MCPResultContext[ContextT_contra], dict[str, Any]],
            dict[str, Any] | Awaitable[dict[str, Any]],
        ]
        | None
    ) = field(default=None, repr=False)
    extract_application_data: (
        Callable[
            [MCPResultContext[ContextT_contra], dict[str, Any]],
            Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None],
        ]
        | None
    ) = field(default=None, repr=False)
    transform_result_timeout: float | None = None
    request_metadata: (
        Callable[
            [MCPMetadataContext[ContextT_contra]],
            Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None],
        ]
        | None
    ) = field(default=None, repr=False)
    before: (
        Callable[
            [MCPCallContext[ContextT_contra], NativeToolCall],
            Allow | StopTurn | ReplaceArguments | Awaitable[Allow | StopTurn | ReplaceArguments],
        ]
        | None
    ) = field(default=None, repr=False)
    approval: (
        Callable[
            [MCPCallContext[ContextT_contra], NativeToolCall],
            bool | StopTurn | Awaitable[bool | StopTurn],
        ]
        | None
    ) = field(default=None, repr=False)
    defer_loading: bool = False

    _name_tools: tuple[str, ...] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.request_metadata is not None and self.name == "codex_apps":
            raise ValueError(
                "MCP request_metadata requires an ordinary StdioMCP or HttpMCP declaration"
            )
        if not isinstance(self.defer_loading, bool):
            raise TypeError("MCP defer_loading must be a bool")
        if not isinstance(self.approval_mode, str):
            raise TypeError("approval_mode must be a string")
        if self.approval_mode not in ("native", "preapproved"):
            raise ValueError("approval_mode must be 'native' or 'preapproved'")
        if (
            not isinstance(self.name, str)
            or re.fullmatch(r"[a-zA-Z0-9_:@/.-]+", self.name) is None
            or self.name == "codex_apps"
            and type(self) not in (HostedVerificationMCP, ChatGPTAppsMCP)
        ):
            raise ValueError("MCP name must be an ordinary native server alias")
        for name in ("startup_timeout", "execution_timeout"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < value <= sys.float_info.max
            ):
                raise ValueError(f"{name} must be a positive finite number")
        for name in (
            "before",
            "transform_result",
            "extract_application_data",
            "approval",
            "request_metadata",
        ):
            callback = getattr(self, name)
            if callback is not None and not callable(callback):
                raise TypeError(f"MCP {name} must be callable")
        if self.transform_result_timeout is not None:
            if not self._has_result_callback:
                raise ValueError(
                    "transform_result_timeout requires transform_result or extract_application_data"
                )
            if (
                isinstance(self.transform_result_timeout, bool)
                or not isinstance(self.transform_result_timeout, (int, float))
                or not 0 < self.transform_result_timeout <= 3600
            ):
                raise ValueError("transform_result_timeout must be in (0, 3600] seconds")
        if isinstance(self.tools, (str, bytes)) or not isinstance(self.tools, Sequence):
            raise TypeError("MCP tools must be a sequence of raw tool names")
        names = tuple(self.tools)
        for name in names:
            if not isinstance(name, str) or not name:
                raise ValueError("MCP tool names must be nonempty strings")
            try:
                name.encode("utf-8")
            except UnicodeError:
                raise ValueError("MCP tool names must contain valid Unicode") from None
        object.__setattr__(self, "tools", names)
        if self._name_tools is None or not set(names).issubset(self._name_tools):
            object.__setattr__(self, "_name_tools", names)
        _tool_names((self,))

    @property
    def _has_before(self) -> bool:
        return self.before is not None or self.approval is not None

    @property
    def _has_result_callback(self) -> bool:
        return self.transform_result is not None or self.extract_application_data is not None

    def _check_context(self, context: object) -> None:
        if context is not None:
            return
        from ._function_tools import _policy_hints, _requires_context

        for name, context_type in (
            ("transform_result", MCPResultContext),
            ("extract_application_data", MCPResultContext),
            ("request_metadata", MCPMetadataContext),
            ("before", MCPCallContext),
            ("approval", MCPCallContext),
        ):
            callback = getattr(self, name)
            if callback is None:
                continue
            parameters = list(inspect.signature(callback).parameters.values())
            if parameters:
                annotation = _policy_hints(callback).get(parameters[0].name)
                if get_origin(annotation) is context_type and _requires_context(annotation):
                    raise TypeError(f"MCP {name} requires application context")

    def tool(self, raw_name: str) -> ToolRef:
        """Select one of this server's declared raw MCP tool names."""
        if raw_name not in self.tools:
            raise ValueError(f"MCP tool {raw_name!r} is not declared on server {self.name!r}")
        ref = ToolRef(**_tool_names((self,))[self.tools.index(raw_name)])
        object.__setattr__(ref, "_mcp_identity", (self.name, raw_name))
        return ref

    def _config(self, environment: Environment[Any] | None = None) -> dict[str, object]:
        config: dict[str, object] = {
            "enabled_tools": list(self.tools),
            "required": True,
            "startup_readiness": "connection",
            "startup_timeout_sec": self.startup_timeout,
            "tool_timeout_sec": self.execution_timeout,
        }
        if self.approval_mode == "preapproved":
            config["tools"] = {name: {"approval_mode": "approve"} for name in self.tools}
        return config


@dataclass(frozen=True, kw_only=True)
class StdioMCP(_MCP[ContextT_contra]):
    """Run a selected native MCP server on the host or the Session environment.

    ``location="host"`` starts the command on the application host.
    ``location="environment"`` requires ``agent.session(environment=...)``
    and starts it on that executor. ``env_vars`` names variables from
    the selected location, never from the other machine. ``env`` captures literal
    values for this child at construction; these override inherited names without
    changing the parent environment. Reserved process identity variables remain
    excluded. ``args`` are individual
    arguments without shell expansion. An omitted ``cwd`` uses Core's runtime
    directory on the host or the Session environment's working directory remotely.

    Session owns the connection and process lifetime. Both timeouts are seconds.
    Use ``tools=[]`` for a resource-only or prompt-only server. Call the Session's
    resource or prompt methods while idle; returned data is not sent to the model.
    ``approval_mode="native"`` keeps native approval rules; "preapproved" allows the
    selected names subject to native constraints. Neither mode sandboxes the
    server process or limits effects the server does not report.

    Optional before and approval callbacks control the selected
    tool call before dispatch. The process may already be running for tool
    discovery. These callbacks cannot sandbox process startup or undisclosed
    side effects. Approval receives any arguments returned by before.

    ``transform_result(context, result)`` returns an MCP CallToolResult dictionary,
    directly or by awaitable, before native history and model consumers. It cannot
    change a peer error to success. Invalid or late replies fail closed.
    Read ``session.events()`` to observe tool activity.
    """

    command: str = field(repr=False)
    args: Sequence[str] = field(default=(), repr=False)
    env_vars: Sequence[str] = field(default=(), repr=False)
    cwd: str | None = field(default=None, repr=False)
    location: Literal["host", "environment"] = "host"
    env: Mapping[str, str] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.location not in ("host", "environment"):
            raise ValueError("StdioMCP location must be 'host' or 'environment'")
        for name in ("args", "env_vars"):
            values = getattr(self, name)
            if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
                raise TypeError(f"MCP {name} must be a sequence of strings")
            object.__setattr__(self, name, tuple(values))
        _launch_text(self.command, "command")
        for argument in self.args:
            _launch_text(argument, "argument", allow_empty=True)
        for name in self.env_vars:
            if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None:
                raise ValueError("MCP env_vars must name environment variables")
        if self.env is not None:
            if not isinstance(self.env, Mapping):
                raise TypeError("MCP env must be a mapping of strings")
            captured = dict(self.env)
            for name, value in captured.items():
                _launch_text(name, "env name")
                if "=" in name:
                    raise ValueError("MCP env names must contain no equals sign")
                _launch_text(value, "env value", allow_empty=True)
            object.__setattr__(self, "env", _McpValues(captured))
        if self.cwd is not None:
            _launch_text(self.cwd, "cwd")

    def _config(self, environment: Environment[Any] | None = None) -> dict[str, object]:
        config = super()._config(environment)
        config.update(command=self.command, args=list(self.args))
        if self.location == "environment":
            if environment is None:
                raise ValueError(
                    f"StdioMCP location={self.location!r} requires a Session environment"
                )
            if environment._is_local:
                config["environment_id"] = "local"
                config["env_vars"] = [
                    name
                    for name in self.env_vars
                    if name not in {"OPENAI_API_KEY", "CODEX_API_KEY"}
                ]
            else:
                config["environment_id"] = "remote"
                config["env_vars"] = [{"name": name, "source": "remote"} for name in self.env_vars]
            cwd = self.cwd if self.cwd is not None else environment.cwd
            if cwd.startswith("file:"):
                uri = urlsplit(cwd)
                path = unquote(uri.path)
                if uri.netloc and uri.netloc != "localhost":
                    cwd = "//" + uri.netloc + path
                elif re.match(r"^/[A-Za-z]:/", path):
                    cwd = path[1:]
                else:
                    cwd = path
            config["cwd"] = cwd
        else:
            config["env_vars"] = list(self.env_vars)
            if self.cwd is not None:
                config["cwd"] = self.cwd
        if self.env is not None:
            config["env"] = dict(self.env)
        return config


@dataclass(frozen=True, kw_only=True)
class HttpMCP(_MCP[ContextT_contra]):
    """Connect to selected MCP tools over native HTTP(S).

    Provide a ``bearer_token`` directly, using ``os.environ`` in application code
    to read credentials from the environment. Session owns the connection. Both timeouts
    are seconds. ``approval_mode="native"`` keeps native approval rules;
    "preapproved" allows selected names subject to native constraints.
    ``headers`` captures literal HTTP fields at construction. Names are
    case-insensitive. Use either an Authorization header or ``bearer_token``.
    Use ``tools=[]`` for a resource-only or prompt-only server. The idle Session's resource
    methods return data without adding it to model context.

    Optional ``approval(context, call)`` receives MCPCallContext and NativeToolCall
    before a selected tool executes. Return True to proceed under native
    rules, False to deny only this call, or StopTurn(reason) to stop and join
    the whole owning turn. Both sync and async callbacks are supported. For a native child,
    ``context.submission`` identifies its exact ChildInvocation.
    ``context.server_name`` and ``context.tool_name`` are the raw MCP names.
    The provider call ID is ``context.call.call_id``; it can be None for calls
    from generated code, whose cell and nested IDs remain separate. Approval receives
    input from before if present. Child StopTurn denies the call and
    joins that turn's remaining cells without stopping another owner.
    Errors and invalid approvals deny locally.

    ``transform_result(context, result)`` returns an MCP CallToolResult before
    native history and model consumers; invalid, failed or late replies withhold
    the result. Read ``session.events()`` to observe tool activity. These settings
    do not limit undisclosed remote server effects.
    """

    url: str = field(repr=False)
    bearer_token: str | None = field(default=None, repr=False)
    headers: Mapping[str, str] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.url, str):
            raise TypeError("MCP url must be a string")
        try:
            self.url.encode("utf-8")
            parsed = urlsplit(self.url)
            valid_url = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and not parsed.fragment
                and not any(character.isspace() for character in self.url)
            )
            _ = parsed.port
        except (ValueError, UnicodeError):
            valid_url = False
        if not valid_url:
            raise ValueError("MCP url must be HTTP(S), without userinfo or fragments")
        if self.bearer_token is not None:
            if not isinstance(self.bearer_token, str):
                raise TypeError("bearer_token must be a string")
            if not self.bearer_token or any(
                not 0x21 <= ord(character) <= 0x7E for character in self.bearer_token
            ):
                raise ValueError("bearer_token must contain visible ASCII characters")
        if self.headers is not None:
            if not isinstance(self.headers, Mapping):
                raise TypeError("MCP headers must be a string mapping")
            headers = dict(self.headers)
            names: set[str] = set()
            for name, value in headers.items():
                if not isinstance(name, str) or not re.fullmatch(
                    r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name
                ):
                    raise ValueError("MCP header names must be HTTP tokens")
                _launch_text(value, "header value", allow_empty=True)
                if any((ord(char) < 0x20 and char != "\t") or ord(char) == 0x7F for char in value):
                    raise ValueError("MCP header values must be valid HTTP field values")
                normalized = name.lower()
                if normalized in names:
                    raise ValueError("MCP headers must have distinct case-insensitive names")
                if normalized == "authorization" and self.bearer_token is not None:
                    raise ValueError("Use either bearer_token or an Authorization header")
                names.add(normalized)
            object.__setattr__(self, "headers", _McpValues(headers))

    def _config(self, environment: Environment[Any] | None = None) -> dict[str, object]:
        config = super()._config(environment)
        config["url"] = self.url
        headers = dict(self.headers or {})
        if self.bearer_token is not None:
            headers["Authorization"] = "Bearer " + self.bearer_token
        if headers:
            config["http_headers"] = headers
        return config


@dataclass(frozen=True, kw_only=True)
class HostedVerificationMCP(HttpMCP[ContextT_contra]):
    """Explicitly register the application's trusted verification service.

    The declaration grants the native host-owned Apps registration. Supply a
    trusted signer through on_interaction. The service must verify and consume
    each challenge before reporting success. Enrollment and keys remain caller-owned.
    Use a separate Session to replace this fixed registration.
    """

    name: Literal["codex_apps"] = field(default="codex_apps", init=False)


@dataclass(frozen=True, kw_only=True)
class ChatGPTAppsMCP(HttpMCP[ContextT_contra]):
    """Explicit personal Apps credentials, separate from the model provider key.

    The application supplies a first-party ChatGPT access token and its exact
    account. No stored login, token refresh, or browser opening occurs. A browser
    completing connector OAuth must use the same ChatGPT identity. Open a fresh
    Session to replace credentials. Connector auth preserves known requested
    scopes and checks the backend's actual grants before reporting completion.
    """

    name: Literal["codex_apps"] = field(default="codex_apps", init=False)
    url: str = field(default="https://chatgpt.com/backend-api/ps/mcp", init=False, repr=False)
    bearer_token: None = field(default=None, init=False, repr=False)
    # ClassVar removes the inherited field; Apps accepts no custom headers.
    headers: ClassVar[None] = None  # type: ignore[misc]
    access_token: str = field(repr=False)
    account_id: str = field(repr=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            not isinstance(self.access_token, str)
            or not self.access_token
            or any(not 0x21 <= ord(character) <= 0x7E for character in self.access_token)
        ):
            raise ValueError("ChatGPT Apps access_token must contain visible ASCII characters")
        valid_account = False
        try:
            valid_account = (
                isinstance(self.account_id, str)
                and 0 < len(self.account_id.encode("utf-8")) <= 1024
                and self.account_id.strip() == self.account_id
                and not any(
                    ord(character) < 0x20 or ord(character) == 0x7F for character in self.account_id
                )
            )
        except Exception:
            pass
        if not valid_account:
            raise ValueError("Invalid ChatGPT Apps account_id")

    def _config(self, environment: Environment[Any] | None = None) -> dict[str, object]:
        config = super()._config(environment)
        config["auth"] = "chatgpt"
        return config


for _declaration in (StdioMCP, HttpMCP, HostedVerificationMCP, ChatGPTAppsMCP):
    _protect_mcp_initializer(_declaration)
    _protect_mcp_methods(_declaration, lambda owner: (owner,))
del _declaration


def _launch_text(value: object, name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise TypeError(f"MCP {name} must be a string")
    if not value and not allow_empty:
        raise ValueError(f"MCP {name} must be nonempty")
    if "\x00" in value:
        raise ValueError(f"MCP {name} must contain no NUL")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError(f"MCP {name} must contain valid Unicode") from None


def _tool_names(servers: tuple[_MCP[Any], ...]) -> list[dict[str, str]]:
    namespaces = [_project_name(server.name) for server in servers]
    namespaces = [name if name.startswith("mcp__") else "mcp__" + name for name in namespaces]
    if len(set(namespaces)) != len(namespaces):
        raise ValueError("MCP server aliases have colliding native namespaces")
    bases: dict[str, str] = {}
    for server, namespace in zip(servers, namespaces, strict=True):
        for raw in server._name_tools or server.tools:
            key = namespace + _project_name(raw)
            if key in bases and bases[key] != server.name:
                raise ValueError("MCP names have a native concatenation collision")
            bases[key] = server.name
    names = _project_tool_names(
        [(server.name, server._name_tools or server.tools) for server in servers]
    )
    projected = [names[server.name, raw] for server in servers for raw in server.tools]
    owners: dict[str, str] = {}
    for server in servers:
        for raw in server.tools:
            identity = names[server.name, raw]
            key = identity["namespace"] + identity["name"]
            if key in owners and owners[key] != server.name:
                raise ValueError("MCP names have a native concatenation collision")
            owners[key] = server.name
    return projected


def _ready(
    receipt: object, servers: tuple[_MCP[Any], ...], available: tuple[_MCP[Any], ...]
) -> bool:
    if not isinstance(receipt, dict):
        return False
    result = receipt.get("result")
    if not isinstance(result, dict):
        return False
    if result.get("publication") != "published" or result.get("ready") is not True:
        return False
    reports = result.get("servers")
    if not isinstance(reports, dict):
        return False
    active = {server.name: server for server in servers}
    for declared in available:
        report = reports.get(declared.name)
        if (
            not isinstance(report, dict)
            or report.get("accepted") is not True
            or report.get("ready") is not True
        ):
            return False
        server = active.get(declared.name)
        if server is None:
            if report.get("status") != "disabled" or report.get("tools") != []:
                return False
            continue
        tools = report.get("tools")
        if (
            report.get("status") != "connected"
            or report.get("listing_error") is not False
            or not isinstance(tools, list)
            or not all(name in tools for name in server.tools)
        ):
            return False
    return True


def _result_policies(servers: tuple[_MCP[Any], ...], generation: int) -> str:
    return json.dumps(
        {
            "generation": generation,
            "metadata_servers": {
                server.name: list(server.tools)
                for server in servers
                if server.request_metadata is not None
            },
            "servers": {
                server.name: dict.fromkeys(
                    server.tools,
                    30.0
                    if server.transform_result_timeout is None
                    else server.transform_result_timeout,
                )
                for server in servers
                if server._has_result_callback
            },
        }
    )


_protect_mcp_methods(_MCP, lambda owner: (owner,))
_preserve_module_identity(globals(), "agents.mcp")
