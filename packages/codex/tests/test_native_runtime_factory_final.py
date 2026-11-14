import asyncio
import json
import threading

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    CapabilityError,
    DecisionHandoff,
    FunctionTool,
    HttpMCP,
    NativeProvider,
    OutputValidationError,
    TurnStoppedError,
    function_tool,
)
from agents.session import Session
from agents.state import LocalStorage
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_configured_agent_tool import peer
from tests.test_native_decision_handoff import Decisions
from tests.test_native_mcp_tools import Peer


@pytest.mark.parametrize(
    "route",
    [
        "fresh",
        "same-settings",
        "inherit-settings",
        "conflicting-settings",
        "outside",
        "foreign",
        "wrong-owner",
        "cancel",
        "cancel-staged",
        "other-model",
        "mcp",
        "durable",
        "explicit-policy",
        "resource-collision",
        "reserved-name",
        "namespace",
        "manual-refusal",
    ],
)
async def test_runtime_factory_keeps_native_selection_and_provider_authority(
    responses, route, monkeypatch, tmp_path
):
    positive = route in {"fresh", "same-settings", "inherit-settings"}
    calls, effects, targets, acquisitions = [], [], [], []
    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    captured = []
    mcp = Peer(names=("read",))
    acquire = NativeProvider._acquire_lease

    def observe(provider):
        acquisitions.append(provider)
        return acquire(provider)

    monkeypatch.setattr(NativeProvider, "_acquire_lease", observe)
    if route == "wrong-owner":
        original = getattr(Session, "_stage_runtime_handoff_target", None)

        def wrong_owner(self, request, message, selection):
            data = json.loads(request)
            data["_sdk_invocation"] += 1
            return original(self, json.dumps(data), message, selection)

        if original is not None:
            monkeypatch.setattr(Session, "_stage_runtime_handoff_target", wrong_owner)

    if route == "explicit-policy":
        session_globals = Session._open.__func__.__globals__
        install = session_globals["_install_profile_options"]

        def omit_opening_permission(options, *args, **kwargs):
            install(options, *args, **kwargs)
            options["tool_policy"]["allowed_tools"] = [
                name
                for name in options["tool_policy"]["allowed_tools"]
                if name != {"namespace": "_agents_sdk", "name": "runtime_handoff_callbacks"}
            ]

        monkeypatch.setitem(session_globals, "_install_profile_options", omit_opening_permission)

    if route == "cancel-staged":
        stage = Session._stage_runtime_handoff_target

        def remember(self, request, message, selection):
            captured.append((request, dict(message), selection))
            return stage(self, request, message, selection)

        monkeypatch.setattr(Session, "_stage_runtime_handoff_target", remember)
        respond = Session._respond_tool

        async def hold_staged(self, state, reply, **kwargs):
            if json.loads(reply).get("handoff_target_profile_id", "").startswith("runtime_"):
                entered.set()
                try:
                    await release.wait()
                finally:
                    cleaned.set()
            return await respond(self, state, reply, **kwargs)

        monkeypatch.setattr(Session, "_respond_tool", hold_staged)

    @function_tool(
        name="exec_command"
        if route == "reserved-name"
        else ("locked_resource" if route == "resource-collision" else "fresh_lookup"),
        namespace="different" if route == "namespace" else None,
    )
    def fresh_lookup() -> str:
        effects.append("once")
        return "fresh result"

    class ManualArgs(BaseModel):
        pass

    async with (
        timeout(25),
        peer() as foreign_model,
        mcp.serve() as mcp_url,
        NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ) as backend,
        NativeProvider(
            base_url=foreign_model.url,
            api_key="never-dispatch",
            supports_websockets=False,
        ) as foreign,
    ):
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()

        def create_target(_context, routing_input):
            assert routing_input == "Create fresh"
            assert threading.get_ident() != loop_thread
            ping = asyncio.run_coroutine_threadsafe(asyncio.sleep(0, result="admitted"), loop)
            assert ping.result(timeout=3) == "admitted"
            calls.append("factory")
            if route == "cancel":

                async def hold():
                    entered.set()
                    try:
                        await release.wait()
                    finally:
                        cleaned.set()
                    raise AssertionError("Cancelled factory was resumed")

                return hold()
            target = Agent(
                name="Runtime fresh",
                instructions="Runtime-only target instructions",
                model="other-model"
                if route == "other-model"
                else (None if route == "inherit-settings" else "gpt-5.5"),
                model_settings={"temperature": 0.8}
                if route == "conflicting-settings"
                else ({"temperature": 0.2} if route == "same-settings" else {}),
                tools=[HttpMCP(name="blocked", url=mcp_url, tools=["read"])]
                if route == "mcp"
                else [
                    FunctionTool(
                        name="fresh_lookup",
                        description="Unbound manual tool",
                        input_type=ManualArgs,
                        output_type=str,
                    )
                ]
                if route == "manual-refusal"
                else [fresh_lookup],
                provider=foreign if route == "foreign" else None,
            )
            targets.append(target)
            return target

        source = Agent(
            name="Source",
            instructions="Choose the runtime factory",
            model="gpt-5.5",
            model_settings={"temperature": 0.2},
            provider=backend,
            decisions=Decisions("outside" if route == "outside" else "fresh"),
            handoffs=[
                DecisionHandoff(
                    name="delegate",
                    description="Create and transfer to a runtime specialist",
                    question="Choose the authorized target factory",
                    targets={"fresh": create_target},
                )
            ],
        )
        if route == "resource-collision":

            class Empty(BaseModel):
                pass

            resource = Agent(
                name="Captured resource",
                instructions="Retain the current manual authority.",
                tools=[
                    FunctionTool(
                        name="locked_resource",
                        description="Manual resource",
                        input_type=Empty,
                        output_type=str,
                    )
                ],
            )
            source = Agent(
                name=source.name,
                instructions=source.instructions,
                model=source.model,
                model_settings=source.model_settings,
                provider=backend,
                decisions=source.decisions,
                handoffs=[*source.handoffs, resource],
            )
        assert calls == [] and responses.requests == []
        responses.respond(function_call("delegate", {"input": "Create fresh"}, "route"))
        if positive:
            responses.respond(function_call("fresh_lookup", {}, "lookup"))
            responses.respond(assistant_output("done"))
        if route == "conflicting-settings":
            responses.respond(assistant_output("Conflicting settings silently ignored"))
        if route == "durable":
            with pytest.raises(CapabilityError, match="factories do not support durable storage"):
                async with source.session(storage=LocalStorage(tmp_path / "storage")):
                    pytest.fail("A dynamic factory has been opening with durable storage")
            assert calls == [] and acquisitions == [] and responses.requests == []
            assert foreign_model.requests == [] and mcp.requests == []
            return
        async with source.session() as session:
            assert calls == [] and responses.requests == []
            turn = await session.submit("Go")
            if route in {"cancel", "cancel-staged"}:
                try:
                    await entered.wait()
                    if route == "cancel-staged":
                        with pytest.raises(CapabilityError, match="in-flight runtime handoff"):
                            await session.save_state()
                    await turn.cancel()
                    with pytest.raises(TurnStoppedError):
                        await turn.get_result()
                    assert cleaned.is_set()
                    assert calls == ["factory"] and effects == []
                    assert len(targets) == (1 if route == "cancel-staged" else 0)
                    assert len(responses.requests) == 1
                    assert foreign_model.requests == [] and foreign not in acquisitions
                    assert await session.get_active_agent() is source
                    assert mcp.requests == []
                    if route == "cancel-staged":
                        assert session._native._runtime_handoff_published() is False
                        assert len(captured) == 1
                        old_request, old_message, old_selection = captured[0]
                        with pytest.raises(RuntimeError, match="Runtime handoff callback expired"):
                            stage(session, old_request, old_message, old_selection)
                        saved = await session.save_state()
                        async with source.session(state=saved) as restored:
                            assert await restored.get_active_agent() is source
                        assert len(responses.requests) == 1 and calls == ["factory"]
                finally:
                    release.set()
                return
            if not positive:
                errors = {
                    "outside": (OutputValidationError, None),
                    "manual-refusal": (ValueError, "ordinary Python tools"),
                    "explicit-policy": (RuntimeError, "excluded by ToolPolicy"),
                    "resource-collision": (RuntimeError, "collides with a captured resource"),
                    "reserved-name": (
                        RuntimeError,
                        "Python tool uses a reserved name: exec_command",
                    ),
                    "namespace": (RuntimeError, "ordinary Python tools"),
                    "conflicting-settings": (ValueError, "preserve its current model settings"),
                    "foreign": (ValueError, "preserve the Session provider"),
                    "wrong-owner": (RuntimeError, "invocation nonce"),
                    "other-model": (RuntimeError, "preserve its current model"),
                    "mcp": (ValueError, "ordinary Python tools"),
                }
                error, match = errors[route]
                with pytest.raises(error, match=match):
                    await turn.get_result()
                assert await session.get_active_agent() is source
                assert calls == ([] if route == "outside" else ["factory"])
                assert effects == [] and len(responses.requests) == 1
                assert foreign_model.requests == [] and foreign not in acquisitions
                assert mcp.requests == []
                return
            result = await turn.get_result()
            assert result.final_output == "done"
            assert await session.get_active_agent() is targets[0]
            with pytest.raises(CapabilityError, match="Runtime-created handoff targets"):
                await session.save_state()
        assert calls == ["factory"] and effects == ["once"]
        assert len(responses.requests) == 3
        assert foreign_model.requests == [] and foreign not in acquisitions
        assert "Runtime-only target instructions" in json.dumps(responses.requests[1])
        assert [tool["name"] for tool in responses.requests[1]["tools"]] == ["fresh_lookup"]

    assert mcp.requests == []
    assert [request["model"] for request in responses.requests] == ["gpt-5.5"] * 3

    assert [request["temperature"] for request in responses.requests] == [0.2] * 3
