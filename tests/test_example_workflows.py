from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, cast

import pytest
from openai.types.responses import ResponseTextDeltaEvent
from pydantic import BaseModel

from agents import (
    Agent,
    AgentBase,
    AgentToolStreamEvent,
    AgentUpdatedStreamEvent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    ItemHelpers,
    ModelSettings,
    OutputGuardrailTripwireTriggered,
    RawResponsesStreamEvent,
    RunContextWrapper,
    Runner,
    input_guardrail,
    output_guardrail,
)
from agents.agent import ToolsToFinalOutputResult
from agents.items import TResponseInputItem
from agents.tool import FunctionTool, FunctionToolResult, function_tool

from .fake_model import FakeModel
from .test_responses import (
    get_final_output_message,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_input_item,
    get_text_message,
)


@dataclass
class EvaluationFeedback:
    feedback: str
    score: Literal["pass", "needs_improvement"]


@dataclass
class OutlineCheckerOutput:
    good_quality: bool
    is_scifi: bool


@pytest.mark.asyncio
async def test_llm_as_judge_loop_handles_dataclass_feedback() -> None:
    """Mimics the llm_as_a_judge example: loop until the evaluator passes the outline."""
    outline_model = FakeModel()
    outline_model.add_multiple_turn_outputs(
        [
            [get_text_message("Outline v1")],
            [get_text_message("Outline v2")],
        ]
    )

    judge_model = FakeModel()
    judge_model.add_multiple_turn_outputs(
        [
            [
                get_final_output_message(
                    json.dumps(
                        {
                            "response": {
                                "feedback": "Add more suspense",
                                "score": "needs_improvement",
                            }
                        }
                    )
                )
            ],
            [
                get_final_output_message(
                    json.dumps({"response": {"feedback": "Looks good", "score": "pass"}})
                )
            ],
        ]
    )

    outline_agent = Agent(name="outline", model=outline_model)
    judge_agent = Agent(name="judge", model=judge_model, output_type=EvaluationFeedback)

    conversation: list[TResponseInputItem] = [get_text_input_item("Tell me a space story")]
    latest_outline: str | None = None

    for expected_outline, expected_score in [
        ("Outline v1", "needs_improvement"),
        ("Outline v2", "pass"),
    ]:
        outline_result = await Runner.run(outline_agent, conversation)
        latest_outline = ItemHelpers.text_message_outputs(outline_result.new_items)
        assert latest_outline == expected_outline

        conversation = outline_result.to_input_list()

        judge_result = await Runner.run(judge_agent, conversation)
        feedback = judge_result.final_output
        assert isinstance(feedback, EvaluationFeedback)
        assert feedback.score == expected_score

        if feedback.score == "pass":
            break

        conversation.append({"content": f"Feedback: {feedback.feedback}", "role": "user"})

    assert latest_outline == "Outline v2"
    assert len(conversation) == 4
    assert judge_model.last_turn_args["input"] == conversation


@pytest.mark.asyncio
async def test_parallel_translation_flow_reuses_runner_outputs() -> None:
    """Covers the parallelization example by feeding multiple translations into a picker agent."""
    translation_model = FakeModel()
    translation_model.add_multiple_turn_outputs(
        [
            [get_text_message("Uno")],
            [get_text_message("Dos")],
            [get_text_message("Tres")],
        ]
    )
    spanish_agent = Agent(name="spanish_agent", model=translation_model)

    picker_model = FakeModel()
    picker_model.set_next_output([get_text_message("Pick: Dos")])
    picker_agent = Agent(name="picker", model=picker_model)

    translations: list[str] = []
    for _ in range(3):
        result = await Runner.run(spanish_agent, input="Hello")
        translations.append(ItemHelpers.text_message_outputs(result.new_items))

    combined = "\n\n".join(translations)
    picker_result = await Runner.run(
        picker_agent,
        input=f"Input: Hello\n\nTranslations:\n{combined}",
    )

    assert translations == ["Uno", "Dos", "Tres"]
    assert picker_result.final_output == "Pick: Dos"
    assert picker_model.last_turn_args["input"] == [
        {"content": f"Input: Hello\n\nTranslations:\n{combined}", "role": "user"}
    ]


