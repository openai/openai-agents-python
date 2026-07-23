"""Use an ACA sandbox for a simple code-writing agent."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import cast

from agents import ModelSettings, Runner
from agents.extensions.sandbox import ACASandboxesClient, ACASandboxesClientOptions
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Filesystem, Shell
from agents.sandbox.entries import File
from agents.sandbox.session import SandboxSession


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    raise RuntimeError(f"{name} must be set.")


async def _read_text(sandbox: SandboxSession, path: str) -> str:
    stream = await sandbox.read(Path(path))
    value = cast(str | bytes, stream.read())
    return value.decode("utf-8") if isinstance(value, bytes) else value


async def main() -> None:
    _required_env("OPENAI_API_KEY")

    manifest = Manifest(
        entries={
            "SPEC.md": File(
                content=(
                    b"# Slugify package\n\n"
                    b"Create `src/slugify.py` with a `slugify(value: str) -> str` function.\n"
                    b"The function must lowercase text, replace runs of non-alphanumeric "
                    b"characters with one hyphen, and remove leading or trailing hyphens.\n"
                    b"Create `tests/test_slugify.py` using the standard-library `unittest` "
                    b"module. Include normal, whitespace, punctuation, and empty-input cases.\n"
                )
            )
        }
    )
    client = ACASandboxesClient(
        region=_required_env("ACA_SANDBOXGROUP_REGION"),
        subscription_id=_required_env("AZURE_SUBSCRIPTION_ID"),
        resource_group=_required_env("ACA_RESOURCE_GROUP"),
        sandbox_group=_required_env("ACA_SANDBOX_GROUP"),
    )
    sandbox = await client.create(
        manifest=manifest,
        options=ACASandboxesClientOptions(
            disk="ubuntu",
            auto_suspend_seconds=300,
            auto_suspend_mode="Memory",
        ),
    )

    try:
        await sandbox.start()
        agent = SandboxAgent(
            name="ACA Code Writer",
            model=os.environ.get("OPENAI_MODEL", "gpt-5.6-sol"),
            instructions=(
                "You are a code-writing agent. Read SPEC.md, implement exactly what it requests, "
                "and run `python -m unittest discover -s tests -v`. Fix the implementation until "
                "all tests pass. Use the filesystem and shell tools; do not merely describe code."
            ),
            default_manifest=manifest,
            capabilities=[Filesystem(), Shell()],
            model_settings=ModelSettings(tool_choice="required"),
        )
        result = await Runner.run(
            agent,
            "Implement the package described in SPEC.md and verify it.",
            run_config=RunConfig(
                sandbox=SandboxRunConfig(session=sandbox),
                workflow_name="ACA code-writing agent example",
            ),
        )

        print(f"\nAgent result:\n{result.final_output}")
        print(f"\nsrc/slugify.py:\n{await _read_text(sandbox, 'src/slugify.py')}")
        print(f"\ntests/test_slugify.py:\n{await _read_text(sandbox, 'tests/test_slugify.py')}")
    finally:
        await client.delete(sandbox)
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
