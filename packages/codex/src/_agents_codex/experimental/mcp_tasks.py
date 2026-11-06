"""Experimental MCP Tasks provider for a host-registered deferred FunctionTool.

Install ``mcp==2.0.0`` and ``mcp-types==2.0.0`` to use this module. Register
one explicitly approved low-impact operation with ``require_reconciliation=False``.
MCP cannot reconcile an accepted create whose reply was lost. The application owns
its journal, access policy, verified result projection, and wake-up schedule.
"""

# These dependencies are present only in a host that opts in to MCP Tasks.
# mypy: disable-error-code=import-not-found
# pyright: reportMissingImports=false
from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import json as json
import math as math
import time as time
from collections.abc import (
    AsyncIterator as AsyncIterator,
    Awaitable as Awaitable,
    Callable as Callable,
    Collection as Collection,
    Iterator as Iterator,
    Mapping as Mapping,
)
from contextlib import asynccontextmanager as asynccontextmanager
from contextvars import copy_context as copy_context
from dataclasses import astuple as astuple, dataclass as dataclass
from functools import partial as partial
from importlib.metadata import PackageNotFoundError as PackageNotFoundError, version as version
from types import MappingProxyType as MappingProxyType
from typing import Any as Any
from urllib.parse import urlsplit as urlsplit

from pydantic import BaseModel as BaseModel, JsonValue as JsonValue
from typing_extensions import Self as Self

from .._public import preserve_module_identity as _preserve_module_identity
from .deferred import (
    JobClaim as JobClaim,
    JobUpdate as JobUpdate,
    _canonical as _canonical,
    _private_text as _private_text,
    _wait_for_job as _wait_for_job,
)

try:
    _supported = version("mcp") == "2.0.0" and version("mcp-types") == "2.0.0"
except PackageNotFoundError:
    _supported = False
if not _supported:
    raise ImportError("MCP Tasks requires mcp==2.0.0 and mcp-types==2.0.0")

import httpx2 as httpx2
from mcp.client.session import ClientSession as ClientSession
from mcp_types import (
    CallToolResult as CallToolResult,
    ElicitRequest as ElicitRequest,
    ElicitRequestFormParams as ElicitRequestFormParams,
    ElicitResult as ElicitResult,
)

from . import _mcp_tasks_wire as wire


class _ApprovedResponses(Mapping[str, ElicitResult]):
    def __init__(self, responses: Mapping[str, ElicitResult]) -> None:
        self._items = MappingProxyType(
            {
                key: ElicitResult(action=reply.action, content=reply.content).model_dump_json()
                for key, reply in responses.items()
            }
        )

    def __getitem__(self, key: str) -> ElicitResult:
        return ElicitResult.model_validate_json(self._items[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)


@dataclass(frozen=True, kw_only=True, repr=False)
class MCPInputReply:
    """An application-approved answer to the exact private challenge in its journal.

    Each key identifies an outstanding form elicitation. Only the host may choose
    the answer. Construction captures each action and its content, including lists.
    Reading a response returns a separate copy. Keep the challenge, request text and
    answer in the application's private journal.
    """

    challenge: str
    responses: Mapping[str, ElicitResult]

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "responses", _ApprovedResponses(self.responses))
            return
        except Exception:
            pass
        raise ValueError("Invalid MCP input approval")


_FORM_TYPES = {"string": (str,), "boolean": (bool,), "integer": (int,), "number": (int, float)}


def _form_contains_token(value: object, token: str) -> bool:
    if isinstance(value, dict):
        return any(
            _form_contains_token(key, token) or _form_contains_token(item, token)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_form_contains_token(item, token) for item in value)
    if value is None:
        return False
    # The HTTP client encodes the typed scalars with Python's UTF-8 JSON encoder.
    encoded = json.dumps(value, ensure_ascii=False)
    if isinstance(value, str):
        # Check the decoded string and its escaped contents, excluding the JSON delimiters.
        return token in value or token in encoded[1:-1]
    return token in encoded


