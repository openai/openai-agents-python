from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from agents import (
    Agent,
    GuardrailFunctionOutput,
    ModelSelectionData,
    ModelSettings,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
    RunBudget,
    RunConfig,
    Runner,
    UserError,
    function_tool,
    output_guardrail,
)
from agents.models.interface import Model, ModelProvider
from agents.models.multi_provider import MultiProvider, MultiProviderMap
from agents.retry import ModelRetrySettings
from agents.sandbox import SandboxAgent
from agents.testing import ScriptedModel, assistant_message, function_call, scripted_sandbox_session


def test_existing_positional_budget_still_binds_to_budget() -> None:
    defaults = RunConfig()
    # The original constructor takes the budget immediately after redact_text.
    fields = RunConfig.__match_args__
    args: list[Any] = [
        getattr(defaults, name) for name in fields[: fields.index("redact_text") + 1]
    ]
    budget = RunBudget(max_model_calls=1)
    args.append(budget)
    config = RunConfig(*args)
    assert config.budget is budget
    assert config.model_selector is None


class RecordingProvider(ModelProvider):
    def __init__(self, models: dict[str | None, Model]):
        self.models = models
        self.selections: list[str | None] = []

    def get_model(self, model_name: str | None) -> Model:
        self.selections.append(model_name)
        return self.models[model_name]


async def run_agent(streamed: bool, agent: Agent[Any], **kwargs: Any) -> Any:
    if streamed:
        result = Runner.run_streamed(agent, "start", **kwargs)
        async for _ in result.stream_events():
            pass
        return result
    return await Runner.run(agent, "start", **kwargs)


@pytest.mark.parametrize("streamed", [False, True], ids=["awaited", "streamed"])
async def test_selects_model_after_failed_tool(streamed: bool) -> None:
    context = {"failed": False}

    @function_tool
    def run_tests() -> str:
        context["failed"] = True
        return "tests failed"

    model_a = ScriptedModel([[function_call("run_tests", "{}", call_id="tests-1")]])
    model_b = ScriptedModel([[assistant_message("fixed")]])
    provider = RecordingProvider({"A": model_a, "B": model_b})
    agent = Agent(name="worker", model="A", tools=[run_tests])
    seen = []

    def select(data):
        assert isinstance(data, ModelSelectionData)
        assert data.context is not None
        seen.append((data.agent_name, data.model_name, data.turn_number, data.context))
        return "B" if data.context["failed"] else None

    config = RunConfig(model_provider=provider, model_selector=select, tracing_disabled=True)
    result = await run_agent(streamed, agent, context=context, run_config=config)
    assert result.final_output == "fixed"
    assert provider.selections == ["A", "B"]
    assert agent.model == "A" and config.model is None
    assert [(name, model, turn) for name, model, turn, _ in seen] == [
        ("worker", "A", 1),
        ("worker", "A", 2),
    ]
    assert all(raw_context is context for _, _, _, raw_context in seen)
    assert len(model_a.calls) == len(model_b.calls) == 1


