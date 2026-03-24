from __future__ import annotations

import argparse
import asyncio
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agents import Agent, ModelSettings, Runner, gen_trace_id, trace
from agents.extensions.experimental.codex import (
    CodexToolStreamEvent,
    ThreadErrorEvent,
    ThreadOptions,
    ThreadStartedEvent,
    TurnCompletedEvent,
    TurnFailedEvent,
    TurnStartedEvent,
    codex_tool,
)
from examples.auto_mode import input_with_fallback, is_auto_mode

DEFAULT_PROMPT = (
    "Build a tiny Python CLI todo manager with add, list, complete, and search commands. "
    "Store data in a local JSON file and add pytest coverage for the core flows."
)
HARNESS_DIR_NAME = ".codex-harness"


class HarnessContext(BaseModel):
    codex_thread_id_builder: str | None = None
    codex_thread_id_qa: str | None = None


class BuildPlan(BaseModel):
    project_name: str
    product_goal: str
    architecture: list[str] = Field(default_factory=list)
    milestones: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    qa_focus: list[str] = Field(default_factory=list)


class BuildRoundReport(BaseModel):
    summary: str
    completed_work: list[str] = Field(default_factory=list)
    validations_run: list[str] = Field(default_factory=list)
    remaining_risks: list[str] = Field(default_factory=list)


class EvaluationIssue(BaseModel):
    severity: Literal["high", "medium", "low"]
    title: str
    evidence: str
    recommendation: str


class EvaluationReport(BaseModel):
    verdict: Literal["pass", "revise"]
    summary: str
    strengths: list[str] = Field(default_factory=list)
    issues: list[EvaluationIssue] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a planner / builder / QA coding harness around the experimental Codex tool."
        )
    )
    parser.add_argument(
        "--prompt",
        help="Short product request to expand into a plan and implement.",
    )
    parser.add_argument(
        "--working-directory",
        help=(
            "Target workspace for Codex. If omitted, the example creates a temporary scratch "
            "workspace so it does not mutate this repository."
        ),
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help="Maximum builder / QA rounds. Defaults to 2, or 1 in auto mode.",
    )
    return parser.parse_args()


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    timestamp = _timestamp()
    lines = str(message).splitlines() or [""]
    for line in lines:
        print(f"{timestamp} {line}")


def make_stream_logger(label: str):
    async def _on_stream(payload: CodexToolStreamEvent) -> None:
        event = payload.event
        if isinstance(event, ThreadStartedEvent):
            log(f"{label}: Codex thread started: {event.thread_id}")
            return
        if isinstance(event, TurnStartedEvent):
            log(f"{label}: Codex turn started")
            return
        if isinstance(event, TurnCompletedEvent):
            log(f"{label}: Codex turn completed: {event.usage}")
            return
        if isinstance(event, TurnFailedEvent):
            log(f"{label}: Codex turn failed: {event.error.message}")
            return
        if isinstance(event, ThreadErrorEvent):
            log(f"{label}: Codex stream error: {event.message}")

    return _on_stream


def create_scratch_workspace() -> Path:
    workspace = Path(tempfile.mkdtemp(prefix="codex-builder-qa-"))
    (workspace / "src" / "scratch_app").mkdir(parents=True, exist_ok=True)
    (workspace / "tests").mkdir(parents=True, exist_ok=True)

    (workspace / "README.md").write_text(
        "# Scratch Workspace\n\n"
        "This temporary workspace is used by the Codex builder / QA example.\n",
        encoding="utf-8",
    )
    (workspace / "AGENTS.md").write_text(
        "# Local Instructions\n\n"
        "- Keep dependencies minimal.\n"
        "- Prefer Python standard library where reasonable.\n"
        "- Add or update pytest tests for shipped behavior.\n"
        "- Use ASCII unless a file already needs Unicode.\n",
        encoding="utf-8",
    )
    (workspace / ".gitignore").write_text(
        f"__pycache__/\n.pytest_cache/\n.venv/\n*.pyc\n{HARNESS_DIR_NAME}/\n",
        encoding="utf-8",
    )
    (workspace / "pyproject.toml").write_text(
        "[project]\n"
        'name = "scratch-app"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.10"\n'
        "dependencies = []\n\n"
        "[build-system]\n"
        'requires = ["setuptools>=68"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n',
        encoding="utf-8",
    )
    (workspace / "src" / "scratch_app" / "__init__.py").write_text("", encoding="utf-8")
    return workspace


