"""Route synthetic requests with optional Decisions and separate specialist Sessions.

Run from the repository root: python -m examples.codex.recipes.decisions_routing.
No credentials or live model calls are involved.
"""

import asyncio
import json
from dataclasses import dataclass

from pydantic import BaseModel

from agents import (
    Agent,
    DecisionError,
    DecisionRequest,
    DecisionResponse,
    OutputValidationError,
    ToolContext,
    function_tool,
)
from agents.testing import ScriptedResponses, text, tool_call
from examples.codex.recipes.specialist_agents import Catalog, Summary, read_catalog


@dataclass
class Routing:
    specialists: dict[str, tuple[Agent[Catalog, Summary], Catalog]]
    descriptions: dict[str, str]
    question: str
    authorized: set[str]
    default: str
    use_decisions: bool = False


class RoutedSummary(BaseModel):
    specialist: str
    route: str
    summary: Summary


@function_tool
async def consult(context: ToolContext[Routing]) -> RoutedSummary:
    """Choose an authorized specialist and return its typed answer to the orchestrator."""
    app = context.context
    candidates = sorted(app.authorized.intersection(app.specialists))
    if not candidates:
        raise PermissionError("No authorized specialist")
    selected, route = app.default, "default"
    if app.use_decisions:
        try:
            selected = await asyncio.wait_for(
                context.decide(
                    "Choose the specialist best suited to this request.",
                    input=json.dumps(
                        {
                            "request": app.question,
                            "candidates": [
                                {"id": name, "description": app.descriptions[name]}
                                for name in candidates
                            ],
                        }
                    ),
                    choices=candidates,
                ),
                timeout=1,
            )
            route = "decision"
        except (asyncio.TimeoutError, DecisionError, OutputValidationError):
            selected, route = app.default, "fallback"

    # Authorization can change while the decision is awaiting its provider.
    if selected not in app.authorized or selected not in app.specialists:
        selected, route = app.default, "fallback"
    if selected not in app.authorized or selected not in app.specialists:
        raise PermissionError("Default specialist is not authorized")

    specialist, specialist_context = app.specialists[selected]
    # Agent.run owns and closes this separate Session, including cancellation.
    result = await asyncio.wait_for(
        specialist.run(app.question, context=specialist_context), timeout=10
    )
    return RoutedSummary(specialist=selected, route=route, summary=result.final_output)


class SyntheticDecisions:
    """Supply deterministic values through the public DecisionProvider contract."""

    def __init__(self, value: str, *, timeout: bool = False, revoke: str | None = None):
        self.value = value
        self.timeout = timeout
        self.revoke = revoke
        self.requests: list[DecisionRequest] = []
        self.cleaned = False
        self.routing: Routing | None = None

    async def get_decision(self, request: DecisionRequest) -> DecisionResponse:
        self.requests.append(request)
        try:
            if self.timeout:
                await asyncio.Future()
            if self.revoke is not None:
                assert self.routing is not None
                self.routing.authorized.discard(self.revoke)
            return DecisionResponse(value=self.value)
        finally:
            self.cleaned = True