@pytest.mark.parametrize("streamed", [False, True])
async def test_none_restores_agent_name_on_next_turn(streamed: bool) -> None:
    @function_tool
    def advance() -> str:
        return "next"

    model_a = ScriptedModel(
        [[function_call("advance", "{}", call_id="first")], [assistant_message("done")]]
    )
    model_b = ScriptedModel([[function_call("advance", "{}", call_id="second")]])
    provider = RecordingProvider({"A": model_a, "B": model_b})
    turns = []

    async def select(data):
        turns.append(data.turn_number)
        return "B" if data.turn_number == 2 else None

    agent = Agent(name="worker", model="A", tools=[advance])
    result = await run_agent(
        streamed,
        agent,
        run_config=RunConfig(model_provider=provider, model_selector=select, tracing_disabled=True),
    )
    assert result.final_output == "done"
    assert provider.selections == ["A", "B", "A"]
    assert turns == [1, 2, 3] and agent.model == "A"


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("pin", ["run-name", "run-instance", "agent-instance"])
async def test_explicit_model_bindings_bypass_selector(streamed: bool, pin: str) -> None:
    model = ScriptedModel([[assistant_message("pinned")]])
    provider = RecordingProvider({"pinned": model})

    def select(data):
        pytest.fail("An explicit model binding must bypass selection.")

    agent = Agent(name="worker", model=model if pin == "agent-instance" else "A")
    config = RunConfig(
        model="pinned" if pin == "run-name" else model if pin == "run-instance" else None,
        model_provider=provider,
        model_selector=select,
        tracing_disabled=True,
    )
    result = await run_agent(streamed, agent, run_config=config)
    assert result.final_output == "pinned"
    assert provider.selections == (["pinned"] if pin == "run-name" else [])
    assert len(model.calls) == 1


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("explicit_settings", [False, True])
async def test_selected_name_controls_defaults_without_overriding_settings(
    streamed: bool, explicit_settings: bool
) -> None:
    model = ScriptedModel([[assistant_message("done")]])
    provider = RecordingProvider({"gpt-5": model})
    agent = Agent(
        name="worker",
        model="gpt-4.1",
        **({"model_settings": ModelSettings(temperature=0.4)} if explicit_settings else {}),
    )
    config = RunConfig(
        model_provider=provider,
        model_selector=lambda data: "gpt-5",
        model_settings=ModelSettings(max_tokens=123),
        tracing_disabled=True,
    )
    result = await run_agent(streamed, agent, run_config=config)
    assert result.final_output == "done" and provider.selections == ["gpt-5"]
    settings = model.calls[0].model_settings
    assert settings.max_tokens == 123
    if explicit_settings:
        assert settings.temperature == 0.4 and settings.reasoning is None
    else:
        assert settings.reasoning is not None and settings.reasoning.effort == "low"
    assert agent.model == "gpt-4.1"


@pytest.mark.parametrize("streamed", [False, True])
async def test_retry_reuses_selection_and_request(streamed: bool) -> None:
    model = ScriptedModel([RuntimeError("transient"), [assistant_message("retried")]])
    provider = RecordingProvider({"B": model})
    turns = []

    def select(data):
        turns.append(data.turn_number)
        return "B"

    result = await run_agent(
        streamed,
        Agent(name="worker", model="A"),
        run_config=RunConfig(
            model_provider=provider,
            model_selector=select,
            model_settings=ModelSettings(
                retry=ModelRetrySettings(
                    max_retries=1, backoff={"initial_delay": 0}, policy=lambda ctx: True
                )
            ),
            tracing_disabled=True,
        ),
    )
    assert result.final_output == "retried"
    assert turns == [1] and provider.selections == ["B"]
    assert len(model.calls) == 2
    assert model.calls[0].input == model.calls[1].input
    assert model.calls[0].model_settings == model.calls[1].model_settings


@pytest.mark.parametrize("streamed", [False, True])
async def test_handoff_selects_for_receiving_agent(streamed: bool) -> None:
    first = ScriptedModel([[function_call("transfer_to_destination", "{}", call_id="handoff")]])
    second = ScriptedModel([[assistant_message("destination answer")]])
    provider = RecordingProvider({"A": first, "B": second})
    destination = Agent(name="destination", model="destination-base")
    agent = Agent(name="source", model="A", handoffs=[destination])
    seen = []

    def select(data):
        seen.append((data.agent_name, data.model_name, data.turn_number))
        return "B" if data.agent_name == "destination" else None

    result = await run_agent(
        streamed,
        agent,
        run_config=RunConfig(model_provider=provider, model_selector=select, tracing_disabled=True),
    )
    assert result.final_output == "destination answer"
    assert provider.selections == ["A", "B"]
    assert seen == [("source", "A", 1), ("destination", "destination-base", 2)]
    assert destination.model == "destination-base"


