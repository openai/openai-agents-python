import json
from unittest.mock import AsyncMock

import httpx2
import pytest
from openai import AsyncOpenAI, BadRequestError
from openai.types.responses.web_search_tool import Filters
from openai.types.shared import Reasoning
from pydantic import field_serializer

from agents import (
    Agent,
    ComputerTool,
    HostedMCPTool,
    ModelRequest,
    ModelSettings,
    ModelTracing,
    RunConfig,
    RunHooks,
    Runner,
    ToolExecutionConfig,
    UserError,
    WebSearchTool,
    function_tool,
)
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_responses import OpenAIResponsesModel, OpenAIResponsesWSModel
from tests.test_computer_tool_lifecycle import FakeComputer


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
@pytest.mark.parametrize("strict", [False, True])
async def test_chat_replay_predicate_runs_once_on_actual_request_only(streamed, strict):
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
            strict_feature_validation=strict,
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


async def _dispatch_chat(model, request):
    return await model.get_response(
        system_instructions=None,
        input=request.input,
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        model_settings=request.model_settings,
        prompt=request.prompt,
        previous_response_id=request.previous_response_id,
        conversation_id=request.conversation_id,
    )


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"model_settings": ModelSettings(reasoning=Reasoning(mode="pro"))}, "reasoning.mode"),
        (
            {"model_settings": ModelSettings(reasoning=Reasoning(context="all_turns"))},
            "reasoning.context",
        ),
        ({"prompt": {}}, "prompt"),
        ({"previous_response_id": ""}, "previous_response_id"),
        ({"conversation_id": ""}, "conversation_id"),
    ],
)
async def test_strict_chat_preflight_matches_dispatch_for_unsupported_settings(kwargs, expected):
    async with AsyncOpenAI(api_key="synthetic") as client:
        client.chat.completions.create = AsyncMock(
            side_effect=AssertionError("Unexpected sampling")
        )
        model = OpenAIChatCompletionsModel("fixture-chat", client, strict_feature_validation=True)
        request = ModelRequest(input="start", **kwargs)
        report = model.preflight(request)
        assert report.static_validation == "failed"
        with pytest.raises(UserError, match=expected) as error:
            await _dispatch_chat(model, request)
        assert report.errors == (str(error.value),)


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize(
    "item, expected",
    [
        (
            {
                "role": "user",
                "content": [{"type": "input_video", "video_url": "https://example.com/video"}],
            },
            "Unsupported content type",
        ),
        (
            {"type": "function_call_output", "call_id": "call", "output": []},
            "tool outputs cannot be empty",
        ),
    ],
)
async def test_strict_chat_checks_static_input_without_calling_replay_predicate(
    item, expected, replay
):
    callbacks = []

    def predicate(context):
        callbacks.append(context)
        return replay

    history = [{"type": "reasoning", "id": "prior", "summary": []}, item]
    async with AsyncOpenAI(api_key="synthetic", base_url="https://api.openai.com/v1") as client:
        client.chat.completions.create = AsyncMock(
            side_effect=AssertionError("Unexpected sampling")
        )
        model = OpenAIChatCompletionsModel(
            "fixture-chat",
            client,
            strict_feature_validation=True,
            should_replay_reasoning_content=predicate,
        )
        request = ModelRequest(input=history)
        report = model.preflight(request)
        assert callbacks == []
        assert report.static_validation == "failed"
        with pytest.raises(UserError, match=expected) as error:
            await _dispatch_chat(model, request)
        assert report.errors == (str(error.value),)
        client.chat.completions.create.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
async def test_legacy_chat_preflight_leaves_reasoning_warning_to_dispatch(caplog):
    async with AsyncOpenAI(api_key="synthetic") as client:
        client.chat.completions.create = AsyncMock(side_effect=RuntimeError("sampling reached"))
        model = OpenAIChatCompletionsModel("fixture-chat", client)
        settings = ModelSettings(reasoning=Reasoning(mode="pro", context="all_turns"))
        request = ModelRequest(input="start", model_settings=settings)
        report = model.preflight(request)
        assert report.static_validation == "passed"
        warning = "Ignoring unsupported reasoning settings"
        assert warning not in caplog.text
        for _ in range(2):
            with pytest.raises(RuntimeError, match="sampling reached"):
                await _dispatch_chat(model, request)
        assert caplog.text.count(warning) == 1


