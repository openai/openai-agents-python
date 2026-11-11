"""Docker tax preparation with an application-selected local skills bundle.

This example has been copying the selected directory into the sandbox with a
read-only source grant. The bundle has been providing
`federal-tax-prep/SKILL.md` and its supporting files.

Usage from the repository root:
    uv run python examples/classic/sandbox/tax_prep.py --skills-dir /absolute/path/to/skills
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import cast

from openai.types.responses import ResponseTextDeltaEvent

from agents.classic import Runner
from agents.classic.items import TResponseInputItem
from agents.classic.run import RunConfig
from agents.classic.sandbox import Manifest, SandboxAgent, SandboxPathGrant, SandboxRunConfig
from agents.classic.sandbox.capabilities import Capabilities, Skills
from agents.classic.sandbox.entries import Dir, LocalDir, LocalFile

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


DATA_PATH = Path(__file__).resolve().parent / "data"
W2_PATH = DATA_PATH / "sample_w2.pdf"
FORM_1040_PATH = DATA_PATH / "f1040.pdf"
DEFAULT_IMAGE = "tax-prep:latest"
DEFAULT_QUESTION = "Please generate a 1040 for filing year 2025."

INSTRUCTIONS = """
You are a federal tax filing agent. Your job is to compute year-end taxes and
produce a filled-out Form 1040 for the specified tax year using the user's
provided documents. Use only the information in the supplied files. If required
data is missing or unclear, ask follow-up questions or note explicit
assumptions. Save the finalized, filled PDF in the `output/` directory and
provide a short summary of key amounts such as income, deductions, tax, and
refund or amount due.

This is a demo, so assume the following unless the workspace says otherwise:
1. Filing status is single.
2. SSN is 123-45-6789.
3. Date of birth is 1991-01-01.
4. There are no other income documents.
5. If a minor data point is still needed, make up a clearly synthetic test value.

Use the `federal-tax-prep` skill to accomplish this task.
""".strip()


def _require_docker_dependency():
    try:
        from docker import from_env as docker_from_env  # type: ignore[import-untyped]
    except Exception as exc:  # pragma: no cover - import path depends on local Docker setup
        raise SystemExit(
            "Docker-backed runs require the Docker SDK.\n"
            "Install the repo dependencies with: make sync"
        ) from exc

    from agents.classic.sandbox.sandboxes.docker import (
        DockerSandboxClient,
        DockerSandboxClientOptions,
    )

    return docker_from_env, DockerSandboxClient, DockerSandboxClientOptions


def _build_manifest(skills_dir: Path) -> Manifest:
    return Manifest(
        extra_path_grants=(SandboxPathGrant(path=str(skills_dir), read_only=True),),
        entries={
            "taxpayer_data": Dir(
                children={"sample_w2.pdf": LocalFile(src=W2_PATH)},
                description="Taxpayer income documents such as W-2s and 1099s.",
            ),
            "reference_forms": Dir(
                children={"f1040.pdf": LocalFile(src=FORM_1040_PATH)},
                description="Blank tax forms the agent can use as templates.",
            ),
            "output": Dir(description="Write finalized tax documents here."),
        },
    )


def _build_agent(*, model: str, skills_dir: Path) -> SandboxAgent:
    skills_dir = skills_dir.resolve(strict=True)
    if not (skills_dir / "federal-tax-prep" / "SKILL.md").is_file():
        raise ValueError("The skills directory must contain federal-tax-prep/SKILL.md")
    return SandboxAgent(
        name="Tax Prep Assistant",
        model=model,
        instructions=(
            INSTRUCTIONS + "\n\n"
            "Inspect the workspace before answering. Keep final explanations concise, and make "
            "sure the final filled files are actually written into `output/`."
        ),
        default_manifest=_build_manifest(skills_dir),
        capabilities=Capabilities.default()
        + [
            Skills(
                from_=LocalDir(src=skills_dir),
            ),
        ],
    )


async def _copy_output_dir(
    *,
    session,
    destination_root: Path,
) -> list[Path]:
    destination_root.mkdir(parents=True, exist_ok=True)
    remote_output_root = session.normalize_path("output")

    pending_dirs = [remote_output_root]
    copied_files: list[Path] = []
    while pending_dirs:
        current_dir = pending_dirs.pop()
        for entry in await session.ls(current_dir):
            entry_path = Path(entry.path)
            if entry.is_dir():
                pending_dirs.append(entry_path)
                continue

            relative_path = entry_path.relative_to(remote_output_root)
            local_path = destination_root / relative_path
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


async def _run_turn(
    *,
    agent: SandboxAgent,
    input_items: list[TResponseInputItem],
    run_config: RunConfig,
) -> list[TResponseInputItem]:
    stream_result = Runner.run_streamed(agent, input_items, run_config=run_config)
    saw_text_delta = False
    async for event in stream_result.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            if not saw_text_delta:
                print("assistant> ", end="", flush=True)
                saw_text_delta = True
            print(event.data.delta, end="", flush=True)
            continue

        if event.type == "run_item_stream_event" and event.name == "tool_called":
            raw_item = getattr(event.item, "raw_item", None)
            tool_name = ""
            if isinstance(raw_item, dict):
                tool_name = cast(str, raw_item.get("name") or raw_item.get("type") or "")
            else:
                tool_name = cast(
                    str,
                    getattr(raw_item, "name", None) or getattr(raw_item, "type", None) or "",
                )
            if tool_name:
                if saw_text_delta:
                    print()
                    saw_text_delta = False
                print(f"[tool call] {tool_name}")

    if saw_text_delta:
        print()

    return stream_result.to_input_list()


async def main(
    *,
    model: str,
    image: str,
    question: str,
    output_dir: Path,
    skills_dir: Path,
) -> None:
    agent = _build_agent(model=model, skills_dir=skills_dir)
    docker_from_env, DockerSandboxClient, DockerSandboxClientOptions = _require_docker_dependency()
    client = DockerSandboxClient(docker_from_env())
    sandbox = await client.create(
        manifest=agent.default_manifest,
        options=DockerSandboxClientOptions(image=image),
    )

    run_config = RunConfig(
        sandbox=SandboxRunConfig(session=sandbox),
        workflow_name="Sandbox tax prep demo",
    )

    conversation: list[TResponseInputItem] = [{"role": "user", "content": question}]

    try:
        async with sandbox:
            conversation = await _run_turn(
                agent=agent,
                input_items=conversation,
                run_config=run_config,
            )

            while True:
                try:
                    additional_input = input("> ")
                except (EOFError, KeyboardInterrupt):
                    break

                conversation.append({"role": "user", "content": additional_input})
                conversation = await _run_turn(
                    agent=agent,
                    input_items=conversation,
                    run_config=run_config,
                )

            copied_files = await _copy_output_dir(session=sandbox, destination_root=output_dir)
    finally:
        await client.delete(sandbox)

    print(f"\nCopied {len(copied_files)} file(s) to {output_dir}")
    for copied_file in copied_files:
        print(copied_file)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-5.6-sol", help="Model name to use.")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="Docker image for the sandbox.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument(
        "--output-dir",
        default="tax-prep-results",
        help="Local directory where files from sandbox output/ will be copied.",
    )
    parser.add_argument(
        "--skills-dir",
        type=Path,
        required=True,
        help="Local skills directory containing federal-tax-prep/SKILL.md.",
    )
    args = parser.parse_args()

    asyncio.run(
        main(
            model=args.model,
            image=args.image,
            question=args.question,
            output_dir=Path(args.output_dir).resolve(),
            skills_dir=args.skills_dir,
        )
    )