@pytest.mark.asyncio
async def test_deterministic_story_flow_stops_when_checker_blocks() -> None:
    """Mimics deterministic flow: stop early when quality gate fails."""
    outline_model = FakeModel()
    outline_model.set_next_output([get_text_message("Outline v1")])
    checker_model = FakeModel()
    checker_model.set_next_output(
        [
            get_final_output_message(
                json.dumps({"response": {"good_quality": False, "is_scifi": True}})
            )
        ]
    )
    story_model = FakeModel()
    story_model.set_next_output(RuntimeError("story should not run"))

    outline_agent = Agent(name="outline", model=outline_model)
    checker_agent = Agent(
        name="checker",
        model=checker_model,
        output_type=OutlineCheckerOutput,
    )
    story_agent = Agent(name="story", model=story_model)

    inputs: list[TResponseInputItem] = [get_text_input_item("Sci-fi please")]
    outline_result = await Runner.run(outline_agent, inputs)
    inputs = outline_result.to_input_list()

    checker_result = await Runner.run(checker_agent, inputs)
    decision = checker_result.final_output

    assert isinstance(decision, OutlineCheckerOutput)
    assert decision.good_quality is False
    assert decision.is_scifi is True
    if decision.good_quality and decision.is_scifi:
        await Runner.run(story_agent, outline_result.final_output)
    assert story_model.first_turn_args is None, "story agent should never be invoked when gated"


@pytest.mark.asyncio
async def test_deterministic_story_flow_runs_story_on_pass() -> None:
    """Mimics deterministic flow: run full path when checker approves."""
    outline_model = FakeModel()
    outline_model.set_next_output([get_text_message("Outline ready")])
    checker_model = FakeModel()
    checker_model.set_next_output(
        [
            get_final_output_message(
                json.dumps({"response": {"good_quality": True, "is_scifi": True}})
            )
        ]
    )
    story_model = FakeModel()
    story_model.set_next_output([get_text_message("Final story")])

    outline_agent = Agent(name="outline", model=outline_model)
    checker_agent = Agent(
        name="checker",
        model=checker_model,
        output_type=OutlineCheckerOutput,
    )
    story_agent = Agent(name="story", model=story_model)

    inputs: list[TResponseInputItem] = [get_text_input_item("Sci-fi please")]
    outline_result = await Runner.run(outline_agent, inputs)
    inputs = outline_result.to_input_list()

    checker_result = await Runner.run(checker_agent, inputs)
    decision = checker_result.final_output
    assert isinstance(decision, OutlineCheckerOutput)
    assert decision.good_quality is True
    assert decision.is_scifi is True

    story_result = await Runner.run(story_agent, outline_result.final_output)
    assert story_result.final_output == "Final story"
    assert story_model.last_turn_args["input"] == [{"content": "Outline ready", "role": "user"}]


@pytest.mark.asyncio
async def test_routing_stream_emits_text_and_updates_inputs() -> None:
    """Mimics routing example stream: text deltas flow through and input history updates."""
    model = FakeModel()
    model.set_next_output([get_text_message("Bonjour")])
    triage_agent = Agent(name="triage_agent", model=model)

    streamed = Runner.run_streamed(triage_agent, input="Salut")

    deltas: list[str] = []
    async for event in streamed.stream_events():
        if isinstance(event, RawResponsesStreamEvent) and isinstance(
            event.data, ResponseTextDeltaEvent
        ):
            deltas.append(event.data.delta)

    assert "".join(deltas) == "Bonjour"
    assert streamed.final_output == "Bonjour"
    assert len(streamed.new_items) == 1
    input_list = streamed.to_input_list()
    assert len(input_list) == 2
    assert input_list[0] == {"content": "Salut", "role": "user"}
    assistant_item = input_list[1]
    assert isinstance(assistant_item, dict)
    assert assistant_item.get("role") == "assistant"
    assert assistant_item.get("type") == "message"
    content: Any = assistant_item.get("content")
    assert isinstance(content, list)
    first_content = content[0]
    assert isinstance(first_content, dict)
    assert first_content.get("text") == "Bonjour"


