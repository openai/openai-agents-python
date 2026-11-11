"""Live manual function results with native, single-use reply authority."""

from __future__ import annotations

import asyncio as asyncio
import base64 as base64
import json as json
import re as re
from collections.abc import Iterator as Iterator, Mapping as Mapping
from copy import deepcopy as deepcopy
from dataclasses import dataclass as dataclass, field as field
from datetime import date as date, datetime as datetime
from decimal import Decimal as Decimal
from email.errors import HeaderParseError as HeaderParseError
from email.headerregistry import Address as Address
from typing import (
    TYPE_CHECKING as TYPE_CHECKING,
    Annotated as Annotated,
    Any as Any,
    Generic as Generic,
    Literal as Literal,
    TypeAlias as TypeAlias,
    TypeVar as TypeVar,
    cast as cast,
)
from urllib.parse import unquote as unquote, urlsplit as urlsplit
from weakref import ReferenceType as ReferenceType, ref as ref

from pydantic import (
    BaseModel as BaseModel,
    Field as Field,
    TypeAdapter as TypeAdapter,
    ValidationError as ValidationError,
)

from ._lifecycle import _join
from ._mcp_errors import _protect_mcp_methods
from ._public import preserve_module_identity as _preserve_module_identity

if TYPE_CHECKING:
    from jsonschema.protocols import Validator

    from ._function_tools import FunctionTool
    from .mcp import ChatGPTAppsMCP, HostedVerificationMCP
    from .native_agents import ChildInvocation
    from .session import Session
    from .telemetry import SpanContext
    from .tools import ToolCallInfo, ToolRef, _CapturedFunction
    from .turn import Turn


_URL_ACCEPT = object()

_FORM_MODES = frozenset({"form", "openai/form", "openaiForm"})
_EXTENSION_FORM_MODES = _FORM_MODES - {"form"}
_FORM_DRAFT = "https://json-schema.org/draft/2020-12/schema"


def _schema_nodes(schema: Any) -> Iterator[Any]:
    """Visit schema positions, without interpreting references or instance data."""
    yield schema
    if not isinstance(schema, dict):
        return
    for key in ("$defs", "properties", "patternProperties", "dependentSchemas"):
        for child in schema.get(key, {}).values():
            yield from _schema_nodes(child)
    for key in (
        "items",
        "contains",
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
    ):
        if key in schema:
            yield from _schema_nodes(schema[key])
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        for child in schema.get(key, []):
            yield from _schema_nodes(child)


def _primitive_select_choices(choices: Any, *, exclusive: bool = False) -> bool:
    """Recognize only the string choices enforced by the primitive reply path."""
    if not isinstance(choices, list) or not all(
        isinstance(choice, dict)
        and not (set(choice) - {"const", "title"})
        and isinstance(choice.get("const"), str)
        for choice in choices
    ):
        return False
    return not exclusive or len({choice["const"] for choice in choices}) == len(choices)


def _primitive_form_schema(schema: Any) -> bool:
    """Keep strict primitive validation only for fully represented select shapes."""
    if (
        not isinstance(schema, dict)
        or schema.get("type") != "object"
        or set(schema)
        - {"type", "properties", "required", "additionalProperties", "title", "description"}
        or not isinstance(schema.get("properties"), dict)
        or not isinstance(schema.get("additionalProperties", False), bool)
    ):
        return False
    common = {"type", "title", "description", "default"}
    keywords = {
        "string": {"format", "minLength", "maxLength", "enum", "enumNames", "oneOf"},
        "boolean": set(),
        "integer": {"minimum", "maximum"},
        "number": {"minimum", "maximum"},
        "array": {"items", "minItems", "maxItems"},
    }
    for prop in schema["properties"].values():
        if (
            not isinstance(prop, dict)
            or not isinstance(prop.get("type"), str)
            or prop["type"] not in keywords
        ):
            return False
        if set(prop) - (common | keywords[prop["type"]]):
            return False
        if "oneOf" in prop and not _primitive_select_choices(prop["oneOf"], exclusive=True):
            return False
        if prop["type"] == "array":
            items = prop.get("items")
            if not isinstance(items, dict):
                return False
            if set(items) == {"type", "enum"} and items["type"] == "string":
                continue
            if set(items) != {"anyOf"} or not _primitive_select_choices(items["anyOf"]):
                return False
    from jsonschema import Draft202012Validator

    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        return False
    return True