@asynccontextmanager
async def _issued_credentials(
    authorization: Callable[[], Awaitable[str]],
) -> AsyncIterator[tuple[Callable[[], Awaitable[str]], set[str]]]:
    issued: set[str] = set()

    async def current() -> str:
        # A host may return a string subclass with custom hashing.
        token = str.__str__(await authorization())
        issued.add(token)
        return token

    try:
        await current()
        yield current, issued
    finally:
        issued.clear()


def _accepts_form(schema: dict[str, Any], content: dict[str, Any]) -> bool:
    """Check the adapter's flat form profile without running peer-defined constraints."""
    fields = schema.get("properties")
    required = schema.get("required", [])
    if (
        schema.keys() - {"$schema", "type", "properties", "required", "additionalProperties"}
        or schema.get("type") != "object"
        or (
            "$schema" in schema
            and schema["$schema"] != "https://json-schema.org/draft/2020-12/schema"
        )
        or not isinstance(schema.get("additionalProperties", False), bool)
        or not isinstance(fields, dict)
        or not isinstance(required, list)
        or any(not isinstance(key, str) or key not in content for key in required)
        or content.keys() - fields.keys()
    ):
        return False
    for key, field in fields.items():
        if not isinstance(field, dict) or field.keys() - {
            "type",
            "enum",
            "items",
            "title",
            "description",
            "default",
        }:
            return False
        kind = field.get("type")
        values = [content[key]] if key in content else []
        if kind == "array":
            items = field.get("items")
            if (
                not isinstance(items, dict)
                or items.keys() - {"type", "enum"}
                or items.get("type") != "string"
                or "enum" in field
                or (key in content and not isinstance(content[key], list))
            ):
                return False
            field, kind, values = items, "string", content.get(key, [])
        elif "items" in field:
            return False
        if not isinstance(kind, str) or kind not in _FORM_TYPES:
            return False
        if any(
            type(item) not in _FORM_TYPES[kind]
            and not (kind == "integer" and isinstance(item, float) and item.is_integer())
            for item in values
        ):
            return False
        if "enum" in field:
            choices = field["enum"]
            if kind != "string":
                return False
            if not isinstance(choices, list) or not all(isinstance(item, str) for item in choices):
                return False
            allowed = set(choices)
            if any(item not in allowed for item in values):
                return False
    return True


def _digest(value: object) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _capture_public_json(value: JsonValue) -> JsonValue:
    if value is None or type(value) is str or type(value) is bool or type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        return [_capture_public_json(item) for item in value]
    if type(value) is dict:
        captured = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("Expected a JSON string key")
            captured[key] = _capture_public_json(item)
        return captured
    raise ValueError("Expected plain finite JSON")


def _poll_at(task: wire.Created | wire.Detail) -> float:
    return time.time() + (task.poll_interval_ms or 0) / 1000


def _valid_endpoint(endpoint: str) -> bool:
    try:
        url = urlsplit(endpoint)
        # Reading port validates its spelling and range before any credentials are requested.
        port = url.port
        return (
            httpx2.URL(endpoint).is_absolute_url
            and bool(url.hostname)
            and (port is None or port > 0)
            and url.username is None
            and url.password is None
            and "?" not in endpoint
            and "#" not in endpoint
            and (
                url.scheme == "https"
                or (url.scheme == "http" and url.hostname in {"127.0.0.1", "::1", "localhost"})
            )
        )
    except (ValueError, httpx2.InvalidURL):
        return False


class _FixedEndpoint:
    def __new__(cls, **options: Any) -> Self:
        # Reject the URL before the generated dataclass initializer can retain it in a traceback.
        if "endpoint" in options and not _valid_endpoint(options.pop("endpoint")):
            raise ValueError("Use a fixed HTTPS endpoint or a loopback HTTP endpoint")
        return super().__new__(cls)


