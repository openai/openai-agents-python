"""External jobs use real manual grants and leave durable effect ownership with the host."""

from __future__ import annotations

import asyncio
import json
import warnings
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import pytest
from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_serializer,
    field_validator,
)

from agents import (
    Agent,
    FunctionTool,
    InteractionRequired,
    NativeError,
    Turn,
    _native,
    function_tool,
)
from agents.experimental.deferred import DeferredTools, JobBinding, JobClaim, JobUpdate
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import next_request, provider


class Input(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    pages: int = Field(ge=1, le=10)


class Output(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    report: str


@dataclass
class Row:
    generation: int = 0
    started: bool = False
    update: JobUpdate | None = None
    result: str | None = None
    mutations: set[str] = field(default_factory=set)


class Journal:
    """Single-event-loop conditional test store. The example owns the distributed store proof."""

    def __init__(self):
        self.rows: dict[JobBinding, Row] = {}
        self.state_json = ""
        self.log: list[str] = []
        self.forbidden: set[str] = set()
        self.force_busy = False

    async def prepare(self, state_json, bindings):
        if "prepare" in self.forbidden:
            raise PermissionError("private-journal-diagnostic")
        self.log.append("prepare")
        self.state_json = state_json
        for binding in bindings:
            self.rows.setdefault(binding, Row())

    async def claim(self, binding, operation):
        if binding not in self.rows or "claim" in self.forbidden:
            raise PermissionError("private-journal-diagnostic")
        row = self.rows[binding]
        row.generation += 1
        if self.force_busy:
            action = "busy"
        elif operation != "advance":
            action = "unknown" if operation in row.mutations else operation
            row.mutations.add(operation)
        elif row.result is not None:
            action = "deliver"
        elif row.update is not None:
            action = "project" if row.update.status == "completed" else "observe"
        elif row.started:
            action = "reconcile_creation"
        else:
            row.started = True
            action = "start"
        self.log.append("claim-" + action)
        return JobClaim(
            binding, "private-operation-key", row.generation, action, row.update, row.result
        )

    async def authorize(self, claim, operation):
        if operation in self.forbidden or self.rows[claim.binding].generation != claim.generation:
            raise PermissionError("private-authorization-diagnostic")
        self.log.append(operation)

    async def record(self, claim, update):
        await self.authorize(claim, "record")
        self.rows[claim.binding].update = update

    async def save_result(self, claim, result_json):
        await self.authorize(claim, "save_result")
        self.rows[claim.binding].result = result_json


class Provider:
    def __init__(self, updates):
        self.updates = list(updates)
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block: str | None = None
        self.active = 0

    async def next(self, name, argument=None):
        self.calls.append((name, argument))
        if self.block == name:
            self.active += 1
            self.entered.set()
            try:
                await self.release.wait()
            finally:
                self.active -= 1
        update = self.updates.pop(0)
        if isinstance(update, BaseException):
            raise update
        return update

    async def start(self, claim, arguments):
        return await self.next("start", arguments.pages)

    async def reconcile_creation(self, claim, arguments):
        return await self.next("reconcile", arguments.pages)

    async def observe(self, claim):
        return await self.next("observe", claim.update.receipt)

    async def respond(self, claim, response):
        return await self.next("respond", response)

    async def cancel(self, claim):
        return await self.next("cancel", claim.update.receipt)


def declared(journal, backend, *, tenant="tenant-a", provider_id="report/v1", **options):
    jobs = DeferredTools(journal=journal, tenant=tenant, run_id="run")
    tool = FunctionTool(
        name="render",
        description="Render a test report.",
        defer_loading=options.pop("defer_loading", False),
        input_type=options.pop("input_type", Input),
        output_type=options.pop("output_type", Output),
    )
    jobs.register(
        tool,
        provider_id=provider_id,
        provider=backend,
        project=options.pop("project", lambda value: Output(report=value["public"])),
        **options,
    )
    return jobs, tool


async def prepare_one(agent, jobs, tool, peer):
    peer.respond(function_call(tool.name, {"pages": 2}, "render-A"))
    async with agent.session() as session:
        await session.submit("Render.")
        req = await next_request(session.interactions(), tool)
        return await jobs.prepare(session, req), req


def done(report="public-report"):
    return JobUpdate(
        "completed", "secret-task-receipt", {"public": report, "secret": "private-artifact"}
    )


async def test_one_call_immediate_and_mixed_siblings(responses):
    journal = Journal()
    backend = Provider([done(), done("second")])
    jobs, tool = declared(journal, backend)
    other = FunctionTool(name="approve", description="Ask host.", input_type=Input, output_type=str)
    effects = []

    @function_tool
    def sibling() -> str:
        effects.append("accepted")
        return "sibling result"

    responses.respond(
        function_call("sibling", {}, "already-done"),
        function_call("render", {"pages": 2}, "A"),
        function_call("render", {"pages": 3}, "B"),
        function_call("approve", {"pages": 4}, "other"),
    )
    responses.respond(assistant_output("finished"))
    agent = Agent(
        instructions="Use the tools.", tools=[tool, other, sibling], provider=provider(responses)
    )
    async with timeout(10), agent.session() as session:
        original = await session.submit("Start.")
        request = await next_request(session.interactions(), tool)
        request.arguments.pages = 9
        async with jobs.start(session, request) as (restored, turn):
            assert isinstance(turn, Turn) and turn.turn_id == original.turn_id
            assert {row.call_id for row in journal.rows} == {"A", "B"}
            remaining = await next_request(restored.interactions(), tool)
            assert await jobs.advance(restored, remaining) is turn
            unrelated = await next_request(restored.interactions(), other)
            assert await unrelated.respond("approved") is None
            assert (await turn.get_result()).final_output == "finished"
            finished = await restored.save_state()
    assert sorted(backend.calls) == [("start", 2), ("start", 3)]
    assert effects == ["accepted"] and len(responses.requests) == 2
    assert json.loads(tool_output(responses.requests[1], request.call_id)) == {
        "report": "public-report"
    }
    assert tool_output(responses.requests[1], "other") == "approved"
    assert tool_output(responses.requests[1], "already-done") == "sibling result"
    assert (
        finished.format_version == 4
        and SessionState.from_json(journal.state_json).format_version == 5
    )
    assert journal.log == [
        "prepare",
        *["claim-start", "start", "record", "project", "save_result", "deliver"] * 2,
    ]
    for secret in ("secret-task-receipt", "private-operation-key", "private-artifact"):
        assert secret not in journal.state_json + finished.to_json() + json.dumps(
            responses.requests
        )


@pytest.mark.parametrize(
    "answered,remaining,early", [("A", "B", False), ("B", "A", False), ("B", "A", True)]
)
async def test_ordinary_reply_during_deferred_prepare(
    responses, monkeypatch, answered, remaining, early
):
    journal, backend = Journal(), Provider([done("deferred")])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(
        function_call(tool.name, {"pages": 1}, "A"), function_call(tool.name, {"pages": 2}, "B")
    )
    responses.respond(assistant_output("finished"))
    async with timeout(10):
        async with agent.session() as session:
            turn = await session.submit("Render twice.")
            stream = session.interactions()
            grants = {}
            for _ in range(2):
                request = await next_request(stream, tool)
                grants[request.call_id] = request

            capturing, release = asyncio.Event(), asyncio.Event()
            manual_ready = session._manual_ready

            async def pause_capture(active):
                ready = await manual_ready(active)
                if ready and (early or asyncio.current_task() is session._checkpoint_task):
                    capturing.set()
                    await release.wait()
                return ready

            monkeypatch.setattr(session, "_manual_ready", pause_capture)
            preparing = asyncio.create_task(jobs.prepare(session, grants["A"]))
            await capturing.wait()
            try:
                assert await grants[answered].respond(Output(report="ordinary")) is None
                if early:
                    # Native reply bookkeeping can still be settling after submit returns.
                    async def settling(active):
                        monkeypatch.setattr(session, "_manual_ready", manual_ready)
                        active.changed.set()
                        return False

                    monkeypatch.setattr(session, "_manual_ready", settling)
                else:
                    while not await session._native._manual_ready():
                        await asyncio.sleep(0)
            finally:
                release.set()
            if answered == "A":
                with pytest.raises(ValueError, match="could not be prepared"):
                    await preparing
                assert not journal.rows and not journal.state_json
                state = await jobs.prepare(session, grants[remaining])
            else:
                state = await preparing
            assert (await session.suspend()).to_json() == state.to_json() == journal.state_json
            assert [binding.call_id for binding in journal.rows] == [remaining]
        async with agent.session(state=state) as restored:
            request = await next_request(restored.interactions(), tool)
            assert request.call_id == remaining
            resumed = await jobs.advance(restored, request)
            assert isinstance(resumed, Turn) and resumed.turn_id == turn.turn_id
            assert (await resumed.get_result()).final_output == "finished"
    assert json.loads(tool_output(responses.requests[1], answered)) == {"report": "ordinary"}
    assert json.loads(tool_output(responses.requests[1], remaining)) == {"report": "deferred"}


async def test_zero_wait_pending_restore_input_cancel_and_result(responses):
    journal, receipt = Journal(), "secret-task-receipt"
    backend = Provider(
        [
            JobUpdate("pending", receipt),
            JobUpdate("input_required", receipt, "private challenge"),
            JobUpdate("pending"),
            JobUpdate("pending"),
            done(),
        ]
    )
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(10):
        state, retired = await prepare_one(agent, jobs, tool, responses)
        for op, expected in [
            ("advance", "pending"),
            ("advance", "input_required"),
            ("respond", "pending"),
            ("cancel", "pending"),
            ("advance", "done"),
        ]:
            async with agent.session(state=SessionState.from_json(journal.state_json)) as restored:
                req = await next_request(restored.interactions(), tool)
                with pytest.raises(ValueError, match="current root manual grant"):
                    await jobs.advance(restored, retired)
                if expected == "done":
                    responses.respond(assistant_output("ready"))
                result = await jobs.advance(
                    restored,
                    req,
                    operation=op,
                    response="approved for current request" if op == "respond" else None,
                )
                if expected == "done":
                    assert (
                        isinstance(result, Turn)
                        and (await result.get_result()).final_output == "ready"
                    )
                else:
                    assert result == expected and len(responses.requests) == 1
    assert state.to_json() == journal.state_json
    assert backend.calls == [
        ("start", 2),
        ("observe", receipt),
        ("respond", "approved for current request"),
        ("cancel", receipt),
        ("observe", receipt),
    ]
    assert len(responses.requests) == 2


@pytest.mark.parametrize("strong", [True, False])
async def test_lost_creation_is_reconciled_or_left_unknown(responses, strong):
    journal = Journal()
    backend = Provider([ConnectionError("secret-lost-create-diagnostic"), done()])
    jobs, tool = declared(journal, backend, require_reconciliation=strong)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(10):
        state, _ = await prepare_one(agent, jobs, tool, responses)
        async with agent.session(state=state) as session:
            req = await next_request(session.interactions(), tool)
            assert await jobs.advance(session, req) == "unknown"
        async with agent.session(state=state) as session:
            req = await next_request(session.interactions(), tool)
            if strong:
                responses.respond(assistant_output("reconciled"))
                turn = await jobs.advance(session, req)
                assert (
                    isinstance(turn, Turn)
                    and (await turn.get_result()).final_output == "reconciled"
                )
            else:
                assert await jobs.advance(session, req) == "unknown"
    assert backend.calls == ([("start", 2), ("reconcile", 2)] if strong else [("start", 2)])
    assert len(responses.requests) == (2 if strong else 1)


async def test_stale_owner_and_pending_optional_wait_leave_no_observer(responses):
    journal = Journal()
    backend = Provider([JobUpdate("pending", "secret-task-receipt"), done()])
    backend.block = "observe"
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(10):
        state, _ = await prepare_one(agent, jobs, tool, responses)
        async with agent.session(state=state) as first:
            req = await next_request(first.interactions(), tool)
            assert await jobs.advance(first, req, wait_seconds=0.05) == "pending"
            assert backend.entered.is_set() and backend.active == 0
            assert len(responses.requests) == 1
            backend.entered.clear()
            task = asyncio.create_task(jobs.advance(first, req))
            await backend.entered.wait()
            async with agent.session(state=state) as second:
                competing = await next_request(second.interactions(), tool)
                journal.force_busy = True
                assert await jobs.advance(second, competing) == "busy"
                backend.release.set()
                with pytest.raises(PermissionError):
                    await task
    assert backend.active == 0
    assert journal.log.count("save_result") == 0 and len(responses.requests) == 1


async def test_partial_preparation_and_foreign_bindings_never_reach_provider(responses):
    journal, backend = Journal(), Provider([done()])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(10):
        journal.forbidden.add("prepare")
        with pytest.raises(PermissionError):
            await prepare_one(agent, jobs, tool, responses)
        assert not journal.rows and not backend.calls
        journal.forbidden.clear()
        state, _ = await prepare_one(agent, jobs, tool, responses)
        for tenant, provider_id in [("tenant-b", "report/v1"), ("tenant-a", "report/v2")]:
            other_jobs, other_tool = declared(
                journal, backend, tenant=tenant, provider_id=provider_id
            )
            other = replace(agent, tools=(other_tool,))
            async with other.session(state=state) as fresh:
                grant = await next_request(fresh.interactions(), other_tool)
                with pytest.raises(ValueError):
                    await jobs.advance(fresh, grant)
                with pytest.raises(PermissionError):
                    await other_jobs.advance(fresh, grant)
        mutated = SessionState.from_json(state.to_json())
        mutated.agent_options["unused-host-field"] = True
        async with agent.session(state=mutated) as fresh:
            req = await next_request(fresh.interactions(), tool)
            with pytest.raises(PermissionError):
                await jobs.advance(fresh, req)
    assert not backend.calls and len(responses.requests) == 2


def test_unstable_input_is_refused_at_registration():
    calls = []

    def generate() -> str:
        calls.append("generated")
        return uuid4().hex

    class Generated(Input):
        request_id: str = Field(default_factory=generate)

    class Nested(Input):
        items: list[Generated]

    class Unordered(Input):
        labels: set[str]

    class Frozen(Input):
        labels: frozenset[str]

    class Validated(Input):
        request_id: str

        @field_validator("request_id")
        @classmethod
        def replace_id(cls, value: str) -> str:
            return generate()

    class Serialized(Input):
        request_id: str

        @field_serializer("request_id")
        def render_id(self, value: str) -> str:
            return generate()

    class Collision(Input):
        request_id: str = Field(serialization_alias="pages")

    class WrongDefault(Input):
        count: int = Field(default=0, gt=0)

    class ChangedConstruction(Input):
        def __new__(cls):
            calls.append("constructed")
            return super().__new__(cls)

    class ChangedCopy(Input):
        def __deepcopy__(self, memo=None):
            calls.append("copied")
            return self

    journal, backend = Journal(), Provider([])
    for shape in (
        Generated,
        Nested,
        Unordered,
        Frozen,
        Validated,
        Serialized,
        Collision,
        WrongDefault,
        ChangedConstruction,
        ChangedCopy,
    ):
        with pytest.raises(ValueError, match="Supply generated IDs explicitly"):
            declared(journal, backend, input_type=shape)
    assert not calls and not journal.log and not backend.calls


async def test_omitted_required_id_fails_before_prepare_and_ordinary_manual_keeps_its_factory(
    responses,
):
    class Required(Input):
        request_id: str

    class Generated(Input):
        request_id: str = Field(default_factory=lambda: uuid4().hex)

    journal, backend = Journal(), Provider([])
    _, tool = declared(journal, backend, input_type=Required)
    manual = FunctionTool(name="live", description="Run.", input_type=Generated, output_type=str)
    agent = Agent(instructions="Render.", tools=[tool, manual], provider=provider(responses))
    async with timeout(10):
        responses.respond(function_call(tool.name, {"pages": 2}, "omitted"))
        async with agent.session() as session:
            turn = await session.submit("Render.")
            with pytest.raises(NativeError, match="arguments could not be delivered"):
                await turn.get_result()
        assert not journal.log and not backend.calls
        responses.respond(function_call(manual.name, {"pages": 2}, "ordinary"))
        async with agent.session() as session:
            turn = await session.submit("Run.")
            grant = await next_request(session.interactions(), manual)
            assert len(grant.arguments.request_id) == 32
            responses.respond(assistant_output("done"))
            await grant.respond("accepted")
            assert (await turn.get_result()).final_output == "done"
    assert tool_output(responses.requests[-1], "ordinary") == "accepted"


@pytest.mark.parametrize("closed", [False, True], ids=["extra-ignore", "extra-forbid"])
async def test_union_input_preserves_fields_on_the_grant_and_at_the_provider(responses, closed):
    class Basic(BaseModel):
        model_config = ConfigDict(
            strict=True,
            extra="forbid" if closed else "ignore",
            json_schema_extra={"additionalProperties": False},
        )
        x: int

    class Extended(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        x: int
        y: int

    class Request(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        item: Basic | Extended = Field(union_mode="left_to_right")

    class Backend(Provider):
        async def start(self, claim, arguments):
            return await self.next("start", arguments.item.model_dump())

    journal, backend = Journal(), Backend([done(), done()])
    jobs, tool = declared(journal, backend, input_type=Request)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(
        function_call(tool.name, {"item": {"x": 1}}, "basic"),
        function_call(tool.name, {"item": {"x": 1, "y": 2}}, "extended"),
    )
    async with timeout(10):
        async with agent.session() as session:
            await session.submit("Render.")
            stream = session.interactions()
            grants = {}
            for _ in range(2):
                req = await next_request(stream, tool)
                grants[req.call_id] = req
            expected = {"x": 1, "y": 2} if closed else {"x": 1}
            assert grants["extended"].arguments.item.model_dump() == expected
            if not closed:
                with pytest.raises(ValueError, match="could not be prepared"):
                    await jobs.prepare(session, grants["basic"])
                assert not journal.log and not backend.calls
                await grants["extended"].respond(Output(report="ambiguous input"))
            state = await jobs.prepare(session, grants["basic"])
        wanted = {"basic": {"x": 1}}
        if closed:
            wanted["extended"] = {"x": 1, "y": 2}
        assert {binding.call_id for binding in journal.rows} == wanted.keys()
        async with agent.session(state=state) as fresh:
            responses.respond(assistant_output("delivered"))
            stream = fresh.interactions()
            for _ in wanted:
                grant = await next_request(stream, tool)
                assert grant.arguments.item.model_dump() == wanted[grant.call_id]
                turn = await jobs.advance(fresh, grant)
                assert backend.calls[-1] == ("start", wanted[grant.call_id])
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "delivered"


async def test_matching_input_aliases_cannot_hide_a_different_branch_on_restore(responses):
    class Basic(BaseModel):
        model_config = ConfigDict(strict=True, json_schema_extra={"additionalProperties": False})
        x: int = 1

    class Extended(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        y: int = Field(serialization_alias="x")

    class Request(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        items: list[Annotated[Basic | Extended, Field(union_mode="left_to_right")]]

    journal, backend = Journal(), Provider([])
    jobs, tool = declared(journal, backend, input_type=Request)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(function_call(tool.name, {"items": [{"y": 1}]}, "original"))
    async with timeout(5):
        async with agent.session() as session:
            await session.submit("Render.")
            grant = await next_request(session.interactions(), tool)
            assert grant.arguments.model_dump(by_alias=True) == {"items": [{"x": 1}]}
            with pytest.raises(ValueError, match="could not be prepared"):
                await jobs.prepare(session, grant)
            state = await session.suspend()
        async with agent.session(state=state) as fresh:
            grant = await next_request(fresh.interactions(), tool)
            with pytest.raises(ValueError, match="could not be completed"):
                await jobs.advance(fresh, grant)
    assert not journal.log and not backend.calls


async def test_explicit_input_and_constant_defaults_reach_start_and_reconciliation(responses):
    class Item(BaseModel):
        model_config = ConfigDict(
            strict=True, extra="forbid", serialize_by_alias=True, validate_default=True
        )
        request_id: str = Field(alias="requestId")
        labels: list[str]
        kind: Literal["red", "blue"] = "blue"
        weight: float | None = None
        enabled: bool = True
        reference: str | int

    class Request(Input):
        item: Item
        previous: Item | int = 0

    class Backend(Provider):
        async def start(self, claim, arguments):
            return await self.next("start", (claim.key, arguments.item.model_dump()))

        async def reconcile_creation(self, claim, arguments):
            return await self.next("reconcile", (claim.key, arguments.item.model_dump()))

    journal, backend = Journal(), Backend([OSError("lost acknowledgment"), done()])
    jobs, tool = declared(journal, backend, input_type=Request)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    args = {"requestId": "explicit", "labels": ["second", "first"], "reference": 17}
    responses.respond(function_call(tool.name, {"pages": 2, "item": args}, "original"))
    async with timeout(10):
        async with agent.session() as session:
            await session.submit("Render.")
            grant = await next_request(session.interactions(), tool)
            state = await jobs.prepare(session, grant)
        async with agent.session(state=state) as first:
            grant = await next_request(first.interactions(), tool)
            assert await jobs.advance(first, grant) == "unknown"
        async with agent.session(state=state) as fresh:
            grant = await next_request(fresh.interactions(), tool)
            responses.respond(assistant_output("delivered"))
            turn = await jobs.advance(fresh, grant)
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "delivered"
    payload = {**args, "kind": "blue", "weight": None, "enabled": True}
    assert backend.calls == [
        (op, ("private-operation-key", payload)) for op in ("start", "reconcile")
    ]
    assert tool_output(responses.requests[-1], "original") == '{"report":"public-report"}'


def test_deferred_result_refuses_rich_declarations_before_work():
    class Unordered(Output):
        tags: set[str]

    class Frozen(Output):
        tags: frozenset[str]

    class Binary(Output):
        model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="utf8")
        payload: bytes

    class Timestamp(Output):
        model_config = ConfigDict(ser_json_temporal="milliseconds")
        value: datetime

    class Renamed(Output):
        model_config = ConfigDict(serialize_by_alias=True)
        label: str = Field(serialization_alias="wireLabel")

    class Transformed(Output):
        @field_serializer("report")
        def render(self, value: str) -> str:
            raise AssertionError("The serializer must not run")

    journal, backend = Journal(), Provider([])
    errors = set()
    for shape in (Unordered, Frozen, Binary, Timestamp, Renamed, Transformed):
        with pytest.raises(ValueError, match="Project rich values.*reply directly") as error:
            declared(journal, backend, output_type=shape)
        errors.add(str(error.value))
    assert len(errors) == 1 and not backend.calls and not journal.rows


def test_output_refuses_defaults_without_rendering_them_or_their_schema():
    calls = []

    def render(value: str) -> str:
        calls.append(value)
        return value

    class Result(Output):
        model_config = ConfigDict(json_schema_extra=lambda schema: calls.append("schema"))
        tag: Annotated[str, PlainSerializer(render, return_type=str)] = "private-default"

    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        with pytest.raises(ValueError, match="required public JSON fields"):
            declared(Journal(), Provider([]), input_type=Result, output_type=Result)
    assert not calls and not seen


async def test_public_wire_fields_survive_denial_and_reject_other_input_spellings(responses):
    class Item(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        wireLabel: str = Field(
            validation_alias=AliasChoices("source", AliasPath("legacy", "label"))
        )
        source: str

    class Result(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        items: list[Item]

    journal, backend = Journal(), Provider([done("ready")])
    jobs, tool = declared(
        journal,
        backend,
        output_type=Result,
        project=lambda raw: Result.model_validate(
            {"items": [{"wireLabel": raw["public"], "source": "original"}]},
            by_name=True,
            by_alias=False,
        ),
    )
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    expected = '{"items":[{"wireLabel":"ready","source":"original"}]}'
    async with timeout(5):
        state, original = await prepare_one(agent, jobs, tool, responses)
        journal.forbidden.add("deliver")
        async with agent.session(state=state) as session:
            request = await next_request(session.interactions(), tool)
            with pytest.raises(PermissionError):
                await jobs.advance(session, request)
        [row] = journal.rows.values()
        assert row.result == expected and len(responses.requests) == 1
        journal.forbidden.clear()
        async with agent.session(state=state) as fresh:
            request = await next_request(fresh.interactions(), tool)
            assert request.call_id == original.call_id
            for invalid in (
                '{"items":[{"source":"original","legacy":{"label":"ready"}}]}',
                " " + expected,
            ):
                row.result = invalid
                with pytest.raises(ValueError):
                    await jobs.advance(fresh, request)
                assert len(responses.requests) == 1
            row.result = expected
            responses.respond(assistant_output("delivered"))
            turn = await jobs.advance(fresh, request)
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "delivered"
    assert tool_output(responses.requests[-1], original.call_id) == expected
    assert backend.calls == [("start", 2)] and journal.log.count("project") == 1


async def test_nonfinite_projection_keeps_the_grant_for_ordered_public_fields(responses):
    class Summary(Output):
        labels: list[str]
        ratio: float | None

    samples = iter([float("nan"), float("inf"), float("-inf"), 0.25])

    def project(private):
        return Summary(
            report=private["public"], labels=sorted(private["labels"]), ratio=next(samples)
        )

    journal = Journal()
    backend = Provider(
        [JobUpdate("completed", "private-receipt", {"public": "ready", "labels": {"b", "a"}})]
    )
    jobs, tool = declared(journal, backend, output_type=Summary, project=project)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        state, original = await prepare_one(agent, jobs, tool, responses)
        async with agent.session(state=state) as fresh:
            request = await next_request(fresh.interactions(), tool)
            for _ in range(3):
                with pytest.raises(ValueError) as error:
                    await jobs.advance(fresh, request)
                assert error.value.__cause__ is error.value.__context__ is None
                [row] = journal.rows.values()
                assert row.update.status == "completed" and row.result is None
                assert (await next_request(fresh.interactions(), tool)).call_id == original.call_id
                assert len(responses.requests) == 1
            responses.respond(assistant_output("delivered"))
            turn = await jobs.advance(fresh, request)
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "delivered"
    expected = '{"report":"ready","labels":["a","b"],"ratio":0.25}'
    assert row.result == tool_output(responses.requests[-1], original.call_id) == expected
    assert backend.calls == [("start", 2)]


async def test_reassigned_field_order_is_refused_before_saving(responses):
    class Pair(Output):
        count: int

    reordered = Pair(report="ready", count=2)
    del reordered.report
    reordered.report = "ready"
    values = iter([reordered, Pair(report="ready", count=2)])
    journal, backend = Journal(), Provider([done()])
    jobs, tool = declared(journal, backend, output_type=Pair, project=lambda _: next(values))
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    state, original = await prepare_one(agent, jobs, tool, responses)
    async with timeout(5), agent.session(state=state) as fresh:
        req = await next_request(fresh.interactions(), tool)
        with pytest.raises(ValueError):
            await jobs.advance(fresh, req)
        [row] = journal.rows.values()
        assert row.result is None and len(responses.requests) == 1
        responses.respond(assistant_output("done"))
        turn = await jobs.advance(fresh, req)
        assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "done"
    expected = '{"report":"ready","count":2}'
    assert row.result == tool_output(responses.requests[-1], original.call_id) == expected
    assert backend.calls == [("start", 2)]


@pytest.mark.parametrize(("entry", "interrupt"), [("prepare", "deadline"), ("start", "caller")])
@pytest.mark.parametrize("phase", ["before-seal", "seal"])
async def test_prepare_interruption_at_native_seal(responses, monkeypatch, entry, interrupt, phase):
    entered, release = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    clock = loop.time
    offset = 0
    monkeypatch.setattr(loop, "time", lambda: clock() + offset)
    journal, backend = Journal(), Provider([done(), done()])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(
        function_call("render", {"pages": 2}, "A"), function_call("render", {"pages": 3}, "B")
    )
    cause = object()
    async with timeout(60), agent.session() as session:
        await session.submit("Render.")
        request = await next_request(session.interactions(), tool)
        native = session._native

        class Gate:
            def __getattr__(self, name):
                return getattr(native, name)

            async def _manual_ready(self):
                if phase == "before-seal" and asyncio.current_task() is session._checkpoint_task:
                    entered.set()
                    await release.wait()
                return await native._manual_ready()

            async def _seal_manual(self, *args, **kwargs):
                state = await native._seal_manual(*args, **kwargs)
                if phase == "seal":
                    entered.set()
                    await release.wait()
                return state

        async def drive():
            error = asyncio.CancelledError if interrupt == "caller" else TimeoutError
            with pytest.raises(error) as stopped:
                if entry == "prepare":
                    await jobs.prepare(session, request, timeout_seconds=10)
                else:
                    async with jobs.start(session, request, timeout_seconds=10):
                        pytest.fail("Interrupted start yielded a session")
            return stopped.value

        session._native = Gate()
        driver = asyncio.create_task(drive())
        try:
            await entered.wait()
            if interrupt == "caller":
                driver.cancel(cause)
            else:
                offset += 11
            # The expired timer or caller cancellation runs before this loop timer.
            notified = loop.create_future()
            loop.call_later(0, notified.set_result, None)
            await notified
            assert not journal.log and not backend.calls
            release.set()
            stopped = await driver
            if interrupt == "caller":
                assert stopped.args == (cause,)
            else:
                assert str(stopped) == "Deferred checkpoint preparation timed out"
        finally:
            release.set()
            session._native = native
            if not driver.done():
                driver.cancel()
                await asyncio.gather(driver, return_exceptions=True)

        assert not backend.calls and len(responses.requests) == 1
        if phase == "before-seal":
            assert not journal.log and not journal.rows
            await request.respond(Output(report="manual"))
        else:
            assert journal.log == ["prepare"]
            state = SessionState.from_json(journal.state_json)
            assert state.to_json() == (await session.suspend()).to_json()
            assert {row.call_id for row in journal.rows} == {"A", "B"}
            async with agent.session(state=state) as restored:
                interactions = restored.interactions()
                grants = [
                    await next_request(interactions, tool),
                    await next_request(interactions, tool),
                ]
                responses.respond(assistant_output("continued"))
                for grant in grants:
                    turn = await jobs.advance(restored, grant)
                assert (
                    isinstance(turn, Turn) and (await turn.get_result()).final_output == "continued"
                )
            assert sorted(backend.calls) == [("start", 2), ("start", 3)]


@pytest.mark.parametrize("outcome", ["denied", "lost-ack", "stalled"])
async def test_unconfirmed_prepare_recovers_sealed_turn_without_retry(
    responses, monkeypatch, outcome
):
    loop = asyncio.get_running_loop()
    clock = loop.time
    offset = 0
    monkeypatch.setattr(loop, "time", lambda: clock() + offset)
    attempts = []

    class Store(Journal):
        async def prepare(self, state_json, bindings):
            nonlocal offset
            attempts.append("write")
            if outcome == "denied":
                raise PermissionError("private-journal-diagnostic")
            if outcome == "stalled":
                offset += 11
                try:
                    await asyncio.Future()
                finally:
                    attempts.append("rollback")
            await super().prepare(state_json, bindings)
            raise OSError("private-journal-diagnostic")

    journal, backend = Store(), Provider([])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(function_call("render", {"pages": 2}, "A"))
    reason = object()
    async with timeout(60), agent.session() as session:
        turn = await session.submit("Render.")
        request = await next_request(session.interactions(), tool)
        native = session._native

        class Interrupt:
            def __getattr__(self, name):
                return getattr(native, name)

            async def _seal_manual(self, *args, **kwargs):
                state = await native._seal_manual(*args, **kwargs)
                driver.cancel(reason)
                return state

        async def start():
            with pytest.raises(asyncio.CancelledError) as stopped:
                async with jobs.start(session, request, timeout_seconds=10):
                    pytest.fail("Interrupted start yielded a session")
            return stopped.value

        session._native = Interrupt()
        try:
            driver = asyncio.create_task(start())
            stopped = await driver
        finally:
            session._native = native
        assert stopped.args == (reason,) and stopped.__context__ is None
        sealed = await session.suspend()

    assert attempts == (["write", "rollback"] if outcome == "stalled" else ["write"])
    assert journal.state_json == (sealed.to_json() if outcome == "lost-ack" else "")
    assert agent.provider.api_key not in sealed.to_json()
    with pytest.raises(InteractionRequired) as paused:
        await turn.get_result()
    assert paused.value.state is not None and paused.value.state.to_json() == sealed.to_json()
    async with agent.session(state=paused.value.state) as restored:
        fresh = await next_request(restored.interactions(), tool)
        assert fresh.call_id == request.call_id
        if not journal.rows:
            with pytest.raises(PermissionError, match="Deferred job operation was denied"):
                await jobs.advance(restored, fresh)
    assert not backend.calls and len(responses.requests) == 1


@pytest.mark.parametrize(("entry", "first_stop"), [("prepare", "caller"), ("start", "deadline")])
async def test_prepare_joins_journal_rollback(responses, monkeypatch, entry, first_stop):
    entered, rolling_back, release = (asyncio.Event() for _ in range(3))
    order = []
    loop = asyncio.get_running_loop()
    clock = loop.time
    offset = 0
    monkeypatch.setattr(loop, "time", lambda: clock() + offset)

    class Store(Journal):
        async def prepare(self, state_json, bindings):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                rolling_back.set()
                await release.wait()
                order.append("rollback")

    journal, backend = Store(), Provider([])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(function_call(tool.name, {"pages": 2}, "render-A"))
    async with timeout(60), agent.session() as session:
        await session.submit("Render.")
        request = await next_request(session.interactions(), tool)

        async def drive():
            try:
                if entry == "prepare":
                    return await jobs.prepare(session, request, timeout_seconds=10)
                async with jobs.start(session, request, timeout_seconds=10):
                    pytest.fail("start advanced past an interrupted prepare")
            finally:
                order.append("caller")

        driver = asyncio.create_task(drive())
        try:
            await entered.wait()
            if first_stop == "caller":
                driver.cancel()
            else:
                offset += 11
            await rolling_back.wait()
            driver.cancel()
            finished, _ = await asyncio.wait((driver,), timeout=0.05)
        finally:
            release.set()
            (result,) = await asyncio.gather(driver, return_exceptions=True)
        assert not finished and order == ["rollback", "caller"]
        if first_stop == "caller":
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert isinstance(result, TimeoutError)
            assert str(result) == "Deferred checkpoint preparation timed out"
        assert not backend.calls and not journal.rows and len(responses.requests) == 1


@pytest.mark.parametrize("first_stop", ["caller", "deadline"])
async def test_start_joins_cancelled_native_restore(responses, monkeypatch, first_stop):
    opened, publish, retiring, release = (asyncio.Event() for _ in range(4))
    homes = []
    loop = asyncio.get_running_loop()
    clock = loop.time
    offset = 0
    monkeypatch.setattr(loop, "time", lambda: clock() + offset)
    create = _native.create_checkpoint_engine
    close_orphan = _native.NativeProviderLease.close_orphan

    async def held_engine(*args, **kwargs):
        engine = await create(*args, **kwargs)
        homes.append(Path(json.loads(args[1])["codex_home"]))
        opened.set()
        await publish.wait()
        return engine

    async def held_close(lease):
        retiring.set()
        await release.wait()
        await close_orphan(lease)

    journal, backend = Journal(), Provider([])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(function_call(tool.name, {"pages": 2}, "render-A"))
    async with timeout(60), agent.session() as session:
        await session.submit("Render.")
        request = await next_request(session.interactions(), tool)
        monkeypatch.setattr(_native, "create_checkpoint_engine", held_engine)
        monkeypatch.setattr(_native.NativeProviderLease, "close_orphan", held_close)

        async def drive():
            async with jobs.start(session, request):
                pytest.fail("start adopted a cancelled restore")

        driver = asyncio.create_task(drive())
        try:
            await opened.wait()
            if first_stop == "caller":
                driver.cancel()
            else:
                offset += 11
            publish.set()
            await retiring.wait()
            driver.cancel()
            finished, _ = await asyncio.wait((driver,), timeout=0.05)
            assert homes[0].is_dir() and not finished
        finally:
            publish.set()
            release.set()
            (result,) = await asyncio.gather(driver, return_exceptions=True)
        if first_stop == "caller":
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert isinstance(result, TimeoutError)
            assert str(result) == "Deferred session restore timed out"
        assert not homes[0].exists()
        assert journal.log == ["prepare"] and not backend.calls and len(responses.requests) == 1


async def test_start_expired_budget_does_not_open_restore(responses, monkeypatch):
    loop = asyncio.get_running_loop()
    clock = loop.time
    offset = 0
    monkeypatch.setattr(loop, "time", lambda: clock() + offset)

    class Store(Journal):
        async def prepare(self, state_json, bindings):
            nonlocal offset
            await super().prepare(state_json, bindings)
            offset += 11

    journal, backend = Store(), Provider([])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(function_call(tool.name, {"pages": 2}, "render-A"))
    async with agent.session() as session:
        await session.submit("Render.")
        request = await next_request(session.interactions(), tool)

        async def unexpected_restore(*args, **kwargs):
            pytest.fail("Expired start opened a restore")

        monkeypatch.setattr(_native, "create_checkpoint_engine", unexpected_restore)
        with pytest.raises(TimeoutError, match="Deferred session restore timed out"):
            async with jobs.start(session, request):
                pytest.fail("Expired start advanced")
        assert journal.log == ["prepare"] and not backend.calls


@pytest.mark.parametrize("phase", ["restore", "provider", "caller-cancel"])
async def test_start_shares_deadline_and_joins_in_flight_work(responses, monkeypatch, phase):
    loop = asyncio.get_running_loop()
    clock = loop.time
    offset = 0.0
    monkeypatch.setattr(loop, "time", lambda: clock() + offset)
    entered_restore, hold_restore, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class JournalWithPrepareCost(Journal):
        async def prepare(self, state_json, bindings):
            nonlocal offset
            await super().prepare(state_json, bindings)
            offset += 20

    journal, backend = JournalWithPrepareCost(), Provider([])
    backend.block = "start"
    jobs, tool = declared(journal, backend, require_reconciliation=False)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    open_session = agent.session

    @asynccontextmanager
    async def gated_restore(**kwargs):
        # Hold entry after the real native Session opens so its cleanup is observable.
        try:
            async with open_session(**kwargs) as restored:
                entered_restore.set()
                if phase == "restore":
                    await hold_restore.wait()
                yield restored
        finally:
            closed.set()

    responses.respond(function_call(tool.name, {"pages": 2}, "render-A"))
    async with open_session() as session:
        await session.submit("Render.")
        req = await next_request(session.interactions(), tool)
        monkeypatch.setattr(agent, "session", gated_restore)

        async def drive():
            async with jobs.start(session, req, timeout_seconds=30) as (_, outcome):
                return outcome

        driver = asyncio.create_task(drive())
        try:
            # The watchdog includes both controlled advances of the loop clock.
            async with timeout(35):
                gate = entered_restore if phase == "restore" else backend.entered
                await gate.wait()
                if phase == "caller-cancel":
                    driver.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await driver
                else:
                    # Only ten seconds of the thirty-second budget remain after prepare.
                    offset += 11
                    if phase == "restore":
                        with pytest.raises(TimeoutError):
                            await driver
                    else:
                        assert await driver == "unknown"
        finally:
            if not driver.done():
                driver.cancel()
                await asyncio.gather(driver, return_exceptions=True)
        assert closed.is_set() and backend.active == 0
    if phase == "restore":
        assert journal.log == ["prepare"] and not backend.calls
    else:
        async with open_session(state=SessionState.from_json(journal.state_json)) as resumed:
            current = await next_request(resumed.interactions(), tool)
            assert await jobs.advance(resumed, current) == "unknown"
        assert backend.calls == [("start", 2)]
    assert len(responses.requests) == 1


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), None, False])
async def test_invalid_entry_budget_precedes_seal_or_effect(responses, value):
    journal, backend = Journal(), Provider([])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(function_call("render", {"pages": 2}, "A"))
    async with timeout(5), agent.session() as session:
        await session.submit("Render.")
        req = await next_request(session.interactions(), tool)
        with pytest.raises(ValueError, match="time limit"):
            async with jobs.start(session, req, timeout_seconds=value):
                pytest.fail("Invalid budget admitted.")
        assert not journal.rows and not backend.calls


async def test_stalled_automatic_sibling_and_structured_agent_refuse_before_create(responses):
    entered, released = asyncio.Event(), asyncio.Event()

    @function_tool
    async def wait_for_host() -> str:
        entered.set()
        await released.wait()
        return "done"

    journal, backend = Journal(), Provider([])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool, wait_for_host], provider=provider(responses))
    responses.respond(
        function_call("render", {"pages": 2}, "A"), function_call("wait_for_host", {}, "B")
    )
    async with timeout(5), agent.session() as session:
        await session.submit("Start.")
        req = await next_request(session.interactions(), tool)
        await entered.wait()
        with pytest.raises(TimeoutError):
            await jobs.prepare(session, req, timeout_seconds=0.04)
        released.set()
    agent = Agent(
        instructions="Render.", tools=[tool], output_type=Output, provider=provider(responses)
    )
    responses.respond(function_call("render", {"pages": 2}, "A"))
    async with timeout(5), agent.session() as session:
        await session.submit("Start.")
        req = await next_request(session.interactions(), tool)
        with pytest.raises(ValueError, match="prepared"):
            await jobs.prepare(session, req)
    assert not backend.calls and not journal.rows


def test_recovery_rejects_callback_before_provider_inspection():
    @function_tool(name="report")
    def callback() -> str:
        return "done"

    class UnusedProvider:
        @property
        def reconcile_creation(self):
            pytest.fail("Registration inspected the provider for a callback")

    jobs = DeferredTools(journal=Journal(), tenant="a", run_id="run")
    with pytest.raises(TypeError, match="application answers"):
        jobs.register(
            callback, provider_id="v1", provider=UnusedProvider(), project=Output.model_validate
        )

    tool = FunctionTool(name="report", description="", input_type=Input, output_type=Output)
    assert (
        jobs.register(tool, provider_id="v1", provider=Provider([]), project=Output.model_validate)
        is None
    )


def test_closed_nested_models_and_strong_provider_registration():
    class Ignored(BaseModel):
        pages: int

    class Nested(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        nested: list[Ignored]

    class OpenMap(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        mapping: dict[str, object]

    class Choice(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        kind: Literal["report"]

    jobs = DeferredTools(journal=Journal(), tenant="a", run_id="run")
    for invalid in [Ignored, Nested, OpenMap]:
        with pytest.raises(ValueError, match="extra fields"):
            jobs.register(
                FunctionTool(name="report", description="", input_type=invalid, output_type=Output),
                provider_id="v1",
                provider=Provider([]),
                project=lambda x: x,
            )
    with pytest.raises(ValueError, match="reconciliation"):
        jobs.register(
            FunctionTool(name="report", description="", input_type=Choice, output_type=Choice),
            provider_id="v1",
            provider=object(),
            project=lambda x: x,
        )


async def test_optional_observation_finishes_in_one_entry_without_new_model_sample(responses):
    journal, backend = Journal(), Provider([JobUpdate("pending", "secret-task-receipt"), done()])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(function_call("render", {"pages": 2}, "fast"))
    responses.respond(assistant_output("ready"))
    async with timeout(5), agent.session() as session:
        await session.submit("Start.")
        req = await next_request(session.interactions(), tool)
        async with jobs.start(session, req, wait_seconds=1.0) as (_, result):
            assert isinstance(result, Turn) and (await result.get_result()).final_output == "ready"
    assert backend.calls == [("start", 2), ("observe", "secret-task-receipt")]
    assert len(responses.requests) == 2 and json.loads(
        tool_output(responses.requests[1], "fast")
    ) == {"report": "public-report"}


@pytest.mark.parametrize("entry", ["start", "advance"])
async def test_optional_observation_error_matches_later_read(responses, entry):
    receipt = "accepted-job-receipt"
    accepted = JobUpdate("pending", receipt)
    journal, backend = Journal(), Provider([accepted, RuntimeError(), RuntimeError()])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        responses.respond(function_call(tool.name, {"pages": 2}, "render-A"))
        async with agent.session() as live:
            await live.submit("Render.")
            req = await next_request(live.interactions(), tool)
            if entry == "start":
                async with jobs.start(live, req, wait_seconds=1) as (_, result):
                    assert result == "unknown"
            else:
                await jobs.prepare(live, req)
        state = SessionState.from_json(journal.state_json)
        if entry == "advance":
            async with agent.session(state=state) as fresh:
                req = await next_request(fresh.interactions(), tool)
                assert await jobs.advance(fresh, req, wait_seconds=1) == "unknown"
        [row] = journal.rows.values()
        assert row.update == accepted and backend.calls == [("start", 2), ("observe", receipt)]
        async with agent.session(state=state) as fresh:
            req = await next_request(fresh.interactions(), tool)
            assert await jobs.advance(fresh, req) == "unknown"
    assert row.update == accepted and row.result is None
    assert backend.calls == [("start", 2), ("observe", receipt), ("observe", receipt)]
    assert len(responses.requests) == 1


@pytest.mark.parametrize(
    ("provider_read", "journal_write", "expected"),
    [
        (asyncio.TimeoutError(), None, "unknown"),
        (ConnectionError(), None, "unknown"),
        (PermissionError(), None, PermissionError),
        (done(), RuntimeError(), ValueError),
        (done(), PermissionError(), PermissionError),
        (done(), asyncio.TimeoutError(), "unknown"),
    ],
)
async def test_optional_read_preserves_failure_boundary(
    responses, provider_read, journal_write, expected
):
    class Store(Journal):
        async def record(self, claim, update):
            if claim.update is not None and journal_write is not None:
                raise journal_write
            await super().record(claim, update)

    accepted = JobUpdate("pending", "secret-task-receipt")
    journal, backend = Store(), Provider([accepted, provider_read])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        state, _ = await prepare_one(agent, jobs, tool, responses)
        async with agent.session(state=state) as fresh:
            req = await next_request(fresh.interactions(), tool)
            advance = jobs.advance(fresh, req, wait_seconds=1)
            if expected == "unknown":
                assert await advance == expected
            else:
                with pytest.raises(expected) as error:
                    await advance
                assert error.value.__context__ is None and error.value.__cause__ is None
    [row] = journal.rows.values()
    assert row.update == accepted and row.result is None
    assert backend.calls == [("start", 2), ("observe", accepted.receipt)]
    assert len(responses.requests) == 1


@pytest.mark.parametrize("phase", ["authorizing", "provider", "recording"])
async def test_optional_read_deadline_preserves_uncertain_journal_outcome(responses, phase):
    joined = asyncio.Event()
    accepted = JobUpdate("pending", "accepted-job-receipt")

    async def block():
        try:
            await asyncio.Future()
        finally:
            joined.set()

    class Store(Journal):
        async def authorize(self, claim, action):
            await super().authorize(claim, action)
            if phase == "authorizing" and action == "observe":
                await block()

        async def record(self, claim, update):
            await super().record(claim, update)
            if phase == "recording" and update.status == "completed":
                await block()

    class Backend(Provider):
        async def observe(self, claim):
            if phase == "provider":
                await block()
            return await super().observe(claim)

    completed = replace(done(), receipt=accepted.receipt)
    journal, backend = Store(), Backend([accepted, completed])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        state, _ = await prepare_one(agent, jobs, tool, responses)
        async with agent.session(state=state) as session:
            req = await next_request(session.interactions(), tool)
            result = await jobs.advance(session, req, wait_seconds=0.05)
            assert result == ("pending" if phase == "provider" else "unknown")
            assert joined.is_set()
    [row] = journal.rows.values()
    assert row.update.status == ("completed" if phase == "recording" else "pending")
    assert row.result is None and len(responses.requests) == 1


@pytest.mark.parametrize("cancel", [False, True], ids=["total-deadline", "caller-cancel"])
async def test_optional_read_is_joined_on_total_timeout_or_cancellation(responses, cancel):
    accepted = JobUpdate("pending", "accepted-job-receipt")
    journal, backend = Journal(), Provider([accepted])
    backend.block = "observe"
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        state, _ = await prepare_one(agent, jobs, tool, responses)
        async with agent.session(state=state) as fresh:
            req = await next_request(fresh.interactions(), tool)
            driving = asyncio.create_task(
                jobs.advance(fresh, req, timeout_seconds=2.0 if cancel else 0.2, wait_seconds=3.0)
            )
            await backend.entered.wait()
            if cancel:
                driving.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await driving
            else:
                assert await driving == "unknown"
    [row] = journal.rows.values()
    assert backend.active == 0 and row.update == accepted
    assert backend.calls == [("start", 2), ("observe", accepted.receipt)]
    assert len(responses.requests) == 1


@pytest.mark.parametrize(
    ("action", "stop"),
    [
        ("start", "caller"),
        ("start", "deadline"),
        ("observe", "observation"),
        ("project", "caller"),
        ("deliver", "caller"),
    ],
)
async def test_cancelled_advance_stops_after_atomic_authorization(responses, action, stop):
    entered, authorized = asyncio.Event(), asyncio.Event()

    class Store(Journal):
        async def authorize(self, claim, operation):
            if operation == action:
                entered.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    # A host store may finish an atomic write before returning.
                    await super().authorize(claim, operation)
                    authorized.set()
            else:
                await super().authorize(claim, operation)

    accepted = JobUpdate("pending", "secret-task-receipt")
    journal = Store()
    backend = Provider([accepted] if action == "observe" else [done()])
    projected = []

    def project(value):
        projected.append(value)
        return Output(report=value["public"])

    jobs, tool = declared(journal, backend, project=project)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        state, _ = await prepare_one(agent, jobs, tool, responses)
        responses.respond(assistant_output("finished"))
        async with agent.session(state=state) as fresh:
            req = await next_request(fresh.interactions(), tool)
            driving = asyncio.create_task(
                jobs.advance(
                    fresh,
                    req,
                    timeout_seconds=0.05 if stop == "deadline" else 2,
                    wait_seconds=0.05 if stop == "observation" else 0,
                )
            )
            await entered.wait()
            if stop == "caller":
                driving.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await driving
            else:
                assert await driving == "unknown"
            assert authorized.is_set()
            assert len(responses.requests) == 1
            assert backend.calls == ([] if action == "start" else [("start", 2)])
            assert len(projected) == (1 if action == "deliver" else 0)
            (row,) = journal.rows.values()
            expected = None if action == "start" else accepted if action == "observe" else done()
            assert row.update == expected
            assert (row.result is not None) == (action == "deliver")


async def test_cancelled_advance_records_an_already_entered_effect(responses):
    entered, recorded = asyncio.Event(), asyncio.Event()

    class Backend(Provider):
        async def start(self, claim, arguments):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                return await super().start(claim, arguments)

    class Store(Journal):
        async def record(self, claim, update):
            await super().record(claim, update)
            recorded.set()

    journal, backend = Store(), Backend([done()])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        state, _ = await prepare_one(agent, jobs, tool, responses)
        responses.respond(assistant_output("finished"))
        async with agent.session(state=state) as fresh:
            req = await next_request(fresh.interactions(), tool)
            driving = asyncio.create_task(jobs.advance(fresh, req))
            await entered.wait()
            driving.cancel()
            with pytest.raises(asyncio.CancelledError):
                await driving
            assert recorded.is_set()
            assert "project" not in journal.log and "deliver" not in journal.log
            assert len(responses.requests) == 1
    (row,) = journal.rows.values()
    assert row.update == done() and row.result is None
    assert backend.calls == [("start", 2)]


@pytest.mark.parametrize(
    ("first_stop", "cancel_during_cleanup", "cleanup_error"),
    [
        ("caller", True, False),
        ("deadline", True, True),
        ("deadline", False, True),
        ("observation", True, False),
    ],
)
async def test_advance_joins_host_cleanup(
    responses, first_stop, cancel_during_cleanup, cleanup_error
):
    observing = first_stop == "observation"
    operation = "observe" if observing else "respond"
    entered, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
    resource = asyncio.Lock()

    class Store(Journal):
        async def authorize(self, claim, action):
            await super().authorize(claim, action)
            if action == operation:
                async with resource:
                    entered.set()
                    try:
                        await asyncio.Future()
                    finally:
                        cleaning.set()
                        await release.wait()
                        cleaned.set()
                        if cleanup_error:
                            raise PermissionError("private cleanup error")

    accepted = JobUpdate("pending" if observing else "input_required", "accepted-job-receipt")
    journal, backend = Store(), Provider([accepted])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        state, _ = await prepare_one(agent, jobs, tool, responses)
        async with agent.session(state=state) as session:
            request = await next_request(session.interactions(), tool)
            if not observing:
                assert await jobs.advance(session, request) == "input_required"
            driver = asyncio.create_task(
                jobs.advance(
                    session,
                    request,
                    operation="advance" if observing else "respond",
                    wait_seconds=0.1 if observing else 0,
                    timeout_seconds=0.2 if first_stop == "deadline" else 5,
                )
            )
            try:
                await entered.wait()
                if first_stop == "caller":
                    driver.cancel()
                await cleaning.wait()
                if cancel_during_cleanup:
                    driver.cancel()
                finished, _ = await asyncio.wait((driver,), timeout=0.05)
                assert not finished and resource.locked()
            finally:
                release.set()
                (outcome,) = await asyncio.gather(driver, return_exceptions=True)
            assert cleaned.is_set() and not resource.locked()
            if first_stop in ("caller", "observation"):
                assert isinstance(outcome, asyncio.CancelledError)
            else:
                assert outcome == "unknown"
    (row,) = journal.rows.values()
    assert row.update == accepted and row.result is None
    assert backend.calls == [("start", 2)] and len(responses.requests) == 1


async def test_completed_core_hosted_mcp_sibling_is_preserved(responses):
    from agents import HttpMCP
    from agents.observation import ToolFinished
    from tests.test_native_mcp_tools import Peer, mcp_call

    journal, backend = Journal(), Provider([done()])
    jobs, tool = declared(journal, backend)
    peer = Peer(label="known-native-sibling")
    responses.respond(
        mcp_call("search", "mcp__records", "native-sibling"),
        function_call("render", {"pages": 3}, "manual"),
    )
    responses.respond(assistant_output("both accepted"))
    async with timeout(15), peer.serve() as url:
        agent = Agent(
            instructions="Use the two tools.",
            provider=provider(responses),
            tools=[tool, HttpMCP(name="records", url=url, tools=["search"])],
        )
        async with agent.session() as session:
            await session.submit("Start.")
            req = await next_request(session.interactions(), tool)
            async for event in session.events():
                if isinstance(event, ToolFinished) and event.call_id == "native-sibling":
                    break
            async with jobs.start(session, req) as (restored, result):
                assert (
                    isinstance(result, Turn)
                    and (await result.get_result()).final_output == "both accepted"
                )
                history = [item async for item in restored.items()]
    assert len(peer.calls) == 1 and backend.calls == [("start", 3)]
    assert len(responses.requests) == 2
    assert "known-native-sibling" in tool_output(responses.requests[1], "native-sibling")
    assert {item["call_id"] for item in history if item["type"] == "function_call_output"} == {
        "native-sibling",
        "manual",
    }


async def test_active_native_child_cannot_prepare_a_job(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestServer

    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import trust
    from tests.test_native_child_manual import ManualChildren, declaration_for
    from tests.test_native_executor_tools import declaration

    class ChildInput(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        key: str

    journal, backend = Journal(), Provider([])
    jobs = DeferredTools(journal=journal, tenant="tenant-a", run_id="run")
    tool = FunctionTool(
        name="lookup",
        description="Read a record.",
        input_type=ChildInput,
        output_type=Output,
    )
    jobs.register(
        tool,
        provider_id="lookup/v1",
        provider=backend,
        project=lambda value: Output(report="unused"),
    )
    peer = ManualChildren()
    async with (
        timeout(20),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with declaration_for(peer, executor, tool).session(
            environment=declaration(executor)
        ) as session:
            await session.submit("Start both children")
            req = await next_request(session.interactions(), tool)
            assert req.subagent_id is not None
            with pytest.raises(ValueError, match="root manual grant"):
                await jobs.prepare(session, req)
    assert not backend.calls and not journal.rows


async def test_partial_sibling_delivery_reuses_ready_result_after_restart(responses):
    journal = Journal()
    backend = Provider([done("first"), JobUpdate("pending", "secret-task-receipt"), done("second")])
    jobs, tool = declared(journal, backend)
    responses.respond(
        function_call("render", {"pages": 1}, "one"), function_call("render", {"pages": 2}, "two")
    )
    responses.respond(assistant_output("both results"))
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        async with agent.session() as original:
            await original.submit("Render twice.")
            anchor = await next_request(original.interactions(), tool)
            state = await jobs.prepare(original, anchor)
        for restarted in [False, True]:
            async with agent.session(state=SessionState.from_json(state.to_json())) as session:
                stream = session.interactions()
                grants = {
                    req.call_id: req
                    for req in [await next_request(stream, tool), await next_request(stream, tool)]
                }
                first = await jobs.advance(session, grants["one"])
                assert isinstance(first, Turn)
                second = await jobs.advance(session, grants["two"])
                if restarted:
                    assert second is first
                    assert (await first.get_result()).final_output == "both results"
                else:
                    assert second == "pending" and len(responses.requests) == 1
    assert backend.calls == [("start", 1), ("start", 2), ("observe", "secret-task-receipt")]
    assert len(responses.requests) == 2
    assert json.loads(tool_output(responses.requests[1], "one")) == {"report": "first"}
    assert json.loads(tool_output(responses.requests[1], "two")) == {"report": "second"}


async def test_retired_session_cannot_start_after_awaiting_authorization(responses):
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingJournal(Journal):
        async def authorize(self, claim, operation):
            await super().authorize(claim, operation)
            if operation == "start":
                entered.set()
                await release.wait()

    journal, backend = WaitingJournal(), Provider([])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        state, _ = await prepare_one(agent, jobs, tool, responses)
        async with agent.session(state=state) as session:
            req = await next_request(session.interactions(), tool)
            driver = asyncio.create_task(jobs.advance(session, req))
            await entered.wait()
            await session.suspend()
            release.set()
            with pytest.raises(ValueError, match="could not be completed"):
                await driver
    assert journal.log == ["prepare", "claim-start", "start"]
    assert not backend.calls and len(responses.requests) == 1


@pytest.mark.parametrize(
    "change", ["remove_mcp", "replace_mcp", "replace_callback", "same_tools", "instructions"]
)
async def test_start_refuses_changed_opening_configuration_before_sealing(responses, change):
    from dataclasses import replace

    from agents import HttpMCP, Tool
    from tests.test_native_mcp_tools import Peer

    journal, backend = Journal(), Provider([done()])
    jobs, tool = declared(journal, backend)
    callbacks = []

    @function_tool(name="policy")
    def opening_callback() -> str:
        callbacks.append("opening")
        return "opening-policy"

    @function_tool(name="policy")
    def current_callback() -> str:
        callbacks.append("current")
        return "current-policy"

    peer = Peer()
    responses.respond(function_call("render", {"pages": 2}, "updated-tools"))
    responses.respond(function_call("policy", {}, "current-policy"))
    responses.respond(assistant_output("finished"))
    async with timeout(20), peer.serve() as url:
        descriptor = HttpMCP(
            name="records", url=url, tools=["search"], bearer_token="opening-credential"
        )
        tools: list[Tool[None]] = [tool, opening_callback, descriptor]
        agent = Agent(
            instructions="Use the current tools.", tools=tools, provider=provider(responses)
        )
        async with agent.session() as session:
            if change == "instructions":
                await session.update(instructions="Use the updated instructions.")
            elif change == "replace_mcp":
                with pytest.raises(ValueError, match="captured"):
                    await session.submit(
                        "foreign",
                        tools=[
                            tool,
                            opening_callback,
                            replace(descriptor, bearer_token="current-credential"),
                        ],
                    )
            elif change == "replace_callback":
                with pytest.raises(ValueError, match="captured"):
                    await session.submit("foreign", tools=[tool, current_callback, descriptor])
            owner = await session.submit(
                "Render.", tools=[tool, opening_callback] if change == "remove_mcp" else None
            )
            request = await next_request(session.interactions(), tool)
            before = len(peer.requests)
            if change == "instructions":
                with pytest.raises(ValueError, match="opening tools and instructions"):
                    async with jobs.start(session, request):
                        pytest.fail("The helper reopened stale instructions")
                assert len(peer.requests) == before
                assert not journal.rows and not journal.state_json and not backend.calls
            assert tool.match(request) is not None
            await request.respond(Output(report="host-approved"))
            assert (await owner.get_result()).final_output == "finished"
            assert callbacks == ["opening"]
            assert not peer.calls
