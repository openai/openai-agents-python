"""Native agent declarations and one-shot convenience methods."""

from __future__ import annotations

import asyncio as asyncio
import base64
import hashlib
import importlib as importlib
import json as json
import math as math
import os as os
import re as re
from collections.abc import (
    AsyncIterator as AsyncIterator,
    Awaitable as Awaitable,
    Callable as Callable,
    Callable as _Callable,
    Mapping as Mapping,
    Sequence as Sequence,
)
from contextlib import (
    AbstractAsyncContextManager as AbstractAsyncContextManager,
    asynccontextmanager as asynccontextmanager,
)
from copy import copy as copy, deepcopy as deepcopy
from dataclasses import (
    FrozenInstanceError as FrozenInstanceError,
    dataclass as dataclass,
    field as field,
    replace as replace,
)
from pathlib import Path as Path
from threading import RLock as RLock
from time import time as _time
from types import (
    MappingProxyType as MappingProxyType,
    MethodType as MethodType,
    TracebackType as TracebackType,
)
from typing import (
    TYPE_CHECKING as TYPE_CHECKING,
    Any as Any,
    Generic as Generic,
    Literal,
    NoReturn,
    SupportsIndex as SupportsIndex,
    cast as cast,
    overload as overload,
)
from urllib.parse import urlsplit as urlsplit

from typing_extensions import TypedDict as TypedDict, TypeVar as TypeVar

from ._hosted_prompts import _capture_hosted_prompt_source, _hosted_prompt_controls
from ._lifecycle import (
    _close_after_error as _close_after_error,
    _join as _join,
    _sync_run as _sync_run,
    _SyncRun as _SyncRun,
)
from ._mcp_errors import _protect_mcp_methods
from ._media import _MAX_AUDIO_INPUT_BYTES, _check_audio_json, _contains_audio
from ._public import preserve_module_identity as _preserve_module_identity
from ._tool_errors import ToolErrorFormatter as ToolErrorFormatter
from ._trace_context import TraceContext as TraceContext, _trace_context as _trace_context
from .compaction import _BeforeCompact
from .completion import _BeforeComplete, _ToolBatchCompletion
from .decisions import DecisionInput, DecisionProvider, _check_provider, _decide
from .guardrails import InputGuardrail, OutputGuardrail
from .initial_messages import _capture_initial_messages, _initial_messages_json
from .input import InitialMessage, _native_input, _response_content
from .model_context import HostedPromptSource, _ContextTransform
from .models import ModelProvider, NativeModel
from .retry import ModelRetrySettings as ModelRetrySettings
from .tools import ToolRef
from .usage import RunBudget

if TYPE_CHECKING:

    class _TracingOpeningOptions(TypedDict, total=False, closed=True):
        _tracing_context: str

    from ._function_tools import Tool
    from ._managed_workspace import ManagedWorkspaceCheckpoint
    from .canonical_storage import CustomStorage
    from .executor import Environment, EnvironmentSource
    from .handoffs import Handoff
    from .input import UserInput
    from .native_agents import NativeAgents
    from .prepared_request import _PrepareRequest
    from .realtime_agents import RealtimeAgentConfig, RealtimeHandoff
    from .recovery import ReconciledCall
    from .schema import OutputSpec
    from .session import (
        Completed,
        Session,
        _BeforeStep,
        _EnvironmentLease,
        _InteractionHandler,
        _OpeningOwner,
    )
    from .state import LocalStorage, SessionState
    from .telemetry import TraceOptions
    from .turn import Turn

ContextT = TypeVar("ContextT", default=None)
OutputT = TypeVar("OutputT", default=str)
SessionContextT = TypeVar("SessionContextT")
NewOutputT = TypeVar("NewOutputT")
_DecisionT = TypeVar("_DecisionT")
TextAgentT = TypeVar("TextAgentT", bound="Agent[Any, str]")
TypedAgentT = TypeVar("TypedAgentT", bound="Agent[Any, Any]")

_MAX_FUNCTION_CONCURRENCY_UNSET: Any = object()
_OBSERVATION_RETENTION_UNSET: Any = object()


class ModelSettings(TypedDict, total=False, closed=True):
    """Native model overrides. Omission preserves the native default.

    ``service_tier=None`` clears the override. These settings select model
    behavior; they do not grant tools, filesystem access, or network access.
    ``input_modalities`` replaces the selected model's declared input capabilities.
    Omission preserves the current selection; ``None`` restores catalogue metadata.
    This declaration never becomes a provider request field or grants file access.
    ``tool_choice`` controls whether the model may call tools in the request.
    Use ``"auto"`` to let the model decide, ``"none"`` to disable tool calls,
    or ``"required"`` to require a call to an offered tool. The default is
    ``"auto"``. A ``ToolRef`` selects one currently admitted eager leaf and
    requires it without adding authority. ``None`` clears the target to auto.
    New request controls accept ``None`` to clear their SDK override. The response
    cap uses an unsigned 64-bit integer for ordinary sampling, not internal
    compaction or reviewers. Providers own numeric ranges and metadata quotas.
    Request TTL (30m/12h) and retention (in_memory/24h) remain
    independent; omission injects neither default. Providers own model support
    and cache behavior. Known formatting loss fails locally. ``store=True``
    requires API-key authentication.

    ``reset_tool_choice=True``: ordinary text sampling only; opt-in release of
    required/named forcing after an
    accepted tool batch on the captured profile. Omission, False, and None:
    persistent forcing. Independent runs and incoming handoff profiles:
    their own declared initial choice. No change to ``tool_choice="none"``.

    ``invocation_timeout_ms`` sets one local model invocation deadline in positive
    integer milliseconds (at most 4,294,967,295). ``None`` clears it. It includes
    internal request waits and retries. Cleanup can finish after expiry; accepted
    terminal responses disarm the clock before result settlement.
    ``attempt_timeout_ms`` applies separately to each native generation attempt;
    retry backoff runs outside this clock. ``retry`` configures native retry
    decisions. Its policy is captured with the Agent, excluded from configuration
    JSON, and never starts a host retry loop.
    """

    reasoning_effort: str
    reasoning_summary: str
    service_tier: str | None
    tool_choice: Literal["auto", "none", "required"] | ToolRef | None
    input_modalities: Sequence[Literal["text", "image", "audio"]] | None
    max_output_tokens: int | None
    temperature: float | None
    top_p: float | None
    top_logprobs: int | None
    parallel_tool_calls: bool | None
    verbosity: Literal["low", "medium", "high"] | None
    metadata: Mapping[str, str] | None
    store: bool | None
    prompt_cache_retention: Literal["in_memory", "24h"] | None
    prompt_cache_ttl: Literal["30m", "12h"] | None
    invocation_timeout_ms: int | None
    attempt_timeout_ms: int | None
    retry: ModelRetrySettings | None
    truncation: Literal["auto", "disabled"] | None
    include: list[str] | None
    context_management: list[dict[str, Any]] | None
    extra_body: Mapping[str, Any] | None
    # Local opt-in; false/None preserve persistent forcing.
    reset_tool_choice: bool | None


def _copy_model_settings(settings: ModelSettings | None) -> ModelSettings:
    if settings is None:
        return {}
    if not isinstance(settings, Mapping):
        raise TypeError("model_settings must be a mapping")
    unknown = settings.keys() - ModelSettings.__annotations__.keys()
    if unknown:
        raise ValueError(f"Unsupported model setting(s): {sorted(unknown)}")
    for timeout_name in ("invocation_timeout_ms", "attempt_timeout_ms"):
        if timeout_name not in settings:
            continue
        timeout_ms = cast(Mapping[str, Any], settings)[timeout_name]
        if timeout_ms is not None and (
            type(timeout_ms) is not int or not 1 <= timeout_ms <= 4_294_967_295
        ):
            raise ValueError(
                f"{timeout_name} must be an integer in the range 1..=4294967295 or None"
            )

    captured: dict[str, Any] = dict(settings)
    for key, value in captured.items():
        if value is None:
            continue
        if key == "retry":
            if not isinstance(value, ModelRetrySettings):
                raise TypeError("retry must be ModelRetrySettings or None")
        elif key == "input_modalities":
            if (
                not isinstance(value, Sequence)
                or isinstance(value, (str, bytes))
                or not value
                or any(item not in ("text", "image", "audio") for item in value)
                or len(set(value)) != len(value)
            ):
                raise ValueError(
                    "input_modalities requires a nonempty sequence "
                    "of distinct text/image/audio values"
                )
            captured[key] = list(value)
        elif key == "max_output_tokens":
            if type(value) is not int or not 0 <= value <= 18_446_744_073_709_551_615:
                raise ValueError("max_output_tokens must be an unsigned 64-bit integer")
        elif key == "top_logprobs":
            if type(value) is not int or not 0 <= value <= 255:
                raise ValueError("top_logprobs must be an unsigned 8-bit integer")
        elif key in ("temperature", "top_p"):
            try:
                finite = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"{key} must be a finite 64-bit number")
        elif key in ("parallel_tool_calls", "store", "reset_tool_choice"):
            if type(value) is not bool:
                raise TypeError(f"{key} must be bool or None")
        elif key == "truncation":
            if value not in ("auto", "disabled"):
                raise ValueError("truncation must be auto or disabled")
        elif key == "include":
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise TypeError("include must be a list of strings")
            captured[key] = list(value)
        elif key in ("context_management", "extra_body"):
            if key == "context_management":
                if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                    raise TypeError("context_management must be a list of JSON objects")
            elif not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
                raise TypeError("extra_body must be a JSON object")
            if key == "extra_body" and set(value) & {
                "input_modalities",
                "reset_tool_choice",
                "tool_choice",
                "client_metadata",
                "service_tier",
                "tools",
                "access_programs",
                "metadata",
                "previous_response_id",
                "stream",
                "parallel_tool_calls",
                "prompt_cache_retention",
                "model",
                "prompt_cache_key",
                "stream_options",
                "conversation",
                "temperature",
                "prompt_cache_options",
                "truncation",
                "top_p",
                "generate",
                "store",
                "context_management",
                "instructions",
                "type",
                "reasoning",
                "include",
                "input",
                "top_logprobs",
                "text",
                "max_output_tokens",
            }:
                raise ValueError(
                    "extra_body cannot replace native request fields; "
                    "use the corresponding model setting"
                )
            captured[key] = json.loads(
                json.dumps(dict(value) if isinstance(value, Mapping) else value, allow_nan=False)
            )
        elif key in ("verbosity", "prompt_cache_retention", "prompt_cache_ttl"):
            allowed = {
                "verbosity": ("low", "medium", "high"),
                "prompt_cache_retention": ("in_memory", "24h"),
                "prompt_cache_ttl": ("30m", "12h"),
            }[key]
            if value not in allowed:
                raise ValueError(f"Unsupported {key}")
        elif key == "metadata":
            if not isinstance(value, Mapping) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
            ):
                raise ValueError("metadata must be a mapping of strings to strings")
            captured[key] = MappingProxyType(dict(value))
        elif key == "tool_choice":
            if isinstance(value, ToolRef):
                if (
                    not isinstance(value.name, str)
                    or not value.name.strip()
                    or (
                        value.namespace is not None
                        and (not isinstance(value.namespace, str) or not value.namespace.strip())
                    )
                ):
                    raise ValueError("tool_choice needs a nonempty captured tool identity")
                captured["tool_choice"] = ToolRef(value.name, value.namespace)
            elif value not in ("auto", "none", "required"):
                raise ValueError("tool_choice must be an outer mode or ToolRef")
    return cast(ModelSettings, captured)