@pytest.mark.asyncio
async def test_strict_chat_preflight_accepts_reasoning_effort():
    async with AsyncOpenAI(api_key="synthetic") as client:
        model = OpenAIChatCompletionsModel("fixture-chat", client, strict_feature_validation=True)
        settings = ModelSettings(reasoning=Reasoning(effort="low"))
        report = model.preflight(ModelRequest(input="start", model_settings=settings))
        assert report.static_validation == "passed"


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("adapter", ["chat", "strict-chat", "responses"])
async def test_duplicate_extra_args_fail_before_hooks_but_direct_dispatch_keeps_type_error(
    adapter, streamed
):
    starts = []

    class Observer(RunHooks):
        async def on_llm_start(self, context, agent, system_prompt, input_items):
            starts.append(agent.name)

    async with AsyncOpenAI(api_key="synthetic") as client:
        sampling = AsyncMock(side_effect=AssertionError("Unexpected sampling"))
        client.chat.completions.create = sampling
        client.responses.create = sampling
        model = (
            OpenAIResponsesModel("fixture-model", client)
            if adapter == "responses"
            else OpenAIChatCompletionsModel(
                "fixture-model", client, strict_feature_validation=adapter == "strict-chat"
            )
        )
        settings = ModelSettings(temperature=0, extra_args={"temperature": 0.7})
        report = model.preflight(ModelRequest(input="start", model_settings=settings))
        message = "multiple values for keyword argument 'temperature'"
        assert report.static_validation == "failed"
        with pytest.raises(UserError, match=message):
            report.raise_for_errors()
        agent = Agent(name="worker", model=model, model_settings=settings)
        for opted_in in (True, False):
            config = RunConfig(
                tracing_disabled=True,
                tool_execution=ToolExecutionConfig(workspace_execution="sdk") if opted_in else None,
            )
            with pytest.raises(UserError if opted_in else TypeError, match=message):
                if streamed:
                    result = Runner.run_streamed(
                        agent, "start", hooks=Observer(), run_config=config
                    )
                    async for _ in result.stream_events():
                        pass
                else:
                    await Runner.run(agent, "start", hooks=Observer(), run_config=config)
            assert starts == ([] if opted_in else ["worker"])
        sampling.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("streamed", [False, True])
async def test_legacy_chat_preflight_warns_only_when_dispatch_replaces_tool_output(
    streamed, caplog
):
    history = [{"type": "function_call_output", "call_id": "call", "output": []}]
    async with AsyncOpenAI(api_key="synthetic") as client:
        sampling = AsyncMock(side_effect=RuntimeError("sampling reached"))
        client.chat.completions.create = sampling
        model = OpenAIChatCompletionsModel("fixture-chat", client)
        report = model.preflight(ModelRequest(input=history))
        assert report.static_validation == "passed"
        warning = "Replacing the tool output with a placeholder"
        assert warning not in caplog.text
        config = RunConfig(
            tracing_disabled=True, tool_execution=ToolExecutionConfig(workspace_execution="sdk")
        )
        agent = Agent(name="worker", model=model)
        with pytest.raises(RuntimeError, match="sampling reached"):
            if streamed:
                result = Runner.run_streamed(agent, history, run_config=config)
                async for _ in result.stream_events():
                    pass
            else:
                await Runner.run(agent, history, run_config=config)
        assert caplog.text.count(warning) == 1
        assert sampling.call_args.kwargs["messages"][-1]["content"] == "[tool output omitted]"