async def run_scenario(
    *,
    workflow: str,
    candidates: tuple[str, str],
    authorized: set[str],
    decision: str,
    expected: str | None,
    expected_route: str = "default",
    use_decisions: bool = False,
    timeout: bool = False,
    revoke: str | None = None,
) -> None:
    decisions = SyntheticDecisions(decision, timeout=timeout, revoke=revoke)
    question = {
        "support": "Explain a duplicate charge.",
        "research": "Check the risk notes for the selected catalog.",
    }[workflow]
    descriptions = {
        "billing": "Charges and invoices",
        "technical": "Connection issues",
        "catalog": "Catalog contents",
        "risk": "Risk notes",
    }
    initial_candidates = sorted(authorized.intersection(candidates))
    # No model responses are queued for a scenario that must reject delegation.
    outer_steps = (
        [[tool_call("consult", {}, call_id="consult")], [text("Done.")]]
        if expected is not None
        else [[tool_call("consult", {}, call_id="consult")]]
    )
    inner_steps = (
        [[tool_call("read_catalog", {}, call_id="catalog")], [text('{"title":"Summary"}')]]
        if expected is not None
        else []
    )
    async with (
        ScriptedResponses(*outer_steps) as outer,
        ScriptedResponses(*inner_steps) as inner,
    ):
        app = Routing(
            specialists={
                name: (
                    Agent(
                        instructions="Read the supplied catalog with the declared tool.",
                        tools=[read_catalog],
                        provider=inner.provider,
                        output_type=Summary,
                    ),
                    Catalog(f"Synthetic {workflow}: {name}"),
                )
                for name in candidates
            },
            authorized=set(authorized),
            descriptions={name: descriptions[name] for name in candidates},
            question=question,
            default=candidates[0],
            use_decisions=use_decisions,
        )
        decisions.routing = app
        orchestrator = Agent(
            instructions="Consult a specialist, then summarize its answer.",
            tools=[consult],
            provider=outer.provider,
            decisions=decisions,
        )
        # Decisions configuration leaves the generative provider unchanged.
        if expected is None:
            try:
                await asyncio.wait_for(orchestrator.run(question, context=app), timeout=20)
            except PermissionError:
                pass
            else:
                raise AssertionError("Unauthorized delegation succeeded")
            assert not inner.requests
        else:
            result = await asyncio.wait_for(orchestrator.run(question, context=app), timeout=20)
            assert result.final_output == "Done."
            output = outer.tool_output("consult")
            assert output is not None
            routed = json.loads(output)
            assert routed["specialist"] == expected
            assert routed["route"] == expected_route
            assert routed["summary"] == {"title": "Summary"}
            assert inner.tool_output("catalog") == f"Synthetic {workflow}: {expected}"
            assert len(outer.requests) == len(inner.requests) == 2

    if use_decisions and initial_candidates:
        assert len(decisions.requests) == 1 and decisions.cleaned
        decision_input = decisions.requests[0].input
        assert isinstance(decision_input, str)
        assert json.loads(decision_input) == {
            "request": question,
            "candidates": [
                {"id": name, "description": descriptions[name]} for name in initial_candidates
            ],
        }
        schema = decisions.requests[0].output_schema
        if len(initial_candidates) == 1:
            assert schema["const"] == initial_candidates[0]
        else:
            assert schema["enum"] == initial_candidates
    else:
        assert not decisions.requests
    print(f"{workflow}: {expected or 'denied'} ({expected_route})")


async def main() -> None:
    support = ("billing", "technical")
    research = ("catalog", "risk")
    await run_scenario(
        workflow="support",
        candidates=support,
        authorized={"billing", "technical"},
        decision="technical",
        expected="billing",
    )
    await run_scenario(
        workflow="support",
        candidates=support,
        authorized={"billing", "technical"},
        decision="technical",
        expected="technical",
        expected_route="decision",
        use_decisions=True,
    )
    await run_scenario(
        workflow="research",
        candidates=research,
        authorized={"catalog", "risk"},
        decision="risk",
        expected="risk",
        expected_route="decision",
        use_decisions=True,
    )
    await run_scenario(
        workflow="research",
        candidates=research,
        authorized={"catalog"},
        decision="risk",
        expected="catalog",
        expected_route="fallback",
        use_decisions=True,
    )
    await run_scenario(
        workflow="support",
        candidates=support,
        authorized={"billing", "technical"},
        decision="unknown",
        expected="billing",
        expected_route="fallback",
        use_decisions=True,
    )
    await run_scenario(
        workflow="support",
        candidates=support,
        authorized={"billing", "technical"},
        decision="technical",
        expected="billing",
        expected_route="fallback",
        use_decisions=True,
        revoke="technical",
    )
    await run_scenario(
        workflow="support",
        candidates=support,
        authorized={"billing", "technical"},
        decision="technical",
        expected="billing",
        expected_route="fallback",
        use_decisions=True,
        timeout=True,
    )
    await run_scenario(
        workflow="research",
        candidates=research,
        authorized=set(),
        decision="risk",
        expected=None,
        use_decisions=True,
    )
    await run_scenario(
        workflow="research",
        candidates=research,
        authorized={"risk"},
        decision="unknown",
        expected=None,
        use_decisions=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