@pytest.mark.parametrize("streamed", [False, True])
async def test_shared_agent_keeps_concurrent_selections_local(streamed: bool) -> None:
    agent = Agent(name="shared", model="A")
    models = {name: ScriptedModel([[assistant_message(name)]]) for name in ("B", "C")}
    provider = RecordingProvider(models)
    entered = {name: asyncio.Event() for name in models}
    release = {name: asyncio.Event() for name in models}

    async def select(data):
        name = data.context["selected"]
        assert data.model_name == "A" and data.turn_number == 1
        entered[name].set()
        await release[name].wait()
        return name

    config = RunConfig(model_provider=provider, model_selector=select, tracing_disabled=True)
    tasks = {
        name: asyncio.create_task(
            run_agent(streamed, agent, context={"selected": name}, run_config=config)
        )
        for name in models
    }
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
        release["C"].set()
        result_c = await asyncio.wait_for(tasks["C"], 2)
        assert result_c.final_output == "C" and not tasks["B"].done()
        release["B"].set()
        assert (await asyncio.wait_for(tasks["B"], 2)).final_output == "B"
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
    assert provider.selections == ["C", "B"]
    assert agent.model == "A" and config.model is None


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("invalid", ["", 42])
async def test_invalid_selection_fails_before_provider_lookup(streamed: bool, invalid: Any) -> None:
    provider = RecordingProvider({})
    with pytest.raises(UserError, match="model_selector|model name|model selection"):
        await run_agent(
            streamed,
            Agent(name="worker", model="A"),
            run_config=RunConfig(
                model_provider=provider,
                model_selector=lambda data: invalid,
                tracing_disabled=True,
            ),
        )
    assert provider.selections == []


@pytest.mark.parametrize("streamed", [False, True])
async def test_selector_failure_never_falls_back(streamed: bool) -> None:
    provider = RecordingProvider({})
    failure = RuntimeError("selection failed")

    async def select(data):
        raise failure

    with pytest.raises(RuntimeError) as caught:
        await run_agent(
            streamed,
            Agent(name="worker", model="A"),
            run_config=RunConfig(
                model_provider=provider, model_selector=select, tracing_disabled=True
            ),
        )
    assert caught.value is failure and provider.selections == []


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("with_selector", [False, True])
async def test_selection_precedes_filter_without_reordering_existing_callbacks(
    streamed: bool, with_selector: bool
) -> None:
    events = []
    model = ScriptedModel([[assistant_message("done")]])

    class Provider(ModelProvider):
        def get_model(self, model_name: str | None) -> Model:
            events.append("provider")
            return model

    def filter_input(data):
        events.append("filter")
        return data.model_data

    def select(data):
        events.append("selector")
        return "B"

    result = await run_agent(
        streamed,
        Agent(name="worker", model="A"),
        run_config=RunConfig(
            model_provider=Provider(),
            model_selector=select if with_selector else None,
            call_model_input_filter=filter_input,
            tracing_disabled=True,
        ),
    )
    assert result.final_output == "done"
    existing_order = ["provider", "filter"] if streamed else ["filter", "provider"]
    assert events == (["selector"] if with_selector else []) + existing_order


@pytest.mark.parametrize("streamed", [False, True])
async def test_dictionary_config_accepts_selector(streamed: bool) -> None:
    model = ScriptedModel([[assistant_message("selected")]])
    provider = RecordingProvider({"B": model})
    result = await run_agent(
        streamed,
        Agent(name="worker", model="A"),
        run_config={
            "model_provider": provider,
            "model_selector": lambda data: "B",
            "tracing_disabled": True,
        },
    )
    assert result.final_output == "selected" and provider.selections == ["B"]