@pytest.mark.parametrize(
    "model, settings, key",
    [
        (
            OpenAIResponsesModel,
            ModelSettings(max_tokens=2, extra_args={"max_output_tokens": 5}),
            "max_output_tokens",
        ),
        (OpenAIResponsesModel, ModelSettings(verbosity="low", extra_args={"text": {}}), "text"),
        (
            OpenAIChatCompletionsModel,
            ModelSettings(
                reasoning=Reasoning(effort="low"), extra_args={"reasoning_effort": "high"}
            ),
            "reasoning_effort",
        ),
        (OpenAIChatCompletionsModel, ModelSettings(extra_args={"messages": []}), "messages"),
        (OpenAIResponsesWSModel, ModelSettings(extra_args={"stream": False}), "stream"),
    ],
    indirect=["model"],
)
def test_preflight_rejects_duplicate_provider_key_even_when_setting_name_differs(
    model, settings, key
):
    report = model.preflight(ModelRequest(input="start", model_settings=settings))
    with pytest.raises(UserError, match=key):
        report.raise_for_errors()


@pytest.mark.parametrize(
    "extra_args",
    [
        {"temperature": 0},
        {"vendor_option": "opaque"},
        {"stream": False},
        {"parallel_tool_calls": True},
    ],
)
def test_preflight_leaves_extra_args_without_definite_collisions_unchecked(model, extra_args):
    report = model.preflight(
        ModelRequest(
            input="start",
            model_settings=ModelSettings(parallel_tool_calls=False, extra_args=extra_args),
        )
    )
    assert report.static_validation == "unknown"
    assert any("extra_args" in item for item in report.unchecked)
    report.raise_for_errors()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("use_chat", [False, True])
async def test_automatic_preflight_preserves_raw_overrides_of_omitted_arguments(use_chat, streamed):
    bodies = []

    def capture(request):
        bodies.append(json.loads(request.content))
        return httpx2.Response(400, json={"error": {"message": "request captured"}})

    async with AsyncOpenAI(
        api_key="synthetic",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(capture)),
    ) as client:
        if use_chat:
            model = OpenAIChatCompletionsModel("fixture-chat", client)
            raw = {"temperature": 0, "reasoning_effort": "low", "logprobs": True}
        else:
            model = OpenAIResponsesModel("fixture-responses", client)
            raw = {"temperature": 0, "max_output_tokens": 10, "text": {"verbosity": "low"}}
        settings = ModelSettings(top_logprobs=2, extra_args=raw)
        agent = Agent(name="worker", model=model, model_settings=settings)
        config = RunConfig(
            tracing_disabled=True, tool_execution=ToolExecutionConfig(workspace_execution="sdk")
        )
        with pytest.raises(BadRequestError, match="request captured"):
            if streamed:
                result = Runner.run_streamed(agent, "start", run_config=config)
                async for _ in result.stream_events():
                    pass
            else:
                await Runner.run(agent, "start", run_config=config)
        (body,) = bodies
        assert {key: body[key] for key in raw} == raw
        assert body["top_logprobs"] == 2


@pytest.mark.asyncio
async def test_responses_preflight_respects_prompt_owned_omissions():
    async with AsyncOpenAI(api_key="synthetic") as client:
        model = OpenAIResponsesModel("fixture", client, model_is_explicit=False)
        request = ModelRequest(
            input="start",
            prompt={"id": "pmpt_fixture"},
            model_settings=ModelSettings(
                tool_choice="lookup",
                extra_args={"model": "other", "tools": [], "tool_choice": "auto"},
            ),
        )
        report = model.preflight(request)
        assert report.static_validation == "unknown"
        report.raise_for_errors()


