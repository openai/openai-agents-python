"""Inspect a request without sampling, uploading files or executing tools.

Run with a model name and optionally --codex. A local check cannot establish
whether the account has access to that model or what host policy permits.
With --codex, the SDK workspace profile blocks native workspace execution by
supplying no execution environments. Native tools may still be advertised, and
native child model requests can still run. Shared child budgets remain unsupported.
"""

import argparse
import asyncio

from agents import ModelRequest, ToolExecutionConfig
from agents.decorators import tool
from agents.models.codex import CodexProvider
from agents.models.openai_provider import OpenAIProvider


@tool
def inspect_workspace() -> str:
    """Return an application-owned workspace summary."""
    return "No pending changes."


async def main(name: str, use_codex: bool) -> None:
    # The placeholder key is sufficient because this example makes no API request.
    provider = CodexProvider() if use_codex else OpenAIProvider(api_key="unused-for-preflight")
    try:
        model = provider.get_model(name)
        report = model.preflight(
            ModelRequest(
                input="Summarize the workspace.",
                tools=[inspect_workspace],
                tool_execution=ToolExecutionConfig(workspace_execution="sdk"),
            )
        )
        print(report.adapter, report.static_validation)
        for route in report.tools:
            print(route.name, route.executor)
        for check in report.unchecked:
            print("Unchecked:", check)
        report.raise_for_errors()
    finally:
        await provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model")
    parser.add_argument("--codex", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.model, args.codex))
