"""
Review a small GitHub repository and produce sandbox-generated findings artifacts.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from textwrap import dedent
from typing import cast

from pydantic import BaseModel, Field

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Filesystem, Shell
from agents.sandbox.entries import File, GitRepo

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from examples.sandbox.tutorials.misc import (
    DEFAULT_SANDBOX_IMAGE,
    console,
    create_sandbox_client_and_session,
    load_env_defaults,
    print_event,
)

DEMO_DIR = Path(__file__).resolve().parent
REPO_NAME = "pypa/sampleproject"
REPO_REF = "621e4974ca25ce531773def586ba3ed8e736b3fc"
DEFAULT_QUESTION = (
    "Review this small Python repository as a maintainer. Run the tests, inspect the "
    "project layout, and return exactly two concise line-level findings: one for "
    "`repo/.github/workflows/test.yml` about concrete nox/test installation reliability, "
    "and one for `repo/src/sample/simple.py` about adding explicit type hints to "
    "`add_one`. Return a patch artifact for the obvious `simple.py` type-hint fix."
)
AGENTS_MD = dedent(
    """\
    # AGENTS.md

    Review the mounted repository under `repo/` like a maintainer.

    - Run `uv run python -m unittest discover -s tests` from `repo/` and report a short result summary.
    - Return exactly two findings, using these exact file paths:
      - `repo/.github/workflows/test.yml`: mention nox and a concrete test-tooling/install concern.
      - `repo/src/sample/simple.py`: mention `add_one` and suggest `-> int` type hints.
    - Do not return findings for `pyproject.toml`, `noxfile.py`, README files, or tests.
    - Do not edit the mounted repository. Return the suggested patch text in `fix_patch`.
    - Set `fix_patch` to a minimal git diff that only edits `repo/src/sample/simple.py` by changing
      `def add_one(number):` to `def add_one(number: int) -> int:`.
    - If you inspect files with shell commands, use paths under `repo/`; use `rg`.
    """
)


class ReviewFinding(BaseModel):
    file: str = Field(
        description=(
            "Exact workspace-relative path under repo/. Preserve casing from the workspace file listing."
        )
    )
    line_number: int = Field(description="1-based line number for the review comment.")
    comment: str = Field(
        description=(
            "Concrete review comment for that line. Include a tiny git-diff-style "
            "suggestion in the comment when the fix is obvious."
        )
    )


class RepoReviewResult(BaseModel):
    test_command: str = Field(description="Exact test command that was run.")
    test_result: str = Field(description="Short summary of the test outcome.")
    findings: list[ReviewFinding] = Field(description="Review findings ordered by severity.")
    review_markdown: str = Field(description="Human-readable review summary in Markdown.")
    fix_patch: str | None = Field(
        description="A minimal git diff patch if a fix was made, otherwise null."
    )


def write_review_artifacts(output_dir: Path, review: RepoReviewResult) -> None:
    output_dir.mkdir(exist_ok=True)
    (output_dir / "review.md").write_text(review.review_markdown.strip() + "\n", encoding="utf-8")
    (output_dir / "findings.jsonl").write_text(
        "\n".join(
            json.dumps(finding.model_dump(mode="json"), sort_keys=True)
            for finding in review.findings
        )
        + "\n",
        encoding="utf-8",
    )
    if review.fix_patch:
        (output_dir / "fix.patch").write_text(review.fix_patch.strip() + "\n", encoding="utf-8")


async def main(model: str, question: str, use_docker: bool, image: str) -> None:
    manifest = Manifest(
        entries={
            "AGENTS.md": File(content=AGENTS_MD.encode("utf-8")),
            "repo": GitRepo(repo=REPO_NAME, ref=REPO_REF),
        }
    )
    agent = SandboxAgent(
        name="Code Reviewer",
        model=model,
        instructions=AGENTS_MD,
        capabilities=[Shell(), Filesystem()],
        model_settings=ModelSettings(tool_choice="required"),
        output_type=RepoReviewResult,
    )

    client, sandbox = await create_sandbox_client_and_session(
        manifest=manifest,
        use_docker=use_docker,
        image=image,
    )
    try:
        async with sandbox:
            result = Runner.run_streamed(
                agent,
                [{"role": "user", "content": question}],
                max_turns=25,
                run_config=RunConfig(
                    sandbox=SandboxRunConfig(session=sandbox),
                    tracing_disabled=True,
                    workflow_name="Repo Review example",
                ),
            )
            async for event in result.stream_events():
                print_event(event)
            if result.final_output is None:
                raise RuntimeError("Code Reviewer returned no structured review output.")
            print_event(str(result.final_output).strip())
            review = cast(RepoReviewResult, result.final_output)
    finally:
        await client.delete(sandbox)

    write_review_artifacts(DEMO_DIR / "output", review)
    console.print(f"[green]Wrote review artifacts to {DEMO_DIR / 'output'}[/green]")


if __name__ == "__main__":
    load_env_defaults(DEMO_DIR / ".env")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        default="gpt-5.4-mini",
        help="Model name to use.",
    )
    parser.add_argument(
        "--question",
        default=DEFAULT_QUESTION,
        help="Prompt to send to the agent.",
    )
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Run this example in Docker instead of Unix-local.",
    )
    parser.add_argument(
        "--image",
        default=DEFAULT_SANDBOX_IMAGE,
        help="Docker image to use when --docker is set.",
    )
    args = parser.parse_args()

    asyncio.run(main(args.model, args.question, args.docker, args.image))
