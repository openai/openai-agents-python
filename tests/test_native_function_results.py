"""Callback return validation uses native admission and the existing result wire."""

from __future__ import annotations

import asyncio
import json
import traceback
import warnings
from collections.abc import Awaitable, Coroutine
from dataclasses import dataclass, field
from typing import Annotated, Any, cast

import pytest
from pydantic import Field, PlainSerializer, TypeAdapter, ValidationError

from agents import Agent, NativeProvider, ToolContext, function_tool
from agents.observation import ToolFinished
from agents.tools import ToolOutput
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)


@dataclass
class Account:
    charges: list[int] = field(default_factory=list)


def _assert_sdk_failure_privacy(error: BaseException, *private: str) -> None:
    assert not isinstance(error, ValidationError)
    assert error.__cause__ is None
    assert error.__context__ is None
    display = str(error) + "".join(traceback.format_exception(error))
    sdk_locals = [
        (frame.f_code.co_name, frame.f_locals)
        for frame, _ in traceback.walk_tb(error.__traceback__)
        if frame.f_globals.get("__name__", "").startswith("agents.")
    ]
    assert sdk_locals
    for marker in private:
        assert marker not in display
        leaked_names = []
        for method, local_vars in sdk_locals:
            for name, value in local_vars.items():
                if marker in repr(value):
                    leaked_names.append((method, name))
                # Inspect only handles directly retained by SDK frames, without
                # entering closures, caller-owned data, or application frames.
                if isinstance(value, asyncio.Future) and value.done() and not value.cancelled():
                    outcome = value.exception()
                    if outcome is None:
                        outcome = value.result()
                    if marker in repr(outcome):
                        leaked_names.append((method, name, "outcome"))
        assert leaked_names == [], (marker, leaked_names)


@pytest.mark.parametrize("result", ["text", {"count": 3}])
async def test_callback_without_return_annotation_uses_released_conversion(responses, result):
    @function_tool
    def lookup(key: str):
        assert key == "record"
        return result

    responses.respond(function_call("lookup", {"key": "record"}, "lookup-1"))
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Lookup.",
        tools=[lookup],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    assert (await agent.run("lookup")).final_output == "done"
    wire = tool_output(responses.requests[1], "lookup-1")
    assert wire == result if isinstance(result, str) else json.loads(wire) == result


