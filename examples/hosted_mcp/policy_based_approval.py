"""Example: Hosted MCP Client with a Programmatic Policy Engine

This example demonstrates how to evaluate tool calls against a predefined
authorization policy before execution or human interruption.
This pattern is essential when deploying agents at scale, where:
- Safe tools (e.g. read-only operations) should be automatically allowed.
- Destructive tools (e.g. drop database) should be automatically blocked.
- Ambiguous/Sensitive tools should be routed to a human approver.

In a production environment, this local `PolicyEngine` would typically be
replaced by a dedicated Identity and Access Management (IAM) service.
"""

import argparse
import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from agents import (
    Agent,
    HostedMCPTool,
    MCPToolApprovalFunctionResult,
    MCPToolApprovalRequest,
    Runner,
    RunResult,
    RunResultStreaming,
)
from examples.auto_mode import confirm_with_fallback


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass
class PolicyResult:
    decision: Decision
    reason: str


class PolicyEngine:
    """A simple policy engine that decides whether a tool call should
    be allowed, denied, or held for human approval.
    """

    def evaluate(self, request: MCPToolApprovalRequest) -> PolicyResult:
        tool_name = request.data.name

        # Read-like tools are always safe
        if tool_name.startswith(("read_", "list_", "search_", "get_")):
            return PolicyResult(Decision.ALLOW, "Read-only tool, allowed by default")

        # Destructive tools are always blocked
        if tool_name.startswith(("delete_", "drop_", "destroy_")):
            return PolicyResult(Decision.DENY, "Destructive tool, blocked by policy")

        # Everything else needs a human to approve
        return PolicyResult(
            Decision.REQUIRE_APPROVAL,
            "Tool has unknown side effects, requires human approval",
        )


def build_approval_handler():
    """Creates an approval callback that integrates the PolicyEngine."""
    engine = PolicyEngine()

    def handle_approval(request: MCPToolApprovalRequest) -> MCPToolApprovalFunctionResult:
        policy_result = engine.evaluate(request)

        print(f"\n[Policy Engine] Evaluating tool: {request.data.name}")
        print(f"[Policy Engine] Decision: {policy_result.decision.value.upper()}")
        print(f"[Policy Engine] Reason: {policy_result.reason}\n")

        if policy_result.decision == Decision.ALLOW:
            return {"approve": True}

        if policy_result.decision == Decision.DENY:
            return {"approve": False, "reason": policy_result.reason}

        # Decision.REQUIRE_APPROVAL -> Fall back to human in the loop
        params: object = request.data.arguments or {}
        approved = confirm_with_fallback(
            f"Human Review Required - Approve running tool (mcp: {request.data.name}, params: {json.dumps(params)})? (y/n) ",
            default=True,
        )

        result: MCPToolApprovalFunctionResult = {"approve": approved}
        if not approved:
            result["reason"] = "User denied"
        return result

    return handle_approval


async def main(verbose: bool, stream: bool) -> None:
    # We set require_approval to "always" so every tool call triggers our handler
    require_approval: Literal["always"] = "always"

    agent = Agent(
        name="Secure Assistant",
        instructions=(
            "You must always use the MCP tools to answer questions. "
            "Use the DeepWiki hosted MCP server to answer questions and do not ask the user for "
            "additional configuration."
        ),
        tools=[
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "deepwiki",
                    "server_url": "https://mcp.deepwiki.com/mcp",
                    "require_approval": require_approval,
                },
                on_approval_request=build_approval_handler(),
            )
        ],
    )

    question = "Which language is the repository openai/codex written in?"

    run_result: RunResult | RunResultStreaming
    if stream:
        run_result = Runner.run_streamed(agent, question)
        async for event in run_result.stream_events():
            if verbose:
                print(event)
            elif (
                event.type == "raw_response_event"
                and event.data.type == "response.output_text.delta"
            ):
                print(event.data.delta, end="", flush=True)
        if not verbose:
            print()
        print(f"Done streaming; final result: {run_result.final_output}")
    else:
        run_result = await Runner.run(agent, question)
        while run_result.interruptions:
            run_result = await Runner.run(agent, run_result.to_state())
        print(run_result.final_output)

    if verbose:
        for item in run_result.new_items:
            print(item)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", default=False)
    parser.add_argument("--stream", action="store_true", default=False)
    args = parser.parse_args()

    asyncio.run(main(args.verbose, args.stream))