class MathHomeworkOutput(BaseModel):
    reasoning: str
    is_math_homework: bool


@pytest.mark.asyncio
async def test_input_guardrail_agent_trips_and_returns_info() -> None:
    """Mimics math guardrail example: guardrail agent runs and trips before main agent completes."""
    guardrail_model = FakeModel()
    guardrail_model.set_next_output(
        [
            get_final_output_message(
                json.dumps({"reasoning": "math detected", "is_math_homework": True})
            )
        ]
    )
    guardrail_agent = Agent(name="guardrail", model=guardrail_model, output_type=MathHomeworkOutput)

    @input_guardrail
    async def math_guardrail(
        context: RunContextWrapper[None], agent: Agent, input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        result = await Runner.run(guardrail_agent, input, context=context.context)
        output = result.final_output_as(MathHomeworkOutput)
        return GuardrailFunctionOutput(
            output_info=output, tripwire_triggered=output.is_math_homework
        )

    main_model = FakeModel()
    main_model.set_next_output([get_text_message("Should not run")])
    main_agent = Agent(name="main", model=main_model, input_guardrails=[math_guardrail])

    with pytest.raises(InputGuardrailTripwireTriggered) as excinfo:
        await Runner.run(main_agent, "Solve 2x+5=11")

    guardrail_result = excinfo.value.guardrail_result
    assert isinstance(guardrail_result.output.output_info, MathHomeworkOutput)
    assert guardrail_result.output.output_info.is_math_homework is True
    assert guardrail_result.output.output_info.reasoning == "math detected"


class MessageOutput(BaseModel):
    reasoning: str
    response: str
    user_name: str | None


@pytest.mark.asyncio
async def test_output_guardrail_blocks_sensitive_data() -> None:
    """Mimics sensitive data guardrail example: trips when phone number is present."""

    @output_guardrail
    async def sensitive_data_check(
        context: RunContextWrapper, agent: Agent, output: MessageOutput
    ) -> GuardrailFunctionOutput:
        contains_phone = "650" in output.response or "650" in output.reasoning
        return GuardrailFunctionOutput(
            output_info={"contains_phone": contains_phone},
            tripwire_triggered=contains_phone,
        )

    model = FakeModel()
    model.set_next_output(
        [
            get_final_output_message(
                json.dumps(
                    {
                        "reasoning": "User shared phone 650-123-4567",
                        "response": "Thanks!",
                        "user_name": None,
                    }
                )
            )
        ]
    )
    agent = Agent(
        name="Assistant",
        model=model,
        output_type=MessageOutput,
        output_guardrails=[sensitive_data_check],
    )

    with pytest.raises(OutputGuardrailTripwireTriggered) as excinfo:
        await Runner.run(agent, "My phone number is 650-123-4567.")

    guardrail_output = excinfo.value.guardrail_result.output.output_info
    assert isinstance(guardrail_output, dict)
    assert guardrail_output["contains_phone"] is True


@pytest.mark.asyncio
async def test_streaming_guardrail_style_cancel_after_threshold() -> None:
    """Mimics streaming guardrail example: stop streaming once threshold is reached."""
    model = FakeModel()
    model.set_next_output(
        [
            get_text_message("Chunk1 "),
            get_text_message("Chunk2 "),
            get_text_message("Chunk3"),
        ]
    )
    agent = Agent(name="talkative", model=model)

    streamed = Runner.run_streamed(agent, input="Start")

    deltas: list[str] = []
    async for event in streamed.stream_events():
        if isinstance(event, RawResponsesStreamEvent) and isinstance(
            event.data, ResponseTextDeltaEvent
        ):
            deltas.append(event.data.delta)
            if len("".join(deltas)) >= len("Chunk1 Chunk2 "):
                streamed.cancel(mode="immediate")

    collected = "".join(deltas)
    assert "Chunk1" in collected
    assert "Chunk3" not in collected
    assert streamed.final_output is None
    assert streamed.is_complete is True


@pytest.mark.asyncio
async def test_streaming_cancel_after_turn_allows_turn_completion() -> None:
    """Ensure cancel(after_turn) lets the current turn finish and final_output is populated."""
    model = FakeModel()
    model.set_next_output([get_text_message("Hello"), get_text_message("World")])
    agent = Agent(name="talkative", model=model)

    streamed = Runner.run_streamed(agent, input="Hi")

    deltas: list[str] = []
    async for event in streamed.stream_events():
        if isinstance(event, RawResponsesStreamEvent) and isinstance(
            event.data, ResponseTextDeltaEvent
        ):
            deltas.append(event.data.delta)
            streamed.cancel(mode="after_turn")

    assert "".join(deltas).startswith("Hello")
    assert streamed.final_output == "World"
    assert streamed.is_complete is True
    assert len(streamed.new_items) == 2


@pytest.mark.asyncio
async def test_streaming_handoff_emits_agent_updated_event() -> None:
    """Mimics routing handoff stream: emits AgentUpdatedStreamEvent and switches agent."""
    delegate_model = FakeModel()
    delegate_model.set_next_output([get_text_message("delegate reply")])
    delegate_agent = Agent(name="delegate", model=delegate_model)

    triage_model = FakeModel()
    triage_model.set_next_output(
        [
            get_text_message("triage summary"),
            get_handoff_tool_call(delegate_agent),
        ]
    )
    triage_agent = Agent(name="triage", model=triage_model, handoffs=[delegate_agent])

    streamed = Runner.run_streamed(triage_agent, input="Help me")

    agent_updates: list[AgentUpdatedStreamEvent] = []
    async for event in streamed.stream_events():
        if isinstance(event, AgentUpdatedStreamEvent):
            agent_updates.append(event)

    assert streamed.final_output == "delegate reply"
    assert streamed.last_agent == delegate_agent
    assert len(agent_updates) >= 1
    assert any(update.new_agent == delegate_agent for update in agent_updates)


@pytest.mark.asyncio
async def test_agent_as_tool_streaming_example_collects_events() -> None:
    """Mimics agents_as_tools_streaming example: on_stream receives nested streaming events."""
    billing_agent = Agent(name="billing")

    received: list[AgentToolStreamEvent] = []

    async def on_stream(event: AgentToolStreamEvent) -> None:
        received.append(event)

    billing_tool = cast(
        FunctionTool,
        billing_agent.as_tool(
            tool_name="billing_agent",
            tool_description="Answer billing questions",
            on_stream=on_stream,
        ),
    )

    async def fake_invoke(ctx, input: str) -> str:
        event_payload: AgentToolStreamEvent = {
            "event": RawResponsesStreamEvent(data=cast(Any, {"type": "output_text_delta"})),
            "agent": billing_agent,
            "tool_call": ctx.tool_call,
        }
        await on_stream(event_payload)
        return "Billing: $100"

    billing_tool.on_invoke_tool = fake_invoke

    main_model = FakeModel()
    main_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("billing_agent", json.dumps({"input": "Need bill"}))],
            [get_text_message("Final answer")],
        ]
    )

    main_agent = Agent(
        name="support",
        model=main_model,
        tools=[billing_tool],
        model_settings=ModelSettings(tool_choice="required"),
    )

    result = await Runner.run(main_agent, "How much is my bill?")

    assert result.final_output == "Final answer"
    assert received, "on_stream should capture nested streaming events"
    assert all(event["agent"] == billing_agent for event in received)
    assert all(
        event["tool_call"] and event["tool_call"].name == "billing_agent" for event in received
    )