async def test_unannotated_callback_still_validates_tool_output_envelope(responses):
    calls = []

    @function_tool
    async def lookup(key: str):
        calls.append(key)
        return ToolOutput(content_items=[{"type": "unsupported", "text": "synthetic"}])

    responses.respond(function_call("lookup", {"key": "record"}, "lookup-invalid"))
    agent = Agent(
        instructions="Lookup.",
        tools=[lookup],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    with pytest.raises((TypeError, ValueError)):
        await agent.run("lookup")
    assert calls == ["record"]
    assert len(responses.requests) == 1


async def test_sync_awaitable_validates_resolved_constraint_before_delivery(responses):
    calls = []

    async def debit(units: int) -> int:
        calls.append(units)
        return -units

    @function_tool
    def charge(units: int) -> Awaitable[Annotated[int, Field(ge=0)]]:
        return debit(units)

    responses.respond(function_call("charge", {"units": 7}, "charge-1"))
    agent = Agent(
        instructions="Charge.",
        tools=[charge],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    with pytest.raises(ValueError, match="Tool result failed validation"):
        await agent.run("Charge")
    assert calls == [7]
    assert len(responses.requests) == 1


async def test_sync_coroutine_validates_and_serializes_resolved_type(responses):
    calls = []

    async def lookup(key: str) -> dict[str, int]:
        calls.append(key)
        return {"count": 3}

    @function_tool
    def wrapped(key: str) -> Coroutine[Any, Any, dict[str, Annotated[int, Field(ge=0)]]]:
        return lookup(key)

    responses.respond(function_call("wrapped", {"key": "synthetic"}, "wrapped-1"))
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Lookup.",
        tools=[wrapped],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    assert (await agent.run("Lookup")).final_output == "done"
    assert calls == ["synthetic"]
    assert json.loads(tool_output(responses.requests[1], "wrapped-1")) == {"count": 3}


async def test_sync_awaitable_keeps_envelope_validation(responses):
    calls = []

    async def lookup(key: str) -> ToolOutput:
        calls.append(key)
        return ToolOutput(content_items=[{"type": "unsupported", "text": "synthetic"}])

    @function_tool
    def wrapped(key: str) -> Awaitable[ToolOutput]:
        return lookup(key)

    responses.respond(function_call("wrapped", {"key": "record"}, "wrapped-invalid"))
    agent = Agent(
        instructions="Lookup.",
        tools=[wrapped],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    with pytest.raises((TypeError, ValueError)):
        await agent.run("Lookup")
    assert calls == ["record"]
    assert len(responses.requests) == 1


def test_async_returning_another_awaitable_is_not_a_resolved_result() -> None:
    async def inner() -> str:
        return "unused"

    async def outer() -> Awaitable[str]:
        return inner()

    with pytest.raises(TypeError, match=r"Tool 'outer'.*return.*Any"):
        function_tool(outer)


def test_sync_awaitable_does_not_waive_required_input_annotation() -> None:
    async def lookup(key: str) -> str:
        return key

    def wrapped(key) -> Awaitable[str]:
        return lookup(key)

    with pytest.raises(TypeError, match=r"parameter 'key'.*annotation"):
        function_tool(wrapped)


class Opaque:
    pass


def test_unsupported_return_annotation_fails_before_opening() -> None:
    def load() -> Opaque:
        return Opaque()

    with pytest.raises(TypeError, match=r"Tool 'load'.*return.*Any"):
        function_tool(load)


async def test_invalid_async_contextual_return_never_delivered_after_one_charge(responses):
    account = Account()

    @function_tool
    async def debit(
        ctx: Annotated[ToolContext[Account], "application context"], units: int
    ) -> Annotated[int, Field(ge=0)]:
        ctx.context.charges.append(units)
        return -units

    responses.respond(function_call("debit", {"units": 7}, "debit-1"))
    responses.respond(assistant_output("should not continue"))
    agent = Agent(
        instructions="Debit exactly once.",
        tools=[debit],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    async with agent.session(context=account) as session:
        turn = await session.submit("debit")
        with pytest.raises(ValueError, match="Tool result failed validation"):
            await turn.get_result()
        assert account.charges == [7]
        assert len(responses.requests) == 1


@pytest.mark.parametrize("mode", ["async", "sync-with-deadline"])
async def test_invalid_callback_result_does_not_expose_rejected_input_or_validation_chain(
    responses, mode
):
    account = Account()
    rejected = "synthetic-private-47"
    original = "synthetic-original-account-47"

    def debit_sync(ctx: ToolContext[Account], units: int, key: str) -> dict[str, int]:
        assert key == original
        ctx.context.charges.append(units)
        return cast(Any, {"balance": rejected})

    async def debit_async(ctx: ToolContext[Account], units: int, key: str) -> dict[str, int]:
        return debit_sync(ctx, units, key)

    debit = function_tool(
        name="debit",
        callback_timeout=10 if mode == "sync-with-deadline" else None,
    )(debit_sync if mode == "sync-with-deadline" else debit_async)

    responses.respond(function_call("debit", {"units": 7, "key": original}, "private-debit"))
    responses.respond(assistant_output("should not continue"))
    agent = Agent(
        instructions="Debit exactly once.",
        tools=[debit],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    async with agent.session(context=account) as session:
        observations = session.events()
        turn = await session.submit("debit")
        with pytest.raises(ValueError) as caught:
            await turn.get_result()
        assert account.charges == [7]
        assert len(responses.requests) == 1
        _assert_sdk_failure_privacy(caught.value, rejected, original)
        items = [item async for item in session.items()]
        # Native abort history can contain a cancellation output, never the rejected result.
        assert rejected not in json.dumps(items)
    assert account.charges == [7]
    assert len(responses.requests) == 1
    finished = [
        event
        async for event in observations
        if isinstance(event, ToolFinished) and event.call_id == "private-debit"
    ]
    assert len(finished) == 1 and finished[0].status == "failed"


@pytest.mark.parametrize("mutation", ["replace", "append"])
async def test_rejected_mutable_argument_result_releases_sdk_aliases(responses, mutation):
    account = Account()
    rejected = "synthetic-private-balance"
    original = "synthetic-original-enrich"
    app_records: list[dict[str, Any]] = []

    @function_tool
    async def enrich(ctx: ToolContext[Account], record: dict[str, Any]) -> dict[str, int]:
        ctx.context.charges.append(7)
        if mutation == "append":
            record["balance"].append(rejected)
        else:
            record["balance"] = rejected
        app_records.append(record)
        return record

    record: dict[str, Any] = {"source": original}
    if mutation == "append":
        record["balance"] = []
    expected = {"source": original, "balance": [rejected] if mutation == "append" else rejected}
    responses.respond(function_call("enrich", {"record": record}, "enrich-alias"))
    responses.respond(assistant_output("should not continue"))
    agent = Agent(
        instructions="Enrich exactly once.",
        tools=[enrich],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    async with agent.session(context=account) as session:
        observations = session.events()
        turn = await session.submit("enrich")
        with pytest.raises(ValueError, match="Tool result failed validation") as caught:
            await turn.get_result()
        assert account.charges == [7]
        assert len(responses.requests) == 1
        assert app_records == [expected]
        _assert_sdk_failure_privacy(caught.value, rejected, original)
        assert rejected not in json.dumps([item async for item in session.items()])
    assert account.charges == [7]
    assert app_records == [expected]
    assert len(responses.requests) == 1
    finished = [
        event
        async for event in observations
        if isinstance(event, ToolFinished) and event.call_id == "enrich-alias"
    ]
    assert len(finished) == 1 and finished[0].status == "failed"


@pytest.mark.parametrize("failure_site", ["callback", "after"])
async def test_callback_body_validation_error_remains_a_callback_failure(responses, failure_site):
    calls: list[str] = []
    errors: list[ValidationError] = []

    def after(ctx: ToolContext[None], output: ToolOutput) -> ToolOutput:
        try:
            return ToolOutput.text(
                str(TypeAdapter(int).validate_python("synthetic-body-not-an-int"))
            )
        except ValidationError as error:
            errors.append(error)
            raise

    @function_tool(after=after if failure_site == "after" else None)
    async def lookup(key: str) -> int:
        calls.append(key)
        if failure_site == "after":
            return 1
        try:
            return TypeAdapter(int).validate_python("synthetic-body-not-an-int")
        except ValidationError as error:
            errors.append(error)
            raise

    responses.respond(function_call("lookup", {"key": "one"}, "lookup-body"))
    responses.respond(assistant_output("should not continue"))
    agent = Agent(
        instructions="Lookup once.",
        tools=[lookup],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    async with agent.session() as session:
        with pytest.raises(ValidationError, match="synthetic-body-not-an-int") as caught:
            await (await session.submit("lookup")).get_result()
        assert len(errors) == 1
        assert caught.value is errors[0]
    assert calls == ["one"]
    assert len(responses.requests) == 1


async def test_valid_typed_sync_return_round_trip_and_bare_namespace(responses):
    calls: list[str] = []

    @function_tool
    def balance(key: str) -> dict[str, int]:
        calls.append(key)
        return {"available": 17}

    responses.respond(function_call("balance", {"key": "record"}, "balance-1"))
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Report the balance.",
        tools=[balance],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    async with agent.session() as session:
        result = await (await session.submit("balance")).get_result()
        assert result.final_output == "done"
        items = [item async for item in session.items()]
        [call] = [
            item
            for item in items
            if item["type"] == "function_call" and item["call_id"] == "balance-1"
        ]
        assert call.get("namespace") is None
    assert calls == ["record"]
    assert json.loads(tool_output(responses.requests[1], "balance-1")) == {"available": 17}


@pytest.mark.parametrize("kind", ["integer", "string"])
async def test_declared_callback_serializer_reaches_provider_once(responses, kind):
    calls: list[int] = []

    def int_result(
        units: int,
    ) -> Annotated[int, PlainSerializer(lambda v: f"units:{v}", return_type=str, when_used="json")]:
        calls.append(units)
        return units

    def str_result(
        units: int,
    ) -> Annotated[str, PlainSerializer(lambda v: f"units:{v}", return_type=str, when_used="json")]:
        calls.append(units)
        return str(units)

    tool = function_tool(name="report")(int_result if kind == "integer" else str_result)
    responses.respond(function_call("report", {"units": 7}, "serialized-1"))
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Report once.",
        tools=[tool],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    result = await agent.run("report")
    assert result.final_output == "done"
    assert calls == [7]
    assert len(responses.requests) == 2
    text = tool_output(responses.requests[1], "serialized-1")
    # Non-string values keep JSON wire semantics; declared strings stay raw text.
    if kind == "integer":
        assert json.loads(text) == "units:7"
    else:
        assert text == "units:7"


def _reject_private_serialization(record: dict[str, Any]) -> str:
    record["balance"].append("synthetic-private-serializer")
    raise ValueError("synthetic-private-serializer")


async def test_serializer_failure_releases_sdk_aliases_and_preserves_one_effect(responses):
    account = Account()
    app_records: list[dict[str, Any]] = []
    original = "synthetic-original-serializer"

    @function_tool
    async def report(
        ctx: ToolContext[Account], record: dict[str, Any]
    ) -> Annotated[dict[str, Any], PlainSerializer(_reject_private_serialization, return_type=str)]:
        ctx.context.charges.append(7)
        app_records.append(record)
        return record

    responses.respond(
        function_call("report", {"record": {"balance": [], "source": original}}, "serializer-fail")
    )
    responses.respond(assistant_output("should not continue"))
    agent = Agent(
        instructions="Report once.",
        tools=[report],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    async with agent.session(context=account) as session:
        observations = session.events()
        turn = await session.submit("report")
        with pytest.raises(ValueError, match="Tool result failed validation") as caught:
            await turn.get_result()
        private = "synthetic-private-serializer"
        assert account.charges == [7]
        assert app_records == [{"balance": [private], "source": original}]
        assert len(responses.requests) == 1
        _assert_sdk_failure_privacy(caught.value, private, original)
        assert private not in json.dumps([item async for item in session.items()])
    assert account.charges == [7]
    assert app_records == [{"balance": [private], "source": original}]
    assert len(responses.requests) == 1
    finished = [
        event
        async for event in observations
        if isinstance(event, ToolFinished) and event.call_id == "serializer-fail"
    ]
    assert len(finished) == 1 and finished[0].status == "failed"


async def test_invalid_declared_serializer_output_never_warns_or_reaches_provider(responses):
    account = Account()
    original = "synthetic-original-warning-case"
    private = "synthetic-private-serializer-output"

    @function_tool
    async def report(
        ctx: ToolContext[Account], units: int, key: str
    ) -> Annotated[
        int, PlainSerializer(lambda value: "synthetic-private-serializer-output", return_type=int)
    ]:
        assert key == original
        ctx.context.charges.append(units)
        return units

    responses.respond(function_call("report", {"units": 7, "key": original}, "warning-result"))
    responses.respond(assistant_output("should not continue"))
    agent = Agent(
        instructions="Report once.",
        tools=[report],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    async with agent.session(context=account) as session:
        observations = session.events()
        turn = await session.submit("report")
        error: ValueError | None = None
        with warnings.catch_warnings(record=True) as emitted:
            warnings.simplefilter("always")
            try:
                await turn.get_result()
            except ValueError as caught:
                error = caught
        assert account.charges == [7]
        actual = {
            "private_warning": any(private in str(item.message) for item in emitted),
            "provider_continued": len(responses.requests) != 1,
            "private_provider_output": private in json.dumps(responses.requests),
            "generic_error": error is not None and str(error) == "Tool result failed validation",
        }
        assert actual == {
            "private_warning": False,
            "provider_continued": False,
            "private_provider_output": False,
            "generic_error": True,
        }
        assert error is not None
        _assert_sdk_failure_privacy(error, original, private)
        assert private not in json.dumps([item async for item in session.items()])
    assert account.charges == [7]
    assert len(responses.requests) == 1
    finished = [
        event
        async for event in observations
        if isinstance(event, ToolFinished) and event.call_id == "warning-result"
    ]
    assert len(finished) == 1 and finished[0].status == "failed"


async def test_explicit_any_returns_existing_tool_output_envelope(responses):
    @function_tool
    def image() -> Any:
        return ToolOutput.error("synthetic-image-result")

    responses.respond(function_call("image", {}, "image-1"))
    responses.respond(assistant_output("failure was handled"))
    agent = Agent(
        instructions="Get the image.",
        tools=[image],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    assert (await agent.run("image")).final_output == "failure was handled"
    assert "synthetic-image-result" in tool_output(responses.requests[1], "image-1")


async def test_declared_tool_output_uses_existing_response_validation(responses):
    @function_tool
    async def report() -> ToolOutput:
        return ToolOutput.text("result")

    responses.respond(function_call("report", {}, "report-1"))
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Use report.",
        tools=[report],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    assert (await agent.run("report")).final_output == "done"
    assert "result" in tool_output(responses.requests[1], "report-1")


@pytest.mark.parametrize(
    ("failure_site", "return_shape"),
    [("callback", "direct"), ("after", "direct"), ("after", "str"), ("after", "structured")],
)
async def test_malformed_tool_output_does_not_expose_envelope_in_failure(
    responses, failure_site, return_shape
):
    account = Account()
    private = "synthetic-private-envelope-914e"
    original = "synthetic-original-envelope-914e"
    hooks: list[str] = []

    async def after(ctx: ToolContext[Account], output: ToolOutput) -> ToolOutput:
        hooks.append(ctx.call_id)
        # Mutating the valid callback result also tests SDK-retained aliases.
        received = output.content_items[0].text
        output.content_items[0] = {
            "type": "inputText",
            "text": {"service_error": private, "original": received},
        }
        return output

    @function_tool(after=after if failure_site == "after" else None)
    async def debit(
        ctx: ToolContext[Account], units: int, key: str
    ) -> ToolOutput | str | dict[str, str]:
        assert key == original
        ctx.context.charges.append(units)
        if failure_site == "after":
            if return_shape == "str":
                return key
            if return_shape == "structured":
                return {"key": key}
            return ToolOutput.text(key)
        return ToolOutput([{"type": "inputText", "text": {"service_error": private}}])

    responses.respond(function_call("debit", {"units": 7, "key": original}, "envelope-debit"))
    responses.respond(assistant_output("should not continue"))
    agent = Agent(
        instructions="Debit exactly once.",
        tools=[debit],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    async with agent.session(context=account) as session:
        observations = session.events()
        turn = await session.submit("debit")
        with pytest.raises(ValueError) as caught:
            await turn.get_result()
        assert account.charges == [7]
        assert len(responses.requests) == 1
        assert private not in json.dumps(responses.requests)
        error = caught.value
        metadata = error.errors() if isinstance(error, ValidationError) else vars(error)
        assert {
            "message": str(error),
            "private_metadata": private in repr(metadata),
        } == {
            "message": "Tool result failed validation",
            "private_metadata": False,
        }
        _assert_sdk_failure_privacy(error, private, original)
        assert private not in json.dumps([item async for item in session.items()])
    assert account.charges == [7]
    assert hooks == (["envelope-debit"] if failure_site == "after" else [])
    assert len(responses.requests) == 1
    finished = [
        event
        async for event in observations
        if isinstance(event, ToolFinished) and event.call_id == "envelope-debit"
    ]
    assert len(finished) == 1 and finished[0].status == "failed"
    assert private not in repr(finished)


async def test_callback_types_and_context_remain_frozen_after_declaration(responses):
    async def debit(ctx: ToolContext[Account], units: int) -> Annotated[int, Field(ge=0)]:
        ctx.context.charges.append(units)
        return -units

    tool = function_tool(debit)
    debit.__annotations__["return"] = Any
    account = Account()
    responses.respond(function_call("debit", {"units": 3}, "debit-frozen"))
    responses.respond(assistant_output("should not continue"))
    agent = Agent(
        instructions="Debit exactly once.",
        tools=[tool],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    async with agent.session(context=account) as session:
        with pytest.raises(ValueError, match="Tool result failed validation"):
            await (await session.submit("debit")).get_result()
    assert account.charges == [3]
    assert len(responses.requests) == 1


async def test_optional_annotated_context_can_open_without_application_context(responses):
    @function_tool
    async def balance(
        ctx: ToolContext[Annotated[Account | None, "optional application context"]],
    ) -> str:
        assert ctx.context is None
        return "no account"

    responses.respond(function_call("balance", {}, "optional-1"))
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Check balance.",
        tools=[balance],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    async with agent.session() as session:
        assert (await (await session.submit("balance")).get_result()).final_output == "done"
    assert "no account" in tool_output(responses.requests[1], "optional-1")
