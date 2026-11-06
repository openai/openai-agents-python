"""The migration surface drives the real native loop with synthetic models."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from threading import Event as ThreadEvent
from typing import Any

import pytest
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseReasoningItem,
)
from pydantic import BaseModel

from agents import NativeError
from agents.classic import Agent, Model, ModelProvider, RunContextWrapper, function_tool
from agents.classic.exceptions import MaxTurnsExceeded, ModelBehaviorError, UserError
from agents.classic.items import ModelResponse
from agents.classic.model_settings import ModelSettings
from agents.classic.models.interface import ModelTracing
from agents.classic.tool_context import ToolContext as ClassicToolContext
from agents.classic.usage import Usage
from agents.migration import SDKRunner, SDKRunResult
from agents.migration._transport import _ResponseCarrier


@pytest.fixture(autouse=True)
def local_transport_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic native tests never send their loopback traffic through a proxy."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")


@pytest.mark.parametrize(
    ("environment", "platform", "os_name", "allowed"),
    [
        ({}, "linux", "posix", True),
        ({}, "darwin", "posix", False),
        ({}, "win32", "nt", False),
        ({"NO_PROXY": "127.0.0.1"}, "darwin", "posix", True),
        ({"no_proxy": "127.0.0.1"}, "win32", "nt", True),
        ({"HTTP_PROXY": "synthetic", "NO_PROXY": "*"}, "linux", "posix", False),
        ({"HTTP_PROXY": "synthetic", "NO_PROXY": "127.0.0.0/8"}, "linux", "posix", False),
        ({"HTTP_PROXY": "synthetic", "NO_PROXY": ".0.0.1"}, "linux", "posix", False),
    ],
)
def test_loopback_route_policy(
    environment: dict[str, str],
    platform: str,
    os_name: str,
    allowed: bool,
) -> None:
    from agents.migration._runner import _check_loopback_route

    if allowed:
        _check_loopback_route(environment, platform, os_name)
    else:
        with pytest.raises(UserError, match="NO_PROXY/no_proxy") as raised:
            _check_loopback_route(environment, platform, os_name)
        assert "synthetic" not in str(raised.value)


@pytest.mark.parametrize("bypass", ["missing", "valid", "lower_only", "conflicting"])
async def test_fresh_process_private_transport_never_reaches_proxy(bypass: str) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    hits: list[str] = []

    async def record(request: web.Request) -> web.Response:
        hits.append(request.path)
        return web.Response(status=502)

    script = """
import asyncio, json
from agents.classic import Agent, Model, ModelProvider
from agents.classic.exceptions import UserError
from agents.classic.items import ModelResponse
from agents.classic.usage import Usage
from openai.types.responses import ResponseOutputMessage
from agents.migration import SDKRunner
events = []
class Selected(Model):
    async def get_response(self, *args, **kwargs):
        events.append("model")
        item = ResponseOutputMessage.model_validate({
            "id":"msg_proxy", "type":"message", "role":"assistant", "status":"completed",
            "content":[{"type":"output_text", "text":"local result", "annotations":[]}]})
        return ModelResponse([item], Usage(), None)
    async def stream_response(self, *args, **kwargs):
        raise AssertionError("not streaming")
        yield
class Provider(ModelProvider):
    def get_model(self, name):
        events.append("provider")
        return Selected()
async def main():
    try:
        result = await SDKRunner.run(
            Agent("Proxy", model="selected"), "synthetic input", model_provider=Provider())
    except UserError as error:
        print(json.dumps({"error":str(error), "events":events}))
    else:
        print(json.dumps({"output":result.final_output, "events":events}))
