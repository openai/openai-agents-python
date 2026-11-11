"""Prompt-owned defaults through the public native request/callback owner."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agents import (
    Agent,
    CustomStorage,
    FunctionTool,
    ModelRequest,
    ModelResponse,
    NativeProvider,
    function_tool,
)
from agents.handoffs import Handoff
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.native_storage_fixtures import MemorySessionStore
from tests.test_native_manual_tools import LookupArgs, next_request


@pytest.mark.parametrize("location", ["thread_settings", "profile"])
async def test_canonical_resume_rejects_saved_prompt_reference(
    responses: ResponsesFixture, location: str
) -> None:
    responses.respond(assistant_output("first"))
    responses.respond(assistant_output("unexpected injected prompt"))
    backend = MemorySessionStore()
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-prompt-key", supports_websockets=False
    ) as provider:
        agent = Agent(
            instructions="Answer.",
            provider=provider,
            handoffs=[Agent(name="Target", instructions="Answer.")]
            if location == "profile"
            else [],
        )
        storage = CustomStorage(backend)
        async with agent.session(storage=storage) as session:
            identity = session.id
            await (await session.submit("first")).get_result()
        document = json.loads(backend._payloads[identity])
        changed = 0
        for row in document["items"]:
            payload = row.get("payload", {})
            profile = (row.get("metadata") or {}).get("sdk_agent_profile")
            if location == "thread_settings" and payload.get("type") == "thread_settings_applied":
                model = payload["thread_settings"]
            elif location == "profile" and isinstance(profile, dict):
                model = profile["model"]
            else:
                continue
            model.setdefault("request_controls", {})["hosted_prompt"] = {
                "reference": {"id": "pmpt_external_saved"},
                "omit_model": True,
                "omit_instructions": True,
                "omit_tools": True,
            }
            changed += 1
        assert changed
        backend._payloads[identity] = json.dumps(document)
        with pytest.raises(Exception, match="Canonical storage failed"):
            async with agent.session(storage=storage, resume=identity) as restored:
                await (await restored.submit("second")).get_result()
    assert len(responses.requests) == 1


@pytest.mark.parametrize("dynamic", [False, True])
async def test_canonical_resume_preserves_current_prompt(
    responses: ResponsesFixture, dynamic: bool
) -> None:
    for _ in range(2):
        responses.respond(assistant_output("current prompt result"))
    reference = {"id": "pmpt_current"}

    def resolve(_context: Any) -> Any:
        return reference

    backend = MemorySessionStore()
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-prompt-key", supports_websockets=False
    ) as provider:
        agent = Agent(
            hosted_prompt=resolve if dynamic else reference,
            provider=provider,
            handoffs=[Agent(name="Target", hosted_prompt=reference)],
        )
        storage = CustomStorage(backend)
        async with agent.session(storage=storage) as session:
            identity = session.id
            await (await session.submit("first")).get_result()
        async with agent.session(storage=storage, resume=identity) as session:
            await (await session.submit("second")).get_result()
    assert len(responses.requests) == 2
    for request in responses.requests:
        assert request["prompt"] == reference
        assert "model" not in request
        assert "instructions" not in request


@pytest.mark.parametrize("explicit", [False, True])
async def test_native_prompt_defaults_and_explicit_empty_overrides(
    responses: ResponsesFixture, explicit: bool
) -> None:
    responses.respond(assistant_output("prompt result"))
    reference = {"id": "pmpt_owned", "variables": {"topic": "captured"}}
    resolutions = 0

    def source(context: Any) -> Any:
        nonlocal resolutions
        resolutions += 1
        return reference

    def transform(context: Any) -> None:
        reference["variables"]["topic"] = "changed after capture"  # type: ignore[index]

    options: dict[str, Any] = (
        {"model": "fixture-explicit", "instructions": "", "tools": []} if explicit else {}
    )
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-prompt-key", supports_websockets=False
    ) as provider:
        agent = Agent(
            provider=provider, hosted_prompt=source, transform_context=transform, **options
        )
        assert (await agent.run("Use current declared prompt.")).final_output == "prompt result"
    assert resolutions == 1
    assert len(responses.requests) == 1
    request = responses.requests[0]
    assert request["prompt"] == {"id": "pmpt_owned", "variables": {"topic": "captured"}}
    if explicit:
        assert request["model"] == "fixture-explicit"
        assert request["instructions"] == ""
        assert request["tools"] == []
    else:
        for field in (
            "model",
            "instructions",
            "tools",
            "reasoning",
            "text",
            "tool_choice",
            "parallel_tool_calls",
        ):
            assert field not in request


async def test_native_static_prompt_public_reads_preserve_later_sessions(
    responses: ResponsesFixture,
) -> None:
    responses.respond(assistant_output("first"))
    responses.respond(assistant_output("second"))
    reference = {
        "id": "pmpt_captured",
        "variables": {"file": {"type": "input_file", "file_id": "file_captured"}},
    }
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-prompt-key", supports_websockets=False
    ) as provider:
        agent = Agent(provider=provider, hosted_prompt=reference)
        exposed: Any = agent.hosted_prompt
        exposed["variables"]["file"]["file_id"] = "file_changed"
        assert (await agent.run("first")).final_output == "first"
        assert (await agent.run("second")).final_output == "second"
    assert len(responses.requests) == 2
    assert all(request["prompt"] == reference for request in responses.requests)


async def test_native_prompt_cold_restore_uses_current_declaration(
    responses: ResponsesFixture,
) -> None:
    for _ in range(2):
        responses.respond(assistant_output("prompt result"))
    current = "pmpt_before"

    def source(context: Any) -> Any:
        return {"id": current}

    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-prompt-key", supports_websockets=False
    ) as provider:
        agent = Agent(hosted_prompt=source, provider=provider)
        async with agent.session() as session:
            await (await session.submit("before restore")).get_result()
            saved = await session.save_state()
        current = "pmpt_current"
        async with agent.session(state=saved) as restored:
            await (await restored.submit("after restore")).get_result()
    assert [request["prompt"]["id"] for request in responses.requests] == [
        "pmpt_before",
        "pmpt_current",
    ]
    assert all("instructions" not in request for request in responses.requests)


@pytest.mark.parametrize("location", ["profile", "thread_settings"])
@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("direct_state", [False, True])
async def test_native_prompt_saved_reference_cannot_replace_current_declaration(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    dynamic: bool,
    direct_state: bool,
) -> None:
    for _ in range(2):
        responses.respond(assistant_output("prompt result"))
    reference = {"id": "pmpt_current"}

    def source(_context: Any) -> Any:
        return reference

    modified = 0

    def replace(value: Any) -> Any:
        nonlocal modified
        if isinstance(value, str):
            try:
                nested = json.loads(value)
            except (ValueError, TypeError):
                return value
            if isinstance(nested, (dict, list)):
                before = modified
                nested = replace(nested)
                return json.dumps(nested) if modified != before else value
        elif isinstance(value, list):
            return [replace(item) for item in value]
        elif isinstance(value, dict):
            if location == "profile" and isinstance(value.get("sdk_agent_profile"), dict):
                model = value["sdk_agent_profile"]["model"]
            elif location == "thread_settings" and value.get("type") == "thread_settings_applied":
                model = value["thread_settings"]
            else:
                model = None
            if model is not None:
                controls = model.setdefault("request_controls", {})
                prompt = controls.setdefault("hosted_prompt", {})
                if prompt is None:
                    controls["hosted_prompt"] = prompt = {}
                prompt["reference"] = {"id": "pmpt_injected"}
                modified += 1
            return {key: replace(item) for key, item in value.items()}
        return value

    async with (
        timeout(30),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-prompt-key", supports_websockets=False
        ) as provider,
    ):
        target = Agent(name="target", hosted_prompt=reference)
        agent = Agent(
            hosted_prompt=source if dynamic else reference,
            provider=provider,
            handoffs=[target],
        )
        async with agent.session() as session:
            await session.update(model="gpt-5.5")
            await (await session.submit("before restore")).get_result()
            saved = await session.save_state()
        payload = replace(json.loads(saved.to_json()))
        assert modified > 0

        def reject_acquisition(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("forged state acquired provider authority")

        monkeypatch.setattr(NativeProvider, "_acquire_lease", reject_acquisition)
        with pytest.raises(ValueError, match="Saved prompt references"):
            if direct_state:
                saved.history[:] = payload["history"]
                state = saved
            else:
                state = SessionState.from_json(json.dumps(payload))
            async with agent.session(state=state) as restored:
                await (await restored.submit("after restore")).get_result()
    assert len(responses.requests) == 1
    assert responses.requests[0]["prompt"]["id"] == "pmpt_current"


@pytest.mark.parametrize("explicit", [False, True])
async def test_native_prompt_callback_handoff_preserves_instruction_presence(
    responses: ResponsesFixture, explicit: bool
) -> None:
    @function_tool
    def transfer() -> str:
        return "transferred"

    responses.respond(assistant_output("prompt result"))
    options: dict[str, Any] = {"instructions": ""} if explicit else {}
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-prompt-key", supports_websockets=False
    ) as provider:
        agent = Agent(
            hosted_prompt={"id": "pmpt_owned"},
            handoffs=[Handoff(Agent(name="target", instructions="Target instructions"), transfer)],
            provider=provider,
            **options,
        )
        assert (await agent.run("Use hosted instructions")).final_output == "prompt result"

    assert len(responses.requests) == 1
    request = responses.requests[0]
    assert request["prompt"] == {"id": "pmpt_owned"}
    if explicit:
        assert request["instructions"] == ""
    else:
        assert "instructions" not in request


@pytest.mark.parametrize("factory", [False, True])
async def test_native_prompt_manual_restore_uses_current_declaration(
    responses: ResponsesFixture, factory: bool
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    resolutions = 0

    def current_prompt(context: Any) -> Any:
        nonlocal resolutions
        resolutions += 1
        return {"id": "pmpt_current"}

    responses.respond(function_call("lookup", {"key": "A"}, "call-A"))
    async with (
        timeout(25),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-prompt-key", supports_websockets=False
        ) as provider,
    ):
        original = Agent(hosted_prompt={"id": "pmpt_before"}, tools=[lookup], provider=provider)
        async with original.session() as source:
            turn = await source.submit("Read a record")
            await next_request(source.interactions(), lookup)
            encoded = (await source.suspend()).to_json()
            assert "pmpt_before" not in encoded
            saved = SessionState.from_json(encoded)

        responses.respond(assistant_output("resumed with current prompt"))
        current = Agent(
            hosted_prompt=current_prompt if factory else {"id": "pmpt_current"},
            tools=[lookup],
            provider=provider,
        )
        async with current.session(state=saved) as restored:
            handle = restored.restored_turn
            assert handle is not None and handle.turn_id == turn.turn_id
            request = await next_request(restored.interactions(), lookup)
            assert request.call_id == "call-A"
            await request.respond("accepted result")
            assert (await handle.get_result()).final_output == "resumed with current prompt"

    assert [request["prompt"] for request in responses.requests] == [
        {"id": "pmpt_before"},
        {"id": "pmpt_current"},
    ]
    assert all("instructions" not in request for request in responses.requests)
    assert all("model" not in request for request in responses.requests)
    assert tool_output(responses.requests[1], "call-A") == "accepted result"
    assert resolutions == int(factory)


async def test_native_prompt_model_update_does_not_inject_default_instructions(
    responses: ResponsesFixture,
) -> None:
    for _ in range(2):
        responses.respond(assistant_output("prompt result"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-prompt-key", supports_websockets=False
    ) as provider:
        agent = Agent(hosted_prompt={"id": "pmpt_current"}, provider=provider)
        async with agent.session() as session:
            await (await session.submit("before model update")).get_result()
            await session.update(model="fixture-updated")
            await (await session.submit("after model update")).get_result()
    assert len(responses.requests) == 2
    assert responses.requests[1]["model"] == "fixture-updated"
    assert "instructions" not in responses.requests[1]
    assert "model_switch" not in str(responses.requests[1]["input"])


@pytest.mark.parametrize("instructions", ["", "Use the explicit instruction update."])
async def test_native_prompt_explicit_instruction_update_overrides_omission(
    responses: ResponsesFixture, instructions: str
) -> None:
    for _ in range(2):
        responses.respond(assistant_output("prompt result"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-prompt-key", supports_websockets=False
    ) as provider:
        agent = Agent(hosted_prompt={"id": "pmpt_current"}, provider=provider)
        async with agent.session() as session:
            await (await session.submit("before instruction update")).get_result()
            await session.update(instructions=instructions)
            await (await session.submit("after instruction update")).get_result()
    assert len(responses.requests) == 2
    assert "instructions" not in responses.requests[0]
    assert responses.requests[1]["instructions"] == instructions


async def test_native_prompt_resolver_receives_routing_hint_without_wire_model(
    responses: ResponsesFixture,
) -> None:
    hints: list[str] = []
    bodies: list[dict[str, Any]] = []

    class Backend:
        def complete(self, request: ModelRequest) -> ModelResponse:
            bodies.append(dict(request.body))
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "resolved prompt"}],
                    }
                ]
            )

    class Resolver:
        def get_model(self, model: str) -> Backend:
            hints.append(model)
            return Backend()

    async with NativeProvider(
        base_url=responses.url,
        model_backend=Resolver(),
        supports_websockets=False,
    ) as provider:
        result = await Agent(hosted_prompt={"id": "pmpt_resolved"}, provider=provider).run("answer")
    assert result.final_output == "resolved prompt"
    assert len(hints) == 1 and isinstance(hints[0], str) and hints[0]
    assert len(bodies) == 1 and "model" not in bodies[0]
    assert bodies[0]["prompt"] == {"id": "pmpt_resolved"}
    assert responses.requests == []
