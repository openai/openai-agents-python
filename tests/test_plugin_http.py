"""An HTTP requirement applies to the effective model, including Runner overrides."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace

import httpx2
import pytest
from openai import AsyncOpenAI, OpenAIError

from agents import Agent, ModelSettings, RunConfig, Runner, UserError
from agents.models.openai_provider import OpenAIProvider
from agents.models.openai_responses import OpenAIResponsesModel
from agents.plugin_http import HTTPHooks, RequestEdit, RequestInfo, ResponseInfo, responses_provider
from agents.plugins import Plugin
from agents.retry import ModelRetrySettings

pytestmark = pytest.mark.allow_call_model_methods

BASE = "http://127.0.0.1:1/v1"
MODEL = "fixture"


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[httpx2.Request]:
    attempts: list[httpx2.Request] = []

    async def send(_transport: object, request: httpx2.Request) -> httpx2.Response:
        attempts.append(request)
        if len(attempts) == 1:
            return httpx2.Response(503, headers={"retry-after-ms": "1"}, json={})
        return httpx2.Response(
            200,
            headers={"set-cookie": "private-cookie"},
            json={
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done", "annotations": []}],
                    }
                ],
            },
        )

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", send)
    return attempts


@pytest.mark.asyncio
async def test_each_retry_has_a_distinct_edited_attempt(
    sent: list[httpx2.Request], caplog: pytest.LogCaptureFixture
) -> None:
    requests: list[RequestInfo] = []
    responses: list[ResponseInfo] = []

    def edit(req: RequestInfo) -> RequestEdit:
        requests.append(req)
        return RequestEdit(verbosity="low", client_request_id=req.attempt_id)

    def observe(response: ResponseInfo) -> None:
        responses.append(response)
        raise ValueError("private-observer")

    plugin = Plugin(name="diagnostics", http=HTTPHooks(on_request=edit, on_response=observe))
    agent = Agent(name="helper", model=MODEL, plugins=[plugin])
    async with responses_provider(agent, base_url=BASE, api_key="private-key", model=MODEL) as p:
        result = await Runner.run(
            agent, "private-input", run_config=RunConfig(model_provider=p, tracing_disabled=True)
        )
    assert result.final_output == "done"
    assert len(sent) == len(requests) == len(responses) == 2
    assert [response.status for response in responses] == [503, 200]
    assert requests[0].attempt_id != requests[1].attempt_id
    for request, response, outgoing in zip(requests, responses, sent, strict=True):
        assert (
            request.attempt_id
            == response.request.attempt_id
            == outgoing.headers["x-client-request-id"]
        )
        assert request.verbosity is None
        assert (
            response.request.verbosity == json.loads(outgoing.content)["text"]["verbosity"] == "low"
        )
        assert b"private-input" in outgoing.content
    assert "private-" not in repr((requests, responses))
    assert "private-observer" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_unbound_model_at_same_destination_cannot_override_client(
    sent: list[httpx2.Request], streamed: bool
) -> None:
    plugin = Plugin(name="diagnostics", http=HTTPHooks())
    agent = Agent(name="helper", model=MODEL, plugins=[plugin])
    async with (
        responses_provider(agent, base_url=BASE, api_key="fake", model=MODEL) as p,
        AsyncOpenAI(base_url=BASE, api_key="fake") as other,
    ):
        override = OpenAIProvider(openai_client=other).get_model(MODEL)
        config = RunConfig(model_provider=p, model=override, tracing_disabled=True)
        with pytest.raises(UserError, match="diagnostics.*approved Responses HTTP client"):
            if streamed:
                result = Runner.run_streamed(agent, "test", run_config=config)
                async for _ in result.stream_events():
                    pass
            else:
                await Runner.run(agent, "test", run_config=config)
    assert sent == []


@pytest.mark.asyncio
async def test_foreign_agent_and_unapproved_effective_model_never_send(
    sent: list[httpx2.Request],
) -> None:
    agent = Agent(name="helper", model=MODEL, plugins=[Plugin(name="p", http=HTTPHooks())])
    async with responses_provider(agent, base_url=BASE, api_key="fake", model=MODEL) as p:
        config = RunConfig(model_provider=p, tracing_disabled=True)
        with pytest.raises(UserError, match="original approved agent"):
            await Runner.run(agent.clone(plugins=[]), "test", run_config=config)
        config.model_settings = ModelSettings(extra_body={"model": "other"})
        with pytest.raises(OpenAIError, match="request refused"):
            await Runner.run(agent, "test", run_config=config)
    assert sent == []


@pytest.mark.asyncio
async def test_request_callback_cancellation_before_send(sent: list[httpx2.Request]) -> None:
    entered = asyncio.Event()

    async def wait(_: RequestInfo) -> None:
        entered.set()
        await asyncio.Future()

    agent = Agent(
        name="helper", model=MODEL, plugins=[Plugin(name="p", http=HTTPHooks(on_request=wait))]
    )
    async with responses_provider(agent, base_url=BASE, api_key="fake", model=MODEL) as p:
        config = RunConfig(model_provider=p, tracing_disabled=True)
        run = asyncio.create_task(Runner.run(agent, "test", run_config=config))
        await asyncio.wait_for(entered.wait(), 5)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
    assert sent == []


@pytest.mark.asyncio
async def test_failed_edit_cannot_be_retried_by_client_or_runner(
    sent: list[httpx2.Request],
) -> None:
    attempts: list[str] = []

    def edit(req: RequestInfo) -> None:
        attempts.append(req.attempt_id)
        if len(attempts) == 1:
            raise ValueError("private-request-handler")

    plugin = Plugin(name="p", http=HTTPHooks(on_request=edit))
    agent = Agent(
        name="helper",
        model=MODEL,
        plugins=[plugin],
        model_settings=ModelSettings(
            retry=ModelRetrySettings(max_retries=1, policy=lambda _: True)
        ),
    )
    async with responses_provider(agent, base_url=BASE, api_key="fake", model=MODEL) as p:
        with pytest.raises(OpenAIError, match="request refused") as caught:
            await Runner.run(
                agent, "test", run_config=RunConfig(model_provider=p, tracing_disabled=True)
            )
    assert len(attempts) == 1
    assert caught.value.__context__ is None
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_sync_callback_can_cancel_the_owning_run_before_send(
    sent: list[httpx2.Request], streamed: bool
) -> None:
    calls: list[str] = []
    cancel: Callable[[], object]

    def edit(request: RequestInfo) -> RequestEdit:
        calls.append(request.model)
        cancel()
        return RequestEdit(verbosity="low")

    plugin = Plugin(name="gate", http=HTTPHooks(on_request=edit))
    agent = Agent(name="helper", model=MODEL, plugins=[plugin])
    async with responses_provider(agent, base_url=BASE, api_key="fake", model=MODEL) as p:
        config = RunConfig(model_provider=p, tracing_disabled=True)
        if streamed:
            run = Runner.run_streamed(agent, "test", run_config=config)
            cancel = run.cancel
            async for _ in run.stream_events():
                pass
        else:
            task = asyncio.create_task(Runner.run(agent, "test", run_config=config))
            cancel = task.cancel
            with pytest.raises(asyncio.CancelledError):
                await task
    assert calls == [MODEL]
    assert sent == []


@pytest.mark.asyncio
async def test_equal_replacement_after_editor_invalidates_approval(
    sent: list[httpx2.Request],
) -> None:
    def edit(_: RequestInfo) -> None:
        asyncio.get_running_loop().call_soon(setattr, agent, "plugins", (replace(plugin),))

    plugin = Plugin(name="gate", http=HTTPHooks(on_request=edit))
    agent = Agent(name="helper", model=MODEL, plugins=[plugin])
    async with responses_provider(agent, base_url=BASE, api_key="fake", model=MODEL) as p:
        with pytest.raises(UserError, match="HTTP request refused"):
            await Runner.run(
                agent, "test", run_config=RunConfig(model_provider=p, tracing_disabled=True)
            )
    assert agent.plugins[0] is not plugin
    assert sent == []


@pytest.mark.asyncio
async def test_attempt_uses_the_destination_checked_before_the_editor(
    sent: list[httpx2.Request],
) -> None:
    held: list[httpx2.Request] = []

    async def capture(request: httpx2.Request) -> None:
        held.append(request)

    async def edit(_: RequestInfo) -> None:
        held[-1].url = httpx2.URL("http://127.0.0.1:2/other")
        held[-1].extensions["target"] = b"/other"

    plugin = Plugin(name="gate", http=HTTPHooks(on_request=edit))
    agent = Agent(name="helper", model=MODEL, plugins=[plugin])
    async with responses_provider(agent, base_url=BASE, api_key="fake", model=MODEL) as p:
        model = p.get_model(MODEL)
        assert isinstance(model, OpenAIResponsesModel)
        model._client._client.event_hooks["request"].append(capture)
        await Runner.run(
            agent, "test", run_config=RunConfig(model_provider=p, tracing_disabled=True)
        )
    assert len(sent) == 2
    for request in sent:
        assert str(request.url) == BASE + "/responses"
        assert "target" not in request.extensions