@pytest.mark.parametrize("streamed", [False, True])
async def test_nested_provider_switch_fails_before_model_lookup(streamed: bool) -> None:
    first = RecordingProvider({})
    second = RecordingProvider({"B": ScriptedModel([[assistant_message("wrong provider")]])})
    leaves = MultiProviderMap()
    leaves.add_provider("first", first)
    leaves.add_provider("second", second)
    groups = MultiProviderMap()
    groups.add_provider("group", MultiProvider(provider_map=leaves))

    with pytest.raises(UserError, match="cannot switch providers"):
        await run_agent(
            streamed,
            Agent(name="worker", model="group/first/A"),
            run_config=RunConfig(
                model_provider=MultiProvider(provider_map=groups),
                model_selector=lambda _: "group/second/B",
                tracing_disabled=True,
            ),
        )
    assert first.selections == second.selections == []


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("nested", [False, True])
async def test_same_provider_alias_keeps_original_route(streamed: bool, nested: bool) -> None:
    model = ScriptedModel([[assistant_message("same provider")]])
    inner = RecordingProvider({"B": model})
    mapping = MultiProviderMap()
    mapping.add_provider("primary", inner)
    mapping.add_provider("alias", inner)
    provider = MultiProvider(provider_map=mapping)
    configured, selected = "primary/A", "alias/B"
    if nested:
        groups = MultiProviderMap()
        groups.add_provider("group", provider)
        groups.add_provider("other-name", provider)
        provider = MultiProvider(provider_map=groups)
        configured, selected = f"group/{configured}", f"other-name/{selected}"
    result = await run_agent(
        streamed,
        Agent(name="worker", model=configured),
        run_config=RunConfig(
            model_provider=provider,
            model_selector=lambda data: selected,
            tracing_disabled=True,
        ),
    )
    assert result.final_output == "same provider"
    assert inner.selections == ["B"] and len(model.calls) == 1


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.allow_call_model_methods
async def test_selected_model_cannot_persist_guarded_output(streamed: bool) -> None:
    # Use both real adapters so a request reaching the network boundary fails the test.
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Guarded output could be stored by {request.url.path}")

    @output_guardrail
    def reject_output(context: Any, agent: Agent[Any], output: str) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as http_client:
        client = AsyncOpenAI(api_key="test", http_client=http_client)
        provider = RecordingProvider(
            {
                "chat": OpenAIChatCompletionsModel("chat", client),
                "responses": OpenAIResponsesModel("responses", client),
            }
        )
        with pytest.raises(UserError, match="server-managed conversation history"):
            await run_agent(
                streamed,
                Agent(name="worker", model="chat", output_guardrails=[reject_output]),
                conversation_id="conv_test",
                run_config=RunConfig(
                    model_provider=provider,
                    model_selector=lambda _: "responses",
                    tracing_disabled=True,
                ),
            )
        assert provider.selections.count("responses") == 1


@pytest.mark.parametrize("streamed", [False, True])
async def test_provider_switch_fails_before_fallback_construction(streamed: bool) -> None:
    # The double detects optional-provider construction before any model can receive data.
    class Provider(MultiProvider):
        def _create_fallback_provider(self, prefix: str) -> ModelProvider:
            pytest.fail("Rejected selection must not construct a fallback provider.")

    with pytest.raises(UserError, match="provider|route"):
        await run_agent(
            streamed,
            Agent(name="worker", model="openai/gpt-4.1"),
            run_config=RunConfig(
                model_provider=Provider(),
                model_selector=lambda data: "litellm/other-model",
                tracing_disabled=True,
            ),
        )


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("handoff", [False, True])
async def test_sandbox_selection_fails_before_session_start(
    streamed: bool, handoff: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = scripted_sandbox_session([])

    async def unexpected_start():
        pytest.fail("Unsupported model selection must not start the sandbox session.")

    monkeypatch.setattr(session, "start", unexpected_start)
    target = SandboxAgent(name="workspace", model="sandbox-base", capabilities=[])
    source = ScriptedModel(
        [[function_call("transfer_to_workspace", "{}", call_id="sandbox-handoff")]]
    )
    provider = RecordingProvider({"source": source})
    seen = []

    def select(data):
        seen.append(data.agent_name)
        return None

    agent = Agent(name="source", model="source", handoffs=[target]) if handoff else target
    with pytest.raises(UserError, match="SandboxAgent|sandbox"):
        await run_agent(
            streamed,
            agent,
            run_config=RunConfig(
                model_provider=provider,
                model_selector=select,
                sandbox={"session": session},
                tracing_disabled=True,
            ),
        )
    assert seen == (["source"] if handoff else [])
    assert provider.selections == (["source"] if handoff else [])
    assert not session.calls and not await session.running()


@pytest.mark.parametrize("streamed", [False, True])
async def test_redacted_model_identity_never_reaches_provider(streamed: bool) -> None:
    provider = RecordingProvider({})
    with pytest.raises(UserError) as caught:
        await run_agent(
            streamed,
            Agent(name="worker", model="A"),
            run_config=RunConfig(
                model_provider=provider,
                model_selector=lambda _: "private-name",
                redact_text=lambda text: text.replace("private-name", "[removed]"),
                tracing_disabled=True,
            ),
        )
    assert provider.selections == []
    assert "private-name" not in str(caught.value)