@pytest.mark.asyncio
async def test_forcing_tool_use_behaviors_align_with_example() -> None:
    """Mimics forcing_tool_use example: default vs first_tool vs custom behaviors."""

    @function_tool
    def get_weather(city: str) -> str:
        return f"{city}: Sunny"

    # default: run_llm_again -> model responds after tool call
    default_model = FakeModel()
    default_model.add_multiple_turn_outputs(
        [
            [
                get_text_message("Tool call coming"),
                get_function_tool_call("get_weather", json.dumps({"city": "Tokyo"})),
            ],
            [get_text_message("Done after tool")],
        ]
    )

    default_agent = Agent(
        name="default",
        model=default_model,
        tools=[get_weather],
        tool_use_behavior="run_llm_again",
        model_settings=ModelSettings(tool_choice=None),
    )

    default_result = await Runner.run(default_agent, "Weather?")
    assert default_result.final_output == "Done after tool"
    assert len(default_result.raw_responses) == 2

    # first_tool: stop_on_first_tool -> final output from first tool result
    first_model = FakeModel()
    first_model.set_next_output(
        [
            get_text_message("Tool call coming"),
            get_function_tool_call("get_weather", json.dumps({"city": "Paris"})),
        ]
    )

    first_agent = Agent(
        name="first",
        model=first_model,
        tools=[get_weather],
        tool_use_behavior="stop_on_first_tool",
        model_settings=ModelSettings(tool_choice="required"),
    )

    first_result = await Runner.run(first_agent, "Weather?")
    assert first_result.final_output == "Paris: Sunny"
    assert len(first_result.raw_responses) == 1

    # custom: uses custom tool_use_behavior to format output, still with required tool choice
    async def custom_tool_use_behavior(
        context: RunContextWrapper[Any], results: list[FunctionToolResult]
    ) -> ToolsToFinalOutputResult:
        return ToolsToFinalOutputResult(
            is_final_output=True, final_output=f"Custom:{results[0].output}"
        )

    custom_model = FakeModel()
    custom_model.set_next_output(
        [
            get_text_message("Tool call coming"),
            get_function_tool_call("get_weather", json.dumps({"city": "Berlin"})),
        ]
    )

    custom_agent = Agent(
        name="custom",
        model=custom_model,
        tools=[get_weather],
        tool_use_behavior=custom_tool_use_behavior,
        model_settings=ModelSettings(tool_choice="required"),
    )

    custom_result = await Runner.run(custom_agent, "Weather?")
    assert custom_result.final_output == "Custom:Berlin: Sunny"


