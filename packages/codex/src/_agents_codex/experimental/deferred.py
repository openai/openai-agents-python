"""Continue a prepared root manual call using an application-owned job journal.

The host owns storage, authorization, leases, provider configuration and result
projection. The SDK never stores a provider receipt in a SessionState. See
DeferredTools.prepare and advance for the two sides of the portable boundary.
"""

from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import json as json
import math as math
import sys as sys
from collections.abc import (
    AsyncIterator as AsyncIterator,
    Awaitable as Awaitable,
    Callable as Callable,
    Coroutine as Coroutine,
    Mapping as Mapping,
    Sequence as Sequence,
)
from contextlib import asynccontextmanager as asynccontextmanager, suppress as suppress
from dataclasses import dataclass as dataclass, replace as replace
from inspect import getattr_static as getattr_static
from types import TracebackType as TracebackType
from typing import (
    Any as Any,
    Literal as Literal,
    Protocol as Protocol,
    TypeVar as TypeVar,
    cast as cast,
)

from pydantic import BaseModel as BaseModel, TypeAdapter as TypeAdapter
from pydantic_core import (
    SchemaError as SchemaError,
    SchemaValidator as SchemaValidator,
    ValidationError as ValidationError,
    core_schema as core_schema,
)

from .._function_tools import FunctionTool as FunctionTool
from .._lifecycle import (
    _error_contexts as _error_contexts,
    _join as _join,
    _retain_cleanup_error as _retain_cleanup_error,
)
from .._public import preserve_module_identity as _preserve_module_identity
from ..manual import ActionRequested as ActionRequested, _CapturedManual as _CapturedManual
from ..session import (
    Session as Session,
    _private_task as _private_task,
    _restore_declaration as _restore_declaration,
    _RestoreDeclaration as _RestoreDeclaration,
)
from ..state import SessionState as SessionState
from ..turn import Turn as Turn

_Input = TypeVar("_Input", bound=BaseModel)
_Context = TypeVar("_Context")
_InputContra = TypeVar("_InputContra", bound=BaseModel, contravariant=True)
_Result = TypeVar("_Result", bound=BaseModel)
_T = TypeVar("_T")
_Status = Literal["pending", "input_required", "completed", "unknown", "cancelled", "failed"]
_Operation = Literal["advance", "respond", "cancel"]
_Pause = Literal["pending", "input_required", "unknown", "cancelled", "failed", "busy"]
_Action = Literal[
    "start",
    "observe",
    "respond",
    "cancel",
    "reconcile_creation",
    "project",
    "deliver",
    "busy",
    "unknown",
]


@dataclass(frozen=True, repr=False)
class JobBinding:
    """Exact checkpoint, request and trusted operation required for a journal claim."""

    tenant: str
    run_id: str
    provider_id: str
    session_id: str
    turn_id: str
    call_id: str
    tool_name: str
    request_digest: str
    checkpoint_digest: str


@dataclass(frozen=True, repr=False)
class JobUpdate:
    """Private provider observation saved by the host journal before projection.

    Return small metadata or an artifact handle here. Keep file contents in
    the provider's store and bound its reads before building this value.
    """

    status: _Status
    receipt: str | None = None
    value: object = None


@dataclass(frozen=True, repr=False)
class JobClaim:
    """An application-issued conditional operation. Its key and receipt stay private."""

    binding: JobBinding
    key: str
    generation: int
    action: _Action
    update: JobUpdate | None = None
    result_json: str | None = None


class JobProvider(Protocol[_InputContra]):
    """A fixed host-selected backend. Every method must enforce current target authority.

    Strong providers also implement reconcile_creation(claim, arguments), with
    atomic duplicate prevention for the immutable key and payload. Optional
    respond(claim, response) and cancel(claim) return JobUpdate. A cancellation
    acknowledgment can still report pending. Input must be host approved and
    correlated to the outstanding request and version at the target.
    Bound network calls and join outstanding I/O on cancellation.
    """

    async def start(self, claim: JobClaim, arguments: _InputContra) -> JobUpdate: ...

    async def observe(self, claim: JobClaim) -> JobUpdate: ...


class JobJournal(Protocol):
    """Application-owned durable storage under the current principal.

    Atomically prepare the exact checkpoint and every binding. Claim only those
    bindings. Return start only to the winner of prepared -> creating. A reply
    or cancel claim leaves the mutation unspent while the provider validates it.
    Reserve the specific approved action immediately before sending. A lost
    acknowledgment leaves that reservation spent across owners. Check authority,
    the immutable binding and the current generation on every read and write.
    Keep the verified result until the application conditionally publishes the
    continued conversation.
    """

    async def prepare(self, state_json: str, bindings: tuple[JobBinding, ...]) -> None: ...

    async def claim(self, binding: JobBinding, operation: _Operation) -> JobClaim: ...

    async def authorize(self, claim: JobClaim, operation: str) -> None: ...

    async def record(self, claim: JobClaim, update: JobUpdate) -> None: ...

    async def save_result(self, claim: JobClaim, result_json: str) -> None: ...


