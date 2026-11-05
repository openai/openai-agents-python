"""A fixed A2A service behind the SDK's existing deferred-tool boundary."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, TypeVar

import anyio
from a2a.client import Client
from a2a.helpers.proto_helpers import new_data_message
from a2a.types import a2a_pb2 as p
from google.protobuf.json_format import MessageToDict
from pydantic import BaseModel

from ..experimental.deferred import JobClaim, JobUpdate
from ._client import _DEFAULT_LIMITS, A2AEndpoint, A2ALimits, _endpoint, _limits, connect_a2a

Operation = Literal["start", "observe", "respond", "cancel"]
_Input = TypeVar("_Input", bound=BaseModel)


def _json(value: object, limit: int) -> str:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(raw.encode()) > limit:
        raise ValueError("A2A value exceeds configured byte limit")
    return raw


@dataclass(frozen=True, kw_only=True, repr=False)
class A2AInputReply:
    """Host-approved answer bound to the exact current remote challenge."""

    task_id: str
    context_id: str
    challenge: str
    generation: int
    result: object = field(repr=False)


class A2AJobProvider(Generic[_Input]):
    """Use with DeferredTools and ``require_reconciliation=False``.

    authorize checks current host authority. authorization returns a current bearer
    token for the fixed endpoint. reserve atomically spends an approved mutation;
    its reservation survives lost acknowledgments. Generic A2A creation has no
    idempotency guarantee. The host owns projection, storage, and wake-up scheduling.
    """

    def __init__(
        self,
        endpoint: A2AEndpoint | Mapping[str, Any],
        *,
        authorize: Callable[[JobClaim, Operation], Awaitable[None]],
        authorization: Callable[[], Awaitable[str]],
        reserve: Callable[[JobClaim, Operation, str], Awaitable[bool]],
        encode: Callable[[_Input], p.Message],
        project: Callable[[p.Task], object],
        limits: A2ALimits | Mapping[str, Any] = _DEFAULT_LIMITS,
    ) -> None:
        endpoint, limits = _endpoint(endpoint), _limits(limits)
        self.endpoint, self.limits = endpoint, limits
        self._authorize, self._authorization, self._reserve = authorize, authorization, reserve
        self._encode, self._project = encode, project
        identity = _json([endpoint.name, endpoint.rpc_url, "1.0"], limits.message_bytes)
        self.id = "a2a/" + hashlib.sha256(identity.encode()).hexdigest()

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[Client]:
        # Discovery, admission and all RPCs in one job operation share a deadline.
        with anyio.fail_after(self.limits.timeout):
            async with connect_a2a(
                self.endpoint, authorization=self._authorization, limits=self.limits
            ) as client:
                yield client

    async def _check(self, claim: JobClaim, operation: Operation) -> None:
        allowed = False
        try:
            if claim.binding.provider_id == self.id:
                await self._authorize(claim, operation)
                allowed = True
        except Exception:
            pass
        if not allowed:
            raise PermissionError("A2A job authority is unavailable")

    def _receipt(self, claim: JobClaim) -> dict[str, str]:
        receipt = (
            json.loads(claim.update.receipt) if claim.update and claim.update.receipt else None
        )
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"task_id", "context_id"}
            or not all(isinstance(value, str) and value for value in receipt.values())
        ):
            raise ValueError("No acknowledged A2A task")
        return receipt

    def _update(self, task: p.Task, expected: dict[str, str] | None = None) -> JobUpdate:
        if (
            not task.id
            or not task.context_id
            or expected is not None
            and (task.id != expected["task_id"] or task.context_id != expected["context_id"])
        ):
            raise ValueError("A2A task identity does not match its receipt")
        receipt = _json(
            {"task_id": task.id, "context_id": task.context_id}, self.limits.message_bytes
        )
        try:
            if task.status.state == p.TASK_STATE_COMPLETED:
                value = self._project(task)
                _json(value, self.limits.message_bytes)
                return JobUpdate("completed", receipt, value)
            if task.status.state in (p.TASK_STATE_INPUT_REQUIRED, p.TASK_STATE_AUTH_REQUIRED):
                kind = (
                    "auth_required"
                    if task.status.state == p.TASK_STATE_AUTH_REQUIRED
                    else "input_required"
                )
                message = MessageToDict(task.status.message)
                if kind == "input_required":
                    proposals = [
                        part["data"] for part in message.get("parts", []) if "data" in part
                    ]
                    proposal = proposals[0] if len(proposals) == 1 else None
                    generation = proposal.get("generation") if isinstance(proposal, dict) else None
                    if (
                        not isinstance(proposal, dict)
                        or not isinstance(proposal.get("challenge"), str)
                        or not proposal["challenge"]
                        or not isinstance(generation, (int, float))
                        or isinstance(generation, bool)
                        or not math.isfinite(generation)
                        or generation <= 0
                        or int(generation) != generation
                    ):
                        return JobUpdate("unknown", receipt)
                value = {"kind": kind, "message": message}
                _json(value, self.limits.message_bytes)
                return JobUpdate("input_required", receipt, value)
        except Exception:
            return JobUpdate("unknown", receipt)
        statuses = {
            p.TASK_STATE_SUBMITTED: "pending",
            p.TASK_STATE_WORKING: "pending",
            p.TASK_STATE_CANCELED: "cancelled",
            p.TASK_STATE_FAILED: "failed",
            p.TASK_STATE_REJECTED: "failed",
        }
        return JobUpdate(statuses.get(task.status.state, "unknown"), receipt)  # type: ignore[arg-type]

    async def start(self, claim: JobClaim, arguments: _Input) -> JobUpdate:
        await self._check(claim, "start")
        message = self._encode(arguments)
        if (
            message.task_id
            or message.context_id
            or any(not (part.HasField("text") or part.HasField("data")) for part in message.parts)
            or not message.parts
        ):
            raise ValueError("A2A creation requires bounded text/JSON without task identity")
        _json(MessageToDict(message), self.limits.message_bytes)
        result = None
        try:
            async with self._connect() as client:
                request = p.SendMessageRequest(
                    message=message,
                    configuration=p.SendMessageConfiguration(return_immediately=True),
                )
                await self._check(claim, "start")
                if not await self._reserve(claim, "start", claim.key):
                    return JobUpdate("unknown")
                async for event in client.send_message(request):
                    if event.HasField("task"):
                        result = self._update(event.task)
                        break
        except Exception:
            pass
        return result if result is not None else JobUpdate("unknown")

    async def observe(self, claim: JobClaim) -> JobUpdate:
        await self._check(claim, "observe")
        receipt = self._receipt(claim)
        result = None
        try:
            async with self._connect() as client:
                await self._check(claim, "observe")
                result = self._update(
                    await client.get_task(p.GetTaskRequest(id=receipt["task_id"])), receipt
                )
        except Exception:
            pass
        return (
            result
            if result is not None
            else JobUpdate("unknown", claim.update.receipt if claim.update else None)
        )

    async def respond(self, claim: JobClaim, response: A2AInputReply) -> JobUpdate:
        await self._check(claim, "respond")
        receipt = self._receipt(claim)
        if not isinstance(response, A2AInputReply) or receipt != {
            "task_id": response.task_id,
            "context_id": response.context_id,
        }:
            raise ValueError("A2A answer belongs to another task")
        if (
            not response.challenge
            or type(response.generation) is not int
            or response.generation <= 0
        ):
            raise ValueError("A2A answer requires a current challenge and generation")
        value = {
            "challenge": response.challenge,
            "generation": response.generation,
            "result": response.result,
        }
        _json(value, self.limits.message_bytes)
        return await self._mutate(claim, receipt, "respond", value)

    async def cancel(self, claim: JobClaim) -> JobUpdate:
        await self._check(claim, "cancel")
        return await self._mutate(claim, self._receipt(claim), "cancel", None)

    async def _mutate(
        self,
        claim: JobClaim,
        receipt: dict[str, str],
        operation: Literal["respond", "cancel"],
        value: dict[str, object] | None,
    ) -> JobUpdate:
        result = None
        invalid = False
        try:
            async with self._connect() as client:
                await self._check(claim, operation)
                task = await client.get_task(p.GetTaskRequest(id=receipt["task_id"]))
                current = self._update(task, receipt)
                if current.status == "unknown":
                    return current
                if operation == "cancel" and current.status not in {"pending", "input_required"}:
                    return current
                if operation == "respond":
                    proposals = [
                        MessageToDict(part.data)
                        for part in task.status.message.parts
                        if part.HasField("data")
                    ]
                    invalid = (
                        task.status.state != p.TASK_STATE_INPUT_REQUIRED
                        or len(proposals) != 1
                        or value is None
                        or any(
                            proposals[0].get(key) != value[key]
                            for key in ("challenge", "generation")
                        )
                    )
                    if not invalid:
                        request = p.SendMessageRequest(
                            message=new_data_message(
                                value,
                                task_id=receipt["task_id"],
                                context_id=receipt["context_id"],
                                role=p.ROLE_USER,
                            ),
                            configuration=p.SendMessageConfiguration(return_immediately=True),
                        )
                if not invalid:
                    action = (
                        receipt
                        if value is None
                        else {
                            **receipt,
                            "challenge": value["challenge"],
                            "generation": value["generation"],
                        }
                    )
                    action_id = _json(action, self.limits.message_bytes)
                    await self._check(claim, operation)
                    if not await self._reserve(claim, operation, action_id):
                        return JobUpdate("unknown", claim.update.receipt if claim.update else None)
                    if operation == "cancel":
                        result = self._update(
                            await client.cancel_task(p.CancelTaskRequest(id=receipt["task_id"])),
                            receipt,
                        )
                    else:
                        async for event in client.send_message(request):
                            if event.HasField("task"):
                                result = self._update(event.task, receipt)
                                break
        except Exception:
            pass
        if invalid:
            raise ValueError(
                "A2A task no longer accepts this answer; authentication requires the host"
            )
        return (
            result
            if result is not None
            else JobUpdate("unknown", claim.update.receipt if claim.update else None)
        )