@pytest.mark.asyncio
async def test_routing_multi_turn_continues_with_handoff_agent() -> None:
    """Mimics routing example multi-turn: first handoff, then continue with delegated agent."""
    delegate_model = FakeModel()
    delegate_model.set_next_output([get_text_message("Bonjour")])
    delegate_agent = Agent(name="delegate", model=delegate_model)

    triage_model = FakeModel()
    triage_model.add_multiple_turn_outputs(
        [
            [get_handoff_tool_call(delegate_agent)],
            [get_text_message("handoff completed")],
        ]
    )
    triage_agent = Agent(name="triage", model=triage_model, handoffs=[delegate_agent])

    first_result = await Runner.run(triage_agent, "Help me in French")
    assert first_result.final_output == "Bonjour"
    assert first_result.last_agent == delegate_agent

    # Next user turn continues with delegate.
    delegate_model.set_next_output([get_text_message("Encore?")])
    follow_up_input = first_result.to_input_list()
    follow_up_input.append({"role": "user", "content": "Encore!"})

    second_result = await Runner.run(delegate_agent, follow_up_input)
    assert second_result.final_output == "Encore?"
    assert delegate_model.last_turn_args["input"] == follow_up_input


@pytest.mark.asyncio
async def test_agents_as_tools_conditional_enabling_matches_preference() -> None:
    """Mimics agents_as_tools_conditional example: only enabled tools are invoked per preference."""

    class AppContext(BaseModel):
        language_preference: str

    def french_spanish_enabled(ctx: RunContextWrapper[AppContext], _agent: AgentBase) -> bool:
        return ctx.context.language_preference in ["french_spanish", "european"]

    def european_enabled(ctx: RunContextWrapper[AppContext], _agent: AgentBase) -> bool:
        return ctx.context.language_preference == "european"

    scenarios = [
        ("spanish_only", {"respond_spanish"}),
        ("french_spanish", {"respond_spanish", "respond_french"}),
        ("european", {"respond_spanish", "respond_french", "respond_italian"}),
    ]

    for preference, expected_tools in scenarios:
        spanish_model = FakeModel()
        spanish_model.set_next_output([get_text_message("ES hola")])
        spanish_agent = Agent(name="spanish", model=spanish_model)

        french_model = FakeModel()
        french_model.set_next_output([get_text_message("FR bonjour")])
        french_agent = Agent(name="french", model=french_model)

        italian_model = FakeModel()
        italian_model.set_next_output([get_text_message("IT ciao")])
        italian_agent = Agent(name="italian", model=italian_model)

        orchestrator_model = FakeModel()
        # Build tool calls only for expected tools to avoid missing-tool errors.
        tool_calls = [
            get_function_tool_call(tool_name, json.dumps({"input": "Hi"}))
            for tool_name in sorted(expected_tools)
        ]
        orchestrator_model.add_multiple_turn_outputs([tool_calls, [get_text_message("Done")]])

        context = AppContext(language_preference=preference)

        orchestrator = Agent(
            name="orchestrator",
            model=orchestrator_model,
            tools=[
                spanish_agent.as_tool(
                    tool_name="respond_spanish",
                    tool_description="Spanish",
                    is_enabled=True,
                ),
                french_agent.as_tool(
                    tool_name="respond_french",
                    tool_description="French",
                    is_enabled=french_spanish_enabled,
                ),
                italian_agent.as_tool(
                    tool_name="respond_italian",
                    tool_description="Italian",
                    is_enabled=european_enabled,
                ),
            ],
            model_settings=ModelSettings(tool_choice="required"),
        )

        result = await Runner.run(orchestrator, "Hello", context=context)

        assert result.final_output == "Done"
        assert (
            spanish_model.first_turn_args is not None
            if "respond_spanish" in expected_tools
            else spanish_model.first_turn_args is None
        )
        assert (
            french_model.first_turn_args is not None
            if "respond_french" in expected_tools
            else french_model.first_turn_args is None
        )
        assert (
            italian_model.first_turn_args is not None
            if "respond_italian" in expected_tools
            else italian_model.first_turn_args is None
        )


