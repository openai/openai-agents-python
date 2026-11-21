"""Compare two scripted handoff paths on labeled fixtures without making API calls."""

import asyncio
import json
from time import perf_counter

from agents import Agent, RunConfig, Runner, handoff
from agents.decisions import DecisionRequest, DecisionResult, decision_handoff
from agents.testing import ScriptedModel, assistant_message, function_call

# Each row records the input, expected route, expected answer and two scripted predictions.
CASES = (
    ("Where can I reset my password?", "account", "Open settings.", "account", "account"),
    ("Where is my invoice?", "billing", "Open invoices.", "account", "billing"),
    ("How do I change my password?", "account", "Open settings.", "account", "billing"),
)


class RecordedDecisions:
    def __init__(self, routes: dict[str, str]):
        self.routes = routes
        self.requests = 0

    async def get_decision(self, request: DecisionRequest) -> DecisionResult[str]:
        assert isinstance(request.input, str)
        self.requests += 1
        return DecisionResult(self.routes[request.input])


async def compare() -> None:
    for path in ("primary", "decisions"):
        provider = RecordedDecisions({text: predicted for text, _, _, _, predicted in CASES})
        correct = completed = requests = 0
        seconds = 0.0
        for text, label, expected, primary, _ in CASES:
            targets = {
                name: Agent[None](name=name, model=ScriptedModel([[assistant_message(answer)]]))
                for name, answer in (("account", "Open settings."), ("billing", "Open invoices."))
            }
            source = Agent[None](name="Triage")
            if path == "primary":
                source.handoffs.extend(
                    handoff(agent, tool_name_override=name) for name, agent in targets.items()
                )
                call = function_call(primary, {}, call_id="transfer")
            else:
                route = decision_handoff(
                    source, provider=provider, question="Which team can help?", targets=targets
                )
                source.handoffs.append(route)
                call = function_call(route.tool_name, {"input": text}, call_id="transfer")
            source.model = ScriptedModel([[call]])
            start = perf_counter()
            result = await Runner.run(source, text, run_config=RunConfig(tracing_disabled=True))
            seconds += perf_counter() - start
            correct += result.last_agent is targets[label]
            completed += result.final_output == expected
            requests += len(result.raw_responses)
        print(
            json.dumps(
                {
                    "path": path,
                    "cases": len(CASES),
                    "route_accuracy": correct / len(CASES),
                    "task_completion": completed / len(CASES),
                    "elapsed_seconds": seconds,
                    "model_requests": requests,
                    "decision_requests": provider.requests,
                    "estimated_cost": None,
                    "fallback": None,
                }
            )
        )


if __name__ == "__main__":
    asyncio.run(compare())