def _is_absolute_uri(value: str) -> bool:
    """Check component syntax without imposing network URL or port-range rules."""
    if re.search(r"[^A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]|%(?![0-9A-Fa-f]{2})", value):
        return False
    uri = urlsplit(value)
    if not uri.scheme:
        return False
    component_chars = r"A-Za-z0-9._~!$&'()*+,;=%-"
    if not re.fullmatch(f"[{component_chars}:@/]*", uri.path) or any(
        not re.fullmatch(f"[{component_chars}:@/?]*", component)
        for component in (uri.query, uri.fragment)
    ):
        return False
    authority = uri.netloc
    if "@" in authority:
        userinfo, authority = authority.split("@", 1)
        if not re.fullmatch(f"[{component_chars}:]*", userinfo):
            return False
    # urlsplit checks IPv6 addresses. IPvFuture has a separate literal grammar.
    ip_literal = r"[0-9A-Fa-f:.]+|v[0-9A-Fa-f]+\.[A-Za-z0-9._~!$&'()*+,;=:-]+"
    return (
        re.fullmatch(f"(?:\\[(?:{ip_literal})\\]|[{component_chars}]*)(?::[0-9]*)?", authority)
        is not None
    )


def _general_form_validator(schema: Any) -> Validator:
    """Support one draft and single-hop references to local definitions only.

    Formats are annotations on general forms. Regex keywords are unsupported.
    Standard primitive forms keep their strict types and format checks.
    No schema loads a resource.
    """
    from jsonschema import Draft202012Validator, validators
    from jsonschema.exceptions import ValidationError as SchemaValidationError
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    validator: Validator | None = None
    try:
        Draft202012Validator.check_schema(schema)
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError
        allowed = (
            set(Draft202012Validator.VALIDATORS) - {"$dynamicRef", "pattern", "patternProperties"}
        ) | {
            "$schema",
            "$defs",
            "$comment",
            "title",
            "description",
            "default",
            "examples",
            "deprecated",
            "readOnly",
            "writeOnly",
            "format",
        }
        nodes = list(_schema_nodes(schema))
        for node in nodes:
            if isinstance(node, dict) and (
                set(node) - allowed or node.get("$schema", _FORM_DRAFT) != _FORM_DRAFT
            ):
                raise ValueError
        # The draft is fixed above. Keep nested draft annotations from selecting
        # the library's default validator instead of the decimal-aware variant.
        for node in nodes:
            if isinstance(node, dict):
                node.pop("$schema", None)
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        # An explicit empty Registry has no retriever; the library refuses
        # missing resources instead of using jsonschema's default downloader.
        registry: Registry[Any] = Registry()
        resolver = registry.resolver_with_root(resource)
        for node in nodes:
            if not isinstance(node, dict) or "$ref" not in node:
                continue
            reference = node["$ref"]
            if not reference.startswith("#/$defs/") or "/" in unquote(reference[len("#/$defs/") :]):
                raise ValueError
            target = resolver.lookup(reference).contents
            Draft202012Validator.check_schema(target)
            if any(isinstance(child, dict) and "$ref" in child for child in _schema_nodes(target)):
                raise ValueError

        def multiple_of(validator: Any, divisor: Any, instance: Any, schema: Any) -> Iterator[Any]:
            if not validator.is_type(instance, "number"):
                return
            # JSON numbers use their decimal spelling, without a rounding tolerance.
            numerator, denominator = Decimal(str(instance)).as_integer_ratio()
            divisor_numerator, divisor_denominator = Decimal(str(divisor)).as_integer_ratio()
            if (numerator * divisor_denominator) % (denominator * divisor_numerator):
                yield SchemaValidationError("Number is not a multiple of the divisor")

        decimal_validator = validators.extend(Draft202012Validator, {"multipleOf": multiple_of})
        validator = decimal_validator(schema, registry=registry)
    except Exception:
        # Library errors can contain private schema data. Retain none of them.
        pass
    if validator is None:
        from .session import InteractionRequired

        raise InteractionRequired("Unsupported MCP form schema")
    return validator


def _is_json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int, float)):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


InputT = TypeVar("InputT", bound=BaseModel)
ResultT = TypeVar("ResultT")


@dataclass(frozen=True, slots=True, init=False)
class ActionReply(Generic[ResultT]):
    """A bound answer for Session.respond or an on_interaction handler.

    Packaging a reply does not consume a live grant. Do not persist it.
    """

    _request: Interaction = field(repr=False)
    _value: ResultT = field(repr=False)

    def __new__(cls) -> ActionReply[ResultT]:
        raise TypeError("Create an ActionReply with a live request.reply(value)")


def _reply(request: Interaction, value: ResultT) -> ActionReply[ResultT]:
    reply = object.__new__(ActionReply)
    object.__setattr__(reply, "_request", request)
    object.__setattr__(reply, "_value", value)
    return reply