@pytest.mark.asyncio
async def test_agents_as_tools_orchestrator_runs_multiple_translations() -> None:
    """Orchestrator calls multiple translation agent tools then summarizes."""
    spanish_model = FakeModel()
    spanish_model.set_next_output([get_text_message("ES hola")])
    spanish_agent = Agent(name="spanish", model=spanish_model)

    french_model = FakeModel()
    french_model.set_next_output([get_text_message("FR bonjour")])
    french_agent = Agent(name="french", model=french_model)

    orchestrator_model = FakeModel()
    orchestrator_model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("translate_to_spanish", json.dumps({"input": "Hi"}))],
            [get_function_tool_call("translate_to_french", json.dumps({"input": "Hi"}))],
            [get_text_message("Summary complete")],
        ]
    )

    orchestrator = Agent(
        name="orchestrator",
        model=orchestrator_model,
        tools=[
            spanish_agent.as_tool("translate_to_spanish", "Spanish"),
            french_agent.as_tool("translate_to_french", "French"),
        ],
    )

    result = await Runner.run(orchestrator, "Hi")

    assert result.final_output == "Summary complete"
    assert spanish_model.last_turn_args["input"] == [{"content": "Hi", "role": "user"}]
    assert french_model.last_turn_args["input"] == [{"content": "Hi", "role": "user"}]
    assert len(result.raw_responses) == 3