asyncio.run(main())
"""
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", record)
    async with TestServer(app) as proxy:
        environment = dict(os.environ)
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "NO_PROXY",
            "no_proxy",
        ):
            environment.pop(name, None)
        environment["HTTP_PROXY"] = str(proxy.make_url("/"))
        environment["HTTPS_PROXY"] = str(proxy.make_url("/"))
        environment["ALL_PROXY"] = str(proxy.make_url("/"))
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        if bypass in {"valid", "conflicting"}:
            environment["NO_PROXY"] = "127.0.0.1"
        if bypass == "lower_only":
            environment["no_proxy"] = "127.0.0.1"
        if bypass == "conflicting":
            environment["no_proxy"] = "example.invalid"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode == 0, stderr.decode()
    result = json.loads(stdout)
    if bypass in {"valid", "lower_only"}:
        assert result == {"output": "local result", "events": ["provider", "model"]}
    else:
        assert "NO_PROXY/no_proxy" in result["error"]
        assert result["events"] == []
    assert hits == []


def message(text: str, *, status: str = "completed") -> ResponseOutputMessage:
    return ResponseOutputMessage.model_validate(
        {
            "id": "msg_answer",
            "type": "message",
            "role": "assistant",
            "status": status,
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
    )


def call(
    name: str, *, call_id: str = "call_1", arguments: str = '{"value":"Ada"}'
) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        id=f"item_{call_id}",
        type="function_call",
        name=name,
        call_id=call_id,
        arguments=arguments,
        status="completed",
    )


class ScriptedModel(Model):
    def __init__(self, *outputs: list[Any]) -> None:
        self.outputs = list(outputs)
        self.requests: list[dict[str, Any]] = []

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.requests.append(deepcopy(kwargs))
        return ModelResponse(
            output=self.outputs.pop(0),
            response_id=f"response_{len(self.requests)}",
            usage=Usage(requests=1, input_tokens=7, output_tokens=3, total_tokens=10),
        )

    async def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        raise AssertionError("SDKRunner must use get_response")
        yield


async def test_public_runner_uses_native_tool_loop_and_original_classic_schema() -> None:
    effects: list[tuple[str, str]] = []
    contexts: list[ClassicToolContext[Any]] = []
    context = {"greeting": "Hello"}

    @function_tool
    async def greet(ctx: RunContextWrapper[dict[str, str]], value: str) -> str:
        assert isinstance(ctx, ClassicToolContext)
        contexts.append(ctx)
        effects.append((ctx.context["greeting"], value))
        return f"{ctx.context['greeting']} {value}"

    original_call = call("greet", arguments=' { "value" : "Ada" } ')
    model = ScriptedModel([original_call], [message("Finished")])
    settings = ModelSettings(temperature=0.2, verbosity="low", store=False)
    agent = Agent(
        "Original",
        instructions="Only original instructions",
        tools=[greet],
        model=model,
        model_settings=settings,
    )
    result = await SDKRunner.run(agent, "Greet Ada", context=context)
    assert isinstance(result, SDKRunResult)
    assert result.final_output == result.native_result.final_output == "Finished"
    assert result.last_agent is agent
    assert result.native_result.turn.session_id
    assert effects == [("Hello", "Ada")]
    assert len(model.requests) == 2
    first, second = model.requests
    assert first["system_instructions"] == "Only original instructions"
    assert first["tools"][0].params_json_schema == greet.params_json_schema
    assert first["tools"][0].name == "greet"
    assert first["model_settings"] == settings
    assert first["tracing"] is ModelTracing.DISABLED
    assert first["previous_response_id"] is None
    assert first["conversation_id"] is None
    assert contexts[0].tool_call == original_call
    assert contexts[0].tool_arguments == original_call.arguments
    assert contexts[0].tool_call_id == original_call.call_id
    assert contexts[0].turn_input == first["input"]
    assert contexts[0].agent is agent
    assert not any(item.get("role") in {"system", "developer"} for item in first["input"])
    assert any(item.get("role") == "user" and "Greet Ada" in str(item) for item in first["input"])
    receipt = next(item for item in second["input"] if item["type"] == "function_call_output")
    assert receipt["call_id"] == "call_1"
    assert "Hello Ada" in str(receipt["output"])


async def test_named_model_uses_only_explicit_provider() -> None:
    model = ScriptedModel([message("named")])
    names = []

    class Provider(ModelProvider):
        def get_model(self, model_name: str | None) -> Model:
            names.append(model_name)
            return model

    result = await SDKRunner.run(
        Agent("Named", model="custom/model"), "hello", model_provider=Provider()
    )
    assert result.final_output == "named"
    assert names == ["custom/model"]
    assert model.requests[0]["system_instructions"] is None


async def test_existing_openai_adapter_preserves_reasoning_and_native_tool_receipts() -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from openai import AsyncOpenAI

    from agents.classic.models.openai_responses import OpenAIResponsesModel

    requests: list[dict[str, Any]] = []
    effects: list[str] = []
    reasoning = {
        "id": "rs_synthetic",
        "type": "reasoning",
        "summary": [],
        "content": [{"type": "reasoning_text", "text": "synthetic reasoning"}],
        "encrypted_content": "synthetic-encrypted-reasoning",
    }

    @function_tool
    def greet(value: str) -> str:
        effects.append(value)
        return "Hello Ada"

    async def handle(request: web.Request) -> web.Response:
        requests.append(await request.json())
        output = (
            [reasoning, call("greet").model_dump(exclude_none=True)]
            if len(requests) == 1
            else [message("Adapter finished").model_dump()]
        )
        return web.json_response(
            {
                "id": f"resp_real_adapter_{len(requests)}",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "instructions": None,
                "metadata": {},
                "model": "gpt-5.5",
                "output": output,
                "parallel_tool_calls": True,
                "temperature": 1,
                "tool_choice": "auto",
                "tools": [],
                "top_p": 1,
                "usage": {
                    "input_tokens": 9,
                    "output_tokens": 4,
                    "total_tokens": 13,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 2},
                },
            }
        )

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="synthetic", base_url=str(server.make_url("/v1")), max_retries=0
        ) as client:
            model = OpenAIResponsesModel(model="gpt-5.5", openai_client=client)
            agent = Agent(
                "Adapter", instructions="Original adapter instructions", model=model, tools=[greet]
            )
            result = await SDKRunner.run(agent, "Greet Ada")
    assert result.final_output == "Adapter finished"
    assert effects == ["Ada"]
    assert len(requests) == 2
    assert requests[0]["instructions"] == "Original adapter instructions"
    assert requests[0]["tools"][0]["parameters"] == greet.params_json_schema
    assert next(item for item in requests[1]["input"] if item["type"] == "reasoning") == reasoning
    assert any(
        item["type"] == "function_call_output" and item["call_id"] == "call_1"
        for item in requests[1]["input"]
    )
    assert result.native_result.usage is not None


async def test_max_turns_stops_before_another_model_call() -> None:
    effects = []

    @function_tool
    def greet(value: str) -> str:
        effects.append(value)
        return value

    model = ScriptedModel([call("greet")], [message("must not run")])
    with pytest.raises(MaxTurnsExceeded):
        await SDKRunner.run(Agent("Bounded", model=model, tools=[greet]), "hello", max_turns=1)
    assert len(model.requests) == 1
    assert effects == ["Ada"]


async def test_model_failure_propagates_once() -> None:
    failure = RuntimeError("synthetic model failure")

    class FailingModel(ScriptedModel):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            self.requests.append(kwargs)
            raise failure

    model = FailingModel()
    with pytest.raises(RuntimeError) as raised:
        await SDKRunner.run(Agent("Failure", model=model), "hello")
    assert raised.value is failure
    assert len(model.requests) == 1


@pytest.mark.parametrize("failure", ["request", "stream"])
async def test_carrier_transport_failure_does_not_retry_inference_or_dispatch_tools(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effects: list[str] = []
    requests = 0
    original = _ResponseCarrier._handle

    @function_tool
    def greet(value: str) -> str:
        effects.append(value)
        return value

    async def fail(
        self: _ResponseCarrier, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal requests
        requests += 1
        if failure == "stream":
            self.pending = (
                b'event: response.created\ndata: {"type":"response.created",'
                b'"response":{"id":"truncated"}}\n\n'
            )
            await original(self, reader, writer)
        else:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            await reader.readexactly(length)
            writer.write(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            self._writers.discard(writer)

    monkeypatch.setattr(_ResponseCarrier, "_handle", fail)
    model = ScriptedModel([call("greet")])
    with pytest.raises(NativeError):
        await SDKRunner.run(Agent("Transport", model=model, tools=[greet]), "hello")
    assert requests == 1
    assert len(model.requests) == 1
    assert effects == []


@pytest.mark.parametrize("arguments", ['{"value": []}', '{"value": "Ada"}'])
async def test_original_invoker_validates_arguments_and_formats_failures(arguments: str) -> None:
    effects: list[str] = []

    @function_tool(failure_error_function=lambda ctx, error: "ORIGINAL_FAILURE")
    def greet(value: str) -> str:
        effects.append(value)
        raise ValueError("synthetic tool failure")

    model = ScriptedModel([call("greet", arguments=arguments)], [message("Handled")])
    result = await SDKRunner.run(Agent("Errors", model=model, tools=[greet]), "hello")
    assert result.final_output == "Handled"
    assert effects == ([] if "[]" in arguments else ["Ada"])
    receipt = next(
        item for item in model.requests[1]["input"] if item["type"] == "function_call_output"
    )
    assert "ORIGINAL_FAILURE" in str(receipt["output"])


async def test_session_cleanup_failure_still_releases_provider_and_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.migration import _runner

    failure = RuntimeError("synthetic cleanup failure")
    original_exit = _runner.Session._exit_scope
    original_provider_close = _runner.NativeProvider.aclose
    original_carrier_close = _runner._ResponseCarrier.close
    closed: list[str] = []

    async def failed_exit(self: Any) -> None:
        await original_exit(self)
        raise failure

    async def provider_close(self: Any) -> None:
        await original_provider_close(self)
        closed.append("provider")

    async def carrier_close(self: Any) -> None:
        await original_carrier_close(self)
        closed.append("carrier")

    monkeypatch.setattr(_runner.Session, "_exit_scope", failed_exit)
    monkeypatch.setattr(_runner.NativeProvider, "aclose", provider_close)
    monkeypatch.setattr(_runner._ResponseCarrier, "close", carrier_close)
    with pytest.raises(RuntimeError) as raised:
        await SDKRunner.run(Agent("Cleanup", model=ScriptedModel([message("done")])), "hello")
    assert raised.value is failure
    assert closed == ["provider", "carrier"]


async def test_cancellation_joins_model_and_discards_suppressed_result() -> None:
    entered, released = asyncio.Event(), asyncio.Event()
    effects = []

    @function_tool
    def greet(value: str) -> str:
        effects.append(value)
        return value

    class HeldModel(ScriptedModel):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                released.set()
                # A custom adapter may suppress cancellation. Its completed
                # function call must remain outside the native dispatch path.
                return ModelResponse([call("greet")], Usage(), None)

    task = asyncio.create_task(
        SDKRunner.run(Agent("Held", model=HeldModel(), tools=[greet]), "hello")
    )
    await asyncio.wait_for(entered.wait(), 15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 15)
    assert released.is_set()
    assert effects == []


@pytest.mark.parametrize("tool_kind", ["sync", "async"])
@pytest.mark.parametrize("cause", ["cancellation", "sibling_failure"])
async def test_teardown_joins_admitted_classic_invocations(tool_kind: str, cause: str) -> None:
    entered, finished, sibling_failed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async_release, cancelled = asyncio.Event(), asyncio.Event()
    thread_release = ThreadEvent()
    loop = asyncio.get_running_loop()
    effects: list[str] = []
    failure = RuntimeError("synthetic sibling failure")

    def sync_body(value: str) -> str:
        loop.call_soon_threadsafe(entered.set)
        try:
            if not thread_release.wait(10):
                raise RuntimeError("test did not release the held worker")
            effects.append(value)
            return value
        finally:
            loop.call_soon_threadsafe(finished.set)

    async def async_body(value: str) -> str:
        entered.set()
        try:
            await async_release.wait()
            effects.append(value)
            return value
        except asyncio.CancelledError:
            cancelled.set()
            raise
        finally:
            finished.set()

    held = function_tool(name_override="held", failure_error_function=None)(
        sync_body if tool_kind == "sync" else async_body
    )

    @function_tool(failure_error_function=None)
    async def fail(value: str) -> str:
        await entered.wait()
        sibling_failed.set()
        raise failure

    tools = [held] if cause == "cancellation" else [held, fail]
    output = [call("held", call_id="held_call")]
    if cause == "sibling_failure":
        output.append(call("fail", call_id="failed_call"))
    model = ScriptedModel(output)
    task = asyncio.create_task(SDKRunner.run(Agent("Joined", model=model, tools=tools), "hello"))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if cause == "cancellation":
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
        else:
            await asyncio.wait_for(sibling_failed.wait(), 10)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.05)
        assert not task.done()
        assert effects == []
        assert not cancelled.is_set()
        thread_release.set()
        async_release.set()
        if cause == "cancellation":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
        else:
            with pytest.raises(RuntimeError) as raised:
                await asyncio.wait_for(task, 10)
            assert raised.value is failure
        assert finished.is_set()
        assert not cancelled.is_set()
        assert effects == ["Ada"]
        assert len(model.requests) == 1
        await asyncio.sleep(0)
        assert effects == ["Ada"]
    finally:
        thread_release.set()
        async_release.set()
        if entered.is_set():
            await asyncio.wait_for(finished.wait(), 10)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "invalid", ["incomplete", "unknown_tool", "invalid_arguments", "non_finite", "duplicate_call"]
)
async def test_validate_whole_response_before_any_tool_runs(invalid: str) -> None:
    effects = []

    @function_tool
    def greet(value: str) -> str:
        effects.append(value)
        return value

    bad = {
        "incomplete": message("partial", status="incomplete"),
        "unknown_tool": call("unknown", call_id="call_bad"),
        "invalid_arguments": call("greet", call_id="call_bad", arguments="[]"),
        "non_finite": call("greet", call_id="call_bad", arguments='{"value": 1e999}'),
        "duplicate_call": call("greet"),
    }[invalid]
    model = ScriptedModel([call("greet"), bad])
    with pytest.raises((UserError, ModelBehaviorError)):
        await SDKRunner.run(Agent("Validate", model=model, tools=[greet]), "hello")
    assert effects == []


@pytest.mark.parametrize(
    "arguments",
    ['{"value":"synthetic-private-argument"', '{"value":"synthetic-private-argument","x":1e999}'],
)
async def test_invalid_arguments_do_not_escape_in_exception_chain(arguments: str) -> None:
    effects = []

    @function_tool
    def greet(value: str) -> str:
        effects.append(value)
        return value

    model = ScriptedModel([call("greet"), call("greet", call_id="bad", arguments=arguments)])
    with pytest.raises(ModelBehaviorError, match="JSON object function arguments") as raised:
        await SDKRunner.run(Agent("Validate", model=model, tools=[greet]), "hello")
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert "synthetic-private-argument" not in str(raised.value)
    assert len(model.requests) == 1
    assert effects == []


@pytest.mark.parametrize("kind", ["function_call", "reasoning", "message"])
@pytest.mark.parametrize("provider_data", [{"thought_signature": "synthetic-signature"}, {}])
async def test_provider_replay_metadata_fails_before_any_tool_runs(
    kind: str, provider_data: dict[str, str]
) -> None:
    effects = []

    @function_tool
    def greet(value: str) -> str:
        effects.append(value)
        return value

    item = {
        "function_call": call("greet", call_id="signed"),
        "reasoning": ResponseReasoningItem(id="rs_signed", type="reasoning", summary=[]),
        "message": message("signed"),
    }[kind].model_copy(update={"provider_data": provider_data})
    model = ScriptedModel([call("greet"), item], [message("must not continue")])
    with pytest.raises(UserError, match="provider_data") as raised:
        await SDKRunner.run(Agent("Validate", model=model, tools=[greet]), "hello")
    assert "synthetic-signature" not in str(raised.value)
    assert len(model.requests) == 1
    assert effects == []


@pytest.mark.parametrize(
    "feature",
    [
        "instructions",
        "handoffs",
        "guardrails",
        "output",
        "hooks",
        "model",
        "settings",
        "approval",
        "dynamic_tool",
        "codex_tool",
        "mcp_tool",
    ],
)
async def test_unsupported_configuration_fails_before_open_or_model(
    feature: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents.migration import _runner

    model = ScriptedModel([message("must not run")])
    agent = Agent("Unsupported", model=model)
    if feature == "instructions":
        agent.instructions = lambda *_: "dynamic"
    elif feature == "handoffs":
        agent.handoffs = [Agent("Other")]
    elif feature == "guardrails":
        agent.input_guardrails = [object()]  # type: ignore[list-item]
    elif feature == "output":

        class Output(BaseModel):
            text: str

        agent.output_type = Output
    elif feature == "hooks":
        agent.hooks = object()  # type: ignore[assignment]
    elif feature == "model":
        agent.model = None
    elif feature == "settings":
        agent.model_settings = ModelSettings(extra_body={"input": "replacement"})
    else:

        @function_tool
        def greet(value: str) -> str:
            return value

        if feature == "approval":
            greet.needs_approval = True
        elif feature == "dynamic_tool":
            greet.is_enabled = lambda *_: True
        elif feature == "codex_tool":
            greet._is_codex_tool = True
        else:
            greet._mcp_tool_binding = ("server", "tool", None)
        agent.tools = [greet]

    async def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("unsupported input opened native resources")

    monkeypatch.setattr(_runner._ResponseCarrier, "start", forbidden)
    with pytest.raises(UserError):
        await SDKRunner.run(agent, "hello")
    assert model.requests == []


async def carrier_request(carrier: _ResponseCarrier, body: Any, *, key: str | None = None) -> bytes:
    from urllib.parse import urlsplit

    url = urlsplit(carrier.url)
    reader, writer = await asyncio.open_connection(url.hostname, url.port)
    payload = json.dumps(body).encode()
    writer.write(
        (
            "POST /v1/responses HTTP/1.1\r\n"
            f"Authorization: Bearer {carrier.key if key is None else key}\r\n"
            f"Content-Length: {len(payload)}\r\n\r\n"
        ).encode()
        + payload
    )
    await writer.drain()
    result = await reader.read()
    writer.close()
    await writer.wait_closed()
    return result


async def test_private_carrier_authenticates_and_consumes_once() -> None:
    carrier = _ResponseCarrier()
    await carrier.start()
    try:
        carrier.pending = b"validated bytes"
        assert b"400" in await carrier_request(carrier, {"model": "gpt-5.5"}, key="wrong")
        assert b"400" in await carrier_request(carrier, [])
        assert carrier.pending == b"validated bytes"
        assert b"validated bytes" in await carrier_request(carrier, {"model": "gpt-5.5"})
        assert carrier.pending is None
        assert b"400" in await carrier_request(carrier, {"model": "gpt-5.5"})
    finally:
        await carrier.close()
    assert not carrier._tasks


async def test_private_carrier_close_joins_accepted_incomplete_requests() -> None:
    from urllib.parse import urlsplit

    carrier = _ResponseCarrier()
    await carrier.start()
    url = urlsplit(carrier.url)
    _reader, writer = await asyncio.open_connection(url.hostname, url.port)
    carrier.pending = b"must be cleared"
    await asyncio.wait_for(carrier.close(), 5)
    assert carrier.pending is None
    assert not carrier._tasks
    writer.close()
    await writer.wait_closed()