@dataclass(frozen=True, slots=True, init=False)
class ActionRequested(Generic[InputT, ResultT]):
    """A live manual call, separate from passive observation replay.

    Arguments and native reply authority are excluded from repr. Requests are
    valid only while their original native invocation remains pending. Closing
    an event cursor does not cancel the invocation. Do not persist this object.
    """

    session_id: str
    subagent_id: str | None
    submission_id: str
    turn_id: str
    call_id: str
    name: str
    namespace: str | None
    arguments: InputT = field(repr=False)
    type: Literal["action_requested"] = field(default="action_requested", init=False)
    _declaration: object = field(repr=False)
    _grant: Any = field(repr=False)
    _invocation: int = field(repr=False)
    _slot: int = field(repr=False)
    _session: ReferenceType[Session[Any, Any]] = field(repr=False, compare=False)
    call: ToolCallInfo
    _tracing_context: str | None = field(default=None, init=False, repr=False, compare=False)

    def __new__(cls) -> ActionRequested[InputT, ResultT]:
        raise TypeError("ActionRequested values are issued by a live native Session")

    def reply(self, value: ResultT) -> ActionReply[ResultT]:
        """Package an Agent.run answer without validating or consuming the grant."""
        return _reply(self, value)

    async def respond(self, value: ResultT) -> None:
        """Validate and deliver to this exact invocation once.

        Invalid results leave the live grant available for correction. Native
        acceptance resolves its waiter; it does not acknowledge durable history.
        """
        session = self._session()
        if session is None:
            from .session import InteractionExpiredError

            raise InteractionExpiredError(
                "Manual tool reply is foreign, expired or already delivered"
            )
        await session._respond(self, value)


class _DefaultApprovalReason(str):
    """Preserve omission while retaining the existing string default."""


_DEFAULT_APPROVAL_REASON = _DefaultApprovalReason("Tool execution was rejected")


@dataclass(frozen=True)
class ApprovalDecision:
    """A managed function decision; always applies within this Session and agent."""

    approve: bool
    always: bool = False
    reason: str = _DEFAULT_APPROVAL_REASON

    def __post_init__(self) -> None:
        if (
            not isinstance(self.approve, bool)
            or not isinstance(self.always, bool)
            or not isinstance(self.reason, str)
        ):
            raise TypeError("Approval decisions require boolean approve/always and a string reason")


class ApprovalRequested(ActionRequested[InputT, Any]):
    """A managed function awaiting a decision; approval executes its registered callback.

    A decision is valid only for this live invocation. Remembered decisions are
    scoped to this Session's exact agent and function declaration. They do not
    turn saved metadata into permission or retry an uncertain effect.
    """

    _approval_generation: int
    _effective_arguments: InputT

    @property
    def effective_arguments(self) -> InputT:
        """Detached validated arguments after the pre-approval input policy."""
        return deepcopy(self._effective_arguments)

    def reply(self, value: bool | ApprovalDecision) -> ActionReply[Any]:
        """Return a decision from on_interaction without executing the callback here."""
        decision = ApprovalDecision(value) if isinstance(value, bool) else value
        if not isinstance(decision, ApprovalDecision):
            raise TypeError("A managed function reply requires an approval decision")
        return _reply(self, decision)

    async def respond(self, value: Any) -> None:
        await super().respond(value)

    async def approve(self, *, always: bool = False) -> None:
        session = self._session()
        if session is None:
            from .session import InteractionExpiredError

            raise InteractionExpiredError("Managed approval is expired")
        await session._decide_approval(self, True, always=always)

    async def reject(self, reason: str = _DEFAULT_APPROVAL_REASON, *, always: bool = False) -> None:
        session = self._session()
        if session is None:
            from .session import InteractionExpiredError

            raise InteractionExpiredError("Managed approval is expired")
        await session._decide_approval(self, False, reason=reason, always=always)


@dataclass(frozen=True, slots=True, init=False)
class _NativeQuestion:
    """A native prompt whose only receipt is submission to Core's queue."""

    session_id: str
    subagent_id: str | None
    submission_id: str | None
    turn_id: str | None
    description: str = field(repr=False)
    _message: dict[str, Any] = field(repr=False)
    _session: ReferenceType[Session[Any, Any]] = field(repr=False, compare=False)
    _tracing: SpanContext | None = field(default=None, init=False, repr=False, compare=False)

    def _release_tracing(self) -> None:
        object.__setattr__(self, "_tracing", None)
        self._message.pop("tracing_context", None)

    def __new__(cls) -> _NativeQuestion:
        raise TypeError("Native requests are issued by a live Session")

    @property
    def call(self) -> ToolCallInfo | None:
        """No qualified originating call is supplied with these native prompts.

        A native approval ID identifies a reply queue, not necessarily a
        provider tool call. Patch approvals can originate in exec_command;
        native MCP approval metadata omits the raw tool name. Server form
        metadata cannot establish which tool, if any, requested the form.
        """
        return None

    def _check_current(self) -> Session[Any, Any]:
        session = self._session()
        if session is None:
            from .session import InteractionExpiredError

            raise InteractionExpiredError("Native request is expired or already answered")
        session._check_question(self)
        return session

    async def _enqueue(self, operation: str, params: dict[str, Any]) -> None:
        session = self._check_current()
        await session._respond_question(self, operation, json.dumps(params, allow_nan=False))


