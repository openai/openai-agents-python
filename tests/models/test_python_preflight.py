from unittest.mock import AsyncMock

import pytest
from openai import AsyncOpenAI

from agents import (
    Agent,
    HostedMCPTool,
    ModelRequest,
    ModelSettings,
    RunConfig,
    Runner,
    ToolExecutionConfig,
    UserError,
    WebSearchTool,
    function_tool,
)
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_responses import OpenAIResponsesModel


@pytest.fixture(params=[OpenAIResponsesModel, OpenAIChatCompletionsModel])
def model(request):
    # A synthetic key and uncalled client keep the test at the adapter boundary.
    client = AsyncOpenAI(api_key="synthetic-test-key", base_url="https://api.openai.com/v1")
    client.responses.create = AsyncMock(side_effect=AssertionError("Unexpected sampling"))
    client.chat.completions.create = AsyncMock(side_effect=AssertionError("Unexpected sampling"))
    return request.param("fixture-chat", client)


@function_tool
def edit_file() -> str:
    """Apply an application-owned edit."""
    raise AssertionError("Preflight must not execute tools")


def test_python_adapters_accept_named_sdk_tool_and_file_without_upload(model):
    report = model.preflight(
        ModelRequest(
            input=[
                {
                    "role": "user",
                    "content": [{"type": "input_file", "file_id": "file-existing-pdf"}],
                }
            ],
            tools=[edit_file],
            model_settings=ModelSettings(tool_choice="edit_file"),
            tool_execution=ToolExecutionConfig(workspace_execution="sdk"),
        )
    )
    assert report.static_validation == "passed"
    assert report.model_name == "fixture-chat"
    assert report.adapter == f"{type(model).__module__}.{type(model).__qualname__}"
    assert [(item.name, item.executor) for item in report.tools] == [("edit_file", "sdk")]
    assert any("files" in check for check in report.unchecked)
    model._client.responses.create.assert_not_called()
    model._client.chat.completions.create.assert_not_called()


@pytest.mark.parametrize(
    "tool",
    [
        WebSearchTool(),
        HostedMCPTool({"server_label": "external", "server_url": "https://service.example/mcp"}),
    ],
)
def test_sdk_profile_refuses_hosted_or_external_workspace_routes(model, tool):
    report = model.preflight(
        ModelRequest(
            input="start",
            tools=[tool],
            tool_execution=ToolExecutionConfig(workspace_execution="sdk"),
        )
    )
    assert report.static_validation == "failed"
    with pytest.raises(UserError, match="SDK callback"):
        report.raise_for_errors()
    assert report.tools[0].executor in ("provider", "external")
    model._client.responses.create.assert_not_called()
    model._client.chat.completions.create.assert_not_called()


@pytest.mark.parametrize(
    "settings",
    [
        ModelSettings(extra_args={"tools": [{"type": "web_search"}]}),
        ModelSettings(extra_body={"tools": [{"type": "web_search"}]}),
    ],
)
def test_sdk_profile_refuses_raw_tool_overrides(model, settings):
    report = model.preflight(
        ModelRequest(
            input="start",
            tools=[edit_file],
            model_settings=settings,
            tool_execution=ToolExecutionConfig(workspace_execution="sdk"),
        )
    )
    with pytest.raises(UserError, match="raw tool"):
        report.raise_for_errors()


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_python_runner_checks_explicit_model_before_dispatch(model, streamed):
    config = RunConfig(
        tracing_disabled=True,
        tool_execution=ToolExecutionConfig(require_native_approval_recovery=True),
    )
    agent = Agent(name="explicit model", model=model, tools=[edit_file])
    with pytest.raises(UserError, match="CodexProvider"):
        if streamed:
            run = Runner.run_streamed(agent, "start", run_config=config)
            async for _ in run.stream_events():
                pass
        else:
            await Runner.run(agent, "start", run_config=config)
    model._client.responses.create.assert_not_called()
    model._client.chat.completions.create.assert_not_called()


def test_preflight_leaves_one_shot_input_unconsumed(model):
    class OneShotContent:
        def __init__(self):
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            yield {"type": "input_text", "text": "one-shot content"}

    content = OneShotContent()
    request = ModelRequest(input=[{"role": "user", "content": content}])
    report = model.preflight(request)
    assert report.static_validation == "unknown"
    assert content.iterations == 0
    assert request.input[0]["content"] is content


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("streamed", [False, True])
async def test_chat_replay_predicate_runs_once_on_actual_request_only(streamed):
    import json

    import httpx

    callbacks = []
    requests = []

    def predicate(context):
        callbacks.append(context.reasoning.item)
        return True

    def respond(request):
        requests.append(request)
        if json.loads(request.content).get("stream"):
            chunks = [
                {
                    "id": "chatcmpl-local",
                    "created": 0,
                    "model": "fixture-chat",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "done"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-local",
                    "created": 0,
                    "model": "fixture-chat",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            ]
            body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=body + "data: [DONE]\n\n"
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-local",
                "created": 0,
                "model": "fixture-chat",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "done",
                        },
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = AsyncOpenAI(api_key="synthetic", http_client=http_client)
        model = OpenAIChatCompletionsModel(
            "fixture-chat",
            client,
            should_replay_reasoning_content=predicate,
        )
        history = [
            {"role": "user", "content": "before"},
            {
                "type": "reasoning",
                "id": "reasoning-prior",
                "summary": [
                    {"type": "summary_text", "text": "A prior reasoning summary."},
                ],
            },
            {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": "continue"},
        ]
        snapshot = repr(history)
        report = model.preflight(ModelRequest(input=history))
        assert report.static_validation == "unknown"
        assert callbacks == [] and repr(history) == snapshot
        if streamed:
            result = Runner.run_streamed(
                Agent(name="replay", model=model),
                history,
                run_config=RunConfig(tracing_disabled=True),
            )
            async for _ in result.stream_events():
                pass
        else:
            result = await Runner.run(
                Agent(name="replay", model=model),
                history,
                run_config=RunConfig(tracing_disabled=True),
            )
    assert result.final_output == "done"
    assert len(callbacks) == 1 and len(requests) == 1
    assert repr(history) == snapshot
