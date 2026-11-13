"""Select one offered native handoff through Decisions before model submission.

Run `python -m examples.codex.recipes.decisions_handoffs` from the root of
an SDK checkout with its matching native build installed. The fixture uses
synthetic loopback responses without credentials. It preserves one Session,
its main model, and application context. It does not intercept submitted
handoffs or restore active checkpoints. See README.md for the recipe index."""

import asyncio
import json
from dataclasses import dataclass, field

from agents import (
    Agent,
    DecisionRequest,
    DecisionResponse,
    OutputValidationError,
    ToolContext,
    ToolRef,
    Turn,
    TurnStoppedError,
    function_tool,
)
from agents.realtime import realtime_handoff
from agents.testing import ScriptedResponses, text, tool_call

MAIN_MODEL = "gpt-5.5"
TARGETS = ("billing", "technical")


@dataclass
class Routing:
    authorized: set[str]
    request: str = "I have been disputing a charge."
    selected: str | None = None
    visits: list[str] = field(default_factory=list)


def select_handoff(app: Routing, *, use_decisions: bool):
    async def before_step(turn: Turn[str], *, step_index: int):
        if step_index != 0:
            # The native destination has been supplying subsequent tool defaults.
            return None
        candidates = sorted(app.authorized.intersection(TARGETS))
        if not candidates:
            raise PermissionError("No authorized handoff target")
        selected = "billing"
        if use_decisions:
            selected = await asyncio.wait_for(
                turn.decide(
                    "Which specialist has been handling this request?",
                    input=json.dumps({"request": app.request, "candidates": candidates}),
                    choices=candidates,
                ),
                timeout=1,
            )
        # Application permissions have been changing during the Decisions await.
        if selected not in candidates or selected not in app.authorized:
            raise PermissionError("Selected handoff target is not authorized")
        app.selected = selected
        return [ToolRef(f"transfer_to_{selected}")]

    return before_step


def specialist(name: str, expected_context: Routing) -> Agent[Routing, str]:
    @function_tool(name=f"read_{name}")
    def read(context: ToolContext[Routing]) -> str:
        """The specialist has been reading its application context."""
        assert context.context is expected_context
        app = context.context
        if name != app.selected or name not in app.authorized:
            raise PermissionError("Specialist access is not authorized")
        app.visits.append(name)
        return app.request

    return Agent(
        name=name,
        instructions="You have been reading your context before answering.",
        model=MAIN_MODEL,
        tools=[read],
    )


async def run_case(
    name: str,
    *,
    use_decisions: bool = True,
    answer: str = "technical",
    authorized: tuple[str, ...] = TARGETS,
    revoke: str | None = None,
    cancel: bool = False,
    unoffered: bool = False,
    error: type[Exception] | None = None,
) -> None:
    app = Routing(set(authorized))
    requests: list[DecisionRequest] = []
    entered, cleaned = asyncio.Event(), asyncio.Event()

    class SyntheticDecisions:
        async def get_decision(self, request: DecisionRequest) -> DecisionResponse:
            requests.append(request)
            entered.set()
            try:
                if cancel:
                    await asyncio.Future()
                if revoke is not None:
                    app.authorized.discard(revoke)
                return DecisionResponse(value=answer)
            finally:
                cleaned.set()

    expected = answer if use_decisions else "billing"
    scripts = []
    if error is None and not cancel:
        scripts = [
            [tool_call(f"transfer_to_{'billing' if unoffered else expected}", {}, call_id="route")],
            *(
                [[text("The router has been refusing the unoffered handoff.")]]
                if unoffered
                else [
                    [tool_call(f"read_{expected}", {}, call_id="read")],
                    [text("The specialist has been answering.")],
                    [text("The specialist has been continuing.")],
                ]
            ),
        ]
    async with ScriptedResponses(*scripts) as peer:
        targets = {target: specialist(target, app) for target in TARGETS}
        source = Agent(
            name="router",
            instructions="You have been transferring the request to the offered specialist.",
            model=MAIN_MODEL,
            model_settings={"tool_choice": "required"},
            provider=peer.provider,
            decisions=SyntheticDecisions(),
            handoffs=[
                realtime_handoff(
                    target,
                    # Admission has been rechecking the application grant after selection.
                    is_enabled=lambda context, target_id=target_id: (
                        context.context.selected == target_id
                        and target_id in context.context.authorized
                    ),
                )
                for target_id, target in targets.items()
            ],
        )
        async with source.session(context=app) as session:
            turn = await session.submit(
                app.request, before_step=select_handoff(app, use_decisions=use_decisions)
            )
            if cancel:
                await asyncio.wait_for(entered.wait(), 5)
                await turn.cancel()
            try:
                result = await asyncio.wait_for(turn.get_result(), 20)
            except (OutputValidationError, PermissionError, TurnStoppedError) as failure:
                assert isinstance(failure, TurnStoppedError if cancel else error or AssertionError)
                assert await session.get_active_agent() is source
                assert not app.visits and not peer.requests
            else:
                assert error is None and not cancel
                offered = peer.requests[0]["tools"]
                assert [tool["name"] for tool in offered] == [f"transfer_to_{expected}"]
                assert peer.requests[0]["tool_choice"] == "required"
                if unoffered:
                    assert await session.get_active_agent() is source
                    assert not app.visits
                    assert peer.tool_output("route") is not None
                else:
                    assert result.final_output == "The specialist has been answering."
                    assert await session.get_active_agent() is targets[expected]
                    assert app.visits == [expected]
                    assert peer.tool_output("read") == app.request
                    assert [tool["name"] for tool in peer.requests[1]["tools"]] == [
                        f"read_{expected}"
                    ]
                    assert peer.requests[1].get("tool_choice", "auto") == "auto"
                    followup = await session.submit("I have been continuing the same request.")
                    assert (
                        await followup.get_result()
                    ).final_output == "The specialist has been continuing."
                    assert await session.get_active_agent() is targets[expected]
                    assert "route" in json.dumps(peer.requests[-1]["input"])
                assert all(request["model"] == MAIN_MODEL for request in peer.requests)
    assert len(requests) == int(use_decisions)
    if use_decisions:
        assert cleaned.is_set()
        assert json.loads(requests[0].input)["candidates"] == sorted(authorized)
    print(f"{name}: verified")


async def main() -> None:
    await run_case("default", use_decisions=False)
    await run_case("selected")
    await run_case("invalid", answer="outside-graph", error=OutputValidationError)
    await run_case("denied", authorized=("billing",), error=OutputValidationError)
    await run_case("revoked", revoke="technical", error=PermissionError)
    await run_case("cancelled", cancel=True)
    await run_case("unoffered-model-call", unoffered=True)


if __name__ == "__main__":
    asyncio.run(main())