@dataclass(frozen=True, slots=True, init=False)
class ApprovalRequest(_NativeQuestion):
    """A real native MCP or Environment approval for this live owning turn.

    respond confirms enqueue, not native acceptance, effect or durability.
    No grant to persist approvals is available here. Repr excludes prompt data.
    """

    submission_id: str
    turn_id: str
    type: Literal["approval_request"] = field(default="approval_request", init=False)

    def reply(
        self, decision: Literal["allow_once", "deny"]
    ) -> ActionReply[Literal["allow_once", "deny"]]:
        """Package a one-shot approval decision; delivery acknowledges enqueue only."""
        return _reply(self, decision)

    async def respond(self, decision: Literal["allow_once", "deny"]) -> None:
        """Enqueue once from this request. An invalid reply may be corrected."""
        self._check_current()
        if decision not in ("allow_once", "deny"):
            raise ValueError("Approval decision must be 'allow_once' or 'deny'")
        message = self._message
        if message["type"] in {"exec_approval_request", "apply_patch_approval_request"}:
            params = {
                "id": message.get("approval_id") or message["call_id"],
                "decision": "approved"
                if decision == "allow_once"
                else {"denied": {"rejection": "denied"}},
            }
            if message["type"] == "exec_approval_request":
                params["turn_id"] = self.turn_id
                operation = "exec_approval"
            else:
                operation = "patch_approval"
            await self._enqueue(operation, params)
            return
        await self._enqueue(
            "resolve_elicitation",
            {
                "server_name": message["server_name"],
                "request_id": message["id"],
                "decision": "accept" if decision == "allow_once" else "decline",
                "content": None,
                "meta": None,
            },
        )


