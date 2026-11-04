"""Declarations for native tools on local and remote executors."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from ipaddress import IPv6Address
from os import PathLike, fspath
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Literal, TypeAlias, get_origin
from urllib.parse import unquote, urlsplit

from typing_extensions import TypeVar

from ._options import RuntimeEnvironment, RuntimeNetworkPolicy

if TYPE_CHECKING:
    from .agent import Agent
    from .native_agents import ChildInvocation
    from .native_interception import StopTurn
    from .session import Session
    from .turn import Turn

ContextT_co = TypeVar("ContextT_co", covariant=True, default=None)
ContextT_contra = TypeVar("ContextT_contra", contravariant=True, default=object)
ExecutorToolName = Literal["exec_command", "write_stdin", "apply_patch"]


def _domain_pattern(value: str) -> str:
    # The proxy matches hosts across all ports. Reject inputs that imply a narrower grant.
    if not value.isascii() or not value.isprintable() or re.search(r"[\s/@#\\]", value):
        raise ValueError("Network domain rules require an ASCII host or host pattern")
    if ":" in value or "[" in value:
        host = value[1:-1] if value.startswith("[") and value.endswith("]") else value
        if "%" in host:
            raise ValueError("Network domain rules do not support IPv6 zone identifiers")
        try:
            address = IPv6Address(host)
            if address.ipv4_mapped is not None:
                return f"::ffff:{address.ipv4_mapped}"
            return str(address)
        except ValueError:
            pass
        raise ValueError("Network domain rules require a host without a port")
    pattern = value.rstrip(".").lower()
    if not pattern or (pattern in ("*", "**") and value.endswith(".")):
        raise ValueError("Network domain rules require a nonempty host or host pattern")
    return pattern


def _domain_rules(name: str, values: Sequence[str]) -> set[str]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be a sequence of strings")
    rules = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{name} must contain strings")
        rules.add(_domain_pattern(value))
    return rules


@dataclass(frozen=True, kw_only=True)
class NetworkPolicy:
    """Set the managed proxy and socket rules for native commands on a Unix executor.

    The proxy checks hostnames for all ports. Supply ASCII hosts, IP addresses or
    scoped patterns such as ``*.example.org``. The proxy denies a host if any
    denial matches. It can resolve a name before it denies a request.

    A Linux executor requires ``allow_all_unix_sockets=True`` for any explicit
    policy. A macOS executor supports the default of ``False``. An all-sockets
    grant permits access to every reachable Unix socket, including abstract
    sockets on Linux. It also permits HTTP forwarding through the proxy on
    macOS. Use a job-private executor that cannot reach privileged host sockets.
    Individual socket-path grants are not supported.

    ``allow_local_binding=True`` permits local listeners where the sandbox supports
    them. It adds loopback and private IPv4 ranges to ``NO_PROXY`` and permits the
    proxy to connect to non-public addresses resolved for allowed hosts. On macOS,
    local binding can also permit direct traffic to any reachable IP on port 53,
    including non-DNS payloads. That traffic bypasses domain rules; the job's
    network isolation must enforce any stricter requirement.
    """

    allowed_domains: Sequence[str] = field(default=(), repr=False)
    denied_domains: Sequence[str] = field(default=(), repr=False)
    allow_all_unix_sockets: bool = False
    allow_local_binding: bool = False

    def __post_init__(self) -> None:
        for name in ("allow_all_unix_sockets", "allow_local_binding"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")
        allowed = _domain_rules("allowed_domains", self.allowed_domains)
        denied = _domain_rules("denied_domains", self.denied_domains)
        object.__setattr__(self, "allowed_domains", tuple(sorted(allowed - denied)))
        object.__setattr__(self, "denied_domains", tuple(sorted(denied)))

    def _to_native(self) -> RuntimeNetworkPolicy:
        return {
            "allowed_domains": list(self.allowed_domains),
            "denied_domains": list(self.denied_domains),
            "allow_all_unix_sockets": self.allow_all_unix_sockets,
            "allow_local_binding": self.allow_local_binding,
        }


@dataclass(frozen=True)
class ExecutorContext(Generic[ContextT_co]):
    """The actual native identity of one executor call.

    Callbacks receive application context and this identity before dispatch or
    before native history, model, hooks, logs or code consume the result.
    These fields do not grant execution authority. The calling tool, cell,
    and native thread cannot be replaced.
    Direct calls set ``call_id``; programmatic tool calls set the outer call, cell,
    and runtime tool call IDs instead.

    ``session`` and ``agent`` identify the owning Python Session and Agent.
    A native child does not create a second Python Agent. To apply policy to the
    actual caller, use ``agent_role`` and ``model``, captured from its native
    turn. The root has no native child role. ``model`` is the executing model
    rather than a default configured on the owning Agent; it is None if Core
    does not supply one.
    """

    context: ContextT_co = field(repr=False)
    submission: Turn[Any] | ChildInvocation
    tool_name: ExecutorToolName
    thread_id: str
    turn_id: str
    outer_call_id: str | None
    cell_id: str | None
    runtime_tool_call_id: str | None
    call_id: str | None = None
    session: Session[Any, Any] = field(repr=False, kw_only=True)
    agent: Agent[Any, Any] = field(repr=False, kw_only=True)
    agent_role: str | None = field(default=None, kw_only=True)
    model: str | None = field(default=None, kw_only=True)


@dataclass(frozen=True, kw_only=True)
class Environment(Generic[ContextT_contra]):
    """Select one authenticated WSS executor or use :meth:`local` for local work.

    The application owns the endpoint, credentials and remote workspace.
    By default no process or file tools are granted to the model. The connection
    can still run StdioMCP servers with location="environment". Select tools
    explicitly to expose them; NativeAgents requires all three tools.
    ``write_stdin`` requires ``exec_command`` and supports interactive TTY input.
    Pass the Environment to ``agent.session(environment=...)``; one Agent can
    serve independent sessions in different environments. A live Session cannot
    replace its connection. The SDK never falls back to executing these tools on
    the application host.

    capability_directories selects discovery roots on the selected executor.
    None omits the native override; an empty sequence explicitly selects no
    roots. Restore uses only its current Environment, never host or saved roots.

    Selected process and file tools can modify the assigned remote workspace.
    The native runtime requests write access only there, with protected native
    metadata and restricted networking. The application owns remote isolation
    and must choose an executor that enforces the required OS and tenant limits.

    ``writable_roots`` grants additional directories on the executor. The working
    directory is always writable under the same metadata protections. An empty
    sequence keeps only that directory and enables strict write enforcement.
    Omitting this setting on a remote executor keeps its legacy approval behavior.
    Strict write enforcement requires a Linux or macOS executor.

    With ``exec_command``, set ``network`` to a NetworkPolicy or a dictionary with
    the same fields. Omit the policy to keep the executor's default restricted
    networking. Explicit policies require a Linux or macOS executor with managed
    proxy support. A macOS executor accepts an empty policy with no domain or
    Unix socket grants. A Linux executor requires explicit access to all Unix
    sockets. Restore requires the same normalized policy on the current
    Environment. To change the policy, start a new session.

    An all-sockets grant lets commands bind, listen and connect to every
    reachable Unix socket. Use it only on a job-private executor. For an offline
    job, isolate its exec-server, proxy and DNS resolver from external networks
    and host service sockets, including read-only mounts and inherited connections.
    The application owns this boundary.

    ``approval(context, arguments)`` returns a bool, directly or by awaitable,
    before a selected executor call. It receives the original arguments, the
    current Session and owning Agent, and the actual root or child turn identity,
    including the native child role and executing model when available.
    False denies a direct call locally. For a generated-code Environment call,
    False also stops and joins its owning root or native child turn, so the code
    cannot catch the denial and continue. Return StopTurn(reason) to stop the
    owning turn explicitly on either route. The SDK joins that turn's owned work before reporting
    "Turn stopped by policy". Other children, the root and later turns remain
    independent. Ordinary child bool approvals stay available. Errors, invalid
    replies, cancellation and expired native decisions also deny.
    An approval never grants extra host or native permissions.

    Only after approval allows the original call, ``transform_arguments(context, value)``
    returns raw argument data or Allow, ReplaceArguments or StopTurn. Raw JSON with
    an action field is still data. A denied call never invokes this transform.
    ``transform_result(context, value)`` returns the accepted result.
    Arguments for exec_command and
    write_stdin are JSON objects; apply_patch arguments are the patch string.
    A result transform runs after the remote effect, before native consumers
    see its result. It does not undo files or processes already changed.
    An arguments transform can also return StopTurn(reason) before dispatch.
    Result transforms do not accept stop decisions.

    Argument and result transforms work for direct and programmatic tool calls.
    A result policy receives the whole result before history, model, hooks, logs or
    generated code consume it. Raw output events are withheld for those calls,
    including later background process output. The policy does not undo effects.
    Unrelated Python and MCP policies remain independent.
    ``policy_timeout`` bounds native transform decisions in seconds (default
    30). Invalid, late or failed decisions fail closed; they never approve the
    original value. The checkpoint stores a digest for policy comparison.
    Its configuration omits policy rules, executor credentials and endpoints.
    Environment argument transforms and approval callbacks apply with HTTP and WebSocket model
    providers. They do not confer native approval: exec or patch may still
    require an interaction that the current Session cannot answer.
    Callbacks apply to both root and native child calls; inspect the supplied
    identity to select different application behavior for each.
    """

    _is_local: ClassVar[bool] = False
    url: str = field(repr=False)
    cwd: str
    tools: Sequence[ExecutorToolName] = ()
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    connect_timeout: float | None = None
    capability_directories: Sequence[str] | None = None
    approval: (
        Callable[
            [ExecutorContext[ContextT_contra], Any], bool | StopTurn | Awaitable[bool | StopTurn]
        ]
        | None
    ) = field(default=None, repr=False)
    transform_arguments: (
        Callable[[ExecutorContext[ContextT_contra], Any], Any | Awaitable[Any]] | None
    ) = field(default=None, repr=False)
    transform_result: (
        Callable[[ExecutorContext[ContextT_contra], Any], Any | Awaitable[Any]] | None
    ) = field(default=None, repr=False)
    policy_timeout: float = 30
    network: NetworkPolicy | RuntimeNetworkPolicy | None = None
    writable_roots: Sequence[str | PathLike[str]] | None = None

    @classmethod
    def local(
        cls,
        *,
        cwd: str | PathLike[str],
        tools: Sequence[ExecutorToolName] = (),
        writable_roots: Sequence[str | PathLike[str]] = (),
        network: NetworkPolicy | RuntimeNetworkPolicy | None = None,
        capability_directories: Sequence[str] | None = None,
        approval: (
            Callable[
                [ExecutorContext[ContextT_contra], Any],
                bool | StopTurn | Awaitable[bool | StopTurn],
            ]
            | None
        ) = None,
        transform_arguments: (
            Callable[[ExecutorContext[ContextT_contra], Any], Any | Awaitable[Any]] | None
        ) = None,
        transform_result: (
            Callable[[ExecutorContext[ContextT_contra], Any], Any | Awaitable[Any]] | None
        ) = None,
        policy_timeout: float = 30,
    ) -> Environment[ContextT_contra]:
        """Run selected tools locally through the SDK's native sandbox.

        Use an existing absolute working directory on Linux or macOS.
        Additional roots must be existing directories on this executor.
        The URL identifies the working directory as a file URI.
        Model commands start with a system executable search path and receive
        no ambient environment variables. They can read the working directory,
        additional writable roots, and platform runtime paths. Run the executor
        on an isolated job host if those paths contain sensitive data.
        Keep the SDK, its native helpers, and LocalStorage profiles outside
        the working directory and additional writable roots.
        """
        path = fspath(cwd)
        return _LocalEnvironment(
            url=_path_uri(path),
            cwd=path,
            tools=tools,
            writable_roots=writable_roots,
            network=network,
            capability_directories=capability_directories,
            approval=approval,
            transform_arguments=transform_arguments,
            transform_result=transform_result,
            policy_timeout=policy_timeout,
        )

    def __post_init__(self) -> None:
        if self._is_local:
            if self.headers or self.connect_timeout is not None:
                raise ValueError("A local executor does not accept connection settings")
        else:
            self._validate_remote_url()
        if self.writable_roots is not None:
            object.__setattr__(self, "writable_roots", _writable_roots(self.writable_roots))
        self._validate_settings()
        if self._is_local:
            object.__setattr__(self, "url", _path_uri(self.cwd))

    def _validate_remote_url(self) -> None:
        if not isinstance(self.url, str):
            raise TypeError("Executor URL must be a string")
        try:
            url = urlsplit(self.url)
            valid_url = (
                url.scheme == "wss"
                and bool(url.hostname)
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
                and not any(
                    character.isspace() or ord(character) < 32 or ord(character) == 127
                    for character in self.url
                )
            )
            _ = url.port
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("Executor URL must use wss:// without userinfo, query or fragment")

    def _validate_settings(self) -> None:
        if not isinstance(self.cwd, str):
            raise TypeError("Executor cwd must be an absolute path or file URI")
        try:
            path = urlsplit(_path_uri(self.cwd))
            valid_path = (
                path.scheme == "file"
                and path.path.startswith("/")
                and not (path.query or path.fragment)
            )
        except ValueError:
            valid_path = False
        if not valid_path:
            raise ValueError("Executor cwd must be an absolute path or file URI")
        if isinstance(self.tools, str) or not isinstance(self.tools, Sequence):
            raise TypeError("Executor tools must be a sequence of native tool names")
        selected = tuple(self.tools)
        if any(name not in ("exec_command", "write_stdin", "apply_patch") for name in selected):
            raise ValueError("Select exec_command, write_stdin or apply_patch")
        if len(set(selected)) != len(selected):
            raise ValueError("Executor tool names must be unique")
        if "write_stdin" in selected and "exec_command" not in selected:
            raise ValueError("write_stdin requires exec_command")
        if self.network is not None:
            if isinstance(self.network, dict):
                object.__setattr__(self, "network", NetworkPolicy(**self.network))
            elif not isinstance(self.network, NetworkPolicy):
                raise TypeError("network must be a NetworkPolicy or dictionary")
            if "exec_command" not in selected:
                raise ValueError("Network policies require exec_command")
        duration = self.connect_timeout
        if duration is not None and (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not 0 < duration <= sys.float_info.max
        ):
            raise ValueError("Executor connect_timeout must be positive and finite or None")
        for name in ("approval", "transform_arguments", "transform_result"):
            callback = getattr(self, name)
            if callback is not None and not callable(callback):
                raise TypeError(f"Environment {name} must be callable")
        if (
            isinstance(self.policy_timeout, bool)
            or not isinstance(self.policy_timeout, (int, float))
            or not 0 < self.policy_timeout <= 3600
        ):
            raise ValueError("Environment policy_timeout must be in (0, 3600] seconds")
        if not isinstance(self.headers, Mapping):
            raise TypeError("Executor headers must be a string mapping")
        headers = {}
        for name, value in self.headers.items():
            if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise ValueError("Invalid executor header name")
            key = name.lower()
            if key in headers:
                raise ValueError("Executor header names must be unique ignoring case")
            if key in {
                "connection",
                "content-length",
                "host",
                "transfer-encoding",
                "upgrade",
            } or key.startswith("sec-websocket-"):
                raise ValueError("Executor header is reserved by the WebSocket connection")
            if not isinstance(value, str) or any(
                (ord(character) < 32 and character != "\t") or ord(character) == 127
                for character in value
            ):
                raise ValueError("Invalid executor header value")
            headers[key] = value
        object.__setattr__(self, "headers", MappingProxyType(headers))
        object.__setattr__(self, "tools", selected)
        if self.capability_directories is not None:
            if isinstance(self.capability_directories, str):
                raise TypeError("capability_directories must be a sequence of remote paths")
            roots = tuple(self.capability_directories)
            for root in roots:
                uri = urlsplit(_path_uri(root))
                if (
                    uri.scheme != "file"
                    or not uri.path.startswith("/")
                    or uri.query
                    or uri.fragment
                ):
                    raise ValueError(
                        "Capability directories must be absolute remote paths or file URIs"
                    )
            object.__setattr__(self, "capability_directories", roots)

    @property
    def _has_callbacks(self) -> bool:
        return any(
            callback is not None
            for callback in (self.approval, self.transform_arguments, self.transform_result)
        )

    def _check_context(self, context: object) -> None:
        if context is not None:
            return
        from ._function_tools import _policy_hints, _requires_context

        for name in ("approval", "transform_arguments", "transform_result"):
            callback = getattr(self, name)
            if callback is None:
                continue
            parameters = list(inspect.signature(callback).parameters.values())
            if parameters:
                annotation = _policy_hints(callback).get(parameters[0].name)
                if get_origin(annotation) is ExecutorContext and _requires_context(annotation):
                    raise TypeError(f"Executor {name} requires application context")

    def _network_policy_digest(self) -> str | None:
        if not isinstance(self.network, NetworkPolicy):
            return None
        encoded = json.dumps(self.network._to_native(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("ascii")).hexdigest()

    def _to_native(self) -> RuntimeEnvironment:
        result: RuntimeEnvironment = {
            "cwd": _path_uri(self.cwd),
            "headers": dict(self.headers),
            "connect_timeout": self.connect_timeout,
        }
        if self._is_local:
            result["local"] = True
        else:
            result["url"] = self.url
        if self.writable_roots is not None:
            result["writable_roots"] = [fspath(root) for root in self.writable_roots]
        if self.capability_directories is not None:
            result["capability_directories"] = [
                _path_uri(root) for root in self.capability_directories
            ]
        if isinstance(self.network, NetworkPolicy):
            result["network"] = self.network._to_native()
        return result


@dataclass(frozen=True, kw_only=True)
class _LocalEnvironment(Environment[ContextT_contra]):
    _is_local = True


EnvironmentSource: TypeAlias = (
    Environment[ContextT_contra] | AbstractAsyncContextManager[Environment[ContextT_contra]]
)


def _path_uri(value: str) -> str:
    if value.startswith("file:"):
        return value
    path = PurePosixPath(value) if value.startswith("/") else PureWindowsPath(value)
    return path.as_uri()


def _writable_roots(values: Sequence[str | PathLike[str]]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence):
        raise TypeError("writable_roots must be a sequence of absolute executor paths")
    roots = set()
    for value in values:
        raw = fspath(value)
        if not isinstance(raw, str):
            raise TypeError("writable_roots must contain text paths")
        if raw.startswith("file:") and (
            re.search(r"[\x00-\x20\x7f]", raw) or re.search(r"%(?![0-9a-fA-F]{2})", raw)
        ):
            raise ValueError("writable_roots must contain valid file URIs")
        try:
            uri = urlsplit(_path_uri(raw))
            path = PurePosixPath(unquote(uri.path, errors="strict"))
            valid = (
                uri.scheme == "file"
                and uri.netloc in ("", "localhost")
                and not (uri.query or uri.fragment)
                and path.is_absolute()
                and path.anchor == "/"
                and len(path.parts) > 1
                and ".." not in path.parts
                and "\0" not in str(path)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("writable_roots must contain absolute executor directories")
        roots.add(path.as_uri())
    return tuple(sorted(roots))
