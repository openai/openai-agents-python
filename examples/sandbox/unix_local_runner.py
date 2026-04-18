"""
Start here if you want the simplest Unix-local sandbox example.

This file mirrors the Docker example, but the sandbox runs as a temporary local
workspace on macOS or Linux instead of inside a Docker container.
"""

import argparse
import asyncio
import io
import sys
import tempfile
from pathlib import Path

from openai.types.responses import ResponseTextDeltaEvent

from agents import Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxPathGrant, SandboxRunConfig
from agents.sandbox.errors import WorkspaceArchiveWriteError
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from examples.sandbox.misc.example_support import text_manifest
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

DEFAULT_QUESTION = (
    "Review this renewal packet. Summarize the customer's situation, the likely blockers, "
    "and the next two actions an account team should take."
)


def _build_manifest(external_dir: Path, scratch_dir: Path) -> Manifest:
    # The manifest is the file tree that will be materialized into the sandbox workspace.
    return text_manifest(
        {
            "account_brief.md": (
                "# Northwind Health\n\n"
                "- Segment: Mid-market healthcare analytics provider.\n"
                "- Annual contract value: $148,000.\n"
                "- Renewal date: 2026-04-15.\n"
                "- Executive sponsor: Director of Data Operations.\n"
            ),
            "renewal_request.md": (
                "# Renewal request\n\n"
                "Northwind requested a 12 percent discount in exchange for a two-year renewal. "
                "They also want a 45-day implementation timeline for a new reporting workspace.\n"
            ),
            "usage_notes.md": (
                "# Usage notes\n\n"
                "- Weekly active users increased 18 percent over the last quarter.\n"
                "- API traffic is stable.\n"
                "- The customer still has one unresolved SSO configuration issue from onboarding.\n"
            ),
            "implementation_risks.md": (
                "# Delivery risks\n\n"
                "- Security questionnaire for the new reporting workspace is not complete.\n"
                "- Customer procurement requires final legal language by April 1.\n"
            ),
        }
    ).model_copy(
        update={
            "extra_path_grants": (
                SandboxPathGrant(
                    path=str(external_dir),
                    read_only=True,
                    description="read-only external renewal packet notes",
                ),
                SandboxPathGrant(
                    path=str(scratch_dir),
                    description="temporary renewal packet scratch files",
                ),
            )
        },
        deep=True,
    )


async def _verify_extra_path_grants() -> None:
    with tempfile.TemporaryDirectory(prefix="agents-unix-local-extra-") as extra_root_text:
        extra_root = Path(extra_root_text)
        external_dir = extra_root / "external"
        scratch_dir = extra_root / "scratch"
        external_dir.mkdir()
        scratch_dir.mkdir()
        external_input = external_dir / "external_input.txt"
        read_only_output = external_dir / "blocked.txt"
        sdk_output = scratch_dir / "sdk_output.txt"
        exec_output = scratch_dir / "exec_output.txt"
        external_input.write_text("external grant input\n", encoding="utf-8")

        client = UnixLocalSandboxClient()
        sandbox = await client.create(manifest=_build_manifest(external_dir, scratch_dir))
        try:
            async with sandbox:
                payload = await sandbox.read(external_input)
                try:
                    await sandbox.write(read_only_output, io.BytesIO(b"should fail\n"))
                except WorkspaceArchiveWriteError:
                    pass
                else:
                    raise RuntimeError(
                        "SDK write to read-only extra path grant unexpectedly worked."
                    )
                await sandbox.write(sdk_output, io.BytesIO(b"sdk grant output\n"))
                exec_result = await sandbox.exec(
                    "sh",
                    "-c",
                    'cat "$1"; printf "%s\\n" "exec grant output" > "$2"',
                    "sh",
                    external_input,
                    exec_output,
                    shell=False,
                )

                if payload.read() != b"external grant input\n":
                    raise RuntimeError(
                        "SDK read from extra path grant returned unexpected content."
                    )
                if sdk_output.read_text(encoding="utf-8") != "sdk grant output\n":
                    raise RuntimeError("SDK write to extra path grant failed.")
                if exec_result.stdout != b"external grant input\n" or exec_result.exit_code != 0:
                    raise RuntimeError("Shell read from extra path grant failed.")
                if exec_output.read_text(encoding="utf-8") != "exec grant output\n":
                    raise RuntimeError("Shell write to extra path grant failed.")
        finally:
            await client.delete(sandbox)

    print("extra_path_grants verification passed")


async def main(model: str, question: str, stream: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="agents-unix-local-extra-") as extra_root_text:
        extra_root = Path(extra_root_text)
        external_dir = extra_root / "external"
        scratch_dir = extra_root / "scratch"
        external_dir.mkdir()
        scratch_dir.mkdir()
        external_note = external_dir / "external_renewal_note.md"
        scratch_note = scratch_dir / "scratch_summary.md"
        external_note.write_text(
            "# External renewal note\n\n"
            "Finance approved discount authority up to 10 percent, but anything higher needs "
            "CFO approval before legal can finalize terms.\n",
            encoding="utf-8",
        )
        manifest = _build_manifest(external_dir, scratch_dir)

        # The sandbox agent sees the manifest as its workspace and uses one shared shell tool
        # to inspect the files before answering.
        agent = SandboxAgent(
            name="Renewal Packet Analyst",
            model=model,
            instructions=(
                "You review renewal packets for an account team. Inspect the packet before "
                "answering. Keep the response concise, business-focused, and cite the file names "
                "that support each conclusion. If a conclusion depends on a file, mention that "
                "file by name. Do not invent numbers or statuses that are not present in the "
                "workspace. The manifest also grants read-only access to an external note at "
                f"`{external_note}` and read-write access to a scratch directory at "
                f"`{scratch_dir}`. Read the external note before answering, and write a brief "
                f"scratch note to `{scratch_note}`."
            ),
            default_manifest=manifest,
            capabilities=[WorkspaceShellCapability()],
        )

        # With Unix-local sandboxes, the runner creates and cleans up the temporary workspace for us.
        run_config = RunConfig(
            sandbox=SandboxRunConfig(client=UnixLocalSandboxClient()),
            workflow_name="Unix local sandbox review",
            tracing_disabled=True,
        )

        if not stream:
            result = await Runner.run(agent, question, run_config=run_config)
            print(result.final_output)
            return

        # The streaming path prints text deltas as they arrive so the example behaves like a demo.
        stream_result = Runner.run_streamed(agent, question, run_config=run_config)
        saw_text_delta = False
        async for event in stream_result.stream_events():
            if event.type == "raw_response_event" and isinstance(
                event.data, ResponseTextDeltaEvent
            ):
                if not saw_text_delta:
                    print("assistant> ", end="", flush=True)
                    saw_text_delta = True
                print(event.data.delta, end="", flush=True)

        if saw_text_delta:
            print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gpt-5.4", help="Model name to use.")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Prompt to send to the agent.")
    parser.add_argument("--stream", action="store_true", default=False, help="Stream the response.")
    parser.add_argument(
        "--verify-extra-path-grants",
        action="store_true",
        default=False,
        help="Run a local extra_path_grants smoke test without calling a model.",
    )
    args = parser.parse_args()

    if args.verify_extra_path_grants:
        asyncio.run(_verify_extra_path_grants())
    else:
        asyncio.run(main(args.model, args.question, args.stream))
