"""
Show how a sandbox agent can combine three tool sources in one run.

This example gives the model:

1. A sandbox workspace to inspect with the shared shell capability.
2. A normal local function tool for approval routing.
3. A local stdio MCP server for reference policy lookups.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from agents import Runner, function_tool
from agents.mcp import MCPServerStdio
from agents.run import RunConfig
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.sandbox.misc.example_support import text_manifest, tool_call_name
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

DEFAULT_QUESTION = (
    "Review this enterprise renewal request. Tell me who needs to approve the discount, "
    "whether security review is still open, and the most important note for the account team. "
    "Confirm the approval and security answers against the reference policy server before you respond."
)


@function_tool
def get_discount_approval_path(discount_percent: int) -> str:
    """Return the approver required for a proposed discount percentage."""
    if discount_percent <= 10:
        return "The account executive can approve discounts up to 10 percent."
    if discount_percent <= 15:
        return "The regional sales director must approve discounts from 11 to 15 percent."
    return "Finance and the regional sales director must both approve discounts above 15 percent."


async def main(model: str, question: str) -> None:
    # This manifest becomes the workspace that the sandbox agent can inspect.
    manifest = text_manifest(
        {
            "renewal_request.md": (
                "# Renewal request\n\n"
                "- Customer: Contoso Manufacturing.\n"
                "- Requested discount: 14 percent.\n"
                "- Renewal term: 12 months.\n"
                "- Requested close date: March 28.\n"
            ),
            "account_notes.md": (
                "# Account notes\n\n"
                "- The customer expanded usage in two plants this quarter.\n"
                "- Security review for the new data export workflow was opened last week.\n"
                "- Procurement wants a final approval map before they send the order form.\n"
            ),
        }
    )

    # The reference MCP server is another local process. The agent can call its tools alongside
    # the sandbox shell tool and the normal Python function tool.
    async with MCPServerStdio(
        name="Reference Policy Server",
        params={
            "command": sys.executable,
            "args": [
                str(Path(__file__).resolve().parent / "misc" / "reference_policy_mcp_server.py")
            ],
        },
    ) as server:
        agent = SandboxAgent(
            name="Renewal Review Assistant",
            model=model,
            instructions=(
                "You review renewal requests. Inspect the packet, use "
                "`get_discount_approval_path` for discount routing, and use the MCP reference "
                "policy server when you need confirmation. Before you answer, you must call "
                "`get_discount_approval_path` and at least one MCP policy tool. "
                "Keep the answer concise and business-ready. Mention which policy topic you "
                "confirmed through MCP."
            ),
            default_manifest=manifest,
            tools=[get_discount_approval_path],
            mcp_servers=[server],
            capabilities=[WorkspaceShellCapability()],
        )

        result = await Runner.run(
            agent,
            question,
            run_config=RunConfig(sandbox=SandboxRunConfig(client=UnixLocalSandboxClient())),
        )
        tool_names: list[str] = []
        for item in result.new_items:
            if getattr(item, "type", None) != "tool_call_item":
                continue
            name = tool_call_name(item.raw_item)
            if name:
                tool_names.append(name)
        if tool_names:
            print(f"[tools used] {', '.join(tool_names)}")
        print(result.final_output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.4", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    args = parser.parse_args()

    asyncio.run(main(args.model, args.question))
