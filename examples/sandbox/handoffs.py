"""
Show how a non-sandbox agent can hand work to a sandbox agent.

The intake agent never sees a workspace directly. It hands document-heavy work
to a sandbox reviewer, and that reviewer then hands the synthesized result to a
plain account-facing writer.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from agents import Agent, Runner
from agents.run import RunConfig
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.sandbox.misc.example_support import text_manifest
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

DEFAULT_QUESTION = (
    "Review the attached onboarding packet and draft a short internal note for the account "
    "executive about what to confirm before kickoff."
)


async def main(model: str, question: str) -> None:
    # The manifest becomes the workspace that only the sandbox reviewer can inspect.
    manifest = text_manifest(
        {
            "customer_background.md": (
                "# Customer background\n\n"
                "- Customer: Bluebird Logistics.\n"
                "- Region: North America.\n"
                "- New purchase: analytics workspace plus SSO.\n"
            ),
            "kickoff_checklist.md": (
                "# Kickoff checklist\n\n"
                "- Security questionnaire is still in review.\n"
                "- Two customer admins still need to complete access training.\n"
                "- Target kickoff date is next Tuesday.\n"
            ),
            "implementation_scope.md": (
                "# Implementation scope\n\n"
                "- The customer wants historical data migration for 5 years of records.\n"
                "- Data engineering support is available only starting next month.\n"
            ),
        }
    )

    # This final agent does not inspect files. It only rewrites reviewed facts into a note.
    account_manager = Agent(
        name="Account Executive Assistant",
        model=model,
        instructions=(
            "You write concise internal updates for account teams. Convert the sandbox review "
            "into a short note with a headline, the top risks, and a recommended next step."
        ),
    )

    # This sandbox agent can inspect the workspace, then hand its findings to the writer above.
    sandbox_reviewer = SandboxAgent(
        name="Onboarding Packet Reviewer",
        model=model,
        instructions=(
            "You inspect onboarding documents in the sandbox, verify the facts, then hand off "
            "to the account executive assistant to draft the final note. Do not answer the user "
            "directly after reviewing the packet."
        ),
        default_manifest=manifest,
        handoffs=[account_manager],
        capabilities=[WorkspaceShellCapability()],
    )

    # The starting agent is a normal agent. It only decides when to hand off into the sandbox.
    intake_agent = Agent(
        name="Deal Desk Intake",
        model=model,
        instructions=(
            "You triage internal requests. If a request depends on attached documents, hand off "
            "to the onboarding packet reviewer immediately."
        ),
        handoffs=[sandbox_reviewer],
    )

    result = await Runner.run(
        intake_agent,
        question,
        run_config=RunConfig(sandbox=SandboxRunConfig(client=UnixLocalSandboxClient())),
    )
    print(result.final_output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.5", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    args = parser.parse_args()

    asyncio.run(main(args.model, args.question))
