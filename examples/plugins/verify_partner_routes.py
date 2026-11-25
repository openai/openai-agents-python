"""Check three installed plugins with loopback inference. Pass --codex to use Codex."""

import argparse
import asyncio
import json
import os
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from agents_knowledge import Hit, knowledge
from agents_masking import masking
from agents_ticket_desk import ticket_desk
from verify_routes import MODEL, route

from agents import Agent, RunConfig, Runner, RunState
from agents.plugins import Deny, Plugin, PluginHooks, RequireApproval, ToolCall

OWNER = "fixture-owner"
SECRET = "fixture-private-code"
REVISION = "desk-v2"
CALLS = [
    ("knowledge_search", {"query": "closure policy"}),
    ("close_ticket", {"ticket_id": "T-1"}),
    ("close_ticket", {"ticket_id": "T-2"}),
    ("close_ticket", {"ticket_id": "T-3"}),
    ("close_ticket", {"ticket_id": "T-4"}),
]


async def verify(codex: bool, workspace: Path):
    requests, searches, attempts, closed = [], [], [], []
    grants = {"T-1", "T-2", "T-3", "T-4"}
    additional_reviews: dict[str, RequireApproval | Deny] = {}
    checked_on_resume = set()

    def review_closures(call: ToolCall):
        return RequireApproval() if call.owner == "ticket-desk" else None

    def check_additional_review(call: ToolCall):
        if call.owner == "ticket-desk" and call.call_id in additional_reviews:
            checked_on_resume.add(call.arguments["ticket_id"])
            return additional_reviews[call.call_id]
        return None

    class Desk:
        async def search(self, query: str, *, limit: int) -> list[Hit]:
            searches.append((query, limit))
            return [Hit("desk.policy", f"Review required. Code {SECRET}.")]

        async def close_if_allowed(self, user: str, ticket_id: str) -> bool:
            attempts.append(ticket_id)
            assert ticket_id in checked_on_resume
            # Simulate revocation after the SDK policy checks but before the backend write.
            if ticket_id == "T-1":
                grants.remove(ticket_id)
            if user != OWNER or ticket_id not in grants:
                return False
            closed.append(ticket_id)
            return True

    desk = Desk()
    clients = {OWNER: desk}
    agent = Agent(
        name="Support",
        model=MODEL,
        plugins=[
            knowledge(lambda user: clients[user]),
            ticket_desk(desk, owner=OWNER),
            masking({"knowledge_search": [SECRET]}),
            Plugin(name="closure-review", hooks=PluginHooks(check_tool=review_closures)),
            Plugin(name="additional-review", hooks=PluginHooks(check_tool=check_additional_review)),
        ],
    )
    async with route(codex, workspace, requests, calls=CALLS) as provider:
        config = RunConfig(model_provider=provider, plugin_revision=REVISION, tracing_disabled=True)
        result = await Runner.run(
            agent,
            "Check the closure policy and close T-1, T-2, T-3 and T-4.",
            context=OWNER,
            run_config=config,
        )
        for turn in (1, 2, 3, 4):
            (pending,) = result.interruptions
            assert pending.call_id == f"call_{turn}"
            assert (pending.name, json.loads(pending.arguments)) == CALLS[turn]
            assert f"T-{turn}" not in attempts and f"T-{turn}" not in closed
            state = result.to_state()
            if turn == 2:
                state.reject(pending, rejection_message="declined")
            else:
                state.approve(pending)
                snapshot = state.to_string()
                assert SECRET not in snapshot and OWNER not in snapshot
                # A new RequireApproval shares the saved plugin decision for this call.
                # Deny if the host needs a separate reviewer and has no such decision.
                additional_reviews[pending.call_id] = (
                    Deny("Missing independent review") if turn == 4 else RequireApproval()
                )
                state = await RunState.from_string(
                    agent, snapshot, plugin_revision=REVISION, context_override=OWNER
                )
            result = await Runner.run(agent, state, run_config=config)

    assert result.final_output == "done" and not result.interruptions
    assert searches == [("closure policy", 3)]
    assert checked_on_resume == {"T-1", "T-3", "T-4"}
    assert attempts == ["T-1", "T-3"] and closed == ["T-3"]
    assert len(requests) == len(CALLS) + 1
    tools = {tool.get("name") for tool in requests[0]["tools"]}
    assert {"close_ticket", "knowledge_search"} <= tools
    outputs = []
    for turn, request in enumerate(requests[1:]):
        (output,) = [
            item["output"]
            for item in request["input"]
            if item.get("type") == "function_call_output" and item.get("call_id") == f"call_{turn}"
        ]
        outputs.append(output)
    assert json.loads(outputs[0]) == {
        "results": [{"source": "desk.policy", "excerpt": "Review required. Code [REDACTED]."}]
    }
    assert outputs[1:] == ["denied", "declined", "closed", "Tool call refused."]
    transcript = repr(requests) + repr(result.to_input_list())
    assert SECRET not in transcript and OWNER not in transcript
    assert "Missing independent review" not in transcript
    print(
        json.dumps(
            {
                "sdk": version("openai-agents"),
                "route": "codex" if codex else "python",
                "requests": len(requests),
                "closed": closed,
                "outputs": outputs,
            }
        )
    )


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("Run this check without Python optimization.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", action="store_true")
    args = parser.parse_args()
    with TemporaryDirectory() as tmp:
        os.environ["CODEX_HOME"] = tmp
        asyncio.run(asyncio.wait_for(verify(args.codex, Path(tmp)), 60))
        assert not (Path(tmp) / "auth.json").exists()
