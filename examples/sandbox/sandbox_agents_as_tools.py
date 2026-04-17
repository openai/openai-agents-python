"""
Show how sandbox agents can be exposed as tools to a normal orchestrator.

Each sandbox reviewer gets its own isolated workspace. The outer orchestrator
does not inspect files directly. It calls the reviewers as tools and combines
their outputs with a normal Python function tool.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agents import Agent, ModelSettings, Runner, function_tool
from agents.run import RunConfig
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.sandbox.misc.example_support import text_manifest, tool_call_name
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

DEFAULT_QUESTION = (
    "Review the Acme renewal materials and give me a short recommendation for the deal desk. "
    "Include pricing risk, rollout risk, and the most important next step."
)


class PricingPacketReview(BaseModel):
    requested_discount_percent: int = Field(
        description="Exact requested discount percentage from pricing_summary.md."
    )
    requested_term_months: int = Field(
        description="Exact requested renewal term in months from pricing_summary.md."
    )
    pricing_risk: Literal["low", "medium", "high"]
    summary: str = Field(description="Short pricing risk summary grounded in the reviewed files.")
    recommended_next_step: str = Field(
        description="Most important commercial next step for the deal desk."
    )
    evidence_files: list[str] = Field(
        description="File names that support the review.", min_length=1
    )


class RolloutRiskReview(BaseModel):
    rollout_risk: Literal["low", "medium", "high"]
    summary: str = Field(description="Short rollout risk summary grounded in the reviewed files.")
    blockers: list[str] = Field(description="Concrete rollout blockers from the reviewed files.")
    recommended_next_step: str = Field(
        description="Most important delivery next step for the deal desk."
    )
    evidence_files: list[str] = Field(
        description="File names that support the review.", min_length=1
    )


async def _structured_tool_output_extractor(result) -> str:
    final_output = result.final_output
    if isinstance(final_output, BaseModel):
        return json.dumps(final_output.model_dump(mode="json"), sort_keys=True)
    return str(final_output)


@function_tool
def get_discount_approval_rule(discount_percent: int) -> str:
    """Return the internal approver required for a proposed discount."""
    if discount_percent <= 10:
        return "Discounts up to 10 percent can be approved by the account executive."
    if discount_percent <= 15:
        return "Discounts from 11 to 15 percent require regional sales director approval."
    return "Discounts above 15 percent require finance and regional sales director approval."


async def main(model: str, question: str) -> None:
    # This manifest is visible only to the pricing reviewer.
    pricing_manifest = text_manifest(
        {
            "pricing_summary.md": (
                "# Pricing summary\n\n"
                "- Current annual contract: $220,000.\n"
                "- Requested renewal term: 24 months.\n"
                "- Requested discount: 15 percent.\n"
                "- Account executive target discount band: 8 to 10 percent.\n"
            ),
            "commercial_notes.md": (
                "# Commercial notes\n\n"
                "- The customer expanded from 120 to 170 paid seats in the last 6 months.\n"
                "- Procurement asked for one final concession to close before quarter end.\n"
            ),
        }
    )

    # This separate manifest is visible only to the rollout reviewer.
    rollout_manifest = text_manifest(
        {
            "rollout_plan.md": (
                "# Rollout plan\n\n"
                "- Customer wants a 30-day rollout for three new regional teams.\n"
                "- Regional admins have not completed training yet.\n"
                "- SSO migration is scheduled for the second week of the rollout.\n"
            ),
            "support_history.md": (
                "# Support history\n\n"
                "- Two high-priority onboarding tickets were closed in the last quarter.\n"
                "- No open production incidents.\n"
                "- Customer success manager asked for a phased launch if the contract closes.\n"
            ),
        }
    )

    pricing_agent = SandboxAgent(
        name="Pricing Packet Reviewer",
        model=model,
        instructions=(
            "You inspect renewal pricing documents and return a structured commercial review. "
            "Inspect the files before answering and extract the exact requested discount percent "
            "and renewal term from pricing_summary.md. "
            "Use the shell tool before answering. requested_discount_percent must match the exact "
            "integer in pricing_summary.md. requested_term_months must match the exact renewal "
            "term from pricing_summary.md. Do not introduce any facts, incidents, or numbers that "
            "are not present in pricing_summary.md or commercial_notes.md. evidence_files must "
            "list only files you actually inspected."
        ),
        default_manifest=pricing_manifest,
        capabilities=[WorkspaceShellCapability()],
        model_settings=ModelSettings(tool_choice="required"),
        output_type=PricingPacketReview,
    )
    rollout_agent = SandboxAgent(
        name="Rollout Risk Reviewer",
        model=model,
        instructions=(
            "You inspect rollout plans and return a structured delivery review. Inspect the files "
            "before answering and keep the output tightly grounded in the rollout documents. "
            "Use the shell tool before answering. blockers must only contain issues that appear in "
            "rollout_plan.md or support_history.md. Do not introduce any extra numbers, incidents, "
            "or stakeholders beyond those files. evidence_files must list only files you actually "
            "inspected."
        ),
        default_manifest=rollout_manifest,
        capabilities=[WorkspaceShellCapability()],
        model_settings=ModelSettings(tool_choice="required"),
        output_type=RolloutRiskReview,
    )

    # Each sandbox-backed tool gets its own run configuration so the workspaces stay isolated.
    pricing_run_config = RunConfig(sandbox=SandboxRunConfig(client=UnixLocalSandboxClient()))
    rollout_run_config = RunConfig(sandbox=SandboxRunConfig(client=UnixLocalSandboxClient()))

    orchestrator = Agent(
        name="Revenue Operations Coordinator",
        model=model,
        instructions=(
            "You coordinate renewal reviews. Before answering, you must use all three tools: "
            "`review_pricing_packet`, `review_rollout_risk`, and `get_discount_approval_rule`. "
            "The review tools return JSON. Use the exact `requested_discount_percent` field from "
            "`review_pricing_packet` when calling `get_discount_approval_rule`. In the final "
            "recommendation, use only facts and numbers that appear in the tool outputs, and do "
            "not add any extra incidents, price points, or contract terms."
        ),
        model_settings=ModelSettings(tool_choice="required"),
        tools=[
            pricing_agent.as_tool(
                tool_name="review_pricing_packet",
                tool_description="Inspect the pricing packet and summarize commercial risk.",
                custom_output_extractor=_structured_tool_output_extractor,
                run_config=pricing_run_config,
            ),
            rollout_agent.as_tool(
                tool_name="review_rollout_risk",
                tool_description="Inspect the rollout packet and summarize implementation risk.",
                custom_output_extractor=_structured_tool_output_extractor,
                run_config=rollout_run_config,
            ),
            get_discount_approval_rule,
        ],
    )

    result = await Runner.run(orchestrator, question)
    tool_names = [
        tool_call_name(item.raw_item)
        for item in result.new_items
        if getattr(item, "type", None) == "tool_call_item"
    ]
    if tool_names:
        print(f"[tools used] {', '.join(tool_names)}")
    print(result.final_output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.4", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    args = parser.parse_args()

    asyncio.run(main(args.model, args.question))