@dataclass(frozen=True, slots=True, init=False)
class _ElicitationRequest(_NativeQuestion):
    """A native MCP form or explicitly answered URL request.

    Primitive forms keep strict types, formats, bounds and enums.
    elicitation/create and OpenAI form methods also support Draft 2020-12
    object schemas with nested JSON values and single-hop local definition
    references. General forms treat formats as annotations;
    regex keywords, resource identifiers, and dynamic references are unsupported.
    A UrlRequest exposes its target URL and verification ID.
    A URL without a native turn ID is Session-scoped:
    submission_id and turn_id are None, even while a turn is active. Do not
    infer an originating turn for those requests. Opted-in child requests keep
    their original native child turn and admission. URL consent uses accept_url;
    completion waits for the issuing server.
    The SDK never visits a URL. Replies acknowledge enqueue, not effect.
    """

    type: Literal["elicitation_request"] = field(default="elicitation_request", init=False)
    _validator: Validator | None = field(default=None, repr=False, compare=False)
    _url_completion: asyncio.Future[bool] | None = field(default=None, repr=False, compare=False)

    @property
    def url(self) -> str | None:
        """The target URL, or None for a form. Reading it never visits the URL."""
        return cast(str | None, self._message["request"].get("url"))

    @property
    def elicitation_id(self) -> str | None:
        """The server's URL interaction identifier; it grants no reply authority."""
        return cast(str | None, self._message["request"].get("elicitation_id"))

    def _reply_url_accept(self) -> ActionReply[object]:
        """Package explicit URL consent for an on_interaction callback."""
        if self.url is None:
            raise ValueError("Only URL requests support URL consent")
        return _reply(cast("Interaction", self), _URL_ACCEPT)

    async def _accept_url(self) -> None:
        """Enqueue URL consent once. This does not establish completion or verification."""
        session = self._check_current()
        if self.url is None:
            raise ValueError("Only URL requests support URL consent")
        if self.subagent_id is None:
            pending = session._native._check_elicitation_pending(
                self._message["server_name"], self._message["id"]
            )
        else:
            pending = await session._native._child_url_pending(self._message["_sdk_question_id"])
            session._check_question(self)
        if self._url_completion is None or not pending:
            from .session import InteractionExpiredError, InteractionRequired

            if self.subagent_id is not None and not pending:
                raise InteractionExpiredError("Native child URL request is expired")
            raise InteractionRequired("This URL request has no live native completion route")
        await self._enqueue(
            "resolve_elicitation",
            {
                "server_name": self._message["server_name"],
                "request_id": self._message["id"],
                "decision": "accept",
                "content": None,
                "meta": None,
            },
        )

    async def _wait_url_completion(self, *, timeout: float) -> None:
        """Wait for this issuing server to report completion of this URL ID.

        A matched server notification is not cryptographic verification. The
        application decides whether this server can report the needed outcome.
        Timeout stops only this observer and raises the built-in TimeoutError.
        Cancelling a waiter only stops that wait.
        """
        session = self._session()
        if session is None:
            from .session import InteractionExpiredError

            raise InteractionExpiredError("URL request is expired")
        await session._wait_url_completion(cast("UrlRequest", self), timeout)

    def _form_reply(self, value: Mapping[str, Any] | None) -> ActionReply[Mapping[str, Any] | None]:
        """Validate detached form values without consuming the live request."""
        return _reply(
            cast("Interaction", self), None if value is None else self._validate_form(value)
        )

    @property
    def _schema(self) -> Mapping[str, Any]:
        """A detached form schema, or an empty mapping for a URL request."""
        return deepcopy(self._message["request"].get("requested_schema", {}))

    def _validate_form(self, value: Mapping[str, Any]) -> dict[str, Any]:
        request = self._message["request"]
        if request["mode"] not in _FORM_MODES:
            raise ValueError("URL verification has no form; use accept_url() or decline()")
        if self._validator is not None:
            valid_extension = None
            try:
                if not isinstance(value, Mapping):
                    raise ValueError
                content = dict(value)
                if not _is_json_value(content):
                    raise ValueError
                json.dumps(content, allow_nan=False)
                self._validator.validate(content)
                valid_extension = content
            except Exception:
                pass
            if valid_extension is None:
                raise ValueError("MCP form failed validation")
            return deepcopy(valid_extension)
        schema = request["requested_schema"]
        if not isinstance(value, Mapping):
            raise ValueError("MCP form requires a mapping")
        properties = schema["properties"]
        if not set(schema.get("required", ())).issubset(value) or (
            schema.get("additionalProperties") is not True and set(value) - set(properties)
        ):
            raise ValueError("MCP form has missing or unknown fields")
        # These are rmcp PrimitiveSchemaDefinition variants, including selects.
        # Native normalizes titled multi-select items to anyOf.
        types = {
            "string": str,
            "boolean": bool,
            "integer": int,
            "number": float,
            "array": list[str],
        }
        valid: dict[str, Any] | None = {}
        try:
            for key, item in value.items():
                if key not in properties:
                    if not _is_json_value({key: item}):
                        raise ValueError
                    json.dumps(item, allow_nan=False)
                    assert valid is not None
                    valid[key] = item
                    continue
                prop = properties[key]
                adapter: TypeAdapter[Any] = TypeAdapter(
                    cast(
                        Any,
                        Annotated[
                            types[prop["type"]],
                            Field(
                                min_length=prop.get("minLength", prop.get("minItems")),
                                max_length=prop.get("maxLength", prop.get("maxItems")),
                                ge=prop.get("minimum"),
                                le=prop.get("maximum"),
                            ),
                        ],
                    )
                )
                parsed = adapter.validate_python(item, strict=True)
                options = prop["items"] if prop["type"] == "array" else prop
                selections = parsed if prop["type"] == "array" else [parsed]
                if "enum" in options and any(
                    selected not in options["enum"] for selected in selections
                ):
                    raise ValueError("MCP form value is outside the offered enum")
                titled = options.get("oneOf", options.get("anyOf"))
                if titled is not None and any(
                    selected not in [choice["const"] for choice in titled]
                    for selected in selections
                ):
                    raise ValueError("MCP form value is outside the offered enum")
                form_format = prop.get("format")
                if form_format == "email":
                    address = Address(addr_spec=parsed)
                    if not address.username or not address.domain:
                        raise ValueError("MCP form email needs a mailbox and domain")
                elif form_format == "uri":
                    if not _is_absolute_uri(parsed):
                        raise ValueError("MCP form requires an absolute URI")
                elif form_format == "date":
                    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", parsed):
                        raise ValueError("MCP form date must be YYYY-MM-DD")
                    date.fromisoformat(parsed)
                elif form_format == "date-time":
                    if not re.fullmatch(
                        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
                        r"(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})",
                        parsed,
                    ):
                        raise ValueError("MCP form requires an ISO date-time with offset")
                    if int(parsed[11:13]) > 23:
                        raise ValueError("MCP form date-time has an invalid hour")
                    if parsed[-1] not in "Zz" and (
                        int(parsed[-5:-3]) > 23 or int(parsed[-2:]) > 59
                    ):
                        raise ValueError("MCP form date-time has an invalid timezone offset")
                    datetime.fromisoformat(
                        parsed.replace("t", "T").replace("Z", "+00:00").replace("z", "+00:00")
                    )
                assert valid is not None
                valid[key] = parsed
        except (HeaderParseError, ValidationError, TypeError, ValueError, RecursionError):
            valid = None
        if valid is None:
            raise ValueError("MCP form failed validation")
        return deepcopy(valid)

    async def _form_respond(self, value: Mapping[str, Any]) -> None:
        self._check_current()
        await self._enqueue_form(self._validate_form(value))

    async def _enqueue_form(self, value: Mapping[str, Any]) -> None:
        await self._enqueue(
            "resolve_elicitation",
            {
                "server_name": self._message["server_name"],
                "request_id": self._message["id"],
                "decision": "accept",
                "content": value,
                "meta": None,
            },
        )

    async def decline(self) -> None:
        """Decline once from this wrapper; no form content is enqueued."""
        await self._enqueue(
            "resolve_elicitation",
            {
                "server_name": self._message["server_name"],
                "request_id": self._message["id"],
                "decision": "decline",
                "content": None,
                "meta": None,
            },
        )


