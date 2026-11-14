"""Native declaration capture without provider dispatch or template expansion."""

from typing import Any

import pytest

from _agents_codex._hosted_prompts import (
    _capture_hosted_prompt_reference,
    _capture_hosted_prompt_source,
    _hosted_prompt_controls,
)


def test_hosted_prompt_reference_detaches_nested_file_variables() -> None:
    source = {
        "id": "pmpt_captured",
        "version": "3",
        "variables": {"name": "Ada", "file": {"type": "input_file", "file_id": "file_1"}},
    }
    captured = _capture_hosted_prompt_reference(source)
    source["variables"]["file"]["file_id"] = "file_changed"  # type: ignore[index]
    assert captured == {
        "id": "pmpt_captured",
        "version": "3",
        "variables": {"name": "Ada", "file": {"type": "input_file", "file_id": "file_1"}},
    }


def test_hosted_prompt_source_preserves_factory_without_invocation() -> None:
    def factory(context: Any) -> Any:
        raise AssertionError("capture must not invoke the prompt factory")

    assert _capture_hosted_prompt_source(factory) is factory
    assert _capture_hosted_prompt_source(None) is None
    assert _capture_hosted_prompt_source({"id": "pmpt_static"}) == {"id": "pmpt_static"}


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {},
        {"id": ""},
        {"id": "   "},
        {"id": 3},
        {"id": "pmpt_1", "version": None},
        {"id": "pmpt_1", "variables": []},
        {"id": "pmpt_1", "variables": {"value": float("nan")}},
        {"id": "pmpt_1", "instructions": "unrecognized"},
    ],
)
def test_hosted_prompt_reference_rejects_malformed_values(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _capture_hosted_prompt_reference(value)


def test_hosted_prompt_omission_preserves_explicit_empty_selections() -> None:
    source = {"id": "pmpt_defaults"}
    assert _hosted_prompt_controls(source, tools_provided=False) == {
        "reference": source,
        "omit_model": True,
        "omit_instructions": True,
        "omit_tools": True,
    }
    assert _hosted_prompt_controls(
        source, model="explicit-model", instructions="", tools_provided=True
    ) == {
        "reference": source,
        "omit_model": False,
        "omit_instructions": False,
        "omit_tools": False,
    }


def test_hosted_prompt_factory_keeps_resolution_at_admitted_boundary() -> None:
    def factory(context: Any) -> Any:
        raise AssertionError("not admitted")

    assert _hosted_prompt_controls(factory, tools_provided=False) == {
        "omit_model": True,
        "omit_instructions": True,
        "omit_tools": True,
    }
    assert _hosted_prompt_controls(None, tools_provided=False) is None


def test_hosted_prompt_raw_reference_conflict_preserves_explicit_data() -> None:
    with pytest.raises(ValueError, match="conflicts"):
        _hosted_prompt_controls({"id": "pmpt_1"}, tools_provided=False, extra_body={"prompt": None})


def test_agent_prompt_instructions_and_explicit_empty_tools() -> None:
    from _agents_codex.agent import Agent
    from _agents_codex.session import _capture_opening_agent

    with pytest.raises(TypeError, match="instructions are required"):
        Agent()  # type: ignore[call-overload]
    source = {"id": "pmpt_default", "variables": {"name": "captured"}}
    agent = Agent(hosted_prompt=source)
    source["variables"]["name"] = "mutated"
    assert agent.instructions == ""
    assert agent.hosted_prompt == {"id": "pmpt_default", "variables": {"name": "captured"}}
    captured = _capture_opening_agent(
        agent, provider=None, model=None, model_settings=None, tool_selection=None
    )
    assert not captured._hosted_instructions_provided
    assert captured.instructions == ""
    assert not captured._hosted_tools_provided
    assert captured.hosted_prompt == agent.hosted_prompt
    explicit = _capture_opening_agent(
        agent, provider=None, model=None, model_settings=None, tool_selection=[]
    )
    assert explicit._hosted_tools_provided
    assert Agent(instructions="", hosted_prompt={"id": "pmpt_empty"}, tools=[]).instructions == ""


@pytest.mark.parametrize("mutation", ["id", "nested", "array"])
def test_agent_static_prompt_reads_cannot_change_later_capture(mutation: str) -> None:
    from _agents_codex.agent import Agent
    from _agents_codex.session import _capture_opening_agent

    expected = {
        "id": "pmpt_immutable",
        "variables": {"nested": {"value": "captured"}, "array": ["captured"]},
    }
    agent = Agent(hosted_prompt=expected)
    exposed: Any = agent.hosted_prompt
    try:
        if mutation == "id":
            exposed["id"] = "pmpt_changed"
        elif mutation == "nested":
            exposed["variables"]["nested"]["value"] = "changed"
        else:
            exposed["variables"]["array"].append("changed")
    except TypeError:
        pass
    for _ in range(2):
        captured = _capture_opening_agent(
            agent, provider=None, model=None, model_settings=None, tool_selection=None
        )
        assert captured.hosted_prompt == expected
    assert agent.hosted_prompt == expected


@pytest.mark.parametrize("prompt", [None, {"id": "pmpt_other"}])
def test_agent_validate_rejects_conflicting_prompt_sources(prompt: Any) -> None:
    from _agents_codex.agent import Agent

    agent = Agent(
        hosted_prompt={"id": "pmpt_reference"},
        model_settings={"extra_body": {"prompt": prompt}},
    )
    with pytest.raises(ValueError, match="hosted_prompt conflicts"):
        agent.validate()


def test_hosted_instruction_string_reads_keep_presence_through_repeated_capture() -> None:
    from dataclasses import FrozenInstanceError

    from _agents_codex.agent import Agent
    from _agents_codex.session import _capture_opening_agent

    agent = Agent(hosted_prompt={"id": "pmpt_default"})
    assert agent.instructions == ""
    assert vars(agent)["instructions"] == ""
    for _ in range(2):
        agent = _capture_opening_agent(
            agent, provider=None, model=None, model_settings=None, tool_selection=None
        )
        assert agent.instructions == ""
        assert not agent._hosted_instructions_provided
    with pytest.raises(FrozenInstanceError):
        agent.instructions = ""
    explicit = Agent(instructions="", hosted_prompt={"id": "pmpt_explicit"})
    assert explicit._hosted_instructions_provided


@pytest.mark.parametrize("mode", ["sync", "async", "awaitable"])
async def test_prompt_composition_keeps_sync_callbacks_in_the_owned_worker(mode, monkeypatch):
    import asyncio
    import json
    import threading
    from types import SimpleNamespace

    import _agents_codex.session as implementation
    from agents import Agent

    loop_thread = threading.get_ident()
    reference = {"id": "pmpt_worker", "variables": {"value": "captured"}}
    called = []

    def sync_source(request):
        called.append(("source", threading.get_ident()))
        return reference

    async def async_source(request):
        called.append(("source", threading.get_ident()))
        await asyncio.sleep(0)
        return reference

    def awaitable_source(request):
        return async_source(request)

    def transform(request):
        called.append(("transform", threading.get_ident()))
        reference["variables"]["value"] = "mutated"

    source = {"sync": sync_source, "async": async_source, "awaitable": awaitable_source}[mode]
    agent = Agent(hosted_prompt=source, transform_context=transform)
    replies = []
    native = SimpleNamespace(
        _check_step=lambda *args: None,
        _reply_context=lambda step, payload, *args: replies.append(json.loads(payload)),
    )

    async def own(state, message, prepare, reply, reject):
        callback, request = prepare(lambda: not state.stopping and not state.failure.done())
        reply(await callback(request))

    owner = SimpleNamespace(
        id="session",
        _context=None,
        _context_transform_purpose="turn",
        _native=native,
        _agent_for_message=lambda message: agent,
        _original_agent_for_message=lambda message: agent,
        _run_owned_context_callback=own,
    )
    state = SimpleNamespace(stopping=False, failure=asyncio.get_running_loop().create_future())
    monkeypatch.setattr(implementation, "_native_callback_cancelled", lambda state: False)
    await implementation.Session._transform_context_body(
        owner,
        state,
        {
            "hosted_prompt": True,
            "hosted_prompt_omissions": {
                "omit_model": True,
                "omit_instructions": True,
                "omit_tools": True,
            },
            "step_id": "step",
            "thread": "thread",
            "instance": None,
            "turn_id": "turn",
            "model": "hint",
            "purpose": "turn",
            "items": [],
        },
    )
    assert replies[0]["hosted_prompt"]["variables"] == {"value": "captured"}
    assert called[-1][0] == "transform" and called[-1][1] != loop_thread
    assert (called[0][1] == loop_thread) == (mode != "sync")


@pytest.mark.parametrize("override", [None, "explicit-model"])
async def test_borrowed_responses_keeps_prompt_owned_model_omitted(override):
    import json

    import httpx
    from openai import AsyncOpenAI

    from agents import ModelRequest, ResponsesModel

    bodies = []

    async def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_prompt",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_prompt",
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "prompt result", "annotations": []}
                        ],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            },
        )

    async with AsyncOpenAI(
        api_key="synthetic-prompt-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        body = {"prompt": {"id": "pmpt_owned"}, "input": []}
        result = await ResponsesModel(client, model=override).complete(
            ModelRequest(body=body, context=None, session_id="session", turn_id="turn")
        )
        assert not client.is_closed()
        assert result.output[0]["content"][0]["text"] == "prompt result"
        assert body == {"prompt": {"id": "pmpt_owned"}, "input": []}
    assert len(bodies) == 1
    assert bodies[0]["prompt"] == {"id": "pmpt_owned"}
    if override is None:
        assert "model" not in bodies[0]
    else:
        assert bodies[0]["model"] == override