def write_artifact(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")


def plan_artifact_path(workspace: Path) -> Path:
    return workspace / HARNESS_DIR_NAME / "plan.json"


def build_artifact_path(workspace: Path, round_number: int) -> Path:
    return workspace / HARNESS_DIR_NAME / f"build_round_{round_number}.json"


def qa_artifact_path(workspace: Path, round_number: int) -> Path:
    return workspace / HARNESS_DIR_NAME / f"qa_round_{round_number}.json"


def create_planner_agent() -> Agent:
    return Agent(
        name="planner_agent",
        instructions=(
            "You are the planner in a long-running coding harness. Expand a terse product request "
            "into a concrete build plan that a coding agent and a QA agent can execute. Be "
            "ambitious enough to produce a real product, but keep the scope small enough that it "
            "can plausibly converge in a few implementation rounds. Focus on product behavior, "
            "verification criteria, and likely risks rather than low-level implementation trivia."
        ),
        output_type=BuildPlan,
    )


def create_builder_agent(workspace: Path) -> Agent[HarnessContext]:
    return Agent(
        name="generator_agent",
        instructions=(
            "You are the generator in a planner / generator / evaluator coding harness. Always use "
            "the codex_builder tool. Work inside the target workspace, implement the highest-value "
            "remaining product slice, and avoid claiming credit for work you did not verify. Favor "
            "coherent, working behavior over broad but stubbed scope. Before you finish each round, "
            "run the most relevant local checks you can and inspect the changed files."
        ),
        tools=[
            codex_tool(
                name="codex_builder",
                sandbox_mode="workspace-write",
                working_directory=str(workspace),
                skip_git_repo_check=True,
                default_thread_options=ThreadOptions(
                    model="gpt-5.4",
                    model_reasoning_effort="medium",
                    network_access_enabled=False,
                    web_search_mode="disabled",
                    approval_policy="never",
                ),
                on_stream=make_stream_logger("builder"),
                use_run_context_thread_id=True,
            )
        ],
        model_settings=ModelSettings(tool_choice="required"),
        output_type=BuildRoundReport,
    )


def create_qa_agent(workspace: Path) -> Agent[HarnessContext]:
    return Agent(
        name="evaluator_agent",
        instructions=(
            "You are the evaluator in a planner / generator / evaluator coding harness. Always use "
            "the codex_qa tool. Be skeptical. If a core requirement is stubbed, broken, or not "
            "actually verified, fail the round. Inspect the workspace, run the most relevant "
            "checks you can, and return concrete evidence plus specific next actions. Do not edit "
            "files unless absolutely required to inspect state, and never fix issues yourself."
        ),
        tools=[
            codex_tool(
                name="codex_qa",
                sandbox_mode="workspace-write",
                working_directory=str(workspace),
                skip_git_repo_check=True,
                default_thread_options=ThreadOptions(
                    model="gpt-5.4",
                    model_reasoning_effort="medium",
                    network_access_enabled=False,
                    web_search_mode="disabled",
                    approval_policy="never",
                ),
                on_stream=make_stream_logger("qa"),
                use_run_context_thread_id=True,
            )
        ],
        model_settings=ModelSettings(tool_choice="required"),
        output_type=EvaluationReport,
    )


def build_generator_prompt(
    *,
    user_prompt: str,
    workspace: Path,
    plan: BuildPlan,
    round_number: int,
    max_rounds: int,
    latest_feedback: EvaluationReport | None,
) -> str:
    feedback_text = (
        latest_feedback.model_dump_json(indent=2)
        if latest_feedback
        else "No previous QA feedback. This is the first implementation round."
    )
    return (
        f"User request:\n{user_prompt}\n\n"
        f"Workspace:\n{workspace}\n\n"
        f"Plan artifact:\n{plan_artifact_path(workspace)}\n\n"
        f"Round:\n{round_number} of {max_rounds}\n\n"
        "Build plan:\n"
        f"{plan.model_dump_json(indent=2)}\n\n"
        "Latest QA feedback:\n"
        f"{feedback_text}\n\n"
        "Use the codex_builder tool to implement the next highest-value improvements now. "
        "If previous QA feedback exists, prioritize fixing those issues before expanding scope. "
        "Keep the workspace in a runnable state."
    )


def build_qa_prompt(
    *,
    user_prompt: str,
    workspace: Path,
    plan: BuildPlan,
    round_number: int,
    build_report: BuildRoundReport,
) -> str:
    return (
        f"User request:\n{user_prompt}\n\n"
        f"Workspace:\n{workspace}\n\n"
        f"Plan artifact:\n{plan_artifact_path(workspace)}\n\n"
        f"Builder report artifact:\n{build_artifact_path(workspace, round_number)}\n\n"
        f"Round:\n{round_number}\n\n"
        "Build plan:\n"
        f"{plan.model_dump_json(indent=2)}\n\n"
        "Latest builder report:\n"
        f"{build_report.model_dump_json(indent=2)}\n\n"
        "Use the codex_qa tool to inspect the workspace and decide whether the app is good enough "
        "to stop. Fail if core acceptance criteria are missing, broken, or unverified. Prefer "
        "concrete evidence such as commands run, files inspected, or observable behavior."
    )


async def main() -> None:
    args = parse_args()
    auto_mode = is_auto_mode()
    max_rounds = args.max_rounds if args.max_rounds is not None else (1 if auto_mode else 2)
    if max_rounds < 1:
        raise ValueError("--max-rounds must be at least 1.")

    user_prompt = args.prompt or input_with_fallback(
        "What should the harness build? ",
        DEFAULT_PROMPT,
    )

    if args.working_directory:
        workspace = Path(args.working_directory).expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        created_scratch = False
    else:
        workspace = create_scratch_workspace()
        created_scratch = True

    planner_agent = create_planner_agent()
    builder_agent = create_builder_agent(workspace)
    qa_agent = create_qa_agent(workspace)
    context = HarnessContext()

    trace_id = gen_trace_id()
    log(f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}")
    log(f"Workspace: {workspace}")
    if created_scratch:
        log("Using a temporary scratch workspace so this repository stays untouched.")

    with trace("Codex builder / QA harness", trace_id=trace_id):
        log("Planning build...")
        plan_result = await Runner.run(planner_agent, user_prompt)
        plan = plan_result.final_output_as(BuildPlan)
        write_artifact(plan_artifact_path(workspace), plan)
        log(f"Plan ready for {plan.project_name}")
        log(f"Milestones: {len(plan.milestones)}")

        latest_feedback: EvaluationReport | None = None
        final_evaluation: EvaluationReport | None = None

        for round_number in range(1, max_rounds + 1):
            log(f"Starting build round {round_number}/{max_rounds}...")
            build_result = await Runner.run(
                builder_agent,
                build_generator_prompt(
                    user_prompt=user_prompt,
                    workspace=workspace,
                    plan=plan,
                    round_number=round_number,
                    max_rounds=max_rounds,
                    latest_feedback=latest_feedback,
                ),
                context=context,
            )
            build_report = build_result.final_output_as(BuildRoundReport)
            write_artifact(build_artifact_path(workspace, round_number), build_report)
            log(f"Builder summary: {build_report.summary}")
            log(f"Builder thread: {context.codex_thread_id_builder}")

            log(f"Starting QA round {round_number}/{max_rounds}...")
            qa_result = await Runner.run(
                qa_agent,
                build_qa_prompt(
                    user_prompt=user_prompt,
                    workspace=workspace,
                    plan=plan,
                    round_number=round_number,
                    build_report=build_report,
                ),
                context=context,
            )
            evaluation = qa_result.final_output_as(EvaluationReport)
            write_artifact(qa_artifact_path(workspace, round_number), evaluation)
            latest_feedback = evaluation
            final_evaluation = evaluation

            log(f"QA verdict: {evaluation.verdict}")
            log(f"QA summary: {evaluation.summary}")
            log(f"QA thread: {context.codex_thread_id_qa}")

            for issue in evaluation.issues:
                log(f"QA issue [{issue.severity}]: {issue.title}")

            if evaluation.verdict == "pass":
                log("Harness converged successfully.")
                break

        if final_evaluation is None:
            raise RuntimeError("No QA evaluation was produced.")

        log("")
        log("Final result")
        log(f"Project: {plan.project_name}")
        log(f"Verdict: {final_evaluation.verdict}")
        log(f"Artifacts: {workspace / HARNESS_DIR_NAME}")
        if final_evaluation.verdict != "pass":
            log("Stopped after the configured round limit.")


if __name__ == "__main__":
    asyncio.run(main())