@dataclass(frozen=True, slots=True, init=False)
class FormRequest(_ElicitationRequest):
    """A live MCP form. Preparing values validates them without consuming authority."""

    mode: Literal["form"] = field(default="form", init=False)
    schema = _ElicitationRequest._schema
    reply = _ElicitationRequest._form_reply
    respond = _ElicitationRequest._form_respond


@dataclass(frozen=True, slots=True, init=False)
class UrlRequest(_ElicitationRequest):
    """Explicit URL consent and passive observation of the issuing peer's completion."""

    mode: Literal["url"] = field(default="url", init=False)
    reply_url_accept = _ElicitationRequest._reply_url_accept
    accept_url = _ElicitationRequest._accept_url
    wait_url_completion = _ElicitationRequest._wait_url_completion

    async def cancel_url_completion(self) -> None:
        """Expire this shared URL lifecycle and cancel the peer completion route."""
        session = self._session()
        if session is None or self._url_completion is None:
            from .session import InteractionExpiredError

            raise InteractionExpiredError("URL request is expired")
        session._check_open()
        session._check_callback_operation("cancel URL completion")
        if self._url_completion.done():
            return
        session._settle_url_completion(self, False)
        await _join(asyncio.create_task(session._cancel_url_completion(self)))

    def reply(self, value: None) -> ActionReply[None]:
        """Prepare a URL decline. Use reply_url_accept() to grant consent."""
        if value is not None:
            raise ValueError("URL requests accept consent or decline, not form values")
        return _reply(self, None)


ElicitationRequest: TypeAlias = FormRequest | UrlRequest


@dataclass(frozen=True, slots=True)
class VerificationProof:
    """A provider's protocol proof. Only the authoritative service can accept it."""

    credential_id: str = field(repr=False)
    signature: str = field(repr=False)


@dataclass(frozen=True, slots=True, init=False)
class VerificationRequest(_NativeQuestion):
    """One live challenge from the application's explicit host registration.

    An on_interaction callback may invoke its trusted signer with this request
    and return reply(proof). Respond acknowledges enqueue, not verifier acceptance.
    Never persist a request, challenge, or proof.
    """

    type: Literal["verification_request"] = field(default="verification_request", init=False)
    _registration: HostedVerificationMCP[Any] | ChatGPTAppsMCP[Any] = field(
        repr=False, compare=False
    )

    @property
    def title(self) -> str:
        """The service-provided UI title; this text grants no authority."""
        return cast(str, self._message["request"]["title"])

    @property
    def challenge(self) -> bytes:
        """The exact immutable challenge bytes to sign, without interpretation."""
        encoded = self._message["request"]["challenge"]
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))

    @property
    def server_name(self) -> str:
        """The host-registered service alias, rather than peer display metadata."""
        return cast(str, self._message["server_name"])

    def reply(self, proof: VerificationProof | None) -> ActionReply[VerificationProof | None]:
        """Package a proof, or None to decline, through the owning callback."""
        return _reply(self, proof)

    async def respond(self, proof: VerificationProof) -> None:
        session = self._check_current()
        if self._registration is not session._hosted_verification:
            from .session import InteractionExpiredError

            raise InteractionExpiredError("Verification registration was replaced")
        valid = False
        try:
            valid = (
                isinstance(proof, VerificationProof)
                and isinstance(proof.credential_id, str)
                and 0 < len(proof.credential_id.encode("utf-8")) <= 1024
                and isinstance(proof.signature, str)
                and 0 < len(proof.signature) <= 172
                and "=" not in proof.signature
                and re.fullmatch(r"[A-Za-z0-9_-]+", proof.signature) is not None
                and 0
                < len(base64.urlsafe_b64decode(proof.signature + "=" * (-len(proof.signature) % 4)))
                <= 128
            )
            if valid:
                raw_signature = base64.urlsafe_b64decode(
                    proof.signature + "=" * (-len(proof.signature) % 4)
                )
                valid = (
                    base64.urlsafe_b64encode(raw_signature).decode().rstrip("=") == proof.signature
                )
        except Exception:
            valid = False
        if not valid:
            raise ValueError("Invalid verification proof")
        await self._enqueue(
            "resolve_elicitation",
            {
                "server_name": self.server_name,
                "request_id": self._message["id"],
                "decision": "accept",
                "content": {"credentialId": proof.credential_id, "signature": proof.signature},
                "meta": None,
            },
        )

    async def decline(self) -> None:
        """Decline once; no challenge or proof is returned."""
        await self._enqueue(
            "resolve_elicitation",
            {
                "server_name": self.server_name,
                "request_id": self._message["id"],
                "decision": "decline",
                "content": None,
                "meta": None,
            },
        )