@dataclass(frozen=True, repr=False)
class _Job:
    tool: FunctionTool[Any, Any]
    provider_id: str
    provider: JobProvider[Any]
    project: Callable[[object], BaseModel]
    max_output_bytes: int
    strong: bool


async def _wait_for_job(
    work: Coroutine[Any, Any, _T], timeout: float, *, interrupted: asyncio.Event | None = None
) -> _T:
    with _private_task(work) as (task, outcome):
        try:
            await asyncio.wait((task,), timeout=timeout)
            if not task.done():
                raise asyncio.TimeoutError
        except (asyncio.CancelledError, asyncio.TimeoutError) as interruption:
            # Keep the first interruption while the host releases its resources.
            if interrupted is not None:
                interrupted.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await _join(asyncio.gather(task, return_exceptions=True))
            # asyncio.wait's traceback owns the task and its copy of the host context.
            public_interruption = interruption.with_traceback(None)
        else:
            task.result()
            try:
                return outcome.result()
            except asyncio.CancelledError:
                # An application operation can raise cancellation with private details.
                pass
            raise asyncio.CancelledError
        finally:
            del task, outcome
        raise public_interruption


_CLOSED_BEFORE_START = "Deferred session closed before entering the start context"


@asynccontextmanager
async def _restored_session(
    selection: _RestoreDeclaration, context: Any, timeout: float
) -> AsyncIterator[tuple[Session[Any, Any], Callable[[], bool]]]:
    # The hook owns its Task and Context. The caller borrows the Session until exit.
    ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    closing = asyncio.Event()
    borrower: asyncio.Task[Any] | None = None
    wake = object()
    woke_borrower = False
    restored: Session[Any, Any] | None = None
    error: BaseException | None = None
    interruption: BaseException | None = None
    exit_info: tuple[type[BaseException] | None, BaseException | None, TracebackType | None] = (
        None,
        None,
        None,
    )
    suppressed = False

    async def own() -> None:
        nonlocal restored, error, interruption, exit_info, suppressed, woke_borrower
        try:
            selection.owner = asyncio.current_task()
            token = _restore_declaration.set(selection)
            try:
                manager = selection.agent.session(state=selection.state, context=context)
                restored = await manager.__aenter__()
            finally:
                selection.owner = None
                _restore_declaration.reset(token)
            ready.set_result(None)
            try:
                await closing.wait()
            except BaseException as problem:
                interruption = problem
                if borrower is not None and not closing.is_set():
                    # Let the active body or provider unwind before the hook closes its Session.
                    woke_borrower = borrower.cancel(wake)
                    with suppress(asyncio.CancelledError):
                        await _join(asyncio.create_task(closing.wait()))
                if exit_info[1] is None:
                    exit_info = (type(problem), problem, problem.__traceback__)
            suppressed = bool(await manager.__aexit__(*exit_info))
        except BaseException as problem:
            error = problem

    work = own()
    try:
        owner = asyncio.create_task(work)
    except BaseException:
        work.close()
        raise
    try:
        done, _ = await asyncio.wait(
            (owner, ready), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            raise TimeoutError("Deferred session restore timed out")
    except BaseException:
        if interruption is None:
            exit_info = sys.exc_info()
            closing.set()
            if not ready.done():
                owner.cancel()
            with suppress(asyncio.CancelledError):
                await _join(owner)
            # A hook that finished entering after the deadline has run its exit.
            if not ready.done() or not isinstance(exit_info[1], TimeoutError):
                raise
    finally:
        selection.api_key = None

    if owner.done() or interruption is not None:
        # The hook stopped before handoff. Join its exit even if the caller cancels later.
        if not owner.done():
            with suppress(asyncio.CancelledError):
                await _join(owner)
        owner.result()
        if error is not None:
            raise error
        if exit_info[1] is not None and not suppressed:
            raise exit_info[1]
        # A suppressed interruption still left no open Session to borrow.
        raise RuntimeError(_CLOSED_BEFORE_START)

    borrower = asyncio.current_task()
    assert borrower is not None
    prior_context = sys.exc_info()[1]
    borrower_error: asyncio.CancelledError | None = None
    try:
        yield cast(Session[Any, Any], restored), lambda: interruption is None
    except BaseException as problem:
        if (
            woke_borrower
            and interruption is not None
            and isinstance(problem, asyncio.CancelledError)
        ):
            borrower_error = problem
            exit_info = (type(interruption), interruption, interruption.__traceback__)
        else:
            exit_info = sys.exc_info()
    finally:
        closing.set()
        try:
            await _join(owner)
        except asyncio.CancelledError:
            if not isinstance(
                exit_info[1], (asyncio.CancelledError, TimeoutError, asyncio.TimeoutError)
            ):
                raise
        finally:
            if woke_borrower and sys.version_info >= (3, 11):
                borrower.uncancel()
    if borrower_error is not None:
        first_cancel: asyncio.CancelledError | None = borrower_error
        # Python 3.10 can move an awaited Task's cancellation message into its context.
        for cause in _error_contexts(borrower_error):
            if cause.args and cause.args[0] is wake:
                first_cancel = None
                # The wake can arrive while the body is unwinding an earlier cancellation.
                for earlier in _error_contexts(cause.__context__):
                    if earlier is prior_context or not isinstance(earlier, asyncio.CancelledError):
                        break
                    first_cancel = earlier
                break
        if first_cancel is not None:
            if error is not None:
                # Context-manager exit can chain the active borrower error to the first cancel.
                _retain_cleanup_error(borrower_error, error, ())
            raise first_cancel
    if error is not None:
        raise error
    if exit_info[1] is not None and not suppressed:
        raise exit_info[1]


def _duration(value: float, *, zero: bool = False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (not zero and value == 0)
    ):
        raise ValueError("Invalid deferred job time limit")


_RESULT_ERROR = (
    "Declare the deferred output as a strict, closed model with required public JSON fields. "
    "Project rich values into that model or supply the reply directly."
)

_INPUT_ERROR = (
    "Deferred inputs must forbid extra fields and use deterministic JSON fields. "
    "Supply generated IDs explicitly as required fields or supply the reply directly."
)


def _json_scalar(value: object) -> bool:
    return (
        value is None
        or type(value) in (str, int, bool)
        or (type(value) is float and math.isfinite(value))
    )


def _ordered_fields(
    node: Mapping[str, Any], input_defaults: list[Mapping[str, Any]] | None
) -> bool:
    """Recognize ordinary field validation and serialization in the compiled declaration."""

    def ordered(child: Mapping[str, Any]) -> bool:
        return _ordered_fields(child, input_defaults)

    for_input = input_defaults is not None
    kind = node.get("type")
    if "serialization" in node or node.get("strict") is False:
        return False
    if kind == "definitions":
        return ordered(node["schema"]) and all(map(ordered, node["definitions"]))
    if kind == "definition-ref":
        return True  # Each definition is checked above.
    if kind == "model":
        config = node.get("config", {})
        fields = node["schema"]
        if fields["type"] != "model-fields":
            return False
        members = fields["fields"]
        if for_input:
            # The binding uses aliases. Each key must identify exactly one provider field.
            keys = {field.get("serialization_alias", name) for name, field in members.items()}
            if len(keys) != len(members) or any(
                getattr_static(node["cls"], method) is not getattr_static(BaseModel, method)
                for method in (
                    "__new__",
                    "__deepcopy__",
                    "__getattribute__",
                    "model_validate_json",
                    "model_dump",
                )
            ):
                return False
        return (
            config.get("strict") is True
            and (for_input or config.get("extra_fields_behavior") == "forbid")
            and config.keys()
            <= (
                {"title", "strict", "extra_fields_behavior", "allow_inf_nan", "ser_json_inf_nan"}
                | {"validate_by_name", "validate_by_alias", "serialize_by_alias"}
                | ({"validate_default"} if for_input else set())
            )
            and not (node.get("custom_init") or node.get("post_init") or node.get("root_model"))
            and fields.keys() <= {"type", "fields", "model_name", "computed_fields"}
            and not fields.get("computed_fields")
            and all(
                field.keys()
                <= {"type", "schema", "metadata", "validation_alias", "serialization_alias"}
                and (
                    for_input
                    or not config.get("serialize_by_alias")
                    or field.get("serialization_alias", name) == name
                )
                and ordered(field["schema"])
                for name, field in members.items()
            )
        )
    if input_defaults is not None and kind == "default":
        if "default" not in node or not _json_scalar(node["default"]):
            return False
        input_defaults.append(node)
        return ordered(node["schema"])
    if for_input and kind == "literal":
        return all(map(_json_scalar, node["expected"]))
    if for_input and kind == "union":
        return all(isinstance(choice, dict) and ordered(choice) for choice in node["choices"])
    if kind == "nullable":
        return ordered(node["schema"])
    if kind == "list":
        return ordered(node.get("items_schema", {}))
    if kind in {"str", "int", "float", "bool", "none"}:
        bounds = {"min_length", "max_length", "pattern", "gt", "ge", "lt", "le", "multiple_of"}
        return node.keys() <= bounds | {"type", "metadata", "ref", "strict", "allow_inf_nan"}
    return False


def _valid_defaults(schema: Mapping[str, Any], defaults: list[Mapping[str, Any]]) -> bool:
    # Pydantic normally skips validation of omitted defaults. Check them only after
    # all branches and referenced definitions have been recognized as ordinary fields.
    for node in defaults:
        field = node["schema"]
        if schema["type"] == "definitions":
            field = core_schema.definitions_schema(field, schema["definitions"])
        try:
            checked = SchemaValidator(field).validate_python(node["default"], strict=True)
        except (SchemaError, ValidationError):
            return False
        if type(checked) is not type(node["default"]):
            return False
    return True


def _check_declaration(model: type[BaseModel], *, for_input: bool) -> None:
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        if for_input:
            raise TypeError("Deferred tool schemas must be Pydantic object models")
    elif model.__pydantic_complete__:
        schema = model.__pydantic_core_schema__
        root = schema["schema"] if schema["type"] == "definitions" else schema
        if for_input and schema["type"] == "definitions" and root["type"] == "definition-ref":
            reference = root.get("schema_ref")
            root = next((d for d in schema["definitions"] if d.get("ref") == reference), {})
        defaults: list[Mapping[str, Any]] = []
        if (
            root.get("type") == "model"
            and root.get("cls") is model
            and _ordered_fields(schema, defaults if for_input else None)
            and _valid_defaults(schema, defaults)
        ):
            return
    raise ValueError(_INPUT_ERROR if for_input else _RESULT_ERROR)


def _closed_schema(model: type[BaseModel]) -> None:
    schema = model.model_json_schema()

    def check(node: dict[str, Any]) -> None:
        kind = node.get("type")
        if kind == "object":
            if node.get("additionalProperties") is not False or "patternProperties" in node:
                raise ValueError("Deferred tool models must forbid extra fields at every depth")
            for child in node.get("properties", {}).values():
                check(child)
        elif kind == "array":
            check(node.get("items", {}))
        elif "anyOf" in node or "oneOf" in node:
            for child in node.get("anyOf", node.get("oneOf", [])):
                check(child)
        elif "$ref" in node:
            ref = node["$ref"]
            if not ref.startswith("#/$defs/") or ref[8:] not in schema.get("$defs", {}):
                raise ValueError("Deferred tool models require local JSON schema definitions")
        elif kind not in {"string", "integer", "number", "boolean", "null"}:
            raise ValueError("Deferred tool models require closed JSON values")

    if schema.get("type") != "object" and "$ref" not in schema:
        raise ValueError("Deferred tool models must describe JSON objects")
    check(schema)
    for definition in schema.get("$defs", {}).values():
        check(definition)
    if model.model_config.get("extra") != "forbid" or model.model_config.get("strict") is not True:
        raise ValueError("Deferred tool models must configure strict=True and extra='forbid'")


def _canonical(model: BaseModel) -> str:
    return json.dumps(
        model.model_dump(mode="json", by_alias=True, round_trip=True, warnings="error"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _same_input(value: Any, checked: Any) -> bool:
    # Two branches can serialize different provider fields under the same alias.
    if type(value) is not type(checked):
        return False
    if isinstance(value, BaseModel):
        return value.__pydantic_fields_set__ == checked.__pydantic_fields_set__ and all(
            _same_input(field, vars(checked)[name]) for name, field in vars(value).items()
        )
    if isinstance(value, list):
        return len(value) == len(checked) and all(
            _same_input(a, b) for a, b in zip(value, checked, strict=True)
        )
    if isinstance(value, float):
        return value.hex() == cast(float, checked).hex()
    return bool(value == checked)


def _pending_inputs(
    session: Session[Any, Any],
    turn_id: str,
    registered: Mapping[str, _CapturedManual],
    *,
    invocation: int | None = None,
) -> dict[tuple[str, str], str]:
    # Session validates and caches each native invocation before copying it to an event.
    active = session._active
    if active is None:
        raise ValueError
    inputs = {}
    for pending, _, raw in session._native._pending_manual_tools():
        if invocation is not None and pending != invocation:
            continue
        call = json.loads(raw)["msg"]
        if call["_sdk_python_tool"]["thread"] == session.id and call["turnId"] == turn_id:
            if entry := registered.get(call["tool"]):
                value = active.manual_inputs[pending]
                checked = entry.input_adapter.validate_python(
                    call["arguments"], strict=True, extra="forbid"
                )
                if not _same_input(value, checked):
                    raise ValueError
                inputs[(entry.name, call["callId"])] = _canonical(value)
    return inputs


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError
    return value


def _private_text(value: object, secrets: Sequence[str]) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in secrets)
    if isinstance(value, dict):
        return any(_private_text(k, secrets) or _private_text(v, secrets) for k, v in value.items())
    return isinstance(value, list) and any(_private_text(item, secrets) for item in value)


class DeferredTools:
    """Register pending function calls and drive their external work under a current host scope."""

    def __init__(self, *, journal: JobJournal, tenant: str, run_id: str) -> None:
        if not tenant or not run_id:
            raise ValueError("Deferred tools require an application tenant and run")
        self._journal = journal
        self._tenant = tenant
        self._run_id = run_id
        self._jobs: dict[str, _Job] = {}

    def register(
        self,
        tool: FunctionTool[_Input, _Result],
        *,
        provider_id: str,
        provider: JobProvider[_Input],
        project: Callable[[object], _Result],
        max_output_bytes: int = 16384,
        require_reconciliation: bool = True,
    ) -> None:
        """Register a function the app answers with a fixed provider and result projection.

        The strong default requires target-side immutable-key creation and
        reconciliation. Explicitly opt out only for approved low-impact work
        whose creation may remain unknown. The byte cap applies to the JSON
        sent to the model. Keep the synchronous projector fast. It runs on the
        host event loop and can delay a timeout if it blocks.

        Supply generated request IDs as required input fields. Inputs may use
        scalar constants, literals, ordered lists and nested strict JSON models.
        Validation and serialization must preserve all provider fields on restore.

        The journal stores the ordinary manual reply JSON in field and list order.
        Declare required public fields on strict models with extra="forbid".
        Use strings, booleans, integers, finite floats, nulls, lists and nested
        models. Input aliases are allowed. Restore uses the public field names.
        Project other types or renamed fields explicitly into this model. Sort
        an unordered source only if that is the order your application wants.
        Submit a reply directly for custom serialization.
        """
        if not isinstance(tool, FunctionTool) or tool._bind is not None:
            raise TypeError("Pending-call recovery requires a FunctionTool the application answers")
        if not tool.name or not provider_id or tool.name in self._jobs:
            raise ValueError("Deferred tools require a unique name and a configured provider")
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError("Deferred tool result limit must be a positive byte count")
        if type(require_reconciliation) is not bool:
            raise TypeError("require_reconciliation must be a bool")
        if require_reconciliation and not callable(getattr(provider, "reconcile_creation", None)):
            raise ValueError("Strong deferred jobs require provider creation reconciliation")
        if not callable(project) or not all(
            callable(getattr(provider, op, None)) for op in ("start", "observe")
        ):
            raise TypeError("Deferred tools require provider start, observe and a result projector")
        _check_declaration(tool.output_type, for_input=False)
        _check_declaration(tool.input_type, for_input=True)
        _closed_schema(tool.input_type)
        self._jobs[tool.name] = _Job(
            tool, provider_id, provider, project, max_output_bytes, require_reconciliation
        )

    def _live(
        self,
        session: Session[Any, Any],
        request: ActionRequested[Any, Any],
        *,
        interrupted: asyncio.Event | None = None,
    ) -> _Job:
        if interrupted is not None and interrupted.is_set():
            raise asyncio.CancelledError
        session._check_open()
        job = self._jobs.get(request.name)
        active = session._active
        if (
            job is None
            or job.tool.match(request) is None
            or request.subagent_id is not None
            or request.session_id != session.id
            or active is None
            or active.handle is None
            or active.handle.submission_id != request.submission_id
            or active.stopping
            or active.failure.done()
            or not session._native._check_manual_tool(request._grant)
        ):
            raise ValueError("Deferred job requires its current root manual grant")
        return job

    def _binding(
        self,
        job: _Job,
        session_id: str,
        turn_id: str,
        call_id: str,
        arguments: str,
        checkpoint_digest: str,
    ) -> JobBinding:
        return JobBinding(
            self._tenant,
            self._run_id,
            job.provider_id,
            session_id,
            turn_id,
            call_id,
            job.tool.name,
            hashlib.sha256(arguments.encode()).hexdigest(),
            checkpoint_digest,
        )

    async def prepare(
        self,
        session: Session[Any, Any],
        request: ActionRequested[Any, Any],
        *,
        timeout_seconds: float = 10.0,
    ) -> SessionState:
        """Seal a live root call and durably prepare every registered pending sibling.

        Call from the host, outside on_interaction. The journal must authorize
        and atomically persist the checkpoint and bindings before returning.
        No provider operation is possible in this method.
        Portable plain and prompted roots are supported. Pending native-schema
        roots cannot be restored and are refused before sealing.

        An interruption during sealing waits for one journal write, with the
        same timeout for that write. Check the journal after an interruption
        or a failed write. The original Session's suspend() returns the sealed
        checkpoint before close. After close, the original Turn's get_result() raises
        InteractionRequired with the same state for manual recovery.
        """
        _duration(timeout_seconds)
        self._live(session, request)
        if (
            session._active is None
            or session._active.restored_pending
            or session._executor is not None
            or session._native_agents is not None
            or (
                session._opening_agent is not None
                and session._opening_agent.programmatic_tool_calling
            )
        ):
            raise ValueError("Deferred preparation requires a new portable root manual boundary")
        interrupted = asyncio.Event()
        try:
            return await _wait_for_job(
                self._prepare(session, request, interrupted, timeout_seconds),
                timeout_seconds,
                interrupted=interrupted,
            )
        except asyncio.TimeoutError:
            problem: Exception = TimeoutError("Deferred checkpoint preparation timed out")
        except PermissionError:
            problem = PermissionError("Deferred checkpoint preparation was denied")
        except Exception:
            problem = ValueError("Deferred checkpoint could not be prepared")
        raise problem

    async def _prepare(
        self,
        session: Session[Any, Any],
        request: ActionRequested[Any, Any],
        interrupted: asyncio.Event,
        timeout: float,
    ) -> SessionState:
        session._check_callback_operation("prepare its own Session")
        active = session._active
        if (
            active is None
            or active.output.mode == "native"
            or not await session._wait_manual_boundary(active, require_request=True)
        ):
            raise ValueError
        registered = {
            entry.name: entry
            for entry in session._catalog.tools
            if isinstance(entry, _CapturedManual)
            and (job := self._jobs.get(entry.name))
            and job.tool is entry.declaration
        }
        inputs: dict[tuple[str, str], str] = {}

        def check_pending() -> None:
            if interrupted.is_set():
                raise asyncio.CancelledError
            inputs.update(_pending_inputs(session, request.turn_id, registered))
            if (request.name, request.call_id) not in inputs:
                raise ValueError
            self._live(session, request)

        try:
            state = await session._suspend(before_seal=check_pending)
        except asyncio.CancelledError:
            # The checkpoint owner joins the seal before propagating interruption.
            if not interrupted.is_set() or not inputs or session._sealed_checkpoint is None:
                raise
            state = SessionState.from_json(session._sealed_checkpoint)
        frame = state.native_continuation
        if frame is None or frame.turn_id != request.turn_id:
            raise ValueError
        state_json = state.to_json()
        digest = hashlib.sha256(state_json.encode()).hexdigest()
        bindings = []
        for call in frame.pending_manual:
            if call.tool in registered:
                bindings.append(
                    self._binding(
                        self._jobs[call.tool],
                        state.source_session_id,
                        frame.turn_id,
                        call.call_id,
                        inputs[(call.tool, call.call_id)],
                        digest,
                    )
                )
        if not any(binding.call_id == request.call_id for binding in bindings):
            raise ValueError
        write = self._journal.prepare(state_json, tuple(bindings))
        if interrupted.is_set():
            await _wait_for_job(write, timeout)
        else:
            await write
        return state

    async def advance(
        self,
        session: Session[Any, Any],
        request: ActionRequested[Any, Any],
        *,
        operation: _Operation = "advance",
        response: object = None,
        wait_seconds: float = 0.0,
        timeout_seconds: float = 10.0,
    ) -> Turn[Any] | _Pause:
        """Advance one current restored grant. Return a live Turn or a host-only status.

        The root may have overridden the Session's default. Without a typed
        Turn witness the returned Turn is type-erased; its output is still
        validated against the prepared contract.

        A zero wait starts once without polling. A wake-up may observe once.
        A positive wait permits one additional bounded observation after start.
        The total timeout covers authorization, provider I/O, persistence and
        delivery. Timeouts never authorize another create. No poller survives.
        Cancellation stops new provider work and delivery after host cleanup.
        Effects that already began can still be recorded before returning.
        Keep using the original prepared state while siblings are outstanding.
        A later restore can deliver their already saved results on fresh grants.

        Automatic delivery rejects a result containing the current model API key,
        job key or provider receipt, even as an incidental substring. Opaque job
        keys and receipts reduce the chance of a match but can still collide.
        A collision does not authorize another start. The host may review the
        result and submit a safe reply on the current FunctionTool request.
        The host must record that completion itself.
        """
        _duration(timeout_seconds)
        _duration(wait_seconds, zero=True)
        if operation not in {"advance", "respond", "cancel"} or (
            response is not None and operation != "respond"
        ):
            raise ValueError("Invalid deferred job operation")
        job = self._live(session, request)
        if operation != "advance" and not callable(getattr(job.provider, operation, None)):
            raise ValueError("Deferred job provider does not support this operation")
        active = session._active
        digest = session._manual_source_digest
        if active is None or not active.restored_pending or digest is None:
            raise ValueError("Restore the prepared checkpoint before advancing a deferred job")
        interrupted = asyncio.Event()
        try:
            return await _wait_for_job(
                self._advance(
                    session, request, job, digest, operation, response, wait_seconds, interrupted
                ),
                timeout_seconds,
                interrupted=interrupted,
            )
        except asyncio.TimeoutError:
            return "unknown"
        except PermissionError:
            problem: Exception = PermissionError("Deferred job operation was denied")
        except OSError:
            return "unknown"
        except Exception:
            problem = ValueError("Deferred job operation could not be completed")
        raise problem

    @asynccontextmanager
    async def start(
        self,
        session: Session[_Context, _T],
        request: ActionRequested[Any, Any],
        *,
        wait_seconds: float = 0.0,
        timeout_seconds: float = 10.0,
    ) -> AsyncIterator[tuple[Session[_Context, _T], Turn[Any] | _Pause]]:
        """Prepare, reopen and advance one live call in a single host operation.

        The context owns the fresh Session. Use it for mixed manual siblings,
        subsequent calls or saving the completed conversation. On a pause,
        leave the context and later load the state from the host journal.
        A prompted root keeps its validated output contract. Later explicit
        requests still use the opening Agent's output default. The active
        outcome has no typed Turn witness, so it is returned as Turn[Any].
        Tool updates or instructions that differ from the opening Agent require
        explicit prepare and restore with a current Agent. This helper refuses
        before sealing a changed Session.
        Preparation, restore and advance share one timeout. Expiry before
        advance raises TimeoutError without calling the provider. If only the
        advance deadline expires, the context yields `unknown` after cleanup.
        """
        _duration(timeout_seconds)
        _duration(wait_seconds, zero=True)
        agent = session._agent
        opening = session._opening_agent
        active = session._active
        if agent is None or opening is None or active is None:
            raise ValueError("Deferred start requires an active Agent Session")
        if (
            session._catalog.tools is not session._bound_tools
            or session._instructions != opening.instructions
        ):
            raise ValueError(
                "Deferred start requires its opening tools and instructions; "
                "use prepare and restore with the current Agent after an update"
            )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        selection = _RestoreDeclaration(
            agent, opening, session._model_api_key, active.output, session._max_function_concurrency
        )
        try:
            selection.state = await self.prepare(session, request, timeout_seconds=timeout_seconds)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Deferred session restore timed out")
            entered = False
            async with _restored_session(selection, session._context, remaining) as (
                restored,
                hook_active,
            ):
                current = restored._manual_request(None, set(), _call_id=request.call_id)
                if current is None:
                    raise ValueError("Restored deferred call is unavailable")
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("Deferred start timed out before advance")
                outcome = await self.advance(
                    restored, current, wait_seconds=wait_seconds, timeout_seconds=remaining
                )
                # The hook can retire while advance joins a timed-out provider.
                if hook_active():
                    entered = True
                    yield restored, outcome
            if not entered:
                # The hook suppressed an error before this context could yield an open Session.
                raise RuntimeError(_CLOSED_BEFORE_START)
        finally:
            selection.api_key = None

    async def _advance(
        self,
        session: Session[Any, Any],
        request: ActionRequested[Any, Any],
        job: _Job,
        digest: str,
        operation: _Operation,
        response: object,
        wait: float,
        interrupted: asyncio.Event,
    ) -> Turn[Any] | _Pause:
        # An application can mutate the model on an event it already received.
        fresh = session._manual_request(request.submission_id, set(), _call_id=request.call_id)
        if fresh is None or job.tool.match(fresh) is None:
            raise ValueError
        entry = session._catalog.tools[fresh._slot]
        if not isinstance(entry, _CapturedManual) or entry.declaration is not job.tool:
            raise ValueError
        arguments = _pending_inputs(
            session, fresh.turn_id, {fresh.name: entry}, invocation=fresh._invocation
        )[(fresh.name, fresh.call_id)]
        binding = self._binding(job, session.id, fresh.turn_id, fresh.call_id, arguments, digest)
        claim = await self._journal.claim(binding, operation)
        if claim.binding != binding or not claim.key or type(claim.generation) is not int:
            raise PermissionError
        self._live(session, fresh, interrupted=interrupted)
        action = claim.action
        if action in ("busy", "unknown"):
            return action
        if operation != "advance" and action != operation:
            raise PermissionError
        if operation == "advance" and action in ("respond", "cancel"):
            raise PermissionError
        update = claim.update
        if action == "start" and (update is not None or claim.result_json is not None):
            raise ValueError
        adapter = entry.output_adapter
        if action == "deliver":
            if claim.result_json is None:
                raise ValueError
            return await self._deliver(
                session, fresh, job, claim, adapter, claim.result_json, interrupted=interrupted
            )
        if action != "project":
            if action in ("observe", "respond", "cancel") and (
                update is None or not update.receipt
            ):
                raise ValueError
            if action == "respond" and (update is None or update.status != "input_required"):
                raise ValueError
            if action == "cancel" and (
                update is None or update.status not in ("pending", "input_required")
            ):
                raise ValueError
            if action == "reconcile_creation" and not job.strong:
                return "unknown"
            method = getattr(job.provider, action, None)
            if action not in {
                "start",
                "observe",
                "respond",
                "cancel",
                "reconcile_creation",
            } or not callable(method):
                raise ValueError
            args: tuple[object, ...]
            if action in {"start", "reconcile_creation"}:
                args = (fresh.arguments,)
            elif action == "respond":
                args = (response,)
            else:
                args = ()
            invoke = cast(Callable[..., Awaitable[JobUpdate]], method)
            update = await self._provider_update(
                session, fresh, claim, action, invoke, *args, _interrupted=interrupted
            )
            self._live(session, fresh, interrupted=interrupted)
            if wait and action == "start" and update is not None and update.status == "pending":
                claim = replace(claim, update=update)
                phase: Literal["authorizing", "provider", "recording"] = "authorizing"

                def observing_phase(value: Literal["authorizing", "provider", "recording"]) -> None:
                    nonlocal phase
                    phase = value

                try:
                    update = await _wait_for_job(
                        self._provider_update(
                            session,
                            fresh,
                            claim,
                            "observe",
                            job.provider.observe,
                            _interrupted=interrupted,
                            _phase=observing_phase,
                        ),
                        wait,
                        interrupted=interrupted,
                    )
                except asyncio.TimeoutError:
                    return "pending" if phase == "provider" else "unknown"
            if update is None:
                return "unknown"
        if update is None:
            raise ValueError
        if update.status != "completed":
            return update.status
        claim = replace(claim, update=update)
        await self._journal.authorize(claim, "project")
        self._live(session, fresh, interrupted=interrupted)
        value = job.project(update.value)
        if type(value) is not job.tool.output_type:
            raise ValueError
        value = adapter.validate_python(value)
        raw = adapter.dump_json(value, warnings="error").decode()
        return await self._deliver(
            session, fresh, job, claim, adapter, raw, interrupted=interrupted, projected=value
        )

    async def _record(self, claim: JobClaim, update: JobUpdate) -> JobUpdate:
        if not isinstance(update, JobUpdate):
            raise ValueError
        previous = claim.update.receipt if claim.update else None
        if previous and update.receipt is None:
            update = replace(update, receipt=previous)
        if (
            update.status
            not in {"pending", "input_required", "completed", "unknown", "cancelled", "failed"}
            or (update.status in {"pending", "input_required"} and not update.receipt)
            or (previous and update.receipt != previous)
        ):
            raise ValueError
        await self._journal.record(claim, update)
        return update

    async def _provider_update(
        self,
        session: Session[Any, Any],
        request: ActionRequested[Any, Any],
        claim: JobClaim,
        action: _Action,
        invoke: Callable[..., Awaitable[JobUpdate]],
        *args: object,
        _interrupted: asyncio.Event,
        _phase: Callable[[Literal["authorizing", "provider", "recording"]], None] | None = None,
    ) -> JobUpdate | None:
        try:
            if _phase is not None:
                _phase("authorizing")
            await self._journal.authorize(claim, action)
            self._live(session, request, interrupted=_interrupted)
            if _phase is not None:
                _phase("provider")
            try:
                update = await invoke(claim, *args)
            except PermissionError:
                raise
            except Exception:
                return None
            if _phase is not None:
                _phase("recording")
            # Record effects that already began, even if the host finished after interruption.
            return await self._record(claim, update)
        except asyncio.TimeoutError:
            # A journal timeout is uncertain even during the shorter optional wait.
            return None

    async def _deliver(
        self,
        session: Session[Any, Any],
        request: ActionRequested[Any, Any],
        job: _Job,
        claim: JobClaim,
        adapter: TypeAdapter[Any],
        raw: str,
        *,
        interrupted: asyncio.Event,
        projected: BaseModel | None = None,
    ) -> Turn[Any]:
        secrets = tuple(
            value
            for value in (
                claim.key,
                claim.update.receipt if claim.update else None,
                session._model_api_key,
            )
            if value
        )
        if len(raw.encode()) > job.max_output_bytes or any(secret in raw for secret in secrets):
            raise ValueError
        public = json.loads(raw, parse_float=_finite_float, parse_constant=_finite_float)
        if _private_text(public, secrets):
            raise ValueError
        value = adapter.validate_json(
            raw, strict=True, extra="forbid", by_name=True, by_alias=False
        )
        if projected is not None:
            # A nullable float can serialize NaN as null and still pass strict restore.
            if adapter.dump_python(projected, warnings="error") != public:
                raise ValueError
            # Reassigning a field can change an instance's ordinary JSON order.
            if adapter.dump_json(value, warnings="error").decode() != raw:
                raise ValueError
            await self._journal.save_result(claim, raw)
        self._live(session, request, interrupted=interrupted)
        await self._journal.authorize(claim, "deliver")
        self._live(session, request, interrupted=interrupted)
        return await session._respond(request, value, expected_json=raw)


_preserve_module_identity(globals(), "agents.experimental.deferred")
