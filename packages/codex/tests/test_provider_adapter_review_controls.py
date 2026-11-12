"""Failed adapter responses retain measurements and native retry authority."""

from types import SimpleNamespace

import pytest

from agents import AnyLLMModel, ChatCompletionsModel, LitellmModel, ModelRequest, ResponsesModel


def request(reports=None, *, disable_retries=False):
    return ModelRequest(
        body={"model": "fixture-adapter-controls", "input": []},
        context=None,
        session_id="fixture-session",
        turn_id="fixture-turn",
        disable_provider_retries=disable_retries,
        _report_usage=(lambda usage: reports.append(usage) or True)
        if reports is not None
        else None,
    )


@pytest.mark.parametrize("reason", ["length", "unsupported"])
async def test_chat_rejected_response_reports_received_usage(reason):
    reports = []

    async def create(**kwargs):
        return {
            "id": "chat-rejected",
            "choices": [{"finish_reason": reason, "message": {"content": "partial"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(ValueError):
        await ChatCompletionsModel(client).complete(request(reports))
    assert len(reports) == 1
    assert reports[0]["input_tokens"] == 2
    assert reports[0]["output_tokens"] == 3
    assert reports[0]["total_tokens"] == 5


@pytest.mark.parametrize("status", ["incomplete", "failed"])
async def test_responses_rejected_response_reports_received_usage(status):
    reports = []

    async def create(**kwargs):
        return {
            "id": "responses-rejected",
            "status": status,
            "output": [],
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        }

    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    with pytest.raises(ValueError):
        await ResponsesModel(client).complete(request(reports))
    assert len(reports) == 1
    assert reports[0]["input_tokens"] == 2
    assert reports[0]["output_tokens"] == 3
    assert reports[0]["total_tokens"] == 5


async def test_litellm_disables_borrowed_router_retries_per_request():
    attempts = []

    async def acompletion(**kwargs):
        attempts.extend(range(1 + kwargs.get("num_retries", 2)))
        return SimpleNamespace(
            model_dump=lambda: {
                "id": "litellm-controlled",
                "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
            }
        )

    result = await LitellmModel(SimpleNamespace(acompletion=acompletion)).complete(
        request(disable_retries=True)
    )
    assert result.output[0]["content"][0]["text"] == "done"
    assert len(attempts) == 1


async def test_anyllm_rejects_retry_ownership_before_borrowed_dispatch():
    calls = []

    async def acompletion(**kwargs):
        calls.append(kwargs)
        return {
            "id": "anyllm-uncontrolled",
            "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
        }

    with pytest.raises(TypeError, match="retries"):
        await AnyLLMModel(SimpleNamespace(acompletion=acompletion)).complete(
            request(disable_retries=True)
        )
    assert calls == []


@pytest.mark.parametrize("adapter", ["chat", "responses"])
async def test_rejected_provider_usage_reaches_live_and_recorded_native_totals(adapter):
    from agents import Agent, ModelRetrySettings, NativeProvider
    from tests._asyncio import timeout

    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        if adapter == "chat":
            return {
                "id": "chat-billed-rejection",
                "choices": [{"finish_reason": "length", "message": {"content": "partial"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            }
        return {
            "id": "responses-billed-rejection",
            "status": "incomplete",
            "output": [],
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        }

    class Client:
        chat = SimpleNamespace(completions=SimpleNamespace(create=create))
        responses = SimpleNamespace(create=create)

        def with_options(self, *, max_retries):
            assert max_retries == 0
            return self

    backend = ChatCompletionsModel(Client()) if adapter == "chat" else ResponsesModel(Client())
    async with timeout(10), NativeProvider(model_backend=backend) as provider:
        async with Agent(
            model="fixture-adapter-usage",
            instructions="Answer.",
            provider=provider,
            model_settings={"retry": ModelRetrySettings(max_retries=0)},
        ).session() as session:
            turn = await session.submit("Get an answer.")
            with pytest.raises(ValueError):
                await turn.get_result()
            assert turn.run_usage.requests == 1
            assert turn.run_usage.missing_usage_requests == 0
            assert turn.run_usage.input_tokens == 2
            assert turn.run_usage.output_tokens == 3
            recorded = [item async for item in session.turns()]
            assert recorded[-1]["run_usage"]["requests"] == 1
            assert recorded[-1]["run_usage"]["missing_usage_requests"] == 0
            assert recorded[-1]["run_usage"]["input_tokens"] == 2
            assert recorded[-1]["run_usage"]["output_tokens"] == 3
    assert len(calls) == 1