@pytest.mark.asyncio
@pytest.mark.parametrize("model_name", ["gpt-5.4", "computer-use-preview"])
@pytest.mark.parametrize(
    "key, typed, raw",
    [
        ("store", {"store": False}, True),
        ("parallel_tool_calls", {"parallel_tool_calls": False}, True),
        ("tool_choice", {"tool_choice": "auto"}, "none"),
        ("tool_choice", {"tool_choice": "computer"}, "auto"),
        ("tools", {}, []),
    ],
)
async def test_responses_preflight_checks_known_collisions_without_loading_dynamic_tools(
    model_name, key, typed, raw
):
    def load_computer(**kwargs):
        raise AssertionError("Preflight must not load a computer")

    async with AsyncOpenAI(api_key="synthetic") as client:
        model = OpenAIResponsesModel(model_name, client)
        report = model.preflight(
            ModelRequest(
                input="start",
                prompt={"id": "pmpt_fixture"} if key == "tools" else None,
                tools=[ComputerTool(computer=load_computer), edit_file],
                model_settings=ModelSettings(**typed, extra_args={key: raw}),
            )
        )
        with pytest.raises(UserError, match=key):
            report.raise_for_errors()


@pytest.mark.asyncio
async def test_responses_preflight_defers_preview_metadata_and_allows_omitted_choice():
    class UnreadableComputer(FakeComputer):
        @property
        def environment(self):
            raise AssertionError("Preflight must not query the computer environment")

        @property
        def dimensions(self):
            raise AssertionError("Preflight must not query the computer dimensions")

    async with AsyncOpenAI(api_key="synthetic") as client:
        model = OpenAIResponsesModel("gpt-5.4", client, model_is_explicit=False)
        report = model.preflight(
            ModelRequest(
                input="start",
                prompt={"id": "pmpt_fixture"},
                tools=[ComputerTool(computer=UnreadableComputer())],
                model_settings=ModelSettings(extra_args={"tool_choice": "auto", "model": "other"}),
            )
        )
        assert report.static_validation == "unknown"
        report.raise_for_errors()


@pytest.mark.asyncio
@pytest.mark.allow_call_model_methods
@pytest.mark.parametrize("with_computer", [False, True], ids=["web", "computer-and-web"])
@pytest.mark.parametrize("streamed", [False, True], ids=["awaited", "streamed"])
async def test_web_search_filters_serialize_once_at_dispatch(with_computer, streamed):
    events = []
    bodies = []

    class DomainFilters(Filters):
        @field_serializer("allowed_domains")
        def serialize_domains(self, domains) -> list[str]:
            events.append("serialize")
            return domains if events.count("serialize") == 1 else ["second.example.org"]

    class Observer(RunHooks):
        async def on_llm_start(self, context, agent, system_prompt, input_items):
            events.append("llm_start")

    def capture(request):
        bodies.append(json.loads(request.content))
        return httpx2.Response(400, json={"error": {"message": "request captured"}})

    domains = ["first.example.org"]
    web = WebSearchTool(filters=DomainFilters(allowed_domains=domains))
    tools = [ComputerTool(computer=FakeComputer()), web] if with_computer else [web]
    async with AsyncOpenAI(
        api_key="synthetic",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(capture)),
    ) as client:
        model = OpenAIResponsesModel("gpt-5.4", client)
        settings = ModelSettings(tool_choice="web_search")
        report = model.preflight(ModelRequest(input="start", tools=tools, model_settings=settings))
        collision = model.preflight(
            ModelRequest(
                input="start",
                tools=tools,
                model_settings=ModelSettings(
                    tool_choice="web_search", extra_args={"tool_choice": "none"}
                ),
            )
        )
        with pytest.raises(UserError, match="tool_choice"):
            collision.raise_for_errors()
        assert events == []
        assert report.static_validation == "unknown"
        assert any("web search" in item for item in report.unchecked)

        agent = Agent(name="worker", model=model, tools=tools, model_settings=settings)
        config = RunConfig(tracing_disabled=True, model_selector=lambda data: None)
        with pytest.raises(BadRequestError, match="request captured"):
            if streamed:
                result = Runner.run_streamed(agent, "start", run_config=config, hooks=Observer())
                async for _ in result.stream_events():
                    pass
            else:
                await Runner.run(agent, "start", run_config=config, hooks=Observer())
        assert events == ["llm_start", "serialize"]
        (body,) = bodies
        assert body["tools"][-1]["filters"]["allowed_domains"] == domains
        assert body["tool_choice"] == {"type": "web_search"}