@dataclass(frozen=True, kw_only=True)
class CodexLogin:
    """Reuse an existing file-backed Codex login for a local native Session.

    Codex locates its usual login directory unless you supply ``codex_home``.
    Set ``account_id`` to require a specific ChatGPT account or workspace.
    The SDK checks the saved login when it opens a Session.

    Trusted workspace execution requires ``credential_directory``, an absolute
    path to an application-owned directory dedicated to credentials.
    The selected ``codex_home/auth.json`` must resolve to ``auth.json`` in that
    directory. Keep the directory and its ancestors in place while Sessions
    use it. The executor cannot read that directory. Use strict execution
    for an existing login whose auth file shares a directory with ordinary data.

    This experimental mode may change or be absent from the public beta or
    general availability of the Agents SDK.
    """

    codex_home: str | Path | None = field(default=None, repr=False)
    account_id: str | None = field(default=None, repr=False)
    credential_directory: str | Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.credential_directory is not None and (
            not isinstance(self.credential_directory, (str, Path))
            or not Path(self.credential_directory).is_absolute()
        ):
            raise ValueError("credential_directory must be an absolute path")
        if self.codex_home is not None and (
            not isinstance(self.codex_home, (str, Path)) or not str(self.codex_home).strip()
        ):
            raise ValueError("codex_home must be a non-empty path")
        if self.account_id is not None and (
            not isinstance(self.account_id, str) or not self.account_id.strip()
        ):
            raise ValueError("account_id must be a non-empty string")


@dataclass(frozen=True, kw_only=True)
class CodexAccount:
    """The saved plan type and optional workspace ID at Session opening."""

    account_id: str | None
    plan_type: str


_SIWC_ISSUER = "https://auth.openai.com"
_SIWC_RESOURCE = "https://api.openai.com/v1"
_SIWC_PROVIDER_PREFIX = "native_agent_siwc_"
_SIWC_VERIFIED_PROVIDER_PREFIX = _SIWC_PROVIDER_PREFIX + "verified_"


def _valid_siwc_bearer(token: str, client_id: str) -> bool:
    """Check the declared route and lifetime of the record's access token."""
    if not isinstance(token, str) or not re.fullmatch(r"[\w-]+\.[\w-]+\.[\w-]+", token, re.ASCII):
        return False
    payload = token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, RecursionError):
        return False
    if not isinstance(claims, dict):
        return False
    scope = claims.get("scope")
    expires = claims.get("exp")
    return (
        claims.get("iss") == _SIWC_ISSUER
        and claims.get("aud") == _SIWC_RESOURCE
        and claims.get("client_id") == client_id
        and isinstance(scope, str)
        and {"resource.invoke", "chatgpt.tokens.use.direct"}.issubset(scope.split())
        and type(expires) is int
        and expires > _time()
    )


@dataclass(frozen=True, kw_only=True, slots=True, repr=False)
class SignInWithChatGPTCredentials:
    """The verified identity and its current bearer from one protected app record.

    Set issuer, client_id, and subject from the application's verified sign-in
    identity and issued registration. Set access_token from the same credential
    record. On refresh, preserve the verified identity and replace the bearer
    with the token issued for that record.
    """

    issuer: str
    client_id: str
    subject: str
    access_token: str


@dataclass(frozen=True, kw_only=True)
class SignInWithChatGPT:
    """Use a registered integration's ChatGPT plan for native Responses.

    After authenticating the current application user, supply an async callback
    that returns one snapshot from that user's protected OAuth credential record.
    At sign-in, the application verifies the ID token and associates its identity
    with the credentials from the same exchange. Authorize the user's access to
    a saved conversation before asking the SDK to restore it. Return None when
    no authenticated credential record is available.

    The SDK binds the Session to the verified identity and captures its bearer
    for Responses. To use a renewed bearer, open a new Session with the same
    verified identity and restore the conversation.

    See https://developers.openai.com/siwc/token-sharing-open-source for
    eligibility, registration, and supported Responses capabilities.
    """

    get_credentials: Callable[[], Awaitable[SignInWithChatGPTCredentials | None]] = field(
        repr=False
    )

    def __post_init__(self) -> None:
        if not callable(self.get_credentials):
            raise TypeError("get_credentials must be an async credential callback")

    async def _resolve(self) -> tuple[str, str]:
        failure: BaseException
        try:
            credentials = await self.get_credentials()
        except asyncio.CancelledError:
            failure = asyncio.CancelledError()
        except Exception:
            failure = RuntimeError("Could not obtain Sign in with ChatGPT credentials")
        else:
            if (
                isinstance(credentials, SignInWithChatGPTCredentials)
                and credentials.issuer == _SIWC_ISSUER
                and isinstance(credentials.client_id, str)
                and credentials.client_id.strip()
                and isinstance(credentials.subject, str)
                and credentials.subject.strip()
                and _valid_siwc_bearer(credentials.access_token, credentials.client_id)
            ):
                identity = (credentials.issuer, credentials.client_id, credentials.subject)
                binding = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                return credentials.access_token, _SIWC_VERIFIED_PROVIDER_PREFIX + binding
            del credentials
            failure = ValueError(
                "Provide verified Sign in with ChatGPT credentials with an active access token"
            )
        # A credential source can include secrets in its exception or cause.
        raise failure