Interaction: TypeAlias = (
    ActionRequested[Any, Any] | ApprovalRequest | ElicitationRequest | VerificationRequest
)


def _question(
    session: Session[Any, Any], message: dict[str, Any], handle: Turn[Any] | ChildInvocation
) -> ApprovalRequest | ElicitationRequest | VerificationRequest:
    from .native_agents import ChildInvocation
    from .session import InteractionExpiredError, InteractionRequired

    cls: type[ApprovalRequest] | type[ElicitationRequest] | type[VerificationRequest] = (
        ApprovalRequest
    )
    validator = None
    kind = message["type"]
    if kind in {"exec_approval_request", "apply_patch_approval_request"}:
        if (
            message.get("turn_id") != handle.turn_id
            or message.get("grant_root") is not None
            or message.get("network_approval_context") is not None
            or message.get("additional_permissions") is not None
            or kind == "exec_approval_request"
            and message.get("kind", "command") != "command"
        ):
            raise InteractionRequired("Only limited in-turn Environment approvals are supported")
        action = (
            message.get("command") if kind == "exec_approval_request" else message.get("changes")
        )
        description = (
            f"{message.get('reason', 'Native tool approval requested')}: {json.dumps(action)}"
        )
    else:
        request = message["request"]
        description = request.get("message", "Native input requested")
        meta = request.get("_meta") or {}
        if not (
            isinstance(message["id"], str)
            and message["id"].startswith("mcp_tool_call_approval_")
            and isinstance(meta, dict)
            and meta.get("codex_approval_kind") == "mcp_tool_call"
        ):
            schema = request.get("requested_schema")
            if request.get("mode") == "url":
                cls = UrlRequest
                description = (
                    f"{description}\nURL: {request['url']}\n"
                    f"Elicitation ID: {request['elicitation_id']}"
                )
            elif request.get("mode") == "openai/userVerification":
                if message["server_name"] != "codex_apps" or session._hosted_verification is None:
                    raise InteractionRequired("Verification requires an explicit host registration")
                cls = VerificationRequest
                description = request["description"]
            elif request.get("mode") in _FORM_MODES:
                if request.get("mode") in _EXTENSION_FORM_MODES or not _primitive_form_schema(
                    schema
                ):
                    validator = _general_form_validator(deepcopy(schema))
            else:
                raise InteractionRequired("Unsupported MCP interaction")
            if cls not in (VerificationRequest, UrlRequest):
                cls = FormRequest
    if isinstance(handle, ChildInvocation) and (
        session._native_agents is None
        or not session._native_agents.interactions
        or message.get("turn_id") != handle.turn_id
    ):
        raise InteractionRequired("Native child interaction has no exact live owner")
    if isinstance(handle, ChildInvocation) and not message.get("_sdk_question_id"):
        # The enabled, supported source expired before the bridge could capture it.
        # No issued request exists, and its delayed observation must not fail the turn.
        raise InteractionExpiredError("Native child request expired before delivery")
    unowned_url = (
        cls in (UrlRequest, VerificationRequest)
        and message["request"].get("mode") in {"url", "openai/userVerification"}
        and not message.get("turn_id", message.get("turnId"))
    )
    result = object.__new__(cls)
    for key, item in {
        "session_id": session.id,
        "subagent_id": handle.child_id if isinstance(handle, ChildInvocation) else None,
        "submission_id": None if unowned_url else handle.submission_id,
        "turn_id": None if unowned_url else handle.turn_id,
        "description": description,
        "_message": deepcopy(message),
        "_session": ref(session),
        "_tracing": None,
        "type": "approval_request"
        if cls is ApprovalRequest
        else "verification_request"
        if cls is VerificationRequest
        else "elicitation_request",
    }.items():
        object.__setattr__(result, key, item)
    if context := message.get("tracing_context"):
        from .telemetry import _retain_context

        tracing = _retain_context(context)
        object.__setattr__(result, "_tracing", tracing)
        result._message["tracing_context"] = tracing._handle
    if isinstance(handle, ChildInvocation):
        result._message["_sdk_child_incarnation"] = handle.instance_id
    if cls is VerificationRequest:
        object.__setattr__(result, "_registration", session._hosted_verification)
    if cls in (FormRequest, UrlRequest):
        object.__setattr__(result, "mode", "form" if cls is FormRequest else "url")
        object.__setattr__(result, "_validator", validator)
        completion = None
        if message["request"].get("mode") == "url" and (
            isinstance(handle, ChildInvocation)
            or session._native._check_elicitation_pending(message["server_name"], message["id"])
        ):
            completion = session._loop.create_future()
            session._url_completions[(message["server_name"], message["id"])] = cast(
                ElicitationRequest, result
            )
        object.__setattr__(result, "_url_completion", completion)
    return result


