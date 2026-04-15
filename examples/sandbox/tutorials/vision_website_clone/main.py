"""
Clone a reference app screenshot as static HTML/CSS with the sandbox filesystem tools.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from textwrap import dedent

from agents import ModelSettings, Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig, WorkspaceReadNotFoundError
from agents.sandbox.capabilities import (
    Filesystem,
    LocalDirLazySkillSource,
    Shell,
    Skills,
)
from agents.sandbox.entries import Dir, File, LocalDir, LocalFile
from agents.sandbox.session import BaseSandboxSession

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
REFERENCE_IMAGE = DEMO_DIR / "reference-site.png"
SKILLS_SOURCE_DIR = DEMO_DIR / "skills"
SANDBOX_SITE_DIR = Path("output") / "site"
REMOTE_REVIEW_ARTIFACTS = (
    Path("output") / "screenshots" / "draft-1.png",
    Path("output") / "screenshots" / "draft-2.png",
    Path("output") / "visual-notes.md",
)
DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_QUESTION = (
    "Inspect the reference screenshot and build a static HTML/CSS reproduction of the "
    "screen. Write output/site/index.html and output/site/styles.css, then capture "
    "browser screenshots, inspect them, and revise the site."
)
AGENTS_MD = dedent(
    """\
    # Vision UI Reproduction Instructions

    Create a static HTML/CSS reproduction of the provided reference screenshot.

    Build only the single screen shown in the reference.

    ## Required workflow (must do)

    - First call `view_image` on `reference/reference-site.png`.
    - Before writing code, write `output/visual-notes.md` with brief layout + typography notes.
    - Write the site to `output/site/index.html` and `output/site/styles.css`.
    - Before taking screenshots, call `load_skill("playwright")` and read `skills/playwright/SKILL.md`.
    - Capture `output/screenshots/draft-1.png`, inspect it, revise, then capture `output/screenshots/draft-2.png`.
    - Do not finish without the screenshots.
    """
)


def build_manifest() -> Manifest:
    return Manifest(
        entries={
            "AGENTS.md": File(content=AGENTS_MD.encode("utf-8")),
            "reference": Dir(
                children={
                    "reference-site.png": LocalFile(src=REFERENCE_IMAGE),
                },
                description="Reference app screenshot to clone.",
            ),
            "output": Dir(description="Write generated website files here."),
        }
    )


def build_agent(model: str) -> SandboxAgent:
    return SandboxAgent(
        name="Vision Website Clone Builder",
        model=model,
        instructions=AGENTS_MD,
        capabilities=[
            Shell(),
            Filesystem(),
            Skills(
                lazy_from=LocalDirLazySkillSource(source=LocalDir(src=SKILLS_SOURCE_DIR)),
                skills_path="skills",
            ),
        ],
        model_settings=ModelSettings(tool_choice="required"),
    )


async def copy_site_output_dir(
    *,
    session: BaseSandboxSession,
    output_dir: Path,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    remote_site_dir = session.normalize_path(SANDBOX_SITE_DIR)
    pending_dirs = [remote_site_dir]
    copied_files: list[Path] = []

    while pending_dirs:
        current_dir = pending_dirs.pop()
        for entry in await session.ls(current_dir):
            entry_path = Path(entry.path)
            if entry.is_dir():
                pending_dirs.append(entry_path)
                continue

            relative_path = entry_path.relative_to(remote_site_dir)
            local_path = output_dir / relative_path
            local_path.parent.mkdir(parents=True, exist_ok=True)

            handle = await session.read(entry_path)
            try:
                payload = handle.read()
            finally:
                handle.close()

            if isinstance(payload, str):
                local_path.write_text(payload, encoding="utf-8")
            else:
                local_path.write_bytes(bytes(payload))
            copied_files.append(local_path)

    return copied_files


async def copy_review_artifacts(
    *,
    session: BaseSandboxSession,
    output_dir: Path,
    remote_artifacts: tuple[Path, ...] = REMOTE_REVIEW_ARTIFACTS,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    copied_files: list[Path] = []

    for remote_artifact in remote_artifacts:
        remote_path = session.normalize_path(remote_artifact)
        relative_artifact = remote_artifact.relative_to(Path("output"))
        local_path = output_dir / relative_artifact
        local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            handle = await session.read(remote_path)
        except WorkspaceReadNotFoundError:
            continue
        try:
            payload = handle.read()
        finally:
            handle.close()

        if isinstance(payload, str):
            local_path.write_text(payload, encoding="utf-8")
        else:
            local_path.write_bytes(bytes(payload))
        copied_files.append(local_path)

    return copied_files


async def main(model: str, question: str, use_docker: bool, image: str, output_dir: Path) -> None:
    client, sandbox = await create_sandbox_client_and_session(
        manifest=build_manifest(),
        use_docker=use_docker,
        image=image,
    )
    try:
        async with sandbox:
            result = Runner.run_streamed(
                build_agent(model),
                [{"role": "user", "content": question}],
                max_turns=30,
                run_config=RunConfig(
                    sandbox=SandboxRunConfig(session=sandbox),
                    tracing_disabled=True,
                    workflow_name="Vision Website Clone example",
                ),
            )
            async for event in result.stream_events():
                print_event(event)
            if result.final_output is None:
                raise RuntimeError("Vision Website Clone Builder returned no final message.")
            print_event(str(result.final_output).strip())
            copied_files = await copy_site_output_dir(session=sandbox, output_dir=output_dir)
            copied_review_files = await copy_review_artifacts(
                session=sandbox,
                output_dir=output_dir,
            )
    finally:
        await client.delete(sandbox)

    expected_files = {output_dir / "index.html", output_dir / "styles.css"}
    if not expected_files <= set(copied_files):
        raise RuntimeError(
            "Vision Website Clone Builder must write output/site/index.html and "
            "output/site/styles.css."
        )

    console.print(f"[green]Copied static site to {output_dir / 'index.html'}[/green]")
    for path in copied_review_files:
        console.print(f"[green]Copied review artifact to {path}[/green]")


if __name__ == "__main__":
    load_env_defaults(DEMO_DIR / ".env")

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
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
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEMO_DIR / "output",
        help="Directory for copied website files.",
    )
    args = parser.parse_args()

    asyncio.run(main(args.model, args.question, args.docker, args.image, args.output_dir))