class _ProviderStrings(dict[str, str]):
    """Frozen captured strings that preserve dataclass asdict/copy/pickle support."""

    def _immutable(self: object, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError("Captured provider routing is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __reduce__(self) -> Any:
        return (type(self), (dict(self),))


@dataclass(frozen=True, kw_only=True)
class NativeProvider:
    """A native Responses connection. Construction performs no I/O.

    For API-key authentication, an omitted key uses ``OPENAI_API_KEY`` when
    the Session opens. Credentials and connection URLs are excluded from the
    descriptor's representation.
    With ``supports_websockets=None``, the SDK permits WebSockets only when
    ``base_url=None``; an explicit ``base_url`` defaults to HTTP. Set ``True``
    to permit WebSockets for a compatible custom provider, or ``False`` to use
    HTTP. The native runtime selects the final transport when WebSockets are
    enabled and owns retries. On Linux, an eligible private one-shot
    ``Agent.run()`` completion may reuse idle HTTP connections when this choice is
    implicit and no routing, proxy or CA override is present. Explicit WebSocket choices and live
    Session transport permission remain unchanged.

    ``request_max_retries`` controls retries of failed HTTP requests;
    ``stream_max_retries`` controls reconnections of dropped response streams.
    Omission uses native defaults (4 and 5 respectively). Zero disables retries
    at that layer. The native runtime caps each configured count at 100.

    Set ``response_mode="buffered"`` to request complete JSON responses from an
    endpoint that supports nonstreaming Responses. Model output events become
    available after the full response arrives, without token-level progress.
    Each HTTP response body is limited to 64 MiB and one million structural
    characters (``{}[],:``) outside strings. Exceeding either limit fails without
    retry. Each model response is buffered separately. Tool calls start after that
    response completes; events remain available while tools execute.
    Successful output uses the normal Session history and tool processing.
    Provider failures deliver no partial output; validated response metadata
    remains available in the turn record. This mode uses HTTP with
    API-key authentication and cannot be combined with
    ``supports_websockets=True`` or plan authentication. The default is ``"streaming"``.
    Native child agents are not supported in buffered mode.

    Set ``auth=CodexLogin()`` to use the saved Codex login over HTTP.
    Omit ``api_key`` and ``base_url`` in this experimental mode.

    Set ``auth=SignInWithChatGPT(get_credentials=...)`` for a registered
    integration with ChatGPT plan usage permission. The SDK uses HTTP streaming
    on OpenAI Responses and keeps the Session bound to the registration.
    Omit ``api_key``, ``base_url``, and custom routing. Leave both retry limits
    unset or set them to zero so the application owns retry timing. The SDK
    reports credential and entitlement failures without switching billing modes.

    Retry budgets and phase timeouts are native provider settings. Omission keeps
    Core defaults. Zero disables retries or sets an immediate phase deadline.
    Core caps request and stream retry budgets at 100 and retains retry safety,
    cancellation, and replay ownership. Timeouts use integer milliseconds.

    Routing maps are captured at construction. ``env_headers`` resolves values
    once; missing/empty values stay omitted. Query pairs use URL encoding.
    Supplied routing options reject same-origin and cross-origin redirects.
    Native auth/protocol/private-hop headers remain reserved. Construct a new
    provider to refresh these captured values.

    Explicit providers are borrowed by Sessions. Close those Sessions before
    leaving the provider's async context or awaiting ``aclose()``.

    Eligible API-key HTTP Responses calls share idle connections across fresh
    Sessions on Linux when proxy routing variables are absent. TLS configuration
    and cached system trust material may remain in use until this explicit
    Provider closes. OS trust changes,
    including removed roots, may require a new Provider to refresh construction.
    With only ``SSL_CERT_FILE`` set,
    each request rereads and validates that file and changes retire cached
    connections. Other CA, proxy-policy, cookie and login modes retain fresh
    transport construction. Closing the Provider joins native owners before
    releasing its idle clients. Sessions retain fresh auth, settings and history.
    Synchronize process-environment changes with SDK calls. Changes between
    calls are supported; client construction does not atomically snapshot the
    ambient environment.
    """

    base_url: str | None = field(default=None, repr=False)
    api_key: str | None = field(default=None, repr=False)
    supports_websockets: bool | None = None
    auth: CodexLogin | SignInWithChatGPT | None = field(default=None, repr=False)
    response_mode: Literal["streaming", "buffered"] = "streaming"
    query_params: Mapping[str, str] | None = field(default=None, repr=False, hash=False)
    headers: Mapping[str, str] | None = field(default=None, repr=False, hash=False)
    env_headers: Mapping[str, str] | None = field(default=None, repr=False, hash=False)
    organization: str | None = field(default=None, repr=False)
    project: str | None = field(default=None, repr=False)
    model_backend: NativeModel | ModelProvider | None = field(default=None, repr=False, hash=False)
    request_max_retries: int | None = None
    stream_max_retries: int | None = None
    stream_idle_timeout_ms: int | None = field(default=None, repr=False)
    websocket_connect_timeout_ms: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for name in (
            "request_max_retries",
            "stream_max_retries",
            "stream_idle_timeout_ms",
            "websocket_connect_timeout_ms",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1
            ):
                raise ValueError(f"{name} must be a non-negative signed 64-bit integer or None")
        for name in ("base_url", "api_key"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a non-empty string when provided")
        if self.supports_websockets is not None and not isinstance(self.supports_websockets, bool):
            raise ValueError("supports_websockets must be a bool or None")
        if self.response_mode not in ("streaming", "buffered"):
            raise ValueError("response_mode must be 'streaming' or 'buffered'")
        if self.response_mode == "buffered" and self.supports_websockets is True:
            raise ValueError("Buffered Responses cannot use WebSockets")
        if self.auth is not None:
            if not isinstance(self.auth, (CodexLogin, SignInWithChatGPT)):
                raise TypeError("auth must be CodexLogin, SignInWithChatGPT or None")
            if self.response_mode == "buffered":
                raise ValueError("Buffered Responses require API-key authentication")
            if isinstance(self.auth, SignInWithChatGPT) and (
                self.request_max_retries not in (None, 0)
                or self.stream_max_retries not in (None, 0)
            ):
                raise ValueError("SignInWithChatGPT requires application-managed retries")
            if self.api_key is not None or self.base_url is not None or self.supports_websockets:
                raise ValueError(
                    f"{type(self.auth).__name__} requires HTTP and its own endpoint and credentials"
                )
        if self.model_backend is not None:
            if not any(
                callable(getattr(self.model_backend, name, None))
                for name in ("complete", "get_model")
            ):
                raise TypeError(
                    "model_backend must implement complete(request) or get_model(model_name)"
                )
            if self.supports_websockets is True or self.auth is not None:
                raise ValueError("model_backend requires HTTP mode without plan authentication")
        self._capture_routing()

    def _capture_routing(self) -> None:
        names = ("query_params", "headers", "env_headers", "organization", "project")
        configured = any(getattr(self, name) is not None for name in names)
        if configured and self.auth is not None:
            raise ValueError("Custom provider routing requires API-key authentication")
        if configured:
            try:
                parsed = urlsplit(self.base_url or "https://api.openai.com/v1")
            except ValueError:
                raise ValueError(
                    "Custom provider routing requires an absolute HTTP(S) base URL"
                ) from None
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise ValueError(
                    "Custom provider routing requires an absolute HTTP(S) URL "
                    "without userinfo or fragment"
                )
        captured: dict[str, dict[str, str] | None] = {}
        for name in ("query_params", "headers", "env_headers"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, Mapping)
                or any(
                    not isinstance(key, str) or not key or not isinstance(item, str)
                    for key, item in value.items()
                )
            ):
                raise TypeError(f"{name} must be a string map with nonempty keys")
            captured[name] = dict(value) if value is not None else None
            object.__setattr__(self, name, _ProviderStrings(value) if value is not None else None)
        protected = {
            "authorization",
            "proxy-authorization",
            "cookie",
            "host",
            "connection",
            "upgrade",
            "content-length",
            "transfer-encoding",
            "te",
            "trailer",
            "keep-alive",
            "proxy-connection",
            "content-type",
            "accept",
            "accept-encoding",
            "user-agent",
            "openai-beta",
            "originator",
            "session_id",
            "x-client-request-id",
            "x-request-id",
            "chatgpt-account-id",
            "openai-organization",
            "openai-project",
            "x-responsesapi-include-timing-metrics",
            "x-sdk-sentinel-grant",
        }
        headers: dict[str, str] = {}
        seen: set[str] = set()
        for field_name in ("headers", "env_headers"):
            for key, value in (captured[field_name] or {}).items():
                lower = key.lower()
                if (
                    not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                    or lower in protected
                    or lower.startswith(("sec-websocket-", "x-codex-", "x-openai-"))
                ):
                    raise ValueError(
                        "Provider header name is invalid or belongs to native transport/auth"
                    )
                if lower in seen:
                    raise ValueError("Provider header names must be unique ignoring case")
                seen.add(lower)
                if field_name == "env_headers":
                    if not value or "=" in value or "\x00" in value:
                        raise ValueError("env_headers values must name environment variables")
                    selected = os.environ.get(value)
                    if selected is None or not selected.strip():
                        continue
                    value = selected
                if any(ord(char) < 32 or ord(char) > 126 for char in value):
                    raise ValueError("Provider header values must contain printable ASCII")
                headers[lower] = value
        for name, header in (
            ("organization", "openai-organization"),
            ("project", "openai-project"),
        ):
            value = getattr(self, name)
            if value is not None:
                if (
                    not isinstance(value, str)
                    or not value.strip()
                    or any(ord(char) < 32 or ord(char) > 126 for char in value)
                ):
                    raise ValueError(f"{name} must be a nonempty printable ASCII string")
                headers[header] = value
        object.__setattr__(
            self,
            "_routing",
            (
                tuple(sorted((captured["query_params"] or {}).items())),
                tuple(sorted(headers.items())),
                configured,
            ),
        )

    def _routing_config(self) -> dict[str, Any]:
        query, headers, _ = vars(self).get("_routing", ((), (), False))
        result: dict[str, Any] = {}
        if self.query_params is not None:
            result["query_params"] = dict(query)
        if any(
            value is not None
            for value in (self.headers, self.env_headers, self.organization, self.project)
        ):
            result["http_headers"] = dict(headers)
        return result

    @property
    def _websockets_enabled(self) -> bool:
        if self.model_backend is not None:
            return False
        return (
            self.auth is None
            and self.response_mode == "streaming"
            and (
                self.supports_websockets
                if self.supports_websockets is not None
                else self.base_url is None
            )
        )

    def __reduce_ex__(self, protocol: SupportsIndex) -> Any:
        reduction = super().__reduce_ex__(protocol)
        if not isinstance(reduction, tuple) or len(reduction) < 3:
            return reduction
        state = reduction[2]
        dictionary = state[0] if isinstance(state, tuple) and state else state
        if not isinstance(dictionary, dict):
            return reduction
        # Copy settings and the closed fence, never another descriptor's resource.
        dictionary = dictionary.copy()
        for name in ("query_params", "headers", "env_headers"):
            if dictionary.get(name) is not None:
                dictionary[name] = dict(dictionary[name])
        dictionary.pop("_transport", None)
        dictionary.pop("_deferred_openings", None)
        state = (dictionary, *state[1:]) if isinstance(state, tuple) else dictionary
        return (*reduction[:2], state, *reduction[3:])

    def __setstate__(self, state: Any) -> None:
        # Object reduction can carry both instance dictionary and subclass slots.
        states = state if isinstance(state, tuple) else (state,)
        for attributes in states:
            if attributes is None:
                continue
            for name, value in attributes.items():
                if name in ("query_params", "headers", "env_headers") and value is not None:
                    value = _ProviderStrings(value)
                object.__setattr__(self, name, value)

    def _acquire_lease(self) -> tuple[Any, Any, Any]:
        # This lock guards only synchronous publication. It is never held across
        # an await and carries no event-loop affinity.
        with _PROVIDER_LOCK:
            if vars(self).get("_closed", False):
                raise RuntimeError("NativeProvider is closed")
            native = importlib.import_module("agents._native")
            transport = vars(self).get("_transport")
            if transport is None:
                transport = native.NativeProviderOwner()
                object.__setattr__(self, "_transport", transport)
            return native, transport, transport.acquire()

    def _defer_opening(self, opening: _OpeningOwner, environment: _EnvironmentLease) -> None:
        # Failed publication is no longer an opening Session. Both its native
        # reservations and its Python manager now belong to provider recovery.
        with _PROVIDER_LOCK:
            opening.defer_environment(environment)
            pending = vars(self).get("_deferred_openings")
            if pending is None:
                pending = {}
                object.__setattr__(self, "_deferred_openings", pending)
            if opening not in pending:
                environment._sync_run = _sync_run.get()
                if environment._sync_run is not None:
                    environment._sync_run.deferred += 1
            pending[opening] = environment

    async def aclose(self) -> None:
        """Release remaining acquisitions and fence this Provider permanently.

        Open or opening Sessions cause an immediate error. Close them first.
        Closing an explicitly shared provider fences every Agent that uses it.
        Cancellation joins the same native retirement before it propagates.
        This also recovers a managed Environment retained after a failed
        run_sync opening, in the task that originally entered its manager.
        """
        with _PROVIDER_LOCK:
            if vars(self).get("_closed", False):
                return
            transport = vars(self).get("_transport")
            if transport is None:
                object.__setattr__(self, "_closed", True)
                return
            deferred = tuple(vars(self).get("_deferred_openings", {}).items())
            # Admission and the close fence happen before any application exit.
            # Deferred reservations are native orphans; other openings stay live.
            native_closing = transport.close()
            if deferred:

                async def release_openings() -> None:
                    for opening, environment in deferred:

                        async def release(
                            opening: _OpeningOwner = opening,
                            environment: _EnvironmentLease = environment,
                        ) -> None:
                            try:
                                await opening.fail_environment(environment)
                            finally:
                                # Native retirement permits exit; only the owner
                                # task proves that manager cleanup has completed.
                                if environment._owner.done():
                                    with _PROVIDER_LOCK:
                                        removed = (
                                            vars(self)
                                            .get("_deferred_openings", {})
                                            .pop(opening, None)
                                        )
                                        if removed is not None and removed._sync_run is not None:
                                            removed._sync_run.deferred -= 1

                        owner = environment._sync_run
                        if owner is not None and owner.loop is not asyncio.get_running_loop():
                            await _join(
                                asyncio.create_task(asyncio.to_thread(owner.recover, release))
                            )
                        else:
                            await release()

                async def close_with_openings() -> bool:
                    try:
                        rotation_save_failed = await native_closing
                    except BaseException as error:
                        # A home removal error alone must not strand a manager.
                        # fail_environment separately proves its resource join.
                        await _close_after_error(error, release_openings)
                        raise
                    await release_openings()
                    return bool(rotation_save_failed)

                operation = close_with_openings()
            else:
                operation = native_closing

        async def retire() -> None:
            rotation_save_failed = await operation
            with _PROVIDER_LOCK:
                object.__setattr__(self, "_closed", True)
                object.__setattr__(self, "_transport", None)
            if rotation_save_failed:
                raise RuntimeError("Could not save the refreshed Codex login")

        await _join(asyncio.create_task(retire()))

    async def __aenter__(self) -> NativeProvider:
        with _PROVIDER_LOCK:
            if vars(self).get("_closed", False):
                raise RuntimeError("NativeProvider is closed")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc is None:
            await self.aclose()
        else:
            await _close_after_error(exc, self.aclose)


_PROVIDER_LOCK = RLock()


@dataclass(init=False, eq=False, repr=False)
class Agent(Generic[ContextT, OutputT]):
    """Declare a native agent without starting a runtime or connection.

    ``decisions`` supplies an optional typed decision provider. Callback
    helpers inherit the captured provider and accept per-call overrides.
    It never changes the Agent's model API.

    Declaration fields are immutable. Construct a new Agent to change defaults;
    tools and model settings are captured at construction. A session captures
    effective overrides when it opens. The native runtime owns
    provider requests, the model loop, admitted calls, and conversation history.
    Python functions execute in the application and use its own authority.
    Include NativeAgents in tools to grant native child agents at opening.

    ``handoffs`` declares named native Agent targets. A synchronous function
    returning an Agent can refer to a target defined later, including cycles.
    Targets are resolved once when the Session captures the graph; subsequent
    model handoffs select from those captured declarations.

    ``transform_context`` runs before each model request, including children,
    output repair, and model-based compaction. Return replacement input items
    or None to keep the original input. The callback does not edit saved history.
    Raising an exception prevents that request from reaching the provider.

    ``before_compact`` runs before automatic or manual native compaction,
    including children. Return a nonempty summary to skip native summarization,
    or None to keep the default. Codex publishes the summary in its history.

    ``before_complete`` checks proposed completion for roots and native children.
    ``tool_batch_completion`` can propose a final value once a direct function
    batch has settled. A ``FinishToolBatch`` value passes through the active
    output type and completion policy. Returning ``None`` lets the turn continue.
    Return None to accept, ContinueTurn with instructions to keep the same turn
    running, or StopTurn to abort by policy. Rejected text may already have
    streamed; only accepted completion settles the turn.

    ``programmatic_tool_calling=True`` also permits native JavaScript cells to call the declared
    function and MCP tools. MCP result transforms run before those cells
    receive results. The cells use the SDK's bundled host and do not grant
    shell tools or a local workspace. Pass Environment to session() for
    remote execution. Use on_interaction or a Session to answer manual requests made by code cells.
    The tools selected for each Code Mode step must have unique generated identifiers.
    If names collide, select only one of the functions with the same generated
    identifier for each step, or use direct mode.
    """

    instructions: str
    name: str | None
    tools: tuple[Tool[ContextT], ...]
    handoffs: tuple[
        Agent[ContextT, OutputT]
        | Handoff[ContextT, OutputT]
        | RealtimeHandoff
        | _Callable[[], Agent[ContextT, OutputT]],
        ...,
    ]
    model: str | None
    model_settings: ModelSettings
    provider: NativeProvider | None
    output_type: OutputSpec[OutputT] | None
    programmatic_tool_calling: bool
    transform_context: _ContextTransform[ContextT] | None
    before_compact: _BeforeCompact[ContextT] | None
    before_complete: _BeforeComplete[ContextT] | None
    decisions: DecisionProvider | None
    realtime: RealtimeAgentConfig | None
    input_guardrails: tuple[InputGuardrail[ContextT], ...]
    output_guardrails: tuple[OutputGuardrail[ContextT], ...]
    tool_batch_completion: _ToolBatchCompletion[ContextT, OutputT] | None
    hosted_prompt: HostedPromptSource | None

    def __setattr__(self, name: str, value: object) -> None:
        # Freeze declared behavior while retaining application-owned method hooks.
        if name in self.__dataclass_fields__:
            raise FrozenInstanceError(f"cannot assign to field {name!r}")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name in self.__dataclass_fields__:
            raise FrozenInstanceError(f"cannot delete field {name!r}")
        object.__delattr__(self, name)

    if TYPE_CHECKING:
        # Pyright uses these allocation types to retain defaults and subclasses.
        # The initializer checks the full call; runtime allocation stays ordinary.
        @overload
        def __new__(
            cls: type[TextAgentT],
            *args: Any,
            tools: Sequence[Tool[ContextT]] | None = None,
            handoffs: Sequence[
                Agent[ContextT, str]
                | Handoff[ContextT, str]
                | RealtimeHandoff
                | _Callable[[], Agent[ContextT, str]]
            ]
            | None = None,
            output_type: None = None,
            transform_context: _ContextTransform[ContextT] | None = None,
            before_compact: _BeforeCompact[ContextT] | None = None,
            before_complete: _BeforeComplete[ContextT] | None = None,
            tool_batch_completion: _ToolBatchCompletion[ContextT, OutputT] | None = None,
            input_guardrails: Sequence[InputGuardrail[ContextT]] | None = None,
            output_guardrails: Sequence[OutputGuardrail[ContextT]] | None = None,
            **kwargs: Any,
        ) -> TextAgentT: ...

        @overload
        def __new__(
            cls: type[TypedAgentT],
            *args: Any,
            output_type: OutputSpec[OutputT],
            tools: Sequence[Tool[ContextT]] | None = None,
            handoffs: Sequence[
                Agent[ContextT, OutputT]
                | Handoff[ContextT, OutputT]
                | RealtimeHandoff
                | _Callable[[], Agent[ContextT, OutputT]]
            ]
            | None = None,
            transform_context: _ContextTransform[ContextT] | None = None,
            before_compact: _BeforeCompact[ContextT] | None = None,
            before_complete: _BeforeComplete[ContextT] | None = None,
            tool_batch_completion: _ToolBatchCompletion[ContextT, OutputT] | None = None,
            input_guardrails: Sequence[InputGuardrail[ContextT]] | None = None,
            output_guardrails: Sequence[OutputGuardrail[ContextT]] | None = None,
            **kwargs: Any,
        ) -> TypedAgentT: ...

        # Subclasses may supply output_type inside their own initializer.
        @overload
        def __new__(cls: type[TypedAgentT], *args: Any, **kwargs: Any) -> TypedAgentT: ...

        def __new__(cls, *args: Any, **kwargs: Any) -> Agent[Any, Any]:
            return object.__new__(cls)

    @overload
    def __init__(
        # Mypy 1.17 needs the class context here to preserve explicit generics
        # while specializing the default output. Pyright rejects this form.
        self: Agent[ContextT, str],  # pyright: ignore[reportInvalidTypeVarUse]
        *,
        instructions: str,
        name: str | None = None,
        tools: Sequence[Tool[ContextT]] | None = None,
        handoffs: Sequence[
            Agent[ContextT, str]
            | Handoff[ContextT, str]
            | RealtimeHandoff
            | _Callable[[], Agent[ContextT, str]]
        ]
        | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        provider: NativeProvider | None = None,
        decisions: DecisionProvider | None = None,
        output_type: None = None,
        programmatic_tool_calling: bool = False,
        transform_context: _ContextTransform[ContextT] | None = None,
        before_compact: _BeforeCompact[ContextT] | None = None,
        before_complete: _BeforeComplete[ContextT] | None = None,
        tool_batch_completion: _ToolBatchCompletion[ContextT, OutputT] | None = None,
        realtime: RealtimeAgentConfig | None = None,
        input_guardrails: Sequence[InputGuardrail[ContextT]] = (),
        output_guardrails: Sequence[OutputGuardrail[ContextT]] = (),
        hosted_prompt: HostedPromptSource | None = None,
    ) -> None: ...

    @overload
    def __init__(
        # Mypy 1.17 needs the class context here to preserve explicit generics
        # while specializing the default output. Pyright rejects this form.
        self: Agent[ContextT, str],  # pyright: ignore[reportInvalidTypeVarUse]
        *,
        instructions: str | None = None,
        hosted_prompt: HostedPromptSource,
        name: str | None = None,
        tools: Sequence[Tool[ContextT]] | None = None,
        handoffs: Sequence[
            Agent[ContextT, str]
            | Handoff[ContextT, str]
            | RealtimeHandoff
            | _Callable[[], Agent[ContextT, str]]
        ]
        | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        provider: NativeProvider | None = None,
        decisions: DecisionProvider | None = None,
        output_type: None = None,
        programmatic_tool_calling: bool = False,
        transform_context: _ContextTransform[ContextT] | None = None,
        before_compact: _BeforeCompact[ContextT] | None = None,
        before_complete: _BeforeComplete[ContextT] | None = None,
        tool_batch_completion: _ToolBatchCompletion[ContextT, OutputT] | None = None,
        input_guardrails: Sequence[InputGuardrail[ContextT]] = (),
        output_guardrails: Sequence[OutputGuardrail[ContextT]] = (),
        realtime: RealtimeAgentConfig | None = None,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        instructions: str,
        name: str | None = None,
        output_type: OutputSpec[OutputT],
        tools: Sequence[Tool[ContextT]] | None = None,
        handoffs: Sequence[
            Agent[ContextT, OutputT]
            | Handoff[ContextT, OutputT]
            | RealtimeHandoff
            | _Callable[[], Agent[ContextT, OutputT]]
        ]
        | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        provider: NativeProvider | None = None,
        decisions: DecisionProvider | None = None,
        programmatic_tool_calling: bool = False,
        transform_context: _ContextTransform[ContextT] | None = None,
        before_compact: _BeforeCompact[ContextT] | None = None,
        before_complete: _BeforeComplete[ContextT] | None = None,
        tool_batch_completion: _ToolBatchCompletion[ContextT, OutputT] | None = None,
        realtime: RealtimeAgentConfig | None = None,
        input_guardrails: Sequence[InputGuardrail[ContextT]] = (),
        output_guardrails: Sequence[OutputGuardrail[ContextT]] = (),
        hosted_prompt: HostedPromptSource | None = None,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        instructions: str | None = None,
        hosted_prompt: HostedPromptSource,
        name: str | None = None,
        output_type: OutputSpec[OutputT],
        tools: Sequence[Tool[ContextT]] | None = None,
        handoffs: Sequence[
            Agent[ContextT, OutputT]
            | Handoff[ContextT, OutputT]
            | RealtimeHandoff
            | _Callable[[], Agent[ContextT, OutputT]]
        ]
        | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        provider: NativeProvider | None = None,
        decisions: DecisionProvider | None = None,
        programmatic_tool_calling: bool = False,
        transform_context: _ContextTransform[ContextT] | None = None,
        before_compact: _BeforeCompact[ContextT] | None = None,
        before_complete: _BeforeComplete[ContextT] | None = None,
        tool_batch_completion: _ToolBatchCompletion[ContextT, OutputT] | None = None,
        input_guardrails: Sequence[InputGuardrail[ContextT]] = (),
        output_guardrails: Sequence[OutputGuardrail[ContextT]] = (),
        realtime: RealtimeAgentConfig | None = None,
    ) -> None: ...

    def __init__(
        self,
        *,
        instructions: str | None = None,
        name: str | None = None,
        tools: Sequence[Tool[ContextT]] | None = None,
        handoffs: Sequence[
            Agent[ContextT, Any]
            | Handoff[ContextT, Any]
            | RealtimeHandoff
            | _Callable[[], Agent[ContextT, Any]]
        ]
        | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        provider: NativeProvider | None = None,
        decisions: DecisionProvider | None = None,
        output_type: OutputSpec[OutputT] | None = None,
        programmatic_tool_calling: bool = False,
        transform_context: _ContextTransform[ContextT] | None = None,
        before_compact: _BeforeCompact[ContextT] | None = None,
        before_complete: _BeforeComplete[ContextT] | None = None,
        tool_batch_completion: _ToolBatchCompletion[ContextT, OutputT] | None = None,
        realtime: RealtimeAgentConfig | None = None,
        input_guardrails: Sequence[InputGuardrail[ContextT]] = (),
        output_guardrails: Sequence[OutputGuardrail[ContextT]] = (),
        hosted_prompt: HostedPromptSource | None = None,
    ) -> None:
        captured_prompt = _capture_hosted_prompt_source(hosted_prompt)
        if not isinstance(instructions, str) and not (
            instructions is None and captured_prompt is not None
        ):
            raise TypeError("instructions are required without a hosted prompt")
        if name is not None and not isinstance(name, str):
            raise TypeError("name must be a string or None")
        if not isinstance(programmatic_tool_calling, bool):
            raise TypeError("programmatic_tool_calling must be a bool")
        if transform_context is not None and not callable(transform_context):
            raise TypeError("transform_context must be callable or None")
        if before_compact is not None and not callable(before_compact):
            raise TypeError("before_compact must be callable or None")
        if before_complete is not None and not callable(before_complete):
            raise TypeError("before_complete must be callable or None")
        if tool_batch_completion is not None and not callable(tool_batch_completion):
            raise TypeError("tool_batch_completion must be callable or None")
        if model is not None and not isinstance(model, str):
            raise TypeError("model must be a string or None")
        if provider is not None and not isinstance(provider, NativeProvider):
            raise TypeError("provider must be a NativeProvider or None")
        from .handoffs import Handoff
        from .realtime_agents import RealtimeAgentConfig, RealtimeHandoff

        captured_handoffs = tuple(handoffs or ())
        if any(
            not isinstance(target, (Agent, Handoff, RealtimeHandoff)) and not callable(target)
            for target in captured_handoffs
        ):
            raise TypeError("handoffs must contain native Agents or synchronous target resolvers")
        if realtime is not None and not isinstance(realtime, RealtimeAgentConfig):
            raise TypeError("realtime must be RealtimeAgentConfig or None")
        object.__setattr__(self, "realtime", realtime._capture() if realtime is not None else None)
        object.__setattr__(self, "hosted_prompt", captured_prompt)
        object.__setattr__(self, "_hosted_tools_provided", tools is not None)
        object.__setattr__(self, "_hosted_instructions_provided", instructions is not None)
        object.__setattr__(self, "instructions", instructions if instructions is not None else "")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "tools", tuple(tools or ()))
        object.__setattr__(self, "handoffs", captured_handoffs)
        object.__setattr__(self, "model", model)
        if decisions is not None:
            _check_provider(decisions)
        object.__setattr__(self, "decisions", decisions)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(
            self, "model_settings", MappingProxyType(_copy_model_settings(model_settings))
        )
        object.__setattr__(self, "output_type", output_type)
        object.__setattr__(self, "programmatic_tool_calling", programmatic_tool_calling)
        object.__setattr__(self, "transform_context", transform_context)
        object.__setattr__(self, "before_compact", before_compact)
        object.__setattr__(self, "before_complete", before_complete)
        object.__setattr__(self, "tool_batch_completion", tool_batch_completion)
        for name, checks, kind in (
            ("input_guardrails", input_guardrails, InputGuardrail),
            ("output_guardrails", output_guardrails, OutputGuardrail),
        ):
            captured_checks = tuple(checks)
            if any(not isinstance(check, kind) for check in captured_checks):
                raise TypeError(f"{name} contains an invalid guardrail")
            object.__setattr__(self, name, captured_checks)

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: None = None,
        output_type: None = None,
    ) -> bool: ...

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: Sequence[_DecisionT],
        output_type: None = None,
    ) -> _DecisionT: ...

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        output_type: type[_DecisionT],
        choices: None = None,
    ) -> _DecisionT: ...

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        output_type: Any,
        choices: None = None,
    ) -> Any: ...

    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: Sequence[Any] | None = None,
        output_type: Any = None,
    ) -> Any:
        """Answer a typed question using this Agent's Decisions default.

        This starts no Agent run and records no conversation or turn usage.
        A per-call provider overrides the declaration's ``decisions`` default.
        """
        return await _decide(
            question,
            input=input,
            provider=_check_provider(provider if provider is not None else self.decisions),
            choices=choices,
            output_type=output_type,
        )

    def validate(self) -> None:
        """Check the default declaration and captured handoff graph without opening a Session.

        Return None on success, or raise the first TypeError or ValueError.
        Uses the opening checks for tool admission, MCP names and output schema.
        Context, Environment, effective overrides and native model support can
        only be checked when a Session opens. This never reads credentials or
        connects to a provider, executor or MCP server.
        """
        from .mcp import _tool_names
        from .schema import _prepare_spec
        from .session import _check_namespaced_tool_collisions, _split_tools

        native = importlib.import_module("agents._native")

        def check(declaration: Agent[Any, Any]) -> None:
            _hosted_prompt_controls(
                declaration.hosted_prompt,
                model=declaration.model,
                instructions=(
                    declaration.instructions
                    if getattr(declaration, "_hosted_instructions_provided", False)
                    else None
                ),
                tools_provided=getattr(declaration, "_hosted_tools_provided", False),
                extra_body=declaration.model_settings.get("extra_body"),
            )
            python_tools, mcp_tools, native_agents = _split_tools(
                declaration.tools, declaration.programmatic_tool_calling
            )
            _check_namespaced_tool_collisions(
                python_tools,
                _tool_names(mcp_tools),
                declaration.tools,
                native_agents=native_agents,
            )
            native._validate_python_tool_declarations(
                json.dumps([json.loads(tool._spec_json()) for tool in python_tools])
            )
            _prepare_spec(declaration.output_type)

        if self.handoffs:
            from ._agent_profiles import _capture_agent_graph

            declarations = tuple(profile.declaration for profile in _capture_agent_graph(self))
        else:
            declarations = (self,)
        for declaration in declarations:
            check(declaration)

    def with_output_type(self, output_type: OutputSpec[NewOutputT]) -> Agent[ContextT, NewOutputT]:
        """Return a declaration variant with the same settings and a new output.

        To restore a per-turn manual override, pass the corresponding variant
        to session(state=...). Current tools, context and provider still apply.
        Application subclass hooks and their policy references are retained;
        the application initializer is not run again.
        Agents with outgoing handoffs require a new graph with compatible
        output types. A per-submit output override can still cover a whole turn.
        """
        if self.handoffs:
            raise ValueError("with_output_type requires an Agent without outgoing handoffs")
        variant = copy(self)
        object.__setattr__(variant, "output_type", output_type)
        return cast("Agent[ContextT, NewOutputT]", variant)

    @overload
    def session(
        self: Agent[None, OutputT],
        *,
        context: None = None,
        state: SessionState | ManagedWorkspaceCheckpoint | None = None,
        restored_output_type: OutputSpec[Any] | None = None,
        initial_messages: Sequence[InitialMessage] | None = None,
        storage: LocalStorage | CustomStorage | None = None,
        resume: str | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[None] | None = None,
        tools: Sequence[Tool[None]] | None = None,
        instruction_files: Sequence[Path] = (),
        reconciled_calls: Sequence[ReconciledCall] = (),
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        on_interaction: _InteractionHandler | None = None,
        tool_error_formatter: ToolErrorFormatter | None = None,
        max_function_concurrency: int | None = None,
        observation_retention: Literal["bounded", "all"] = "bounded",
        include_model_output_items: bool = False,
        include_raw_provider_events: bool = False,
        input_guardrails: Sequence[InputGuardrail[Any]] = (),
        output_guardrails: Sequence[OutputGuardrail[Any]] = (),
        project_guidance: bool = False,
        discover_agents_md: bool = False,
    ) -> AbstractAsyncContextManager[Session[None, OutputT]]: ...

    @overload
    def session(
        self: Agent[object, OutputT],
        *,
        context: object = None,
        state: SessionState | ManagedWorkspaceCheckpoint | None = None,
        restored_output_type: OutputSpec[Any] | None = None,
        initial_messages: Sequence[InitialMessage] | None = None,
        storage: LocalStorage | CustomStorage | None = None,
        resume: str | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[object] | None = None,
        tools: Sequence[Tool[object]] | None = None,
        instruction_files: Sequence[Path] = (),
        reconciled_calls: Sequence[ReconciledCall] = (),
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        on_interaction: _InteractionHandler | None = None,
        tool_error_formatter: ToolErrorFormatter | None = None,
        max_function_concurrency: int | None = None,
        observation_retention: Literal["bounded", "all"] = "bounded",
        include_model_output_items: bool = False,
        include_raw_provider_events: bool = False,
        input_guardrails: Sequence[InputGuardrail[Any]] = (),
        output_guardrails: Sequence[OutputGuardrail[Any]] = (),
        project_guidance: bool = False,
        discover_agents_md: bool = False,
    ) -> AbstractAsyncContextManager[Session[object, OutputT]]: ...

    @overload
    def session(
        self,
        *,
        context: ContextT,
        state: SessionState | ManagedWorkspaceCheckpoint | None = None,
        restored_output_type: OutputSpec[Any] | None = None,
        initial_messages: Sequence[InitialMessage] | None = None,
        storage: LocalStorage | CustomStorage | None = None,
        resume: str | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[ContextT] | None = None,
        tools: Sequence[Tool[ContextT]] | None = None,
        instruction_files: Sequence[Path] = (),
        reconciled_calls: Sequence[ReconciledCall] = (),
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        on_interaction: _InteractionHandler | None = None,
        tool_error_formatter: ToolErrorFormatter | None = None,
        max_function_concurrency: int | None = None,
        observation_retention: Literal["bounded", "all"] = "bounded",
        include_model_output_items: bool = False,
        include_raw_provider_events: bool = False,
        input_guardrails: Sequence[InputGuardrail[Any]] = (),
        output_guardrails: Sequence[OutputGuardrail[Any]] = (),
        project_guidance: bool = False,
        discover_agents_md: bool = False,
    ) -> AbstractAsyncContextManager[Session[ContextT, OutputT]]: ...

    @asynccontextmanager
    async def session(
        self: Agent[SessionContextT, OutputT],
        *,
        context: SessionContextT | None = None,
        state: SessionState | ManagedWorkspaceCheckpoint | None = None,
        restored_output_type: OutputSpec[Any] | None = None,
        initial_messages: Sequence[InitialMessage] | None = None,
        storage: LocalStorage | CustomStorage | None = None,
        resume: str | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[SessionContextT] | None = None,
        tools: Sequence[Tool[SessionContextT]] | None = None,
        instruction_files: Sequence[Path] = (),
        reconciled_calls: Sequence[ReconciledCall] = (),
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        on_interaction: _InteractionHandler | None = None,
        tool_error_formatter: ToolErrorFormatter | None = None,
        max_function_concurrency: int | None = _MAX_FUNCTION_CONCURRENCY_UNSET,
        observation_retention: Literal["bounded", "all"] = _OBSERVATION_RETENTION_UNSET,
        include_model_output_items: bool = False,
        include_raw_provider_events: bool = False,
        input_guardrails: Sequence[InputGuardrail[Any]] = (),
        output_guardrails: Sequence[OutputGuardrail[Any]] = (),
        project_guidance: bool = False,
        discover_agents_md: bool = False,
    ) -> AsyncIterator[Session[SessionContextT, OutputT]]:
        """Open one conversation and close all runtime work on context exit.

        restored_output_type selects only a pending restored turn's output
        decoder, validator and repair policy. It must match the saved contract.
        It leaves this Session's future output default unchanged. Use str for a
        restored plain-text override; omit it to use the source Agent's default.

        initial_messages seeds settled user messages and assistant text without
        starting a model turn. It accepts UserMessage media through the current
        native route; Core can normalize images. Pass it only for a fresh opening,
        without state or resume, even when the sequence is empty. The Session
        becomes usable after native publication and its durability fence. An
        in-memory store adds no crash guarantee. Failed startup can leave accepted
        history in persistent storage; recover it instead of resubmitting seeds.

        observation_retention="all" keeps every emitted observation in memory for
        this Session lifetime, including for late readers. Memory grows with
        emitted payloads until Session references are released. The default "bounded"
        retains a limited suffix and reports gaps. This policy is not checkpointed.
        include_model_output_items=True records ordinary assistant messages and
        provider reasoning summaries in the Session's observation stream.
        include_raw_provider_events=True captures Responses provider JSON. Use
        include_execution_details=True on the observation reader to receive it.
        Capture uses the Session's in-memory observation retention policy.

        max_function_concurrency bounds automatic Python callbacks per turn owner,
        including validation and hooks through reply settlement. None adds no
        limit. Manual requests and native tools do not consume slots. Each child
        owner has an independent limit. Callback deadlines exclude this wait.
        When a DeferredTools.start session hook omits this argument, the restored
        Session inherits the source limit. An explicit value, including None,
        overrides that limit.

        on_interaction handles supported live requests for this Session's entire
        lifetime, including requests without Turn IDs. Return the exact request's
        reply. A failure affects the identified Turn, or the Session for an
        unscoped request. Closing joins started handlers. The handler is registered
        after supported opening completes; it does not enable startup prompts.

        parent_trace parents startup and defaults every new root or restored manual
        continuation. None supplies no explicit parent. Pass a fresh worker
        carrier on restore; trace context is never restored from saved state.

        An explicit provider here overrides this Agent's provider. Both are
        borrowed; the application closes them. When neither is supplied, this
        Session creates and closes its own provider, including on opening failure.
        `model=None` inherits the Agent model. Present model_settings keys
        override Agent defaults; only a present `service_tier=None` clears a
        value. Other omitted keys keep the Agent or native defaults.

        Omitted ``tools`` or ``tools=None`` uses this Agent's tools.
        A sequence replaces the finite opening definitions and defaults for
        this Session. An empty sequence admits none. Current callbacks and MCP
        connection details are captured at opening. Fresh and idle-restored
        Sessions acquire external MCP resources when an operation selecting
        them starts. Startup errors are reported on that Turn. Restoring an
        already-admitted pending manual turn readies its selected resources
        before exposing its replies.

        trace_options captures native tracing defaults before opening. Every
        submission inherits its disabled/content restrictions, including when
        another ambient scope or explicit parent changes the trace linkage.
        Tracing context and export credentials never enter SessionState.

        project_guidance loads native workspace instructions and enables native
        workspace image viewing. It requires an Environment and uses only that
        executor's granted paths. It does not grant command or write access.

        ``environment`` belongs to this session, so one Agent can use different
        application-owned executors for different users. A supplied Environment is
        borrowed. A standard async context manager yielding an Environment
        enters and exits in one SDK lease task. Native work closes before exit,
        including on opening failure. Lease-local ContextVar changes stay in
        that task. Restore uses only the supplied current environment and roots;
        checkpoints contain no credentials.

        With ``state``, restore an idle or settled manual native checkpoint
        using the effective provider, current tools, and application context. Tool declarations and
        instructions must match the saved conversation. Saved model settings
        take precedence over this Agent's model settings. Non-None model or
        model_settings overrides, including an empty mapping, are rejected with
        state. Use update(model=...) on the restored Session after it becomes idle.
        Live submission handles
        and workspace files do not transfer to the restored Session. Each restore
        owns isolated native storage; applications coordinate shared state files.
        Programmatic tool calling uses this Agent's current declaration, not a saved setting;
        enabling it on restore requires ``programmatic_tool_calling=True`` again.
        Load checkpoints only from trusted application storage: their history
        includes instructions. Unverifiable histories are rejected.

        With ``storage=CustomStorage(backend)``, use an application-owned
        canonical native backend. See SessionStore for its exclusive
        ownership and commit contract. This preview supports root Sessions;
        child agents, goals, checklist, code mode, manual and native tools,
        and environments are rejected.

        With ``storage=LocalStorage(path)``, use Core's native local storage.
        Retain Session.id and supply ``resume=id`` in a later process to reopen
        the same conversation. This requires the current tools, context and
        provider. An unknown root or descendant tool outcome raises LocalRecoveryRequired.
        Check the effect using application records and pass reconciled_calls
        with the original root Session ID. The SDK records each result in the
        owning thread without restarting the interrupted work. Read descendant
        results with ``session.items(subagent_id=call.thread_id)``. Local storage and portable
        ``state`` cannot be combined. Local storage rejects code mode because
        individual nested tool identities lack durable intent and result receipts.

        Pass instruction_files to load trusted UTF-8 files as application
        instructions. Use files and parent directories that untrusted actors
        cannot modify. Each file's receipt identifies the exact bytes loaded.

        Set discover_agents_md=True to trust Codex instruction files on the
        application host and in the selected Environment. Core loads user
        guidance from CODEX_HOME (default ~/.codex), followed by project
        guidance from the nearest Git root through the Environment's working
        directory, within the executor's read policy. Without a Git root,
        Core checks only the working directory. Without an Environment,
        Core loads only user guidance. Core checks AGENTS.override.md before
        AGENTS.md at each location. More specific files take precedence over
        broader guidance. Application instructions, including instruction_files,
        have developer priority. This option loads instruction files only.
        Pass it again to discover files when restoring or resuming. Previously
        supplied instructions can still appear in the conversation history.
        """
        try:
            initial_messages = _capture_initial_messages(
                initial_messages, state=state, resume=resume
            )
            async with self._session_context(
                context=context,
                state=state,
                restored_output_type=restored_output_type,
                initial_messages=initial_messages,
                storage=storage,
                resume=resume,
                provider=provider,
                model=model,
                model_settings=model_settings,
                environment=environment,
                tools=tools,
                instruction_files=instruction_files,
                project_guidance=project_guidance,
                reconciled_calls=reconciled_calls,
                parent_trace=parent_trace,
                trace_options=trace_options,
                on_interaction=on_interaction,
                tool_error_formatter=tool_error_formatter,
                max_function_concurrency=max_function_concurrency,
                observation_retention=observation_retention,
                include_model_output_items=include_model_output_items,
                include_raw_provider_events=include_raw_provider_events,
                input_guardrails=input_guardrails,
                output_guardrails=output_guardrails,
                discover_agents_md=discover_agents_md,
            ) as session:
                from .session import Session

                assert isinstance(session, Session)
                yield session
        finally:
            del trace_options, environment

    @asynccontextmanager
    async def _session_context(
        self: Agent[SessionContextT, OutputT],
        *,
        context: SessionContextT | None = None,
        state: SessionState | ManagedWorkspaceCheckpoint | None = None,
        restored_output_type: OutputSpec[Any] | None = None,
        initial_messages: Sequence[InitialMessage] | None = None,
        storage: LocalStorage | CustomStorage | None = None,
        resume: str | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[SessionContextT] | None = None,
        tools: Sequence[Tool[SessionContextT]] | None = None,
        instruction_files: Sequence[Path] = (),
        reconciled_calls: Sequence[ReconciledCall] = (),
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        on_interaction: _InteractionHandler | None = None,
        tool_error_formatter: ToolErrorFormatter | None = None,
        max_function_concurrency: int | None = None,
        observation_retention: Literal["bounded", "all"] = _OBSERVATION_RETENTION_UNSET,
        include_model_output_items: bool = False,
        include_raw_provider_events: bool = False,
        input_guardrails: Sequence[InputGuardrail[Any]] = (),
        output_guardrails: Sequence[OutputGuardrail[Any]] = (),
        discover_agents_md: bool = False,
        _atomic_first_turn: bool = False,
        _completed_input: str | None = None,
        _private_text_run: bool = False,
        project_guidance: bool = False,
    ) -> AsyncIterator[Session[SessionContextT, OutputT] | Completed[OutputT]]:
        if tool_error_formatter is not None and not callable(tool_error_formatter):
            raise TypeError("tool_error_formatter must be callable or None")
        if on_interaction is not None and not callable(on_interaction):
            raise TypeError("on_interaction must be callable or None")
        from .session import Completed, Session
        from .telemetry import _capture

        try:
            parent_trace = _trace_context(parent_trace)
            captured = _capture(trace_options)
        finally:
            del trace_options
        trace_error: BaseException | None = None
        try:
            session = await Session[SessionContextT, OutputT]._open(
                self,
                context=context,
                state=state,
                restored_output_type=restored_output_type,
                initial_messages_json=_initial_messages_json(initial_messages),
                storage=storage,
                resume=resume,
                environment=environment,
                provider=provider,
                model=model,
                model_settings=model_settings,
                tool_selection=tools,
                instruction_files=instruction_files,
                project_guidance=project_guidance,
                reconciled_calls=reconciled_calls,
                max_function_concurrency=max_function_concurrency,
                tool_error_formatter=tool_error_formatter,
                observation_retention=observation_retention,
                include_model_output_items=include_model_output_items,
                include_raw_provider_events=include_raw_provider_events,
                input_guardrails=input_guardrails,
                output_guardrails=output_guardrails,
                discover_agents_md=discover_agents_md,
                parent_trace=parent_trace,
                _atomic_first_turn=_atomic_first_turn,
                _completed_input=_completed_input,
                _private_text_run=_private_text_run,
                **cast(
                    "_TracingOpeningOptions",
                    {"_tracing_context": captured.context} if captured.context is not None else {},
                ),
            )
            if isinstance(session, Completed):
                yield session
                return
            session._trace_context = captured.context
            if on_interaction is not None:
                session._start_interaction_handler(on_interaction)
            try:
                yield session
            except BaseException as error:
                lease = session._environment_lease
                await _close_after_error(error, session.aclose)
                if not session._closed or session._environment_lease is not None:
                    cast(Any, error).cleanup = session
                if lease is not None:
                    lease.raise_if_interrupted(error)
                raise
            else:
                lease = session._environment_lease
                try:
                    await session.aclose()
                except BaseException as error:
                    if lease is not None:
                        lease.raise_if_interrupted(error)
                    raise

        except BaseException as error:
            trace_error = error
            raise
        finally:
            del environment
            captured.close(trace_error)

    @overload
    async def run(
        self: Agent[None, OutputT],
        input: UserInput | None,
        *,
        context: None = None,
        on_interaction: _InteractionHandler | None = None,
        tool_error_formatter: ToolErrorFormatter | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[None] | None = None,
        tools: Sequence[ToolRef | Tool[None] | Environment[None] | NativeAgents[None]]
        | None = None,
        before_step: _BeforeStep[None] | None = None,
        state: SessionState | None = None,
        initial_messages: Sequence[InitialMessage] | None = None,
        return_items: bool = False,
        capture_error_details: bool = False,
        return_state: bool = False,
        return_history: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = None,
        observation_retention: Literal["bounded", "all"] = "bounded",
        input_guardrails: Sequence[InputGuardrail[Any]] = (),
        output_guardrails: Sequence[OutputGuardrail[Any]] = (),
        return_raw_usage: bool = False,
    ) -> Completed[OutputT]: ...

    @overload
    async def run(
        self: Agent[object, OutputT],
        input: UserInput | None,
        *,
        context: object = None,
        on_interaction: _InteractionHandler | None = None,
        tool_error_formatter: ToolErrorFormatter | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[object] | None = None,
        tools: Sequence[ToolRef | Tool[object] | Environment[object] | NativeAgents[object]]
        | None = None,
        before_step: _BeforeStep[object] | None = None,
        state: SessionState | None = None,
        initial_messages: Sequence[InitialMessage] | None = None,
        return_items: bool = False,
        capture_error_details: bool = False,
        return_state: bool = False,
        return_history: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = None,
        observation_retention: Literal["bounded", "all"] = "bounded",
        input_guardrails: Sequence[InputGuardrail[Any]] = (),
        output_guardrails: Sequence[OutputGuardrail[Any]] = (),
        return_raw_usage: bool = False,
    ) -> Completed[OutputT]: ...

    @overload
    async def run(
        self,
        input: UserInput | None,
        *,
        context: ContextT,
        on_interaction: _InteractionHandler | None = None,
        tool_error_formatter: ToolErrorFormatter | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[ContextT] | None = None,
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents[ContextT]]
        | None = None,
        before_step: _BeforeStep[ContextT] | None = None,
        state: SessionState | None = None,
        initial_messages: Sequence[InitialMessage] | None = None,
        return_items: bool = False,
        capture_error_details: bool = False,
        return_state: bool = False,
        return_history: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = None,
        observation_retention: Literal["bounded", "all"] = "bounded",
        input_guardrails: Sequence[InputGuardrail[Any]] = (),
        output_guardrails: Sequence[OutputGuardrail[Any]] = (),
        return_raw_usage: bool = False,
    ) -> Completed[OutputT]: ...

    async def run(
        self: Agent[SessionContextT, OutputT],
        input: UserInput | None,
        *,
        context: SessionContextT | None = None,
        on_interaction: _InteractionHandler | None = None,
        tool_error_formatter: ToolErrorFormatter | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[SessionContextT] | None = None,
        tools: Sequence[
            ToolRef
            | Tool[SessionContextT]
            | Environment[SessionContextT]
            | NativeAgents[SessionContextT]
        ]
        | None = None,
        before_step: _BeforeStep[SessionContextT] | None = None,
        state: SessionState | None = None,
        initial_messages: Sequence[InitialMessage] | None = None,
        return_items: bool = False,
        capture_error_details: bool = False,
        return_state: bool = False,
        return_history: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = _MAX_FUNCTION_CONCURRENCY_UNSET,
        observation_retention: Literal["bounded", "all"] = _OBSERVATION_RETENTION_UNSET,
        input_guardrails: Sequence[InputGuardrail[Any]] = (),
        output_guardrails: Sequence[OutputGuardrail[Any]] = (),
        return_raw_usage: bool = False,
    ) -> Completed[OutputT]:
        """Run one submission through a Session and close it on completion.

        parent_trace sets the W3C parent for native work, including a restored
        manual continuation. trace_options controls captured SDK workflow policy.

        max_function_concurrency follows session() and bounds automatic callbacks.

        provider, model, and model_settings follow the same precedence and
        restore restrictions as session(). An implicit provider is retired when
        the run closes, including failure and cancellation.

        prepare_request(turn, request, *, preparation_index, recovery_attempt)
        edits a detached next-request sampling copy. Return its complete value
        or None. Use an explicitly HTTP provider and eager ordinary function/MCP
        tools without children, Environment or code mode. The callback must
        tolerate repeated preparation and avoid business effects. HTTP retries
        reuse its captured edit; native stream recovery calls it again. Separate
        output correction attempts are unsupported; set max_retries=0 or use
        NativeOutput. Callback operations cannot suspend or export active state. Cancellation joins
        callback settlement before a later operation can start.

        on_interaction receives a live request and returns request.reply(value),
        directly or through an awaitable. The SDK validates and delivers it.
        For approvals, use reply("allow_once") or reply("deny"). For forms,
        use reply(values); reply(None) declines a form or URL request.
        Native replies acknowledge enqueue only. Direct Session callers can use
        session.respond(request.reply(value)) or request-specific response methods.
        Handlers may run concurrently; synchronous
        handlers run in worker threads. The SDK joins them before closing.
        Without a handler, a plain or native-output Python/manual-only run can complete or
        raise InteractionRequired with a checkpoint at a settled manual call.
        Other interactive combinations require a handler or Agent.session().

        budget limits this logical run's next root sampling admission, including
        output repair. Pass on_interaction if the budgeted run uses manual tools.
        The SDK cannot capture a budgeted active run or add a budget to a
        restored active turn. return_state=True can still save an idle result.

        Pass state to restore with current bindings. For a supported pending
        manual checkpoint, input=None resumes the existing turn. Idle state
        needs input for a new turn. return_state=True captures a successful
        state before close; save_state restrictions and failures still apply.
        return_history=True captures conversation records before close for replay
        through another Session's transact_history. That Session supplies its own instructions.
        History above 4,096 original records or 8 MiB raises NativeError after completion.

        return_raw_usage=True returns original usage from accepted Responses requests
        completed during this invocation, in response order.
        The SDK keeps these objects out of history and checkpoints. A capture above
        4,096 responses or 8 MiB raises NativeError after completion.
        return_items=True retains the accepted assistant and tool items in
        Completed.items with their producing Agents.
        capture_error_details=True retains run evidence on submission errors.
        Read the snapshot from RunDiagnosticError.details. The original error is its cause.
        """
        capture_error_details = capture_error_details is True
        audio_budget = [_MAX_AUDIO_INPUT_BYTES]
        initial_messages = _capture_initial_messages(
            initial_messages, state=state, _audio_budget=audio_budget
        )
        if input is not None and (
            audio_budget[0] != _MAX_AUDIO_INPUT_BYTES or _contains_audio(input)
        ):
            parts = _native_input(input, _audio_budget=audio_budget)
            _check_audio_json({"initial_messages": initial_messages, "input": parts})
            if not isinstance(input, str):
                input = cast("UserInput", {"role": "user", "content": _response_content(parts)})
        error_input = deepcopy(input) if capture_error_details else None
        options: dict[str, Any] = {}
        if capture_error_details:
            options["include_raw_provider_events"] = True
        raw_usage_token = None
        produced_items_token = None
        try:
            from ._function_tools import FunctionTool
            from .manual import _CapturedApproval
            from .prepared_request import _check_output, _check_profile, _check_tools
            from .schema import PromptedOutput, _prepare_spec
            from .session import (
                CapabilityError,
                InteractionRequired,
                NativeError,
                RunDiagnosticError,
                _handoff_default_names,
                _produced_items_restore,
                _raw_usage_restore,
                _split_tools,
            )
            from .state import SessionState
            from .tools import _HOSTED_TOOLS, WebSearch, _CapturedFunction

            if budget is not None and not isinstance(budget, RunBudget):
                raise TypeError("budget must be a RunBudget or None")
            if prepare_request is not None:
                if not callable(prepare_request):
                    raise TypeError("prepare_request must be callable")
                if getattr(self.session, "__func__", None) is Agent.session:
                    _check_output(_prepare_spec(self.output_type))
                    effective_provider = self.provider if provider is None else provider
                    if effective_provider is not None and not isinstance(
                        effective_provider, NativeProvider
                    ):
                        raise TypeError("provider must be a NativeProvider or None")
                    _check_profile(
                        self,
                        websockets=True
                        if effective_provider is None
                        else effective_provider._websockets_enabled,
                        environment=environment,
                    )
                    if tools is None and before_step is None and state is None:
                        _check_tools(self.tools)
            if tool_error_formatter is not None and not callable(tool_error_formatter):
                raise TypeError("tool_error_formatter must be callable or None")
            if on_interaction is not None and not callable(on_interaction):
                raise TypeError("on_interaction must be callable")
            if not isinstance(return_state, bool):
                raise TypeError("return_state must be a bool")
            if state is not None and not isinstance(state, SessionState):
                raise TypeError("state must be a SessionState")
            resume_manual = state is not None and state.native_continuation is not None
            if resume_manual:
                if budget is not None:
                    raise CapabilityError("Cannot set a budget when restoring an active checkpoint")
                if input is not None:
                    raise ValueError("Use input=None to resume a pending manual checkpoint")
                if on_interaction is None:
                    error = InteractionRequired(
                        "Resuming a pending manual checkpoint requires on_interaction "
                        "or Agent.session()"
                    )
                    error.state = state
                    raise error
                if tools is not None or before_step is not None or prepare_request is not None:
                    raise ValueError(
                        "Cannot change tools, before_step or prepare_request "
                        "on an already admitted turn"
                    )
            elif input is None:
                raise TypeError("input is required unless resuming a pending manual checkpoint")

            declared, mcp_tools, native_agents = _split_tools(
                self.tools, self.programmatic_tool_calling
            )
            pause_manual = tuple(
                tool
                for tool in declared
                if on_interaction is None
                and before_step is None
                and prepare_request is None
                and (tool._bind is None or tool.needs_approval is not False)
                and (tools is None or tool in tools or tool.ref in tools)
            )
            if state is None and budget is not None and pause_manual:
                raise CapabilityError(
                    "An active run with a budget cannot be captured for manual replies. "
                    "Pass on_interaction or use Agent.session() for live replies."
                )
            # A restored operation selects tools from the captured active profile.
            if (
                state is None
                and pause_manual
                and (
                    self.programmatic_tool_calling
                    or isinstance(self.output_type, PromptedOutput)
                    or environment is not None
                    or native_agents is not None
                    or before_step is not None
                    or any(tool._requires_live_manual_session for tool in declared)
                    or (
                        mcp_tools
                        and (tools is None or _split_tools(tools, available=self.tools)[1])
                    )
                )
            ):
                raise InteractionRequired(
                    "Pausing for manual replies requires ordinary Python callbacks "
                    "and bare manual tools, "
                    "plain output or native strict output, "
                    "no environment, children or before_step. "
                    "Selected MCP bindings and native MCP work require live replies. "
                    "Pass on_interaction or use Agent.session() for live replies."
                )
            # Capture before opening. An override may expose the Session and its
            # observations, including when it delegates to the original method.
            session_factory: _Callable[
                ...,
                AbstractAsyncContextManager[Session[SessionContextT, OutputT] | Completed[OutputT]],
            ] = self.session
            if initial_messages is not None:
                options["initial_messages"] = initial_messages
            if tool_error_formatter is not None:
                options["tool_error_formatter"] = tool_error_formatter
            if on_interaction is not None:
                options["on_interaction"] = on_interaction
            if parent_trace is not None:
                options["parent_trace"] = _trace_context(parent_trace)
            if trace_options is not None:
                options["trace_options"] = trace_options
            if state is not None:
                options["state"] = state
            if provider is not None:
                options["provider"] = provider
            if model is not None:
                options["model"] = model
            if model_settings is not None:
                options["model_settings"] = model_settings
            if input_guardrails:
                options["input_guardrails"] = input_guardrails
            if output_guardrails:
                options["output_guardrails"] = output_guardrails
            if observation_retention is not _OBSERVATION_RETENTION_UNSET:
                options["observation_retention"] = observation_retention
            if max_function_concurrency is not _MAX_FUNCTION_CONCURRENCY_UNSET:
                options["max_function_concurrency"] = max_function_concurrency
            if environment is not None:
                options["environment"] = environment
            if (
                isinstance(session_factory, MethodType)
                and session_factory.__self__ is self
                and session_factory.__func__ is _BUILTIN_SESSION_FACTORY
                and state is None
                and initial_messages is None
                and input is not None
                and tools is None
                and environment is None
                and before_step is None
                and prepare_request is None
                and on_interaction is None
                and budget is None
                and not input_guardrails
                and not output_guardrails
            ):
                # Only the built-in one-shot opening may defer its initial
                # instruction receipt to the first ordinary submission.
                session_factory = self._session_context
                options["_atomic_first_turn"] = True
                if not (
                    return_history or return_raw_usage or return_items or capture_error_details
                ) and _private_text_run_eligible(
                    self, input, context, return_state, parent_trace, trace_options
                ):
                    options["_private_text_run"] = True
                if not (
                    return_history or return_raw_usage or return_items or capture_error_details
                ) and _completed_run_eligible(
                    self, input, context, return_state, parent_trace, trace_options
                ):
                    options["_completed_input"] = input
            if return_raw_usage and resume_manual:
                assert state is not None
                raw_usage_token = _raw_usage_restore.set((self, state, asyncio.current_task()))
            if (return_items or capture_error_details) and resume_manual:
                assert state is not None
                produced_items_token = _produced_items_restore.set(
                    (self, state, asyncio.current_task())
                )
            async with session_factory(
                context=cast(SessionContextT, context),
                **options,
            ) as session:
                from .session import Completed

                if raw_usage_token is not None:
                    _raw_usage_restore.reset(raw_usage_token)
                    raw_usage_token = None
                if produced_items_token is not None:
                    _produced_items_restore.reset(produced_items_token)
                    produced_items_token = None
                if isinstance(session, Completed):
                    return session
                if (
                    pause_manual
                    or (prepare_request is not None and on_interaction is None)
                    or (
                        session._profiles
                        and on_interaction is None
                        and before_step is None
                        and prepare_request is None
                    )
                ):
                    # A delegating factory may change the opening roster or its
                    # default operation. Check the same effective selection that
                    # submit will capture, before it admits work or starts MCP.
                    opening = session._effective_agent()
                    handoffs = session._effective_handoffs()
                    effective = session._default_tools if tools is None else tools
                    selected, selected_mcp, _ = _split_tools(
                        opening.tools if effective is None else effective,
                        opening.programmatic_tool_calling,
                        available=None if effective is None else session._bindings,
                        ceiling=opening.tools,
                        environment=session._executor,
                        native_agents=None if effective is None else session._native_agents,
                        handoffs=handoffs,
                    )
                    if on_interaction is None and before_step is None and prepare_request is None:
                        selected_manual = tuple(
                            tool
                            for tool in selected
                            if tool._bind is None or tool.needs_approval is not False
                        )
                        if state is not None:
                            active_profile = session._profile_current
                            pause_declarations: Sequence[Tool[Any]] = declared
                            if (
                                active_profile is not None
                                and session._profiles[active_profile[0]].profile.agent is not self
                            ):
                                pause_declarations = opening.tools
                            pause_manual = tuple(
                                tool
                                for tool in selected_manual
                                if any(tool is grant for grant in pause_declarations)
                            )
                        # Selection can include a replacement from a delegating factory.
                        # Budget preflight sees that tool without granting checkpoint authority.
                        has_reachable_manual = bool(selected_manual)
                        # An explicit operation keeps its original declaration
                        # ceiling. Omitted tools follow captured destination defaults.
                        names = _handoff_default_names(effective, handoffs)
                        pending = [
                            target
                            for name, target in (handoffs or {}).items()
                            if tools is None and (names is None or name in names)
                        ]
                        visited: set[str] = set()
                        for target in pending:
                            if target in visited:
                                continue
                            visited.add(target)
                            profile = session._profiles[target].profile
                            target_manual = tuple(
                                tool
                                for tool in profile.declaration.tools
                                if isinstance(tool, FunctionTool)
                                and (tool._bind is None or tool.needs_approval is not False)
                                and not tool._requires_live_manual_session
                            )
                            has_reachable_manual |= bool(target_manual)
                            pause_manual += tuple(
                                tool
                                for tool in target_manual
                                if profile.agent is not self
                                or any(tool is grant for grant in declared)
                            )
                            pending.extend(profile.handoffs.values())
                        if budget is not None and has_reachable_manual:
                            raise CapabilityError(
                                "An active run with a budget cannot be captured "
                                "for manual replies. Pass on_interaction or use "
                                "Agent.session() for live replies."
                            )
                    if (
                        prepare_request is not None
                        and before_step is None
                        and any(
                            tool._bind is None or tool.needs_approval is not False
                            for tool in selected
                        )
                    ):
                        raise InteractionRequired(
                            "Running manual tools with prepare_request requires on_interaction. "
                            "Use Agent.session() to reply to requests directly."
                        )
                    # Submit can republish bound tools beyond the restored idle catalog.
                    if any(tool is eligible for tool in selected for eligible in pause_manual) and (
                        opening.programmatic_tool_calling
                        or session._default_output.mode == "prompted"
                        or session._executor is not None
                        or session._native_agents is not None
                        or session._storage is not None
                        or selected_mcp
                        or any(
                            isinstance(tool, (_CapturedFunction, _CapturedApproval))
                            and tool._requires_live_manual_session
                            for tool in session._bound_tools
                        )
                        or any(
                            isinstance(tool, (WebSearch, *_HOSTED_TOOLS)) for tool in opening.tools
                        )
                    ):
                        raise InteractionRequired(
                            "Pausing for manual replies requires ordinary Python callbacks "
                            "and bare manual tools, "
                            "plain output or native strict output, "
                            "no local storage, environment, children or before_step. "
                            "Selected MCP bindings and native MCP work require live replies. "
                            "Pass on_interaction or use Agent.session() for live replies."
                        )
                session._fail_unhandled_questions = (
                    session._interaction_handler is None and not pause_manual
                )
                if return_raw_usage:
                    session._start_raw_usage()
                if return_items or capture_error_details:
                    session._start_produced_items()
                if resume_manual:
                    restored = session.restored_turn
                    if restored is None:
                        raise NativeError("Restored manual checkpoint has no pending submission")
                    submission = cast("Turn[OutputT]", restored)
                else:
                    assert input is not None
                    submit_options: dict[str, Any] = {}
                    if prepare_request is not None:
                        submit_options["prepare_request"] = prepare_request
                    if budget is not None:
                        submit_options["budget"] = budget
                    if "_completed_input" in options and session._last_turn is not None:
                        submission = cast("Turn[OutputT]", session._last_turn)
                    else:
                        submission = await session.submit(
                            input, tools=tools, before_step=before_step, **submit_options
                        )
                if session._interaction_handler is None and pause_manual:
                    return await session._run_with_interactions(
                        submission,
                        on_interaction,
                        return_state=return_state,
                        return_history=return_history,
                        return_raw_usage=return_raw_usage,
                        return_items=return_items,
                        capture_error_details=capture_error_details,
                        error_input=error_input,
                        pause_manual=pause_manual,
                    )
                try:
                    result = await submission.get_result()
                except Exception as error:
                    if capture_error_details:
                        details = await session._capture_run_error_details(submission, error_input)
                        if details is not None:
                            raise RunDiagnosticError(details) from error
                    raise
                if return_state:
                    result = replace(result, state=await session.save_state())
                if return_history:
                    result = replace(result, history=await session._run_history())
                if return_raw_usage:
                    result = replace(result, raw_usage=session._take_raw_usage(result.turn))
                if return_items:
                    result = replace(result, items=session._take_produced_items(result.turn))
                return result
        finally:
            if raw_usage_token is not None:
                _raw_usage_restore.reset(raw_usage_token)
            if produced_items_token is not None:
                _produced_items_restore.reset(produced_items_token)
            options.pop("trace_options", None)
            del trace_options

    @overload
    def run_sync(
        self: Agent[None, OutputT],
        input: UserInput | None,
        *,
        context: None = None,
        on_interaction: _InteractionHandler | None = None,
        tool_error_formatter: ToolErrorFormatter | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[None] | None = None,
        tools: Sequence[ToolRef | Tool[None] | Environment[None] | NativeAgents[None]]
        | None = None,
        before_step: _BeforeStep[None] | None = None,
        state: SessionState | None = None,
        initial_messages: Sequence[InitialMessage] | None = None,
        return_items: bool = False,
        capture_error_details: bool = False,
        return_state: bool = False,
        return_history: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = None,
        observation_retention: Literal["bounded", "all"] = "bounded",
        input_guardrails: Sequence[InputGuardrail[Any]] = (),
        output_guardrails: Sequence[OutputGuardrail[Any]] = (),
        return_raw_usage: bool = False,
    ) -> Completed[OutputT]: ...

    @overload
    def run_sync(
        self: Agent[object, OutputT],
        input: UserInput | None,
        *,
        context: object = None,
        on_interaction: _InteractionHandler | None = None,
        tool_error_formatter: ToolErrorFormatter | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[object] | None = None,
        tools: Sequence[ToolRef | Tool[object] | Environment[object] | NativeAgents[object]]
        | None = None,
        before_step: _BeforeStep[object] | None = None,
        state: SessionState | None = None,
        initial_messages: Sequence[InitialMessage] | None = None,
        return_items: bool = False,
        capture_error_details: bool = False,
        return_state: bool = False,
        return_history: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = None,
        observation_retention: Literal["bounded", "all"] = "bounded",
        input_guardrails: Sequence[InputGuardrail[Any]] = (),
        output_guardrails: Sequence[OutputGuardrail[Any]] = (),
        return_raw_usage: bool = False,
    ) -> Completed[OutputT]: ...

    @overload
    def run_sync(
        self,
        input: UserInput | None,
        *,
        context: ContextT,
        on_interaction: _InteractionHandler | None = None,
        tool_error_formatter: ToolErrorFormatter | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[ContextT] | None = None,
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents[ContextT]]
        | None = None,
        before_step: _BeforeStep[ContextT] | None = None,
        state: SessionState | None = None,
        initial_messages: Sequence[InitialMessage] | None = None,
        return_items: bool = False,
        capture_error_details: bool = False,
        return_state: bool = False,
        return_history: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = None,
        observation_retention: Literal["bounded", "all"] = "bounded",
        input_guardrails: Sequence[InputGuardrail[Any]] = (),
        output_guardrails: Sequence[OutputGuardrail[Any]] = (),
        return_raw_usage: bool = False,
    ) -> Completed[OutputT]: ...

    def run_sync(
        self: Agent[SessionContextT, OutputT],
        input: UserInput | None,
        *,
        context: SessionContextT | None = None,
        on_interaction: _InteractionHandler | None = None,
        tool_error_formatter: ToolErrorFormatter | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[SessionContextT] | None = None,
        tools: Sequence[
            ToolRef
            | Tool[SessionContextT]
            | Environment[SessionContextT]
            | NativeAgents[SessionContextT]
        ]
        | None = None,
        before_step: _BeforeStep[SessionContextT] | None = None,
        state: SessionState | None = None,
        initial_messages: Sequence[InitialMessage] | None = None,
        return_items: bool = False,
        capture_error_details: bool = False,
        return_state: bool = False,
        return_history: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = _MAX_FUNCTION_CONCURRENCY_UNSET,
        observation_retention: Literal["bounded", "all"] = _OBSERVATION_RETENTION_UNSET,
        input_guardrails: Sequence[InputGuardrail[Any]] = (),
        output_guardrails: Sequence[OutputGuardrail[Any]] = (),
        return_raw_usage: bool = False,
    ) -> Completed[OutputT]:
        """Run through session() outside an event loop.

        on_interaction, state, return_history, return_raw_usage, and return_items follow run().
        A sync or async handler
        returns request.reply(value); the SDK delivers it before closing.

        If opening with a managed Environment fails and automatic cleanup leaves
        the implicit provider open, the original error carries ``error.provider``.
        Outside an event loop, call ``asyncio.run(error.provider.aclose())`` to
        retry cleanup. Explicit providers remain the caller's responsibility and
        are not attached to the error.
        """
        initial_messages = _capture_initial_messages(initial_messages, state=state)
        if budget is not None and not isinstance(budget, RunBudget):
            raise TypeError("budget must be a RunBudget or None")
        options: dict[str, Any] = {}
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                if parent_trace is not None:
                    options["parent_trace"] = _trace_context(parent_trace)
                if trace_options is not None:
                    options["trace_options"] = trace_options
                if state is not None:
                    options["state"] = state
                if initial_messages is not None:
                    options["initial_messages"] = initial_messages
                if return_items:
                    options["return_items"] = return_items
                if capture_error_details is True:
                    options["capture_error_details"] = True
                if return_state is not False:
                    options["return_state"] = return_state
                if return_history is not False:
                    options["return_history"] = return_history
                if return_raw_usage:
                    options["return_raw_usage"] = return_raw_usage
                if tool_error_formatter is not None:
                    options["tool_error_formatter"] = tool_error_formatter
                if on_interaction is not None:
                    options["on_interaction"] = on_interaction
                if provider is not None:
                    options["provider"] = provider
                if model is not None:
                    options["model"] = model
                if model_settings is not None:
                    options["model_settings"] = model_settings
                if input_guardrails:
                    options["input_guardrails"] = input_guardrails
                if output_guardrails:
                    options["output_guardrails"] = output_guardrails
                if observation_retention is not _OBSERVATION_RETENTION_UNSET:
                    options["observation_retention"] = observation_retention
                if max_function_concurrency is not _MAX_FUNCTION_CONCURRENCY_UNSET:
                    options["max_function_concurrency"] = max_function_concurrency
                if environment is not None:
                    options["environment"] = environment
                if prepare_request is not None:
                    options["prepare_request"] = prepare_request
                if budget is not None:
                    options["budget"] = budget
                return _SyncRun().run(
                    self.run(
                        input,
                        context=cast(SessionContextT, context),
                        tools=tools,
                        before_step=before_step,
                        **options,
                    )
                )
            raise RuntimeError("run_sync cannot run inside an event loop; await agent.run()")
        finally:
            options.pop("trace_options", None)
            del trace_options


