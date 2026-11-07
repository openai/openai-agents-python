"""Require a saved report before accepting the agent's final answer."""

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from agents import (
    Agent,
    CompletionContext,
    ContinueTurn,
    RunBudget,
    ToolContext,
    function_tool,
)


@dataclass
class Report:
    path: Path


@function_tool
def save_report(ctx: ToolContext[Report], text: str) -> str:
    """Save the completed report."""
    ctx.context.path.write_text(text, encoding="utf-8")
    return "Report saved."


def require_report(request: CompletionContext[Report]) -> ContinueTurn | None:
    if not request.context.path.is_file():
        return ContinueTurn("Save the report with save_report before finishing.")
    return None


def main() -> None:
    agent = Agent(
        instructions="Write a short project report and save it with save_report.",
        tools=[save_report],
        before_complete=require_report,
    )
    with TemporaryDirectory() as directory:
        report = Report(Path(directory) / "report.md")
        result = agent.run_sync(
            "The prototype is complete. The deployment date is still undecided.",
            context=report,
            budget=RunBudget(max_requests=5),
        )
        print(result.final_output)
        print(report.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