@dataclass(frozen=True, kw_only=True, repr=False)
class MCPTasksProvider(_FixedEndpoint):
    """Bind one remote tool to a trusted endpoint and a current authorization callback.

    ``authorize(claim, operation)`` checks the current tenant, principal, provider
    binding and fence. Return the bearer token without its prefix or raise to deny.
    The client checks authorization for each HTTP request and refuses redirects.
    Each exchange runs its MCP session and network transport on a private asyncio
    loop with debugging off. The application keeps its own loop and debug settings.
    Authorization, mutation reservations and admission callbacks run on the caller's loop.
    The initial authorization and each exchange use the configured timeout. Up to four
    exchanges run per process. Waiting for a slot counts toward the exchange timeout.
    Cancellation closes the transport and waits for cooperative host callback cleanup.
    A system lookup keeps its worker slot until the operating system returns.
    For HTTPS it uses the host's httpx2 proxy and CA settings, including
    ``HTTPS_PROXY``, ``ALL_PROXY``, ``NO_PROXY``, ``SSL_CERT_FILE``, and
    ``SSL_CERT_DIR``. The provider checks these settings at construction, before
    the application can claim a job, and uses the current settings on each exchange.
    Settings that prevent the client from opening raise ``PermissionError``.
    After changing host egress, construct a fresh provider before admitting new
    jobs. A later denial cannot make an already claimed creation safe to retry.
    OpenSSL looks up certificates in hashed CA directories during TLS verification.
    Loopback HTTP test peers always connect directly. Tool arguments cannot change
    the configured endpoint or tool.

    A completed update contains empty ``content`` and the tool's ``isError`` bit.
    By default no peer text, structured fields or metadata enters the completed journal
    record. To release a field, supply a fast synchronous ``admit_result(claim, result)``.
    The adapter captures a plain, finite JSON copy of its return under ``structuredContent``
    before recording. A callback exception or invalid JSON produces a failed update with
    its existing receipt and no value. The ordinary tool projector can then shape admitted
    fields and ``isError`` into its public output model.

    For example, store a closed mapping of permitted codes to public labels under each
    exact ``JobBinding``, such as ``{"done": "ready", "empty": "empty"}``. Load it from
    trusted host storage when restoring the job. Pass this function as ``admit_result``::

        def admit_state(claim, result):
            fields = result.structured_content
            if not isinstance(fields, dict) or not isinstance(fields.get("state"), str):
                raise ValueError("Expected a state code")
            return {"state": public_labels[claim.binding][fields["state"]]}

    Read ``value["structuredContent"]["state"]`` in the public projector. Unknown jobs
    or codes fail admission. The peer can choose any allowed code. When the outcome
    must be independently verified, compare the field with the host's record for that
    job and return the local value. The host owns this callback and its lookup data.
    Copying or logging arbitrary peer text can disclose credentials from earlier calls.
    Before resuming jobs created under an older policy, verify or quarantine completed
    updates and saved results. Those rows can resume without another provider call.

    To record a new task ID or form, supply a fast synchronous
    ``admit_pending(claim, task_id, requests_json)``. It receives the exact ID and
    either ``None`` at creation or compact JSON with sorted keys for the complete
    input-request mapping, including all prompts and schemas. Treat these inputs
    as private even on refusal. Return exactly ``True`` only
    after checking these values against trusted data for this job's binding. A peer
    can copy credentials from a previous job or request. A permissive callback can
    release them. A missing callback, refusal or ordinary exception leaves creation
    unknown without a receipt, or a form unknown with its existing receipt. Cached
    updates can return without another admission. Restore the policy with the job
    and verify or quarantine older pending rows before use. A lost or refused
    create receipt does not permit another create.

    A pending update records the earliest next observation poll. Explicit replies and
    cancellation re-read the task before dispatch, even before that deadline.

    Form replies are disabled by default. Set ``allow_input_replies=True`` only for
    a vetted target that atomically binds each request key to one immutable request
    for the task's lifetime and rejects retired or satisfied keys at update. A changed
    request needs a new key and fresh approval. Standard ``tasks/update`` carries no
    revision precondition. Accepted forms support flat strings, booleans, numbers and
    string lists, including string choices. Other validation constraints are refused.

    For replies and cancellation, supply ``reserve_mutation(claim, reply)``. The SDK
    calls it after validation and current HTTP authorization, immediately before sending.
    In one durable transaction check the current tenant, principal, job, provider,
    generation, receipt, operation and approval. For a reply, compare its frozen
    challenge and answers and reserve each selected request key, all or none. For
    cancellation, ``reply`` is ``None``. Reserve the task's single cancellation.
    Keys stay spent across generations, challenge changes and lost acknowledgments.
    Return ``True`` only after a new reservation commits. Otherwise deny the send.
    Claiming a control and checking credentials must leave those reservations untouched.
    After a crash, observe the task. A crash before dispatch can leave a spent key
    with an unknown outcome.
    """

    endpoint: str
    tool: str
    authorize: Callable[[JobClaim, str], Awaitable[str]]
    timeout_seconds: float = 10.0
    allow_input_replies: bool = False
    reserve_mutation: Callable[[JobClaim, MCPInputReply | None], Awaitable[bool]] | None = None
    admit_result: Callable[[JobClaim, CallToolResult], JsonValue] | None = None
    admit_pending: Callable[[JobClaim, str, str | None], bool] | None = None

    def __post_init__(self) -> None:
        if not self.tool or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("MCP Tasks requires a selected tool and a positive finite timeout")
        if urlsplit(self.endpoint).scheme == "https":
            wire.check_https_transport()

    def _scope(self, claim: JobClaim) -> str:
        return _digest([self.endpoint, self.tool, astuple(claim.binding), claim.key])

    async def _authorize(self, claim: JobClaim, action: str) -> str:
        if claim.action != action and not (
            action == "observe" and claim.action == "start" and claim.update is not None
        ):
            raise PermissionError("MCP operation does not match the current claim")
        original = current = copy_context()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        token = ""

        async def credential() -> None:
            nonlocal current, token
            try:
                token = await self.authorize(claim, action)
                if isinstance(token, str) and token:
                    return
            except Exception:
                pass
            finally:
                current = copy_context()
            raise PermissionError("MCP operation is not currently authorized")

        try:
            await _wait_for_job(credential(), self.timeout_seconds)
            if loop.time() >= deadline:
                raise asyncio.TimeoutError
            return token
        finally:
            token = ""
            # Keep the host's credential bindings visible to the rest of this operation.
            variable = None
            try:
                for variable in current:
                    if variable not in original or original[variable] is not current[variable]:
                        variable.set(current[variable])
            finally:
                del original, current, variable

    async def _exchange(
        self,
        claim: JobClaim,
        authorization: Callable[[], Awaitable[str]],
        operation: Callable[[ClientSession], Awaitable[JobUpdate | CallToolResult]],
        reserve: Callable[[], Awaitable[bool]] | None = None,
        previous: JobUpdate | None = None,
        issued: Collection[str] = (),
    ) -> JobUpdate:
        outcome = await wire.exchange(
            self.endpoint,
            authorization,
            self.timeout_seconds,
            operation,
            reserve,
        )
        if isinstance(outcome, CallToolResult):
            return self._complete(claim, outcome, previous)
        if outcome.status == "input_required" or (previous is None and outcome.status == "pending"):
            if self.admit_pending is None:
                raise wire.Unavailable
            assert outcome.receipt is not None and isinstance(outcome.value, dict)
            task_id = requests_json = None
            try:
                task_id = json.loads(outcome.receipt)[1]
                if outcome.status == "input_required":
                    requests_json = json.dumps(
                        outcome.value["requests"],
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    if any(
                        token in task_id
                        or token in requests_json
                        or _form_contains_token(outcome.value["requests"], token)
                        for token in issued
                    ):
                        raise wire.Unavailable
                if self.admit_pending(claim, task_id, requests_json) is True:
                    return outcome
            except Exception:
                pass
            finally:
                del task_id, requests_json
            raise wire.Unavailable
        return outcome

    def _complete(
        self, claim: JobClaim, result: CallToolResult, previous: JobUpdate | None
    ) -> JobUpdate:
        if result.result_type != "complete":
            raise wire.Unavailable
        value: dict[str, JsonValue] = {"content": [], "isError": result.is_error}
        if self.admit_result is not None:
            try:
                value["structuredContent"] = _capture_public_json(self.admit_result(claim, result))
            except Exception:
                return JobUpdate(status="failed", receipt=previous.receipt if previous else None)
        return JobUpdate(
            status="completed", receipt=previous.receipt if previous else None, value=value
        )

    def _reservation(
        self, claim: JobClaim, reply: MCPInputReply | None
    ) -> Callable[[], Awaitable[bool]]:
        if self.reserve_mutation is None:
            raise PermissionError("MCP mutations require a host reservation")
        return partial(self.reserve_mutation, claim, reply)

    async def start(self, claim: JobClaim, arguments: BaseModel) -> JobUpdate:
        """Send at most one tools/call. A missing create receipt leaves the outcome unknown."""
        authorization = partial(self._authorize, claim, "start")
        async with _issued_credentials(authorization) as (current, issued):
            try:
                serialized = _canonical(arguments)
                digest = hashlib.sha256(serialized.encode()).hexdigest()
                payload = json.loads(serialized) if digest == claim.binding.request_digest else None
            except Exception:
                payload = None
            if not isinstance(payload, dict):
                raise PermissionError("MCP arguments do not match the prepared operation")

            async def call(session: ClientSession) -> JobUpdate | CallToolResult:
                raw = await wire.call_tool(session, self.tool, payload)
                if isinstance(raw, wire.Created):
                    encoded_id = json.dumps(raw.task_id)[1:-1]
                    if any(token in raw.task_id or token in encoded_id for token in issued):
                        raise wire.Unavailable
                    receipt = json.dumps([self._scope(claim), raw.task_id], separators=(",", ":"))
                    return JobUpdate(
                        status="pending", receipt=receipt, value={"poll_at": _poll_at(raw)}
                    )
                return raw

            try:
                return await self._exchange(claim, current, call)
            except wire.Unavailable:
                return JobUpdate(status="unknown")

    def _task(self, claim: JobClaim) -> tuple[JobUpdate, float]:
        update = claim.update
        if update and update.receipt and update.status in {"pending", "input_required", "unknown"}:
            try:
                scope, task_id = json.loads(update.receipt)
                if scope == self._scope(claim) and isinstance(task_id, str) and task_id:
                    deadline = (
                        update.value.get("poll_at", 0) if isinstance(update.value, dict) else 0
                    )
                    return update, deadline
            except (ValueError, TypeError):
                pass
            finally:
                scope = task_id = None
        raise PermissionError("MCP receipt does not belong to this prepared operation")

    async def _get(self, session: ClientSession, update: JobUpdate) -> wire.Detail:
        # Unpack the validated receipt on the wire loop, after local control checks.
        assert update.receipt is not None
        task_id = json.loads(update.receipt)[1]
        try:
            detail: wire.Detail = await session.send_request(
                wire.GetOrCancel(method="tasks/get", params=wire.TaskParams(task_id=task_id)),
                wire.Detail,
            )
            if detail.task_id != task_id:
                raise wire.Unavailable
            return detail
        finally:
            del task_id

    def _status(self, claim: JobClaim, task: wire.Detail) -> JobUpdate | CallToolResult:
        assert claim.update is not None
        receipt = claim.update.receipt
        if task.status == "completed" and task.result is not None:
            return task.result
        if task.status in ("cancelled", "failed"):
            return JobUpdate(status=task.status, receipt=receipt)
        value: dict[str, Any] = {"poll_at": _poll_at(task)}
        if task.status == "working":
            return JobUpdate(status="pending", receipt=receipt, value=value)
        if task.status == "input_required" and task.input_requests:
            requests = {
                k: v.model_dump(mode="json", by_alias=True, exclude_none=True)
                for k, v in task.input_requests.items()
            }
            value.update(
                requests=requests, challenge=_digest([receipt, task.last_updated_at, requests])
            )
            return JobUpdate(status="input_required", receipt=receipt, value=value)
        raise wire.Unavailable

    async def observe(self, claim: JobClaim) -> JobUpdate:
        """Read job truth from tasks/get, respecting the peer's durable poll deadline."""
        authorization = partial(self._authorize, claim, "observe")
        async with _issued_credentials(authorization) as (current, issued):
            update, deadline = self._task(claim)
            remaining = deadline - time.time()
            if remaining > 0:
                # During the core's bounded fast path the start claim owns one optional read.
                # On a later wake the application can schedule another wake at the saved deadline.
                if claim.action != "start" or remaining > self.timeout_seconds:
                    return update
                await asyncio.sleep(remaining)

            async def read(session: ClientSession) -> JobUpdate | CallToolResult:
                return self._status(claim, await self._get(session, update))

            try:
                return await self._exchange(claim, current, read, previous=update, issued=issued)
            except wire.Unavailable:
                return JobUpdate(status="unknown", receipt=update.receipt)

    async def respond(self, claim: JobClaim, response: object) -> JobUpdate:
        """Re-read the challenge and send one captured answer to a host-qualified target."""
        if self.allow_input_replies is not True:
            raise PermissionError("The host has not enabled MCP input replies for this target")
        authorization = partial(self._authorize, claim, "respond")
        async with _issued_credentials(authorization) as (current_authorization, issued):
            update, deadline = self._task(claim)
            if (
                not isinstance(response, MCPInputReply)
                or update.status != "input_required"
                or not isinstance(update.value, dict)
                or response.challenge != update.value.get("challenge")
                or not response.responses
            ):
                raise PermissionError("MCP input approval does not match the current request")
            reserve = self._reservation(claim, response)

            answers: dict[str, ElicitResult] = {}

            async def reserve_answer() -> bool:
                endpoints = (self.endpoint, str(httpx2.URL(self.endpoint)))
                if any(
                    _private_text(reply.content, endpoints)
                    or any(
                        _form_contains_token(key, token)
                        or _form_contains_token(reply.content, token)
                        for token in issued
                    )
                    for key, reply in answers.items()
                ):
                    return False
                return await reserve()

            async def answer(session: ClientSession) -> JobUpdate | CallToolResult:
                nonlocal deadline
                task = await self._get(session, update)
                current = self._status(claim, task)
                if (
                    isinstance(current, CallToolResult)
                    or current.status != "input_required"
                    or not isinstance(current.value, dict)
                    or (current.value["challenge"] != response.challenge)
                ):
                    return current
                deadline = current.value["poll_at"]
                answers.update(response.responses)
                for key, reply in answers.items():
                    request = (task.input_requests or {}).get(key)
                    if (
                        request is None
                        or not isinstance(request, ElicitRequest)
                        or not isinstance(request.params, ElicitRequestFormParams)
                    ):
                        raise wire.Unsupported
                    content = reply.content
                    if _private_text(content, (task.task_id, claim.key)):
                        raise wire.Unsupported
                    if reply.action == "accept":
                        if content is None or not _accepts_form(
                            request.params.requested_schema, content
                        ):
                            raise wire.Unsupported
                    elif content is not None:
                        raise wire.Unsupported
                await session.send_request(
                    wire.Update(
                        params=wire.UpdateParams(task_id=task.task_id, input_responses=answers)
                    ),
                    wire.Acknowledged,
                )
                return JobUpdate(
                    status="pending",
                    receipt=update.receipt,
                    value={"poll_at": current.value["poll_at"]},
                )

            try:
                return await self._exchange(
                    claim, current_authorization, answer, reserve_answer, update, issued
                )
            except wire.Unavailable:
                return JobUpdate(
                    status="unknown", receipt=update.receipt, value={"poll_at": deadline}
                )
            finally:
                answers.clear()

    async def cancel(self, claim: JobClaim) -> JobUpdate:
        """Confirm the task is active and reserve its cancellation before sending."""
        authorization = partial(self._authorize, claim, "cancel")
        await authorization()
        update, deadline = self._task(claim)
        reserve = self._reservation(claim, None)
        if update.status == "unknown":
            return update

        async def send(session: ClientSession) -> JobUpdate | CallToolResult:
            nonlocal deadline
            task = await self._get(session, update)
            current = self._status(claim, task)
            if (
                isinstance(current, CallToolResult)
                or current.status not in {"pending", "input_required"}
                or not isinstance(current.value, dict)
            ):
                return current
            deadline = current.value["poll_at"]
            await session.send_request(
                wire.GetOrCancel(
                    method="tasks/cancel", params=wire.TaskParams(task_id=task.task_id)
                ),
                wire.Acknowledged,
            )
            return JobUpdate(status="pending", receipt=update.receipt, value={"poll_at": deadline})

        try:
            return await self._exchange(claim, authorization, send, reserve, update)
        except wire.Unavailable:
            return JobUpdate(status="unknown", receipt=update.receipt, value={"poll_at": deadline})


_preserve_module_identity(globals(), "agents.experimental.mcp_tasks")