_protect_mcp_methods(Agent, lambda owner: (owner, *owner.tools))
_BUILTIN_SESSION_FACTORY = Agent.session
_preserve_module_identity(globals(), "agents.agent")


def _completed_run_eligible(
    agent: Agent[Any, Any],
    input: UserInput | None,
    context: Any,
    return_state: bool,
    parent_trace: TraceContext | Mapping[str, str | None] | None,
    trace_options: TraceOptions | Mapping[str, Any] | None,
) -> bool:
    """Private completion profile; native archive and output bounds remain unchanged."""
    eligible = _private_text_run_eligible(
        agent, input, context, return_state, parent_trace, trace_options
    )
    return eligible and not agent.tools


def _private_text_run_eligible(
    agent: Agent[Any, Any],
    input: UserInput | None,
    context: Any,
    return_state: bool,
    parent_trace: TraceContext | Mapping[str, str | None] | None,
    trace_options: TraceOptions | Mapping[str, Any] | None,
) -> bool:
    """Text-run transport candidate; atomic opening checks resolved tools."""
    return (
        type(agent) is Agent
        and type(input) is str
        and context is None
        and not agent.programmatic_tool_calling
        and agent.transform_context is None
        and agent.before_compact is None
        and agent.before_complete is None
        and agent.tool_batch_completion is None
        and not agent.output_guardrails
        and not agent.input_guardrails
        and not return_state
        and not agent.handoffs
        and (agent.output_type is None or agent.output_type is str)
        and parent_trace is None
        and trace_options is None
    )