@dataclass(frozen=True, slots=True)
class _PreparedManualResult:
    """Serialized result for the declared call in a waiting checkpoint."""

    declaration: FunctionTool[Any, Any, Any] = field(repr=False)
    call_id: str
    output: str = field(repr=False)


@dataclass(frozen=True)
class _CapturedManual:
    name: str
    spec_json: str
    declaration: FunctionTool[Any, Any, Any] = field(repr=False)
    input_adapter: TypeAdapter[Any] = field(repr=False)
    output_adapter: TypeAdapter[Any] = field(repr=False)
    namespace: None = field(default=None, init=False)

    def _encode_result(
        self, value: Any, *, expected_json: str | None = None, strict: bool = False
    ) -> str:
        from .tools import ToolOutput

        wire = self.output_adapter.dump_json(
            value, warnings="error" if strict or expected_json is not None else True
        ).decode()
        # Declared strings keep raw text after their serializer has run once.
        text = json.loads(wire) if isinstance(value, str) else wire
        if not isinstance(text, str):
            text = wire
        if expected_json is not None and text != expected_json:
            raise ValueError("Manual tool result differs from its verified projection")
        response = value if isinstance(value, ToolOutput) else ToolOutput.text(text)
        response._validate()
        return json.dumps(response.to_native())

    @property
    def _ref(self) -> ToolRef:
        return self.declaration.ref

    def request(
        self,
        grant: Any,
        invocation: int,
        slot: int,
        session: Session[Any, Any],
        handle: Turn[Any] | ChildInvocation,
        call_id: str,
        arguments: Any,
        code_origin: dict[str, Any] | None,
        *,
        model: str | None = None,
        tracing_context: str | None = None,
    ) -> ActionRequested[Any, Any]:
        from .native_agents import ChildInvocation
        from .tools import ToolCallInfo

        child = handle if isinstance(handle, ChildInvocation) else None
        turn_id = handle.turn_id
        assert turn_id is not None
        request = object.__new__(ActionRequested)
        for key, value in {
            "session_id": session.id,
            "subagent_id": child.child_id if child is not None else None,
            "submission_id": handle.submission_id,
            "turn_id": turn_id,
            "call_id": call_id,
            "name": self.name,
            "namespace": self.namespace,
            "call": ToolCallInfo(
                tool=self.declaration.ref,
                session_id=session.id,
                turn_id=turn_id,
                call_id=call_id if code_origin is None else None,
                child_id=child.child_id if child is not None else None,
                child_incarnation=child.instance_id if child is not None else None,
                role=None,
                model=model,
                route="direct" if code_origin is None else "code",
                cell_id=code_origin["cell_id"] if code_origin is not None else None,
                nested_call_id=code_origin["nested_call_id"] if code_origin is not None else None,
            ),
            "arguments": arguments,
            "type": "action_requested",
            "_declaration": self.declaration,
            "_grant": grant,
            "_invocation": invocation,
            "_slot": slot,
            "_session": ref(session),
            "_tracing_context": tracing_context,
        }.items():
            object.__setattr__(request, key, value)
        return request


@dataclass(frozen=True)
class _CapturedApproval(_CapturedManual):
    function: _CapturedFunction = field(repr=False)

    @property
    def _requires_live_manual_session(self) -> bool:
        return self.function._requires_live_manual_session

    def request(self, *args: Any, **kwargs: Any) -> ApprovalRequested[Any]:
        manual = super().request(*args, **kwargs)
        request = object.__new__(ApprovalRequested)
        for name in ActionRequested.__dataclass_fields__:
            object.__setattr__(request, name, getattr(manual, name))
        return request


def _request_mcp(request: Any) -> Any:
    session = request._session()
    registration = getattr(request, "_registration", None)
    if session is None:
        return (registration,)
    return (session._opening_agent, *session._opening_agent.tools, registration)


for _request_type in (
    ActionRequested,
    ApprovalRequest,
    _ElicitationRequest,
    FormRequest,
    UrlRequest,
    VerificationRequest,
):
    _protect_mcp_methods(_request_type, _request_mcp)


_preserve_module_identity(globals(), "agents.manual")
